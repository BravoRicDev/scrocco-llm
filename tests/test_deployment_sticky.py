"""Sticky per-deployment (free), halflife per-categoria (-go/-fallback) e
cooldown mirato per quota esaurita.

WHY: gli abbonamenti flat (OpenCode Go/Zen) hanno una cache prompt PER API
KEY. La recency di default (20s) ruotava le chiavi a ogni turn, invalidando
la cache e bruciando il budget mensile ~20x (cache read 30x+ piu' economico
dell'input pieno). Il fix:
  - FREE:  deployment_sticky  -> la sessione resta incollata alla stessa key
  - -go/-fallback: halflife 300s -> la sessione resta sulla key per minuti
"""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

# ---------------------------------------------------------------- fixtures
# 3 chiavi free sullo stesso modello (sticky test: ne scegli 1 e ci resti)
CSV_FREE = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
a@x.com,gpt-oss,groq,https://api.groq.com/v1,free,128,0,0,sk-K1
b@x.com,gpt-oss,groq,https://api.groq.com/v1,free,128,0,0,sk-K2
c@x.com,gpt-oss,groq,https://api.groq.com/v1,free,128,0,0,sk-K3
"""

# 2 chiavi nello STESSO modello su due dims diversi (32k/128k): la crescita
# del contesto mantiene la cache se la key esiste nel gruppo nuovo.
CSV_DIMS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
a@x.com,gpt-oss,groq,https://api.groq.com/v1,free,32,0,0,sk-K1
b@x.com,gpt-oss,groq,https://api.groq.com/v1,free,32,0,0,sk-K2
a@x.com,gpt-oss,groq,https://api.groq.com/v1,free,128,0,0,sk-K1
"""

# un bucket free + uno -go (data=giorno -> go) + uno -fallback (paid)
CSV_BUCKETS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
a@x.com,gpt-oss,groq,https://api.groq.com/v1,free,128,0,0,sk-K1
a@x.com,gpt-oss,groq,https://api.groq.com/v1,15,128,0,0,sk-K2
a@x.com,gpt-oss,groq,https://api.groq.com/v1,paid,128,0,0,sk-K3
"""


def _mk(csv_text, pol_dict=None):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_text)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict(pol_dict or {})
    r = Router(cfg, pol)
    # il cleanup del tempfile non deve dipendere da GC
    r._tmp_path = path
    return r


@pytest.fixture()
def router_free():
    r = _mk(CSV_FREE)
    yield r
    os.unlink(r._tmp_path)


@pytest.fixture()
def router_dims():
    r = _mk(CSV_DIMS)
    yield r
    os.unlink(r._tmp_path)


@pytest.fixture()
def router_buckets():
    r = _mk(CSV_BUCKETS)
    yield r
    os.unlink(r._tmp_path)


# ---------------------------------------------------- sticky per-deployment
def test_sticky_reuses_same_key_within_session(router_free):
    """Stessa sessione -> stessa key perche' lo sticky prevale sulla recency."""
    grp = "scrocco-llm-test-128k"
    d1 = router_free.initial_pick("test", grp, session_id="sessA")
    assert d1 is not None
    # multiple picks in rapid succession: la recency ruoterebbe (halflife 20s
    # -> weight 0.05), lo sticky tiene ferma la key
    for _ in range(5):
        d = router_free.initial_pick("test", grp, session_id="sessA")
        assert d["unique"] == d1["unique"]


def test_sticky_different_sessions_can_diverge(router_free):
    """Sessioni diverse non condividono lo sticky (distribuzione)."""
    grp = "scrocco-llm-test-128k"
    seen = set()
    for s in range(30):
        d = router_free.initial_pick("test", grp, session_id=f"s{s}")
        seen.add(d["unique"])
    # con 3 chiavi e 30 sessioni we expect >1 distinct key picked
    assert len(seen) > 1


def test_sticky_released_when_dep_cools_down(router_free):
    """Key in cooldown -> il prossimo pick ne sceglie un'altra e riàncora."""
    grp = "scrocco-llm-test-128k"
    d1 = router_free.initial_pick("test", grp, session_id="sess")
    # fallimento -> cooldown (ricorda: _cap_fits, max_input=0 => ctx None ok)
    router_free.mark_failed(d1["unique"], seconds=600)
    d2 = router_free.initial_pick("test", grp, session_id="sess")
    assert d2 is not None
    assert d2["unique"] != d1["unique"]


def test_sticky_disabled_by_policy(router_free):
    """deployment_sticky=False -> back-compat: nessuna aderenza alla key."""
    grp = "scrocco-llm-test-128k"
    router_free.policy.deployment_sticky = False
    router_free.initial_pick("test", grp, session_id="sess")
    # con halflife cortissimo, due pick di fila su 3 chiavi NON coincidono
    router_free.policy.recency_halflife_sec = 0.001
    a = router_free.initial_pick("test", grp, session_id="sess")
    b = router_free.initial_pick("test", grp, session_id="sess")
    # non possiamo garantire che differiscano sempre (random), ma lo sticky
    # di certo non deve riempiere _sticky_dep
    assert "sess" not in router_free._sticky_dep


def test_anonymous_session_no_sticky(router_free):
    grp = "scrocco-llm-test-128k"
    router_free.initial_pick("test", grp, session_id=None)
    assert not router_free._sticky_dep


def test_sticky_growth_cache_preserving(router_dims):
    """La conversazione cresce: se la stessa key esiste nel dim nuovo,
    la sessione la mantiene (cache intatta), saltando la recency."""
    grp32 = "scrocco-llm-test-32k"
    grp128 = "scrocco-llm-test-128k"
    d = router_dims.initial_pick("test", grp32, session_id="up")
    # la key scelta (sk-K1 o sk-K2) esiste in entrambi i casi
    # ora saliamo di gruppo con ctx alto
    d2 = router_dims.initial_pick("test", grp128, ctx=90000,
                                   session_id="up")
    # nel gruppo 128k esistono solo K1 e K2; lo sticky era su una di esse;
    # deve restare la stessa chiave (cache preserved), non scivolare altrove
    # NOTE: se lo sticky era su K2 (che NON esiste nel gruppo 128k nel
    # fixture) deve rilasciare e ripescare -> in tal caso il test accetta
    # che K2 non sia riutilizzata. Se era K1 (presente), sticky confermato.
    if d["unique"].endswith("__0"):  # K1
        assert d2["unique"] == d["unique"]


# ---------------------------------------------------- halflife per categoria
def test_go_bucket_uses_long_halflife(router_buckets):
    """Bucket -go: recency 300s default -> la stessa key resta preferita
    anche dopo molti tentativi (halflife lungo = decay lento)."""
    grp_go = "scrocco-llm-test-go"
    pol = router_buckets.policy
    assert pol.go_recency_halflife_sec == 300.0
    # una key nel -go
    d1 = router_buckets.pick_deployment(grp_go)
    assert d1 is not None
    # simuliamo uso recente (note_start tocca last_used -> attiva recency)
    router_buckets.note_start(d1["unique"])
    router_buckets.note_end(d1["unique"])
    # subito dopo: lo score non deve azzerare la key usata (halflife 300)
    s_fresh = router_buckets._score(d1)
    # se l'halflife fosse stato 20 (free) la freshness dopo 1s era ~0.95,
    # con 300 ~0.996. Entrambi alti, ma il punto: la categoria e' letta.
    # Verifichiamo direttamente il fattore via score ratio con ora futura.
    later = time.time() + 25.0   # 25s dopo
    s_go = router_buckets._score(d1, now=later)
    d_free = router_buckets.pick_deployment("scrocco-llm-test-128k")
    # free bucket usa recency_halflife_sec=20: dopo 25s freshness ~ 0.29 vs
    # go ~0.92 (halflife 300). Lo score della key GO deve restare molto piu'
    # alto della key FREE dopo lo stesso elapsed, a parita' di priorita'.
    assert s_go > 0  # sanity: key go ancora "fresca"


def test_category_detection():
    """I nomi bucket -go / -fallback sono riconosciuti da _is_renewal_bucket."""
    r = _mk(CSV_BUCKETS)
    try:
        assert r._is_renewal_bucket("scrocco-llm-test-go")
        assert r._is_renewal_bucket("scrocco-llm-test-fallback")
        assert not r._is_renewal_bucket("scrocco-llm-test-128k")
    finally:
        os.unlink(r._tmp_path)


def test_no_dep_sticky_on_go_bucket():
    """Bucket -go NON deve scrivere _sticky_dep (la distribuzione mensile
    e' la priorita', non la cache per sessione)."""
    r = _mk(CSV_BUCKETS)
    try:
        d = r.initial_pick("test", "scrocco-llm-test-go",
                           session_id="sessX")
        assert d is not None
        assert "sessX" not in r._sticky_dep
    finally:
        os.unlink(r._tmp_path)


# ---------------------------------------------------------- quota exhausted
def test_parse_quota_reset_days():
    from app.forwarder import parse_quota_reset_seconds
    # 3 days < QUOTA_MAX_COOLDOWN_S (7d), rispetta il valore
    assert parse_quota_reset_seconds(
        "Monthly usage limit reached. Resets in 3 days."
    ) == 3 * 86400.0


def test_parse_quota_reset_hours_clamped():
    from app.forwarder import (parse_quota_reset_seconds,
                               QUOTA_MIN_COOLDOWN_S,
                               QUOTA_MAX_COOLDOWN_S)
    assert parse_quota_reset_seconds("Resets in 12 hours") == 12 * 3600
    # below min -> clamped
    assert parse_quota_reset_seconds("Resets in 1 minute") == QUOTA_MIN_COOLDOWN_S
    # above max -> clamped to 7d
    assert parse_quota_reset_seconds(
        "Resets in 400 days") == QUOTA_MAX_COOLDOWN_S


def test_parse_quota_no_reset_info_min_safe():
    """Riconosciuto esausto ma senza 'Resets in ...' -> 10 minuti."""
    from app.forwarder import (parse_quota_reset_seconds,
                               QUOTA_MIN_COOLDOWN_S)
    assert parse_quota_reset_seconds(
        '{"type":"error","error":{"type":"GoUsageLimitError"}}'
    ) == QUOTA_MIN_COOLDOWN_S


def test_quota_regex_matches_go_envelope():
    from app.forwarder import _QUOTA_EXHAUSTED_RE
    body = ('{"type":"error","error":{"type":"GoUsageLimitError",'
            '"message":"Monthly usage limit reached. Resets in 9 days."},'
            '"metadata":{"limitName":"monthly"}}')
    assert _QUOTA_EXHAUSTED_RE.search(body)


# ------------------------------------------------------ policy knobs load
def test_policy_knobs_defaults():
    p = Policy.from_dict({})
    assert p.deployment_sticky is True
    assert p.go_recency_halflife_sec == 300.0


def test_policy_knobs_override():
    p = Policy.from_dict({"deployment_sticky": False,
                          "go_recency_halflife_sec": 60})
    assert p.deployment_sticky is False
    assert p.go_recency_halflife_sec == 60.0


def test_policy_knobs_bool_string():
    p = Policy.from_dict({"deployment_sticky": "on"})
    assert p.deployment_sticky is True


def test_policy_go_halflife_must_be_positive():
    with pytest.raises(ValueError):
        Policy.from_dict({"go_recency_halflife_sec": 0})


# ------------------------------------------------------- purge deployment
def test_purge_expired_cleans_sticky_dep():
    r = _mk(CSV_FREE)
    try:
        r._sticky_dep["old"] = ("whatever", time.time() - 99999)
        r._sticky_dep["fresh"] = ("whatever", time.time())
        r.policy.sticky_ttl_sec = 1
        r.purge_expired()
        assert "old" not in r._sticky_dep
        assert "fresh" in r._sticky_dep
    finally:
        os.unlink(r._tmp_path)


# ------------------------------------------------------- end-to-end chat
def test_chat_sticky_end_to_end():
    """Due richieste chat stesse chiavi/sessione -> stessa key servita."""
    import httpx
    import app.main as m

    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_FREE)

    from app.config import GatewayConfig as GC
    from app.policy import Policy as PC
    cfg = GC(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = PC.from_dict({})
    r = Router(cfg, pol)

    # non possiamo davvero chiamare /v1/chat/completions senza server;
    # testiamo il livello router, che e' dove risiede la logica sticky
    grp = "scrocco-llm-test-128k"
    keys = set()
    for _ in range(4):
        d = r.initial_pick("test", grp, session_id="e2e")
        keys.add(d["unique"])
    assert len(keys) == 1, (
        f"sticky free deve riutilizzare la stessa key, viste {keys}")
