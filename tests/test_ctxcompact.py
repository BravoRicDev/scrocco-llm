"""Test per il troncamento cache-aware del contesto (app/ctxcompact.py)."""

from types import SimpleNamespace

from app.ctxcompact import (CtxCompactConfig, compact_tool_outputs,
                            create_ctxcompact_config,
                            ctxcompact_config_from_policy)


def _tool(content):
    return {"role": "tool", "tool_call_id": "c", "content": content}


def _asst():
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": "c", "type": "function",
                            "function": {"name": "bash", "arguments": "{}"}}]}


def _conversation(t1=5000, t2=5000, t3=5000):
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        _asst(), _tool("x" * t1),
        {"role": "user", "content": "u2"},
        _asst(), _tool("y" * t2),
        {"role": "user", "content": "u3"},
        _asst(), _tool("z" * t3),
    ]


class TestConfig:
    def test_defaults(self):
        c = CtxCompactConfig()
        assert c.enabled is True
        assert c.keep_turns == 4
        assert c.min_saved_tokens == 500
        assert "{n}" in c.stub_text

    def test_from_dict(self):
        c = create_ctxcompact_config({"cache_aware": {"context_truncation": {
            "enabled": False, "keep_turns": 2, "max_tool_output_chars": 50,
            "min_saved_tokens": 10, "stub_text": "[cut {n}]"}}})
        assert c.enabled is False
        assert c.keep_turns == 2
        assert c.max_tool_output_chars == 50
        assert c.min_saved_tokens == 10
        assert c.stub_text == "[cut {n}]"

    def test_from_policy(self):
        pol = SimpleNamespace(cache_ctx_truncation_enabled=True,
                              cache_ctx_keep_turns=2,
                              cache_ctx_max_tool_output_chars=100,
                              cache_ctx_min_saved_tokens=5,
                              cache_ctx_stub_text="[s {n}]")
        c = ctxcompact_config_from_policy(pol)
        assert c.keep_turns == 2
        assert c.max_tool_output_chars == 100
        assert c.stub_text == "[s {n}]"


class TestCompact:
    def test_stubs_only_old(self):
        msgs = _conversation()
        new, rep = compact_tool_outputs(msgs, CtxCompactConfig(keep_turns=2))
        assert rep["changed"] is True
        assert rep["stubbed"] == 1                 # solo il primo tool
        assert new[3]["content"].startswith("[tool output omesso")
        assert new[6]["content"] == "y" * 5000     # ultimi 2 turni intatti
        assert new[9]["content"] == "z" * 5000
        assert msgs[3]["content"] == "x" * 5000    # input non mutato

    def test_pairing_preserved(self):
        msgs = _conversation()
        new, _ = compact_tool_outputs(msgs, CtxCompactConfig(keep_turns=1))
        assert len(new) == len(msgs)
        assert [m["role"] for m in new] == [m["role"] for m in msgs]
        assert new[2].get("tool_calls") and new[3]["role"] == "tool"

    def test_idempotent(self):
        msgs = _conversation()
        cfg = CtxCompactConfig(keep_turns=2)
        once, rep1 = compact_tool_outputs(msgs, cfg)
        assert rep1["changed"] is True
        twice, rep2 = compact_tool_outputs(once, cfg)
        assert rep2["changed"] is False
        assert twice == once

    def test_small_output_untouched(self):
        msgs = _conversation(t1=50)
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=2, max_tool_output_chars=2000))
        assert rep["changed"] is False

    def test_min_saved_gate(self):
        msgs = _conversation(t1=800)
        new, rep = compact_tool_outputs(
            msgs, CtxCompactConfig(keep_turns=2, min_saved_tokens=100000))
        assert rep["changed"] is False
        assert new == msgs

    def test_no_user_message(self):
        msgs = [{"role": "system", "content": "s"}, _asst(), _tool("x" * 9000)]
        new, rep = compact_tool_outputs(msgs, CtxCompactConfig())
        assert rep["changed"] is False
