"""model_preference efficace anche a freddo: (2a) base su |score| e (2b)
tie-break per preferenza prima della latenza. deepseek-v4.1-flash (pref alto)
deve battere i fratelli anche senza statistiche."""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,model_preference
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text,100
t@x.com,m-b,deepinfra,https://api.deepinfra.com/v1/openai,free,32,8000,0,K-B,text,50
t@x.com,m-c,together,https://api.together.xyz/v1,free,32,8000,0,K-C,text,-50
"""
GRP = "scrocco-llm-test-32k"


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    r.policy.adaptive_pick = True
    yield r
    os.unlink(path)


def _dep(router, model):
    return next(d for d in router.config.groups[GRP] if d["model"] == model)


def test_pick_prefers_higher_preference(router):
    d = router.pick_deployment(GRP, need=frozenset({"text"}))
    assert d is not None
    assert d["model"] == "m-a"          # pref 100 > 50 > -50


def test_reputation_prefers_high_pref(router):
    a, b, c = (_dep(router, m) for m in ("m-a", "m-b", "m-c"))
    sa = router._reputation_score(a["unique"], a)
    sb = router._reputation_score(b["unique"], b)
    sc = router._reputation_score(c["unique"], c)
    assert sa < sb < sc                 # lower is better


def test_reputation_base_zero_is_neutral(router):
    router.policy.model_preference_base = 0.0
    a, b = _dep(router, "m-a"), _dep(router, "m-b")
    assert router._reputation_score(a["unique"], a) == \
        router._reputation_score(b["unique"], b) == 0.0


def test_tiebreak_pref_before_latency(router):
    # a freddo con base=0 i punteggi sono pari -> decide il tie-break:
    # la preferenza (m-a=100) deve battere la latenza migliore di m-b.
    router.policy.model_preference_base = 0.0
    a, b = _dep(router, "m-a"), _dep(router, "m-b")
    router._avg_latencies[a["unique"]] = 5000.0     # a: lento
    router._avg_latencies[b["unique"]] = 100.0      # b: veloce
    d = router.pick_deployment(GRP, need=frozenset({"text"}))
    assert d["model"] == "m-a"


def test_cold_start_is_preference_times_base(router):
    """Cold start = -(pref × model_preference_base): pref 100 -> -1000,
    pref 50 -> -500, pref -50 -> +500. La preferenza domina da freddo."""
    a, b, c = (_dep(router, m) for m in ("m-a", "m-b", "m-c"))
    assert router._reputation_score(a["unique"], a) == -1000.0
    assert router._reputation_score(b["unique"], b) == -500.0
    assert router._reputation_score(c["unique"], c) == 500.0


def test_cold_start_persists_and_accumulates(router):
    """Il seed e' persistito in _base_scores: successi/fallimenti si sommano
    sopra, non resettano la partenza."""
    a = _dep(router, "m-a")
    u = a["unique"]
    assert router._reputation_score(u, a) == -1000.0
    router.record_success(u, 100.0)
    # -10 dep + key/prov condivisi (log-norm) -> leggermente sotto -1010
    assert router._reputation_score(u, a) == pytest.approx(-1015.8, abs=1.0)
    router.record_failure(u, "http_429", 429)
    # +5 dep; key/prov si compensano (-2+2) -> torna a -1005 esatto
    assert router._reputation_score(u, a) == -1005.0


def test_cold_start_survives_restart(router):
    """Il seed finisce in dump_stats (base_scores): dopo un restart il
    deployment preferito riparte ancora da -1000, non da 0."""
    a = _dep(router, "m-a")
    u = a["unique"]
    router._reputation_score(u, a)
    dump = router.dump_stats()
    r2 = Router(router.config, router.policy)
    r2.load_stats(dump)
    assert r2._reputation_score(u, a) == -1000.0


def test_parsing_model_preference_base():
    assert Policy.from_dict({}).model_preference_base == 10.0
    assert Policy.from_dict({"model_preference_base": 0}).model_preference_base == 0.0
    assert Policy.from_dict(
        {"circuit_breaker": {"scope": "dep"}}).circuit_breaker_scope == "dep"
    with pytest.raises(ValueError):
        Policy.from_dict({"circuit_breaker": {"scope": "bogus"}})
    with pytest.raises(ValueError):
        Policy.from_dict({"model_preference_base": -1})
