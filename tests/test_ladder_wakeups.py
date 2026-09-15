"""Risvegli cooldown nella ladder (ladder_cooldown_wakeups): prima di
escalare a -go la scala prova fino a N dim in cooldown (stantii)."""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,alpha-free,groq,https://api.groq.com/openai/v1,free,32,8000,0,D32A,text
t@x.com,beta-free,groq,https://api.groq.com/openai/v1,free,32,8000,0,D32B,text
t@x.com,gamma-free,groq,https://api.groq.com/openai/v1,free,64,8000,0,D64A,text
t@x.com,mgo,groq,https://api.groq.com/openai/v1,,32,8000,0,DGO,text
"""
PROF = "test"


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    yield r
    os.unlink(path)


def _cool(r, u):
    """Cooled E stantio con evidenza di QUOTA (il wakeup ora e' solo-429) e
    finestra di quota riaperta (niente soft/hint attivi)."""
    now = time.time()
    r._cooldown[u] = now + 3600.0            # ancora cooled
    r._cooldown_since[u] = now - 1800.0      # age = 1800 >= 300
    r._cooldown_full_map()[u] = 3600.0
    r.stats_for(u).last_reason = "http_429"
    r._key_soft.clear()
    r._key_hints.clear()


def _dims(cfg):
    out = []
    for u in cfg.chains[PROF]:
        d = cfg.deployment_by_unique(u)
        g = str((d or {}).get("group", ""))
        if not g.endswith("-go") and not g.endswith("-fallback"):
            out.append(u)
    return out


def _go(cfg):
    out = []
    for u in cfg.chains[PROF]:
        d = cfg.deployment_by_unique(u)
        g = str((d or {}).get("group", ""))
        if g.endswith("-go"):
            out.append(u)
    return out


def test_wakeup_cap_then_go(router):
    dims = _dims(router.config)
    assert len(dims) >= 3
    for u in dims:
        _cool(router, u)
    ladder = router.config.chains[PROF]
    tried = set()
    picks = []
    for _ in range(len(dims) + 3):
        d = router._walk_ladder_resilient(ladder, None, None, None, tried)
        if d is None:
            break
        picks.append(d["unique"])
        tried.add(d["unique"])
    # i primi 3 pick sono risvegli di dim (cap default 3), il 4o e' -go
    assert set(picks[:3]) == set(dims[:3])
    assert picks[3] in _go(router.config)


def test_wakeups_disabled_goes_straight_to_go(router):
    router.policy.ladder_cooldown_wakeups = 0
    dims = _dims(router.config)
    for u in dims:
        _cool(router, u)
    d = router._walk_ladder_resilient(
        router.config.chains[PROF], None, None, None, set())
    assert d is not None
    assert d["unique"] in _go(router.config)


def test_wakeup_cap_one(router):
    """Budget A FINESTRA per deployment: con cap 1 lo STESSO dim non viene
    svegliato due volte (un altro dim puo' esserlo)."""
    router.policy.ladder_cooldown_wakeups = 1
    dims = _dims(router.config)
    for u in dims:
        _cool(router, u)
    ladder = router.config.chains[PROF]
    tried = set()
    first = router._walk_ladder_resilient(ladder, None, None, None, tried)
    assert first is not None and first["unique"] in dims
    assert len(router._wake_times[first["unique"]]) == 1
    tried.add(first["unique"])
    router._walk_ladder_resilient(ladder, None, None, None, tried)
    assert len(router._wake_times[first["unique"]]) == 1   # non ri-svegliato


def test_fresh_cooldown_not_woken(router):
    dims = _dims(router.config)
    now = time.time()
    for u in dims:                            # cooldown fresco (age=0)
        router._cooldown[u] = now + 3600.0
        router._cooldown_since[u] = now
    d = router._walk_ladder_resilient(
        router.config.chains[PROF], None, None, None, set())
    assert d["unique"] in _go(router.config)


def test_parsing_ladder_knobs():
    p = Policy.from_dict({"ladder_skip_after": 10,
                          "ladder_cooldown_wakeups": 5})
    assert p.ladder_skip_after == 10
    assert p.ladder_cooldown_wakeups == 5
    assert Policy.from_dict({}).ladder_cooldown_wakeups == 20
    assert Policy.from_dict({}).ladder_cooldown_wakeup_window_sec == 3600
    # nuovi toggle "risveglio"
    assert Policy.from_dict({}).initial_pick_cooldown_wakeup is True
    assert Policy.from_dict(
        {"initial_pick_cooldown_wakeup": False}
    ).initial_pick_cooldown_wakeup is False
    assert Policy.from_dict({"ladder_stale_max": 0}).ladder_stale_max == 0


def test_stale_dim_retry_before_go_default(router):
    """Default: con dim e -go tutti stantii, STEP 3 (dims stantii) viene
    prima di STEP 4 (-go stantii)."""
    router.policy.ladder_cooldown_wakeups = 0        # isola STEP 3
    for u in _dims(router.config) + _go(router.config):
        _cool(router, u)
    d = router._walk_ladder_resilient(
        router.config.chains[PROF], None, None, None, set())
    assert d is not None and d["unique"] in _dims(router.config)


def test_stale_max_zero_disables_dim_stale_retry(router):
    """ladder_stale_max=0 disattiva STEP 3: resta il -go stantio (STEP 4)."""
    router.policy.ladder_cooldown_wakeups = 0
    router.policy.ladder_stale_max = 0
    for u in _dims(router.config) + _go(router.config):
        _cool(router, u)
    d = router._walk_ladder_resilient(
        router.config.chains[PROF], None, None, None, set())
    assert d is not None and d["unique"] in _go(router.config)


def test_wakeup_cap_ten_wakes_all_dims(router):
    """Cap 10 (> dims disponibili): il risveglio prova TUTTI i dim cooled
    prima di passare al -go."""
    router.policy.ladder_cooldown_wakeups = 10
    dims = _dims(router.config)
    for u in dims:
        _cool(router, u)
    tried = set()
    picks = []
    for _ in range(len(dims) + 1):
        d = router._walk_ladder_resilient(
            router.config.chains[PROF], None, None, None, tried)
        if d is None:
            break
        picks.append(d["unique"])
        tried.add(d["unique"])
    assert set(picks[:len(dims)]) == set(dims)
    assert picks[len(dims)] in _go(router.config)


def test_wakeup_orders_by_smallest_residual(router):
    """Fra i dim cooled-429 vince il 'piu' pronto' (residuo MINORE), poi a
    salire."""
    router.policy.ladder_cooldown_wakeups = 10
    dims = _dims(router.config)
    now = time.time()
    for i, u in enumerate(dims):
        router._cooldown[u] = now + 1000.0 * (i + 1)
        router._cooldown_since[u] = now - 1800.0
        router._cooldown_full_map()[u] = 100000.0   # non schiacciare i residui
        router.stats_for(u).last_reason = "http_429"
    router._key_soft.clear()
    router._key_hints.clear()
    tried = set()
    picks = []
    for _ in range(len(dims)):
        d = router._walk_ladder_resilient(
            router.config.chains[PROF], None, None, None, tried)
        assert d is not None and d["unique"] in dims
        picks.append(d["unique"])
        tried.add(d["unique"])
    _by_res = sorted(dims, key=lambda u: router.cooldown_residual(u))
    assert picks[0] == _by_res[0]                    # il piu' pronto per primo
    res = [round(router.cooldown_residual(u), -2) for u in picks]
    assert res == sorted(res)                        # poi a salire (tolleranza)