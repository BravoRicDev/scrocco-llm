"""Hint quota dagli header X-RateLimit-*: reset esatto nei 429 e riduzione
proattiva del cap appreso (budget guard) prima del muro."""
import time

import httpx
import pytest

from app import forwarder as F
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text
t@x.com,m-b,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-B,text
"""


@pytest.fixture()
def router():
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, Policy.from_dict({}))
    os.unlink(path)


def test_parse_rate_limits_headers():
    resp = httpx.Response(200, headers={
        "X-RateLimit-Requests-Remaining": "12",
        "X-RateLimit-Requests-Limit": "30",
        "X-RateLimit-Requests-Reset": "1726248000",
        "X-RateLimit-Tokens-Remaining": "8000",
        "x-ratelimit-tokens-limit": "10000",
    })
    rl = F._rate_limits_from(resp)
    assert rl["requests_remaining"] == 12.0
    assert rl["requests_limit"] == 30.0
    assert rl["requests_reset"] == 1726248000.0
    assert rl["tokens_remaining"] == 8000.0
    assert rl["tokens_limit"] == 10000.0


def test_parse_no_headers():
    assert F._rate_limits_from(httpx.Response(200)) == {}


def test_retry_after_prefers_epoch_reset():
    """Reset esatto (epoch) su 429 vince su Retry-After: cooldown al
    millisecondo invece del floor."""
    now = time.time()
    resp = httpx.Response(429, headers={
        "Retry-After": "60",
        "X-RateLimit-Requests-Reset": str(int(now) + 25),
    })
    try:
        F.set_retry_after_floor(10)
        v = F._retry_after_from(resp, "", "groq")
        assert 20 <= v <= 30                # ~25s, non 60
    finally:
        F.set_retry_after_floor(10)


def test_retry_after_delta_reset():
    resp = httpx.Response(429, headers={
        "X-RateLimit-Requests-Reset": "8",   # delta-seconds, non epoch
    })
    try:
        F.set_retry_after_floor(0)           # nessun floor: 8s esatti
        assert F._retry_after_from(resp, "", "groq") == 8.0
    finally:
        F.set_retry_after_floor(10)


def test_note_rate_limit_reduces_learned_cap(router):
    dep = next(d for d in router.config.groups["scrocco-llm-test-32k"]
               if d["api_key"] == "K-A")
    u = dep["unique"]
    s = router.stats_for(u)
    s.min_cap_learned = 50.0                # cap gia' appreso alto
    router.note_rate_limit(u, {"requests_remaining": 2})
    assert s.min_cap_learned == 3.0         # 2 + safety 1
    # hint non-più-stringente: non alza mai il cap
    router.note_rate_limit(u, {"requests_remaining": 2})
    assert s.min_cap_learned == 3.0
    # sopra la soglia (3): nessun effetto
    router.note_rate_limit(u, {"requests_remaining": 10})
    assert s.min_cap_learned == 3.0


def test_note_rate_limit_noop_without_evidence(router):
    dep = next(d for d in router.config.groups["scrocco-llm-test-32k"]
               if d["api_key"] == "K-B")
    u = dep["unique"]
    router.note_rate_limit(u, {"requests_remaining": 2})
    s = router.stats_for(u)
    assert s.min_cap_learned == 3.0         # remaining + safety 1


def test_note_rate_limit_guard_disabled(router):
    dep = next(d for d in router.config.groups["scrocco-llm-test-32k"]
               if d["api_key"] == "K-A")
    u = dep["unique"]
    router.policy.budget_guard["enabled"] = False
    router.note_rate_limit(u, {"requests_remaining": 1})
    assert router.stats_for(u).min_cap_learned == 0.0


def test_note_rate_limit_handles_bad_input(router):
    dep = next(d for d in router.config.groups["scrocco-llm-test-32k"]
               if d["api_key"] == "K-A")
    u = dep["unique"]
    router.note_rate_limit(u, {})
    router.note_rate_limit(u, {"requests_remaining": "x"})
    router.note_rate_limit(u, None)
    assert router.stats_for(u).min_cap_learned == 0.0


def test_policy_parses_rate_hint_threshold():
    p = Policy.from_dict({"budget_guard": {"rate_hint_threshold": 5}})
    assert p.budget_guard["rate_hint_threshold"] == 5
    assert Policy.from_dict({}).budget_guard["rate_hint_threshold"] == 3
    with pytest.raises(ValueError):
        Policy.from_dict({"budget_guard": {"rate_hint_threshold": 0}})