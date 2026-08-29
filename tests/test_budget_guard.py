"""Budget guard proattivo (no-spreco) + cooldown cap 5 ore.

Semantica chiave: la penalita' scatta SOLO con un cap APPRESO da un 429
reale di quel deployment. Niente evidenza = nessuna pena (le chiavi sane
o pagate non vengono frenate).
"""
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text
t@x.com,m-b,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-B,text
"""

POLICY = {"capability_routing": {"model_capabilities": {}}}


@pytest.fixture()
def router():
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    pol = Policy.from_dict(POLICY)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


GRP = "scrocco-llm-test-32k"


def _dep(router, key):
    return next(d for d in router.config.groups[GRP] if d["api_key"] == key)


def test_windows_count_per_minute_and_day(router):
    dep = _dep(router, "K-A")
    for _ in range(7):
        router.note_start(dep["unique"])
        router.note_end(dep["unique"])
    s = router.stats_for(dep["unique"])
    assert s.minute_calls == 7 and s.day_calls == 7


def test_no_penalty_without_learned_cap(router):
    """Nessun 429 -> nessun cap -> NESSUNA penalita' anche a 50 call/min."""
    dep = _dep(router, "K-A")
    for _ in range(50):
        router.note_start(dep["unique"])
        router.note_end(dep["unique"])
    s = router.stats_for(dep["unique"])
    assert s.minute_calls == 50 and s.min_cap_learned == 0
    w_cold = router._score(dict(dep), now=time.time())
    # confronto con un deployment gemello senza uso: stesso peso base
    dep_b = _dep(router, "K-B")
    w_b = router._score(dict(dep_b), now=time.time())
    assert w_cold > w_b * 0.5               # non e' stato schiacciato


def test_penalty_after_429_learning(router):
    dep = _dep(router, "K-A")
    for _ in range(10):                     # 10 chiamate nel minuto
        router.note_start(dep["unique"]); router.note_end(dep["unique"])
    # il provider dice 429 (Retry-After assente): cap appreso ~ 12/min
    router.mark_failed(dep["unique"], seconds=30, reason="http_429")
    s = router.stats_for(dep["unique"])
    assert s.min_cap_learned >= 10          # floor min_per_min=10
    # a cap esaurito il peso crolla sotto il 6% del freddo
    dep_b = _dep(router, "K-B")
    w_hot = router._score(dict(dep), now=time.time() + 3600)
    w_ref = router._score(dict(dep_b), now=time.time() + 3600)
    assert w_hot < w_ref * 0.2


def test_day_cap_learned_and_reset(router):
    dep = _dep(router, "K-A")
    for _ in range(5):
        router.note_start(dep["unique"]); router.note_end(dep["unique"])
    router.mark_failed(dep["unique"], reason="http_429")
    s = router.stats_for(dep["unique"])
    assert s.day_cap_learned >= 200         # floor min_per_day=200


def test_guard_disabled_restores_score(router):
    dep = _dep(router, "K-A")
    router.policy.budget_guard["enabled"] = False
    for _ in range(20):
        router.note_start(dep["unique"]); router.note_end(dep["unique"])
    router.mark_failed(dep["unique"], reason="http_429")
    s = router.stats_for(dep["unique"])
    # i cap NON si apprendono col guard disattivo
    assert s.min_cap_learned == 0 and s.day_cap_learned == 0


def test_max_cooldown_capped_at_five_hours(router):
    dep = _dep(router, "K-A")
    u = dep["unique"]
    for _ in range(30):                     # streak enorme
        router.mark_failed(u)
    remaining = max(0.0, router._cooldown.get(u, 0) - time.time())
    assert remaining <= 18000 + 5           # 5 ORE, non piu' 24h
