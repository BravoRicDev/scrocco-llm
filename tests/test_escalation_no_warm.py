"""Bucket di escalation (-go/-fallback): NIENTE warm/refill/canary/gara lenta.

Bug: quando il deployment corrente e' su un bucket -go/-fallback il router
continuava a consultare il pool caldo e a far partire canary di refill + gara
lenta, ma il warm non li usa mai (e' FREE-only) e le sonde erano sprecate.

Fix (decisione utente):
  - `initial_pick`: il blocco warm NON viene consultato per -go/-fallback
    (`not _is_renewal_bucket(group_name)`);
  - streaming/non-streaming: per un dep su gruppo di escalation si salta
    refill/canary/slow-race/hedge (`is_escalation_group`);
  - difese centrali: `warm_fill_canary`/`warm_wake_canary` -> None,
    `hedge_canaries` -> [], `slow_race_allowed` -> False.
I bucket free restano invariati (warm/refill/slow ancora attivi).
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.opencode_gate import set_allow_opencode_zen, set_spoofing_request
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,order,scrocco-llm-test
t@x.com,m/p1,groq,https://api.groq.com/openai/v1,free,100,100000,5,10,K-P1
t@x.com,m/z1,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,5,0,K-Z1
t@x.com,m/g1,opencode-go,https://opencode.ai/zen/go/v1,15,200,200000,5,20,K-G1
t@x.com,m/f1,groq,https://api.groq.com/openai/v1,fallback,100,100000,5,5,K-F1
"""

POLICY = {"capability_routing": {"model_capabilities": {
    "m/p1": ["text"], "m/z1": ["text"], "m/g1": ["text"], "m/f1": ["text"]}},
    "ladder_skip_after": 20, "ladder_stale_max": 10,
    "dims_ladder_floor": True, "deployment_sticky": False}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    monkeypatch.delenv("OPENCODE_CAUTIOUS", raising=False)
    monkeypatch.delenv("OPENCODE_GO", raising=False)
    monkeypatch.delenv("BACKGROUND_CAUTIOUS", raising=False)
    set_allow_opencode_zen(None)
    set_spoofing_request(False)
    yield
    set_allow_opencode_zen(None)
    set_spoofing_request(False)


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    pol = Policy.from_dict(POLICY)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def _group_of(r, suffix):
    return next(g for g in r.config.groups if g.endswith(suffix))


def _count_warm_pool(r, monkeypatch):
    calls = {"n": 0}
    orig = r._warm_pool

    def counting(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(r, "_warm_pool", counting)
    return calls


NEED = frozenset({"text"})


def test_initial_pick_go_skips_warm(router, monkeypatch):
    go_group = _group_of(router, "-go")
    calls = _count_warm_pool(router, monkeypatch)
    d = router.initial_pick("test", go_group, need=NEED, session_id="ses-x")
    assert calls["n"] == 0
    assert d is not None
    assert router.config.deployment_by_unique(d["unique"])["group"] == go_group
    assert d["model"] == "m/g1"


def test_initial_pick_fallback_skips_warm(router, monkeypatch):
    fb_group = _group_of(router, "-fallback")
    calls = _count_warm_pool(router, monkeypatch)
    d = router.initial_pick("test", fb_group, need=NEED, session_id="ses-x")
    assert calls["n"] == 0
    assert d is not None
    assert router.config.deployment_by_unique(d["unique"])["group"] == fb_group
    assert d["model"] == "m/f1"


def test_initial_pick_free_still_uses_warm(router, monkeypatch):
    free_group = _group_of(router, "-100k")
    calls = _count_warm_pool(router, monkeypatch)
    d = router.initial_pick("test", free_group, need=NEED, session_id="ses-x")
    assert calls["n"] == 1
    assert d is not None


def test_slow_race_allowed_false_on_go_true_on_free(router):
    go_group = _group_of(router, "-go")
    free_group = _group_of(router, "-100k")
    assert router.slow_race_allowed(
        "ses-x", "test", go_group, NEED, None, 4096, set()) is False
    assert router.slow_race_allowed(
        "ses-x", "test", free_group, NEED, None, 4096, set()) is True


def test_warm_fill_canary_inhibited_on_escalation(router):
    for suffix in ("-go", "-fallback"):
        g = _group_of(router, suffix)
        gdep = router.config.groups[g][0]
        assert router.warm_fill_canary(
            "test", gdep, NEED, None, 100, set(), g) is None


def test_warm_wake_canary_inhibited_on_escalation(router):
    for suffix in ("-go", "-fallback"):
        g = _group_of(router, suffix)
        gdep = router.config.groups[g][0]
        assert router.warm_wake_canary(
            "test", gdep, NEED, None, 100, set(), g) is None


def test_hedge_canaries_inhibited_on_escalation(router):
    for suffix in ("-go", "-fallback"):
        g = _group_of(router, suffix)
        gdep = router.config.groups[g][0]
        assert router.hedge_canaries(
            "test", gdep, NEED, None, set(), g) == []