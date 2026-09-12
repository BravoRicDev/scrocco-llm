"""Test per il rilevamento dei fake tool-call (app/fakecall.py)."""

from types import SimpleNamespace

from app.fakecall import (DEFAULT_PATTERNS, FakeCallConfig,
                          create_fake_call_config, fake_config_from_policy,
                          is_escalation_group, looks_like_fake_tool_call,
                          message_fake_pattern)


def _msg(content, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}


def _payload(with_tools=True):
    p = {"messages": [{"role": "user", "content": "hi"}]}
    if with_tools:
        p["tools"] = [{"type": "function",
                       "function": {"name": "bash", "parameters": {}}}]
    return p


class TestLooksLike:
    def test_detect_arg_key(self):
        assert looks_like_fake_tool_call(
            "<bash><arg_key>command</arg_key>") == "<arg_key>"

    def test_detect_antml(self):
        assert looks_like_fake_tool_call("antml:invoke name=edit") == "antml:"

    def test_clean_text_none(self):
        assert looks_like_fake_tool_call("Ciao, come posso aiutarti?") is None

    def test_disabled(self):
        cfg = FakeCallConfig(enabled=False)
        assert looks_like_fake_tool_call("<arg_key>x</arg_key>", cfg) is None

    def test_non_string(self):
        assert looks_like_fake_tool_call(None) is None


class TestMessage:
    def test_detect_with_tools_no_toolcalls(self):
        pat = message_fake_pattern(_msg("<edit><arg_value>x</arg_value>"),
                                   _payload(), FakeCallConfig())
        assert pat == "<arg_value>"

    def test_ignore_when_tool_calls_present(self):
        data = _msg("ok", tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "bash", "arguments": "{}"}}])
        assert message_fake_pattern(data, _payload(), FakeCallConfig()) is None

    def test_ignore_without_tools(self):
        assert message_fake_pattern(_msg("<edit>x</edit>"),
                                    _payload(False), FakeCallConfig()) is None

    def test_content_list_parts(self):
        data = _msg([{"type": "text", "text": "<bash> ls"}])
        assert message_fake_pattern(data, _payload(),
                                    FakeCallConfig()) == "<bash"


class TestEscalationGroup:
    def test_go(self):
        assert is_escalation_group("scrocco-llm-mioaruba-go", "-go", "-fallback")

    def test_fallback(self):
        assert is_escalation_group("scrocco-llm-mioaruba-fallback",
                                   "-go", "-fallback")

    def test_normal(self):
        assert not is_escalation_group("scrocco-llm-mioaruba-200k",
                                       "-go", "-fallback")


class TestConfig:
    def test_default(self):
        cfg = create_fake_call_config(None)
        assert cfg.enabled is True
        assert cfg.max_escalations == 2
        assert "antml:" in cfg.patterns
        assert set(cfg.patterns) == set(DEFAULT_PATTERNS)

    def test_from_dict(self):
        cfg = create_fake_call_config({"tool_repair": {"fake_call": {
            "enabled": False, "patterns": ["<x>"], "max_escalations": 5,
            "stream_hold_max_bytes": 100, "stream_hold_timeout_ms": 200}}})
        assert cfg.enabled is False
        assert cfg.patterns == ("<x>",)
        assert cfg.max_escalations == 5
        assert cfg.stream_hold_max_bytes == 100
        assert cfg.stream_hold_timeout_ms == 200

    def test_from_policy(self):
        pol = SimpleNamespace(tool_repair_fake_call_enabled=True,
                              tool_repair_fake_call_patterns=("<p>",),
                              tool_repair_fake_call_max_escalations=3,
                              tool_repair_fake_call_hold_max_bytes=7,
                              tool_repair_fake_call_hold_timeout_ms=8)
        cfg = fake_config_from_policy(pol)
        assert cfg.patterns == ("<p>",)
        assert cfg.max_escalations == 3
        assert cfg.stream_hold_max_bytes == 7
        assert cfg.stream_hold_timeout_ms == 8
