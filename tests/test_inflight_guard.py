"""Limitatore PREDITIVO anti-burst (budget guard + inflight).

Semantica: la soglia scatta SOLO con un cap appreso da un 429 reale. Quando
un deployment supera `safety_ratio` (default 0.8) del cap, viene saltato a
favore di un fratello ancora sotto soglia PRIMA che l'upstream risponda 429.
"""
import os
import tempfile

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


def test_below_threshold_not_saturated(router):
    dep = _dep(router, "K-A")
    s = router.stats_for(dep["unique"])
    s.min_cap_learned = 10.0
    s.minute_calls = 7                     # < 8 (80% di 10)
    assert router._virtually_saturated(dep, 0.8, True) is False


def test_at_threshold_saturated(router):
    dep = _dep(router, "K-A")
    s = router.stats_for(dep["unique"])
    s.min_cap_learned = 10.0
    s.minute_calls = 8                     # == 80%
    assert router._virtually_saturated(dep, 0.8, True) is True


def test_no_cap_no_saturation(router):
    dep = _dep(router, "K-A")
    s = router.stats_for(dep["unique"])
    s.minute_calls = 999
    assert router._virtually_saturated(dep, 0.8, True) is False


def test_inflight_covers_minute_rollover(router):
    dep = _dep(router, "K-A")
    s = router.stats_for(dep["unique"])
    s.min_cap_learned = 10.0
    s.minute_calls = 0                     # minuto appena ruotato
    s.inflight = 9                         # ma 9 richieste sono ancora in volo
    assert router._virtually_saturated(dep, 0.8, True) is True
    assert router._virtually_saturated(dep, 0.8, False) is False


def test_day_cap_saturation(router):
    dep = _dep(router, "K-A")
    s = router.stats_for(dep["unique"])
    s.day_cap_learned = 100.0
    s.day_calls = 85
    assert router._virtually_saturated(dep, 0.8, True) is True


def test_filter_deviates_to_sibling(router):
    a, b = _dep(router, "K-A"), _dep(router, "K-B")
    sa = router.stats_for(a["unique"])
    sa.min_cap_learned = 10.0
    sa.minute_calls = 9
    out = router._apply_inflight_guard([a, b])
    assert [d["unique"] for d in out] == [b["unique"]]


def test_filter_keeps_all_when_all_saturated(router):
    a, b = _dep(router, "K-A"), _dep(router, "K-B")
    for dep in (a, b):
        s = router.stats_for(dep["unique"])
        s.min_cap_learned = 10.0
        s.minute_calls = 9
    out = router._apply_inflight_guard([a, b])
    assert len(out) == 2


def test_safety_ratio_zero_disables(router):
    a, b = _dep(router, "K-A"), _dep(router, "K-B")
    router.policy.budget_guard["safety_ratio"] = 0
    sa = router.stats_for(a["unique"])
    sa.min_cap_learned = 10.0
    sa.minute_calls = 10
    out = router._apply_inflight_guard([a, b])
    assert len(out) == 2


def test_guard_disabled(router):
    a, b = _dep(router, "K-A"), _dep(router, "K-B")
    router.policy.budget_guard["enabled"] = False
    sa = router.stats_for(a["unique"])
    sa.min_cap_learned = 10.0
    sa.minute_calls = 10
    out = router._apply_inflight_guard([a, b])
    assert len(out) == 2


def test_parsing_safety_ratio_and_count_inflight():
    pol = Policy.from_dict({"budget_guard": {"safety_ratio": 0.5,
                                             "count_inflight": False}})
    assert pol.budget_guard["safety_ratio"] == 0.5
    assert pol.budget_guard["count_inflight"] is False
