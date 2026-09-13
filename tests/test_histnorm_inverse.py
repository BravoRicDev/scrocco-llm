"""Orfani INVERSI nella history: assistant con tool_calls senza il `tool`
col risultato (anche solo per UNA delle call). La catena viene bonificata
senza sintetizzare risultati e senza mai lasciare il payload rotto."""
from app.histnorm import HistNormConfig, normalize_messages


def _tc(cid):
    return {"id": cid, "type": "function",
            "function": {"name": "bash", "arguments": "{}"}}


def test_partial_dangling_keeps_valid_call():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "run",
         "tool_calls": [_tc("call_1"), _tc("call_2")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    out, rep = normalize_messages(msgs)
    assert rep["dangling_tool_calls"] == 1
    assert rep["dropped_tool_calls"] == 1
    asst = [m for m in out if m["role"] == "assistant"][0]
    assert [tc["id"] for tc in asst["tool_calls"]] == ["call_1"]
    # il risultato valido resta in catena
    assert any(m.get("role") == "tool"
               and m.get("tool_call_id") == "call_1" for m in out)


def test_all_dangling_keeps_content():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "thinking...",
         "tool_calls": [_tc("call_1")]},
    ]
    out, rep = normalize_messages(msgs)
    assert rep["dropped_tool_calls"] == 1
    asst = [m for m in out if m["role"] == "assistant"][0]
    assert asst["content"] == "thinking..."
    assert "tool_calls" not in asst


def test_all_dangling_empty_assistant_dropped():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [_tc("call_1")]},
    ]
    out, rep = normalize_messages(msgs)
    assert rep["dropped_tool_calls"] == 1
    assert all(m["role"] != "assistant" for m in out)


def test_valid_chain_untouched():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [_tc("call_1")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    out, rep = normalize_messages(msgs)
    assert rep["dangling_tool_calls"] == 0
    assert rep["dropped_tool_calls"] == 0
    assert len(out) == 4


def test_result_in_head_still_counts():
    # il risultato e' nel prefisso (head): non deve essere considerato orfano
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "tool_calls": [_tc("call_1")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "done"},
    ]
    out, rep = normalize_messages(msgs)
    assert rep["dangling_tool_calls"] == 0
    assert any(m.get("tool_calls") for m in out)


def test_disable_dangling_keeps_broken_chain():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "x", "tool_calls": [_tc("call_1")]},
    ]
    cfg = HistNormConfig(drop_dangling_tool_calls=False)
    out, rep = normalize_messages(msgs, cfg)
    assert rep["dangling_tool_calls"] == 0
    assert any(m.get("tool_calls") for m in out)
