"""F11 — concorrenza pesata a TOKEN (non a conteggio).

Il peso di una richiesta in volo e' il PREFILL REALE (ctx_est): 3 turni da 90k
valgono 270k token, 3 da 5k ne valgono 15k. Il limite a conteggio satura i dep
grossi con 3 heavy o lascia passare troppi heavy su un free-tier fragile.
Check: inflight_tokens + ctx_new <= max_input * conc_token_ratio, con tetto
duro a conteggio (conc_max_limit) che resta. ratio=0 -> comportamento storico.
"""
import os
import tempfile

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,model_preference
t@x.com,ds-a,prov,https://x/v1,20,32,200000,0,K1,text,100
t@x.com,ds-b,prov,https://x/v1,20,32,200000,0,K2,text,100
t@x.com,small,prov,https://x/v1,20,32,40000,0,K3,text,100
"""
GO = "scrocco-llm-test-go"


def _router(**pk):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict({})
    pol.conc_token_ratio = 0.5
    for k, v in pk.items():
        setattr(pol, k, v)
    r = Router(cfg, pol)
    return r, path


def _dep(r, model):
    return next(d for d in r.config.groups[GO] if d["model"] == model)


def test_heavy_blocked_when_budget_full():
    """Un heavy non fa due prefill pesanti in parallelo sullo stesso dep."""
    r, path = _router()
    try:
        d1, d2 = _dep(r, "ds-a"), _dep(r, "ds-b")
        r.note_start(d1["unique"], 90000)          # 90k gia' in prefill
        # budget = 200000 * 0.5 = 100k; 90k + 90k > 100k -> d1 escluso
        out = r._apply_concurrency_limit([d1, d2], 90000)
        assert d1 not in out and d2 in out
    finally:
        os.unlink(path)


def test_many_lights_parallel_ok():
    """N light passano in parallelo: il budget e' in token, non in richieste."""
    r, path = _router()
    try:
        d1, d2 = _dep(r, "ds-a"), _dep(r, "ds-b")
        for _ in range(6):                         # 6 x 5k = 30k < 100k
            r.note_start(d1["unique"], 5000)
        assert d1 in r._apply_concurrency_limit([d1, d2], 5000)
    finally:
        os.unlink(path)


def test_single_request_alone_always_passes():
    """Nessun deadlock: una richiesta da sola non si esclude mai."""
    r, path = _router()
    try:
        d1 = _dep(r, "ds-a")
        assert r._apply_concurrency_limit([d1], 190000) == [d1]
    finally:
        os.unlink(path)


def test_small_window_dep_saturates_sooner():
    r, path = _router()
    try:
        d1, d2 = _dep(r, "small"), _dep(r, "ds-a")
        r.note_start(d1["unique"], 15000)          # budget 40000*0.5 = 20k
        out = r._apply_concurrency_limit([d1, d2], 15000)   # 15k+15k > 20k
        assert d1 not in out and d2 in out
    finally:
        os.unlink(path)


def test_hard_cap_still_applies():
    """Il tetto a CONTEGGIO resta (anti-abuso), anche se i token stanno."""
    r, path = _router(conc_max_limit=3)
    try:
        d1, d2 = _dep(r, "ds-a"), _dep(r, "ds-b")
        for _ in range(3):
            r.note_start(d1["unique"], 1000)       # 3 x 1k: token ok
        out = r._apply_concurrency_limit([d1, d2], 1000)
        assert d1 not in out and d2 in out
    finally:
        os.unlink(path)


def test_ratio_zero_legacy_counting():
    r, path = _router()
    try:
        r.policy.conc_token_ratio = 0.0
        d1, d2 = _dep(r, "ds-a"), _dep(r, "ds-b")
        for _ in range(3):
            r.note_start(d1["unique"], 90000)
        out = r._apply_concurrency_limit([d1, d2], 90000)
        assert d1 not in out and d2 in out
    finally:
        os.unlink(path)


def test_unknown_ctx_legacy_counting():
    """Contesto ignoto (None/0) -> conteggio storico, mai crash."""
    r, path = _router()
    try:
        d1, d2 = _dep(r, "ds-a"), _dep(r, "ds-b")
        for _ in range(3):
            r.note_start(d1["unique"])
        out = r._apply_concurrency_limit([d1, d2], None)
        assert d1 not in out and d2 in out
    finally:
        os.unlink(path)


def test_start_end_symmetric_tokens():
    r, path = _router()
    try:
        d1 = _dep(r, "ds-a")
        u = d1["unique"]
        s = r.stats_for(u)
        r.note_start(u, 50000)
        assert s.inflight_tokens == 50000
        r.note_end(u, 50000)
        assert s.inflight_tokens == 0
        r.note_end(u, 50000)                       # mai negativo
        assert s.inflight_tokens == 0
    finally:
        os.unlink(path)


def test_pick_deployment_honors_token_budget():
    """Integrazione: il pick con ctx esclude il dep col budget pieno."""
    r, path = _router()
    try:
        d1 = _dep(r, "ds-a")
        r.note_start(d1["unique"], 90000)
        picks = {r.pick_deployment(GO, need=frozenset({"text"}),
                                   ctx=90000)["model"] for _ in range(20)}
        assert "ds-a" not in picks
    finally:
        os.unlink(path)
