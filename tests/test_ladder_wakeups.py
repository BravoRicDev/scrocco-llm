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
    now = time.time()
    r._cooldown[u] = now + 3600.0            # ancora cooled
    r._cooldown_since[u] = now - 1800.0      # age = 1800 >= 300


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
    router.policy.ladder_cooldown_wakeups = 1
    dims = _dims(router.config)
    for u in dims:
        _cool(router, u)
    ladder = router.config.chains[PROF]
    tried = set()
    first = router._walk_ladder_resilient(ladder, None, None, None, tried)
    tried.add(first["unique"])
    second = router._walk_ladder_resilient(ladder, None, None, None, tried)
    assert first["unique"] in dims
    assert second["unique"] in _go(router.config)


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
    assert Policy.from_dict({}).ladder_cooldown_wakeups == 3
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
