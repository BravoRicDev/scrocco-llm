from app.histnorm import HistNormConfig, normalize_messages


def _asst(content=None, tool_calls=None):
    m = {"role": "assistant"}
    if content is not None:
        m["content"] = content
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    return m


def test_drop_orphan_tool():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "call_x", "content": "orphan"},
    ]
    out, rep = normalize_messages(msgs)
    assert rep["shown_orphan_tool"] == 1
    assert all(m["role"] != "tool" for m in out)


def test_keep_valid_tool():
    msgs = [
        {"role": "user", "content": "hi"},
        _asst(tool_calls=[{"id": "call_1", "type": "function",
                           "function": {"name": "bash", "arguments": "{}"}}]),
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    out, rep = normalize_messages(msgs)
    assert rep["shown_orphan_tool"] == 0
    assert len(out) == 3


def test_dangling_tool_calls():
    msgs = [
        {"role": "user", "content": "hi"},
        _asst(tool_calls=[{"id": "call_1", "type": "function",
                           "function": {"name": "bash", "arguments": "{}"}}]),
    ]
    out, rep = normalize_messages(msgs)
    assert rep["dangling_tool_calls"] == 1
    # l'assistant vuoto con call pendente viene scartato
    assert all(not m.get("tool_calls") for m in out)


def test_empty_assistant_dropped():
    msgs = [{"role": "user", "content": "hi"},
            _asst(content="   "),
            {"role": "assistant", "content": "ok"}]
    out, rep = normalize_messages(msgs)
    assert rep["empty_assistant"] == 1
    assert len(out) == 2


def test_prefix_preserved():
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "tool", "tool_call_id": "ghost", "content": "orphan"}]
    out, rep = normalize_messages(msgs)
    assert rep["tail_start"] == 3
    assert out[:3] == msgs[:3]          # prefisso intatto
    assert len(out) == 4


def test_dedupe_system_full():
    msgs = [{"role": "system", "content": "s"},
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"}]
    out, rep = normalize_messages(msgs, HistNormConfig(tail_only=False))
    assert rep["dup_system"] == 1
    assert len(out) == 2


def test_disabled():
    msgs = [{"role": "user", "content": "u"},
            {"role": "tool", "tool_call_id": "x", "content": "c"}]
    out, rep = normalize_messages(msgs, HistNormConfig(enabled=False))
    assert out is msgs
