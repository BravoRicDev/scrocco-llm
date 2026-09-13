"""Timeout upstream adattivo per-deployment (max(floor, avg*multiplier))."""
import httpx
import pytest

import app.forwarder as F
from app.policy import Policy


@pytest.fixture(autouse=True)
def _reset():
    saved = (F.ADAPTIVE_TIMEOUT, F.TIMEOUT_FLOOR_SEC, F.TIMEOUT_MULTIPLIER,
             F.TIMEOUT_MAX_SEC, F._LATENCY_LOOKUP)
    yield
    (F.ADAPTIVE_TIMEOUT, F.TIMEOUT_FLOOR_SEC, F.TIMEOUT_MULTIPLIER,
     F.TIMEOUT_MAX_SEC, F._LATENCY_LOOKUP) = saved


def test_floor_applied_for_fast_avg():
    F.set_adaptive_timeout(enabled=True, floor_sec=15.0, multiplier=8.0,
                           max_sec=600.0)
    F.set_latency_lookup(lambda u: 1000.0)      # 1s * 8 = 8 < floor 15
    t = F._timeout_for({"unique": "u"})
    assert isinstance(t, httpx.Timeout)
    assert t.read == 15.0


def test_multiplier_applied():
    F.set_adaptive_timeout(enabled=True, floor_sec=15.0, multiplier=8.0,
                           max_sec=600.0)
    F.set_latency_lookup(lambda u: 3000.0)      # 3s * 8 = 24
    t = F._timeout_for({"unique": "u"})
    assert abs(t.read - 24.0) < 1e-9


def test_max_cap_applied():
    F.set_adaptive_timeout(enabled=True, floor_sec=15.0, multiplier=8.0,
                           max_sec=600.0)
    F.set_latency_lookup(lambda u: 300000.0)    # 300s * 8 = 2400 -> 600
    t = F._timeout_for({"unique": "u"})
    assert t.read == 600.0


def test_identical_to_default_returns_none():
    F.set_adaptive_timeout(enabled=True, floor_sec=15.0, multiplier=8.0,
                           max_sec=600.0)
    # 22500ms * 8 = 180s == default read -> nessun override
    F.set_latency_lookup(lambda u: 22500.0)
    assert F._timeout_for({"unique": "u"}) is None
    assert F._timeout_kw({"unique": "u"}) == {}


def test_disabled_returns_none():
    F.set_adaptive_timeout(enabled=False)
    F.set_latency_lookup(lambda u: 3000.0)
    assert F._timeout_for({"unique": "u"}) is None


def test_no_latency_returns_none():
    F.set_adaptive_timeout(enabled=True)
    F.set_latency_lookup(lambda u: None)
    assert F._timeout_for({"unique": "u"}) is None


def test_lookup_exception_is_safe():
    F.set_adaptive_timeout(enabled=True)
    F.set_latency_lookup(lambda u: (_ for _ in ()).throw(RuntimeError("x")))
    assert F._timeout_for({"unique": "u"}) is None


def test_parsing_adaptive_timeout():
    pol = Policy.from_dict({"adaptive_timeout_enabled": False,
                            "adaptive_timeout_floor_sec": 5,
                            "adaptive_timeout_multiplier": 3,
                            "adaptive_timeout_max_sec": 120})
    assert pol.adaptive_timeout_enabled is False
    assert pol.adaptive_timeout_floor_sec == 5.0
    assert pol.adaptive_timeout_multiplier == 3.0
    assert pol.adaptive_timeout_max_sec == 120.0
    assert Policy.from_dict({}).adaptive_timeout_enabled is True
