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


# ================= F5: frontiera LAZY del reasoning_content =================
from app.histnorm import HistNormConfig as _HNC   # noqa: E402


def _conv_r():
    return [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0",
         "reasoning_content": "R0" * 200},
        {"role": "tool", "tool_call_id": "c1", "content": "t1"},
        {"role": "assistant", "content": "a1",
         "reasoning_content": "R1" * 200},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a2",
         "reasoning_content": "R2" * 200},
        {"role": "user", "content": "u2"},
    ]


def test_reasoning_strip_old_keep_recent():
    out, rep = normalize_messages(_conv_r(), _HNC(tail_only=False, drop_orphan_tool=False))
    assert "reasoning_content" not in out[2]
    assert "reasoning_content" not in out[4]
    assert out[6]["reasoning_content"]                       # l'ultimo resta
    assert rep["reasoning_trimmed"] == 2 and rep["changed"]


def test_reasoning_keep_recent_n():
    out, rep = normalize_messages(
        _conv_r(), _HNC(tail_only=False, drop_orphan_tool=False, reasoning_keep_recent=2))
    assert out[4].get("reasoning_content") and out[6].get("reasoning_content")
    assert "reasoning_content" not in out[2]
    assert rep["reasoning_trimmed"] == 1


def test_reasoning_truncate_mode():
    out, rep = normalize_messages(
        _conv_r(), _HNC(tail_only=False, drop_orphan_tool=False, reasoning_content_max_chars=100))
    rc = out[2]["reasoning_content"]
    assert rc.startswith("R0") and "troncato" in rc and len(rc) < 420
    # deterministico: due passate = byte identici
    out2, _ = normalize_messages(out, _HNC(tail_only=False,
                                           reasoning_content_max_chars=100))
    assert out2[2]["reasoning_content"] == rc


def test_reasoning_off_flag():
    out, rep = normalize_messages(
        _conv_r(), _HNC(tail_only=False, drop_orphan_tool=False, reasoning_content_max_chars=-1))
    assert all(m.get("reasoning_content") for m in out
               if m.get("role") == "assistant")
    assert rep.get("reasoning_trimmed", 0) == 0


def test_reasoning_idempotent_noop_light():
    cfg = _HNC(tail_only=False, drop_orphan_tool=False)
    a, _ = normalize_messages(_conv_r(), cfg)
    b, _ = normalize_messages(a, cfg)
    assert a == b                                            # byte-stabile
