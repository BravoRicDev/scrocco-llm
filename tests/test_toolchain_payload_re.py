"""Errori di CATENA TOOL rotta devono essere provider-side (rotazione, mai
raw al client): la history viene comunque bonificata da histnorm."""
from app.forwarder import _PAYLOAD_SCHEMA_RE


def _m(body: str) -> bool:
    return bool(_PAYLOAD_SCHEMA_RE.search(body))


def test_anthropic_tool_result_missing():
    assert _m("Each `tool_use` block must have a corresponding "
              "`tool_result` block in the next message.")
    assert _m("unexpected `tool_use_id` found in `tool_result` blocks: "
              "toolu_123. Each `tool_result` block must have a "
              "corresponding `tool_use` block in the previous message.")


def test_openai_tool_role_requires_tool_calls():
    assert _m("Invalid parameter: messages with role 'tool' must be a "
              "response to a preceding message with 'tool_calls'.")
    assert _m("An assistant message with 'tool_calls' must be followed by "
              "tool messages responding to each tool_call_id")


def test_unknown_tool_call_id():
    assert _m("tool_call_id 'call_abc' not found")
    assert _m("unknown tool_call_id")


def test_no_false_positive_on_generic_text():
    assert not _m("rate limit exceeded, retry in 30s")
    assert not _m("The requested model is not available.")
