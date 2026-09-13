"""thought_signature DUMMY-FILL per Gemini 3.

Se la history arriva da un altro modello (rotazione), i tool_call non hanno la
`thought_signature` reale. Google 3 valida solo il TURNO CORRENTE e documenta
due firme dummy ufficiali che saltano la validazione:
  - "skip_thought_signature_validator"
  - "context_engineering_is_the_way_to_go"
Con `thought_sig_dummy_fill` attivo il forwarder le inietta sui tool_call non
firmati del turno corrente, così Gemini resta eleggibile come gli altri.
"""
import contextlib

import pytest

from app import forwarder, thought_sig
from app.policy import Policy
from app.thought_sig import ThoughtSignatureCache

DUMMY = "skip_thought_signature_validator"


@pytest.fixture
def fresh_cache(monkeypatch):
    cache = ThoughtSignatureCache()
    monkeypatch.setattr(forwarder, "THOUGHT_SIGS", cache)
    return cache


@contextlib.contextmanager
def dummy_ctx(enabled: bool, value: str = DUMMY):
    tok = thought_sig.set_dummy_fill(enabled, value)
    try:
        yield
    finally:
        thought_sig.reset_dummy_fill(tok)


def _tc(tcid: str) -> dict:
    return {"id": tcid, "type": "function",
            "function": {"name": "f", "arguments": "{}"}}


def _sig(body, msg_idx=1):
    return (body["messages"][msg_idx]["tool_calls"][0]
            .get("extra_content", {}).get("google", {}).get(
                "thought_signature"))


# ----------------------------------------------------------- injection
def test_dummy_fill_current_turn(fresh_cache):
    body = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [_tc("call_1")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "r"},
    ]}
    with dummy_ctx(True):
        forwarder._inject_thought_signatures(body)
    assert _sig(body) == DUMMY


def test_dummy_fill_off_no_injection(fresh_cache):
    body = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [_tc("call_1")]},
    ]}
    with dummy_ctx(False):
        forwarder._inject_thought_signatures(body)
    assert _sig(body) is None


def test_no_dummy_in_previous_turn(fresh_cache):
    """Tool_call PRIMA dell'ultimo user -> turno precedente: non validato."""
    body = {"messages": [
        {"role": "assistant", "tool_calls": [_tc("call_old")]},
        {"role": "user", "content": "nuova domanda"},
    ]}
    with dummy_ctx(True):
        forwarder._inject_thought_signatures(body)
    assert _sig(body, 0) is None


def test_real_cached_sig_wins_over_dummy(fresh_cache):
    fresh_cache.store("call_1", "REAL")
    body = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [_tc("call_1")]},
    ]}
    with dummy_ctx(True):
        forwarder._inject_thought_signatures(body)
    assert _sig(body) == "REAL"


def test_dummy_value_configurable(fresh_cache):
    body = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [_tc("call_1")]},
    ]}
    with dummy_ctx(True, "context_engineering_is_the_way_to_go"):
        forwarder._inject_thought_signatures(body)
    assert _sig(body) == "context_engineering_is_the_way_to_go"


def test_no_dummy_when_value_empty(fresh_cache):
    body = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [_tc("call_1")]},
    ]}
    with dummy_ctx(True, ""):
        forwarder._inject_thought_signatures(body)
    assert _sig(body) is None


def test_client_signature_untouched(fresh_cache):
    tc = {"id": "call_1", "type": "function",
          "extra_content": {"google": {"thought_signature": "CLIENT"}},
          "function": {"name": "f", "arguments": "{}"}}
    body = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [tc]},
    ]}
    with dummy_ctx(True):
        forwarder._inject_thought_signatures(body)
    assert _sig(body) == "CLIENT"


def test_payload_not_polluted(fresh_cache):
    tc = _tc("call_1")
    payload = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [tc]},
    ]}
    body = dict(payload)
    with dummy_ctx(True):
        forwarder._inject_thought_signatures(body)
    assert "extra_content" not in payload["messages"][1]["tool_calls"][0]


# ----------------------------------------------------------- policy
def test_policy_defaults_dummy_fill_on():
    p = Policy()
    assert p.thought_sig_dummy_fill is True
    assert p.thought_sig_dummy_value == "skip_thought_signature_validator"


def test_policy_parses_dummy_fill_off():
    p = Policy.from_dict({"thought_sig_dummy_fill": False})
    assert p.thought_sig_dummy_fill is False


def test_policy_parses_dummy_value():
    p = Policy.from_dict({"thought_sig_dummy_value": "context_engineering_is_the_way_to_go"})
    assert p.thought_sig_dummy_value == "context_engineering_is_the_way_to_go"
