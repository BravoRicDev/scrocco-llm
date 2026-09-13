"""Lifecycle cooldown: streak decay su inattivita', jitter anti thundering
herd, e auto-retirement dopo troppi probe passivi falliti."""
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


class _FakeKH:
    def __init__(self):
        self.retired = set()
        self.saved = 0
        self.last_reason = None

    def is_retired(self, u):
        return u in self.retired

    def set_state(self, u, state, reason=None):
        if state == "retired":
            self.retired.add(u)
        self.last_reason = reason

    def save(self):
        self.saved += 1


# --------------------------------------------------------------- streak decay
def test_decay_halflife(router):
    router.policy.cooldown_streak_halflife_sec = 1800.0
    now = time.time()
    assert router._decay_streak(8, now - 1800, now) == 4
    assert router._decay_streak(8, now - 3600, now) == 2
    assert router._decay_streak(0, now - 3600, now) == 0
    assert router._decay_streak(8, 0, now) == 8          # mai fallito
    router.policy.cooldown_streak_halflife_sec = 0.0
    assert router._decay_streak(8, now - 99999, now) == 8


def test_mark_failed_applies_decay(router):
    u = _dep(router, "K-A")["unique"]
    s = router.stats_for(u)
    s.fail_streak = 8
    s.last_fail_ts = time.time() - 1800          # una halflife
    router.mark_failed(u, seconds=60)
    assert s.fail_streak == 5                    # 8 -> 4, poi +1


# --------------------------------------------------------------------- jitter
def test_jitter_bounds(router):
    vals = [router._apply_jitter(1000) for _ in range(50)]
    assert all(880 <= v <= 1120 for v in vals)
    router.policy.cooldown_jitter_ratio = 0
    assert router._apply_jitter(1000) == 1000


def test_mark_failed_jitter(router):
    pol = router.policy
    pol.cooldown_mode = "linear"
    pol.cooldown_base_min = 10
    pol.cooldown_linear_mult_min = 0
    u = _dep(router, "K-A")["unique"]
    secs = router.mark_failed(u)                 # 600s jitterato +/-12%
    assert 520 <= secs <= 680


# --------------------------------------------------- probe escalation + retire
def test_probe_cap_retires(router, monkeypatch):
    import app.main as M
    fake = _FakeKH()
    monkeypatch.setattr(M, "KEYHEALTH", fake, raising=False)
    u = _dep(router, "K-A")["unique"]
    s = router.stats_for(u)
    router._cooldown[u] = time.time() + 100
    router._cooldown_full[u] = 200.0
    router._cooldown_since[u] = time.time()
    s.probe_fail_streak = router.policy.probe_retire_after - 1
    router.mark_failed_double_residual(u, reason="http_429", status=429)
    assert u in fake.retired
    assert fake.last_reason == "probe_escalation_cap"
    assert fake.saved == 1


def test_probe_cap_disabled(router, monkeypatch):
    import app.main as M
    fake = _FakeKH()
    monkeypatch.setattr(M, "KEYHEALTH", fake, raising=False)
    router.policy.probe_retire_after = 0
    u = _dep(router, "K-A")["unique"]
    s = router.stats_for(u)
    router._cooldown[u] = time.time() + 100
    router._cooldown_full[u] = 200.0
    router._cooldown_since[u] = time.time()
    s.probe_fail_streak = 99
    router.mark_failed_double_residual(u, reason="http_429", status=429)
    assert u not in fake.retired


def test_probe_fail_reset_on_success(router):
    u = _dep(router, "K-A")["unique"]
    s = router.stats_for(u)
    s.probe_fail_streak = 3
    router.note_result(u, 100.0)
    assert s.probe_fail_streak == 0
    s.probe_fail_streak = 3
    router.clear_cooldown(u)
    assert s.probe_fail_streak == 0


def test_probe_fail_persist(router):
    u = _dep(router, "K-A")["unique"]
    router.stats_for(u).probe_fail_streak = 4
    d = router.dump_stats()
    assert d["stats"][u]["probe_fail_streak"] == 4
    r2 = Router(router.config, router.policy)
    r2.load_stats(d)
    assert r2.stats_for(u).probe_fail_streak == 4


def test_parsing_knobs():
    pol = Policy.from_dict({"cooldown_streak_halflife_sec": 60,
                            "probe_retire_after": 3,
                            "cooldown_jitter_ratio": 0.2})
    assert pol.cooldown_streak_halflife_sec == 60
    assert pol.probe_retire_after == 3
    assert pol.cooldown_jitter_ratio == 0.2
