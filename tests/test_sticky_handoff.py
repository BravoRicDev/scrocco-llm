"""Warm handoff dello sticky session quando il failover resta nella stessa
famiglia di modello (preserva la prompt cache del provider)."""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,shared-model,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text
t@x.com,shared-model,deepinfra,https://api.deepinfra.com/v1/openai,free,32,8000,0,K-B,text
t@x.com,other-model,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-C,text
"""
GRP = "scrocco-llm-test-32k"
SES = "fq_test123"


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    yield r
    os.unlink(path)


def _dep(router, key):
    return next(d for d in router.config.groups[GRP] if d["api_key"] == key)


def test_same_family_moves_sticky(router):
    a, b = _dep(router, "K-A"), _dep(router, "K-B")
    assert a["family"] == b["family"]
    router.dep_sticky_set(SES, a["unique"])
    assert router.sticky_handoff(SES, b) is True
    assert router.dep_sticky_get(SES) == b["unique"]


def test_different_family_keeps_sticky(router):
    a, c = _dep(router, "K-A"), _dep(router, "K-C")
    assert a["family"] != c["family"]
    router.dep_sticky_set(SES, a["unique"])
    assert router.sticky_handoff(SES, c) is False
    assert router.dep_sticky_get(SES) == a["unique"]


def test_disabled_policy_no_move(router):
    a, b = _dep(router, "K-A"), _dep(router, "K-B")
    router.policy.sticky_handoff_same_family = False
    router.dep_sticky_set(SES, a["unique"])
    assert router.sticky_handoff(SES, b) is False
    assert router.dep_sticky_get(SES) == a["unique"]


def test_no_sticky_no_move(router):
    b = _dep(router, "K-B")
    assert router.sticky_handoff("nonexistent", b) is False


def test_none_next_no_move(router):
    a = _dep(router, "K-A")
    router.dep_sticky_set(SES, a["unique"])
    assert router.sticky_handoff(SES, None) is False


def test_same_target_no_move(router):
    a = _dep(router, "K-A")
    router.dep_sticky_set(SES, a["unique"])
    assert router.sticky_handoff(SES, a) is False


def test_missing_session_no_move(router):
    b = _dep(router, "K-B")
    assert router.sticky_handoff(None, b) is False


def test_parsing_sticky_handoff_flag():
    assert Policy.from_dict({}).sticky_handoff_same_family is True
    assert Policy.from_dict(
        {"sticky_handoff_same_family": False}
    ).sticky_handoff_same_family is False
