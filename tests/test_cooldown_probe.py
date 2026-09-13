"""Probe passivo dei deployment dormienti + decay della penalita'.

Semantica: quando NON ci sono chiavi vive, un deployment in cooldown da
>= cooldown_probe_after_ratio del suo tempo diventa un "probe"; il successo
lo riabilita (clear_cooldown lato forwarder), il fallimento raddoppia il
cooldown (mark_failed_double_residual lato forwarder).
"""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text
t@x.com,m-b,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-B,text
"""
GRP = "scrocco-llm-test-32k"


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, Policy.from_dict({}))
    os.unlink(path)


def _dep(router, key):
    return next(d for d in router.config.groups[GRP] if d["api_key"] == key)


def _set_progress(router, key, frac, total=100.0):
    dep = _dep(router, key)
    u = dep["unique"]
    now = time.time()
    router._cooldown[u] = now + total * (1.0 - frac)
    router._cooldown_full[u] = total
    router._cooldown_since[u] = now - total * frac
    return u


def test_progress_and_ready(router):
    u = _set_progress(router, "K-A", 0.5)
    assert router.cooldown_progress(u) == pytest.approx(0.5, abs=0.02)
    assert router.probe_ready(u) is True


def test_not_ready_early(router):
    u = _set_progress(router, "K-A", 0.1)
    assert router.cooldown_progress(u) == pytest.approx(0.1, abs=0.02)
    assert router.probe_ready(u) is False


def test_ratio_threshold(router):
    router.policy.cooldown_probe_after_ratio = 0.9
    u = _set_progress(router, "K-A", 0.5)
    assert router.probe_ready(u) is False


def test_probe_disabled(router):
    router.policy.cooldown_probe_enabled = False
    u = _set_progress(router, "K-A", 0.95)
    assert router.probe_ready(u) is False


def test_decay_linear(router):
    u = _set_progress(router, "K-A", 0.5)
    assert router._cooldown_decay(u) == pytest.approx(0.5, abs=0.02)
    v = _set_progress(router, "K-B", 1.0)
    assert router._cooldown_decay(v) == pytest.approx(0.0, abs=0.02)


def test_decay_not_cooled_is_one(router):
    assert router._cooldown_decay(_dep(router, "K-A")["unique"]) == 1.0


def test_decay_disabled(router):
    router.policy.cooldown_probe_decay = False
    u = _set_progress(router, "K-A", 0.9)
    assert router._cooldown_decay(u) == 1.0


def test_pick_prefers_ripe_probe_when_all_cooled(router):
    ua = _set_progress(router, "K-A", 0.5)    # maturo
    _set_progress(router, "K-B", 0.1)         # non maturo
    chosen = router.pick_deployment(GRP)
    assert chosen is not None and chosen["unique"] == ua


def test_pick_normal_when_alive(router):
    _set_progress(router, "K-A", 0.9)         # cooled ma maturo
    alive = _dep(router, "K-B")["unique"]
    chosen = router.pick_deployment(GRP)
    assert chosen["unique"] == alive          # il probe non ruba il turno


def test_pick_last_resort_when_none_ripe(router):
    _set_progress(router, "K-A", 0.1)
    _set_progress(router, "K-B", 0.2)
    assert router.pick_deployment(GRP) is not None


def test_policy_parsing():
    pol = Policy.from_dict({"cooldown_probe_enabled": False,
                            "cooldown_probe_after_ratio": 0.7,
                            "cooldown_probe_decay": False})
    assert pol.cooldown_probe_enabled is False
    assert pol.cooldown_probe_after_ratio == 0.7
    assert pol.cooldown_probe_decay is False
