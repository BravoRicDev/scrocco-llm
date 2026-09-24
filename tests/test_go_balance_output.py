"""Bilanciamento del bucket -go (intervento go_balance).

Metrica a freddo = TOKEN DI OUTPUT consumati nella finestra rolling (default 5h),
pool unico sui rinnovi futuri (solo sort_key==0 resta tier assoluto) e stickiness
`last_go`/cache-holder limitata a `go_stick_ttl_sec` (default 10 minuti).
"""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router
from app.session_ctx import set_current_session

BASE = "scrocco-llm-test"
HDR = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
       f"{BASE},caps,intelligence_score,model_preference,order\n")
# m/deep: rinnovo vicino (sort_key 26). m/codex: rinnovo piu' lontano (29).
CSV = HDR + (
    f"a@x.com,m/deep,opencode-go,https://x/v1,20,1000,250000,0,K-D1,,8,100,20\n"
    f"a@x.com,m/codex,opencode-go,https://x/v1,23,1000,250000,0,K-C1,,8,100,20\n"
)


def _build(policy=None):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
               Policy.from_dict(policy or {}))
    # sort_key deterministici (date-indipendenti): deep=26, codex=29.
    for d in r.config.groups[f"{BASE}-go"]:
        d["sort_key"] = 26.0 if d["model"] == "m/deep" else 29.0
    return r, path


@pytest.fixture()
def r():
    rr, path = _build()
    yield rr
    os.unlink(path)


def _go(r):
    return r.config.groups[f"{BASE}-go"]


def _by_model(r, model):
    return next(d for d in _go(r) if d["model"] == model)


def test_output_window_somma_e_pota():
    rr, path = _build()
    try:
        now = time.time()
        rr.note_output_tokens("u1", 999, ts=now - 20000)   # fuori finestra 5h
        rr.note_output_tokens("u1", 50, ts=now - 100)
        rr.note_output_tokens("u1", 100, ts=now)
        assert rr.output_tokens_window("u1", now=now) == 150
    finally:
        os.unlink(path)


def test_output_window_zero_se_nessun_campione():
    rr, path = _build()
    try:
        assert rr.output_tokens_window("mai-visto") == 0
    finally:
        os.unlink(path)


def test_note_output_tokens_ignora_zero_e_negativi():
    rr, path = _build()
    try:
        rr.note_output_tokens("u2", 0)
        rr.note_output_tokens("u2", None)
        rr.note_output_tokens("u2", -5)
        assert rr.output_tokens_window("u2") == 0
    finally:
        os.unlink(path)


def test_cold_pick_min_output_finestra_5h(r):
    """deep ha consumato molto output, codex zero -> sceglie codex."""
    deep = _by_model(r, "m/deep")
    codex = _by_model(r, "m/codex")
    r.note_output_tokens(deep["unique"], 100000, ts=time.time())
    for _ in range(5):
        d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
        assert d["unique"] == codex["unique"], d["unique"]


def test_flat_pool_usa_anche_tier_superiore(r):
    """Con flat_pool (default) il dep con sort_key piu' alto viene usato se
    l'altro ha consumato di piu' (bilanciamento fra rinnovi)."""
    deep = _by_model(r, "m/deep")
    codex = _by_model(r, "m/codex")
    assert deep["sort_key"] < codex["sort_key"]
    r.note_output_tokens(deep["unique"], 50000, ts=time.time())
    d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
    assert d["unique"] == codex["unique"]


def test_flat_pool_disabled_tier_stretto():
    """Con flat_pool=false vale il tier minimo (sort_key): codex MAI scelto."""
    rr, path = _build({"go_balance": {"flat_pool": False}})
    try:
        deep = _by_model(rr, "m/deep")
        rr.note_output_tokens(deep["unique"], 99999, ts=time.time())
        for _ in range(5):
            d = rr.pick_deployment(f"{BASE}-go", need=None, ctx=100)
            assert d["unique"] == deep["unique"]
    finally:
        os.unlink(path)


def test_go_balance_disabled_usa_prefill(r):
    """Con enabled=false si torna alla metrica prefill-24h (`note_usage`)."""
    rr, path = _build({"go_balance": {"enabled": False}})
    try:
        deep = _by_model(rr, "m/deep")
        codex = _by_model(rr, "m/codex")
        for _ in range(50):
            rr.note_usage(deep["unique"], ctx_est=8000)
        d = rr.pick_deployment(f"{BASE}-go", need=None, ctx=100)
        assert d["unique"] == codex["unique"]
    finally:
        os.unlink(path)


def test_last_go_scade_dopo_go_stick_ttl(r):
    """`last_go` (e l'holder) valgono solo entro `go_stick_ttl_sec`: passati
    ~10 min la sessione ripesca bilanciando."""
    set_current_session("sess-x")
    deep = _by_model(r, "m/deep")
    r.note_output_tokens(deep["unique"], 100000, ts=time.time())
    r.note_session_success("sess-x", deep["unique"], latency_ms=100, ctx_est=100)
    # fresco: last_go vince (deep), anche se piu' consumato.
    d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
    assert d["unique"] == deep["unique"]
    # invecchia OLTRE go_stick_ttl_sec (600): sia last_go sia cache-holder.
    old = time.time() - 700
    r._last_go_map()["sess-x"] = (deep["unique"], old)
    r._cache_ok()["sess-x"] = (deep["unique"], old)
    d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
    assert d["unique"] != deep["unique"], "stickiness -go scaduta"


def test_stickiness_valida_entro_go_stick_ttl(r):
    """Subito dopo il successo, `last_go` vince (cache calda)."""
    set_current_session("sess-y")
    codex = _by_model(r, "m/codex")
    r.note_session_success("sess-y", codex["unique"], latency_ms=100, ctx_est=100)
    d = r.pick_deployment(f"{BASE}-go", need=None, ctx=100)
    assert d["unique"] == codex["unique"]


def test_policy_defaults():
    p = Policy.from_dict({})
    assert p.go_balance_enabled is True
    assert p.go_balance_flat_pool is True
    assert p.go_balance_window_sec == 18000
    assert p.go_stick_ttl_sec == 600


def test_policy_parse_block():
    p = Policy.from_dict({
        "go_balance": {"enabled": False, "flat_pool": False,
                       "window_sec": 3600},
        "go_stick_ttl_sec": 120,
    })
    assert p.go_balance_enabled is False
    assert p.go_balance_flat_pool is False
    assert p.go_balance_window_sec == 3600
    assert p.go_stick_ttl_sec == 120


def test_policy_parse_invalid():
    with pytest.raises(ValueError):
        Policy.from_dict({"go_balance": {"window_sec": 0}})
    with pytest.raises(ValueError):
        Policy.from_dict({"go_balance": {"enabled": "non-bool"}})
    with pytest.raises(ValueError):
        Policy.from_dict({"go_balance": "stringa"})
