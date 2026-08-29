"""Stima del contesto fedele al payload, guardia max_input universale e
ctx nei percorsi di fallback/sticky."""
import json
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router, estimate_tokens

MSG = [{"role": "user", "content": "hi"}]

HEAVY_TOOLS = [{"type": "function",
                "function": {"name": f"tool_{i}",
                             "description": "x" * 400,
                             "parameters": {"type": "object"}}}
               for i in range(10)]

# un solo deployment con max_input dichiarato 8000 su finestra 128k:
# senza la guardia universale un ctx=90k lo sceglieva comunque.
CSV_GUARD = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
t@x.com,gpt-oss-120b,groq,https://api.groq.com/openai/v1,free,128,8000,0,sk-FAKE-KEY-12345678
"""

# due dims (32k/128k) per testare sticky + escalation con payload cresciuto
CSV_STICKY = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
t@x.com,gpt-oss-120b,groq,https://api.groq.com/openai/v1,free,32,32000,0,sk-FAKE-KEY-12345678
t@x.com,qwen/qwen2.5-vl-72b,groq,https://api.groq.com/openai/v1,free,128,128000,0,sk-FAKE-KEY-12345678
"""


@pytest.fixture()
def router_guard():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_GUARD)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, Policy.from_dict({}))
    os.unlink(path)


@pytest.fixture()
def router_sticky():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_STICKY)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, Policy.from_dict({}))
    os.unlink(path)


# ------------------------------------------------------------- stima payload
def test_estimate_counts_tools():
    base = estimate_tokens(MSG)
    est = estimate_tokens(MSG, tools=HEAVY_TOOLS)
    # 10 schemi da ~430 caratteri / 4 -> oltre 1000 token extra
    assert est > base + 500


def test_estimate_counts_tool_calls_and_reasoning():
    msgs = [{"role": "assistant", "content": None,
             "tool_calls": [{"id": "c", "type": "function",
                             "function": {"name": "shell",
                                          "arguments": json.dumps(
                                              {"cmd": "x" * 800})}}],
             "reasoning_content": "r" * 400}]
    assert estimate_tokens(msgs) >= 100


def test_estimate_backward_compat_no_tools():
    assert estimate_tokens(MSG) == estimate_tokens(MSG, tools=None)
    assert estimate_tokens([]) == 0
    assert estimate_tokens(None) == 0


def test_estimate_content_none_only_tool_calls_not_zero():
    msgs = [{"role": "assistant", "content": None,
             "tool_calls": [{"function": {"name": "f",
                                          "arguments": "{\"a\":1}"}}]}]
    assert estimate_tokens(msgs) > 0


# ------------------------------------------------- guardia max_input (P2)
def test_pick_deployment_respects_max_input(router_guard):
    g = "scrocco-llm-test-128k"
    assert router_guard.pick_deployment(g, frozenset(), None,
                                        ctx=90_000) is None
    assert router_guard.pick_deployment(g, frozenset(), None,
                                        ctx=5_000) is not None


def test_walk_chain_respects_max_input(router_guard):
    u = router_guard.config.chains["test"][0]
    assert router_guard._walk_chain([u], None, frozenset(),
                                    90_000) is None
    assert router_guard._walk_chain([u], None, frozenset()) is not None


def test_fallback_after_propagates_ctx(router_guard):
    assert router_guard.fallback_after("test", None, frozenset(),
                                       90_000) is None
    assert router_guard.fallback_after("test", None, frozenset()) \
        is not None


def test_fallback_next_ladder_branch_propagates_ctx(router_guard):
    dep = router_guard.pick_deployment("scrocco-llm-test-128k",
                                       frozenset(), None)
    nxt = router_guard.fallback_next("test", dep, frozenset(),
                                     scope="group", ctx=90_000)
    assert nxt is None                      # scala finita: guardia blocca


# ------------------------------------------------------- sticky con ctx
def test_sticky_escalates_on_grown_payload(router_sticky):
    small = router_sticky.resolve_group_for_request(
        "scrocco-llm-test", MSG, "sess-1", frozenset({"text"}), 20_000)
    assert small.endswith("-32k")
    grown = router_sticky.resolve_group_for_request(
        "scrocco-llm-test", MSG, "sess-1", frozenset({"text"}), 90_000)
    assert grown is not None and grown.endswith("-128k")


def test_sticky_kept_when_payload_fits(router_sticky):
    first = router_sticky.resolve_group_for_request(
        "scrocco-llm-test", MSG, "sess-2", frozenset({"text"}), 20_000)
    again = router_sticky.resolve_group_for_request(
        "scrocco-llm-test", MSG, "sess-2", frozenset({"text"}), 25_000)
    assert again == first
