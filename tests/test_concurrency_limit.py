"""Dynamic Inflight Concurrency Limit per-deployment.

Ogni deployment ha un limite di connessioni concorrenti: se inflight >=
limite viene escluso dalle NUOVE richieste (le in volo proseguono). Il limite
e' appreso empiricamente: default conservativo 3, sale di 1 dopo N successi
consecutivi a saturazione (max 10), dimezza sui 429/503 (min 1). La colonna
`concurrent_limit` del CSV impone un limite FISSO (nessun apprendimento)."""
import os
import tempfile

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,model_preference,concurrent_limit
t@x.com,ds-a,prov,https://x/v1,20,32,8000,0,K1,text,100,
t@x.com,ds-b,prov,https://x/v1,20,32,8000,0,K2,text,100,
t@x.com,mimo,prov,https://x/v1,20,32,8000,0,K3,text,50,
t@x.com,fixed,prov,https://x/v1,20,32,8000,0,K4,text,0,2
"""
GO = "scrocco-llm-test-go"


def _router(policy=None):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, policy or Policy.from_dict({}))
    r.policy.adaptive_pick = True
    return r, path


def _dep(router, grp, model):
    return next(d for d in router.config.groups[grp] if d["model"] == model)


def test_default_limit_is_conservative():
    r, path = _router()
    try:
        d = _dep(r, GO, "ds-a")
        assert r._concurrent_limit_for(d) == 3
    finally:
        os.unlink(path)


def test_fixed_limit_wins_from_csv():
    r, path = _router()
    try:
        d = _dep(r, GO, "fixed")
        assert d["concurrent_limit"] == 2
        assert r._concurrent_limit_for(d) == 2
        # saturo a 2: escluso dalle nuove richieste; se TUTTO il gruppo e'
        # saturo il pick procede comunque (edge estremo -> originale)
        r.note_start(d["unique"])
        r.note_start(d["unique"])
        deps = r._apply_concurrency_limit([d])
        assert deps == [d]
        # con un'alternativa sana, il saturo viene escluso
        d2 = _dep(r, GO, "ds-a")
        out = r._apply_concurrency_limit([d, d2])
        assert d not in out and d2 in out
    finally:
        os.unlink(path)


def test_saturated_dep_excluded_from_pick():
    r, path = _router()
    try:
        d = _dep(r, GO, "ds-a")
        # satura ds-a al limite dinamico (3)
        for _ in range(3):
            r.note_start(d["unique"])
        picks = {r.pick_deployment(GO, need=frozenset({"text"}))["model"]
                 for _ in range(30)}
        assert "ds-a" not in picks
    finally:
        os.unlink(path)


def test_learn_raises_limit_after_streak():
    r, path = _router()
    try:
        r.policy.conc_learn_success_streak = 5
        d = _dep(r, GO, "ds-a")
        assert r._concurrent_limit_for(d) == 3
        s = r.stats_for(d["unique"])
        # 5 successi CON inflight >= limite (saturazione gestita senza errori)
        for _ in range(5):
            s.inflight = 3
            r.note_result(d["unique"], 100.0)
        assert r._concurrent_limit_for(d) == 4
    finally:
        os.unlink(path)


def test_success_below_saturation_does_not_raise():
    r, path = _router()
    try:
        r.policy.conc_learn_success_streak = 3
        d = _dep(r, GO, "ds-a")
        s = r.stats_for(d["unique"])
        # inflight resta sotto il limite (mai saturato) -> nessuna salita
        for _ in range(10):
            s.inflight = 1
            r.note_result(d["unique"], 100.0)
        assert r._concurrent_limit_for(d) == 3
    finally:
        os.unlink(path)


def test_429_halves_dynamic_limit():
    r, path = _router()
    try:
        d = _dep(r, GO, "ds-a")
        r._concl()[d["unique"]] = 8
        r.mark_failed(d["unique"], seconds=60, status=429)
        assert r._concurrent_limit_for(d) == 4
        r.mark_failed(d["unique"], seconds=60, status=429)
        assert r._concurrent_limit_for(d) == 2
        r.mark_failed(d["unique"], seconds=60, status=503)
        assert r._concurrent_limit_for(d) == 1     # floor 1
    finally:
        os.unlink(path)


def test_fixed_limit_not_halved_by_429():
    r, path = _router()
    try:
        d = _dep(r, GO, "fixed")
        r.mark_failed(d["unique"], seconds=60, status=429)
        assert r._concurrent_limit_for(d) == 2
    finally:
        os.unlink(path)