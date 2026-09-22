"""Unit test dell'assemblatore SSE→JSON (`protocols.sse_to_chat_obj`), usato
dal redirect non-stream -> motore stream sotto hold."""
import json

import pytest

from app.protocols import (chat_obj_to_sse, sse_to_chat_obj)


def _chunk(**kw):
    return ("data: " + json.dumps(kw) + "\n\n").encode()


def test_content_multichunk():
    chunks = [
        _chunk(id="c1", created=1, model="m",
               choices=[{"index": 0, "delta": {"role": "assistant"},
                         "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {"content": "Ciao"},
                         "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {"content": " mondo"},
                         "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
        b"data: [DONE]\n\n",
    ]
    obj = sse_to_chat_obj(chunks)
    assert obj["id"] == "c1" and obj["model"] == "m"
    assert obj["object"] == "chat.completion"
    ch = obj["choices"][0]
    assert ch["message"]["content"] == "Ciao mondo"
    assert ch["finish_reason"] == "stop"


def test_tool_calls_merge_by_index():
    chunks = [
        _chunk(choices=[{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "t0", "type": "function",
             "function": {"name": "get_weather", "arguments": ""}}]},
            "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "{\"ci"}}]},
            "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "ty\":\"Roma\"}"}}]},
            "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
    ]
    obj = sse_to_chat_obj(chunks)
    tcs = obj["choices"][0]["message"]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "t0"
    assert tcs[0]["function"]["name"] == "get_weather"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"city": "Roma"}
    assert obj["choices"][0]["finish_reason"] == "tool_calls"


def test_reasoning_and_usage():
    chunks = [
        _chunk(choices=[{"index": 0, "delta": {"reasoning_content": "penso "},
                         "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {"reasoning_content": "molto"},
                         "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {"content": "ok"},
                         "finish_reason": None}]),
        _chunk(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}],
               usage={"prompt_tokens": 10, "completion_tokens": 3,
                      "total_tokens": 13}),
    ]
    obj = sse_to_chat_obj(chunks)
    msg = obj["choices"][0]["message"]
    assert msg["reasoning_content"] == "penso molto"
    assert msg["content"] == "ok"
    assert obj["usage"]["completion_tokens"] == 3


def test_error_event_raises():
    chunks = [_chunk(error={"message": "boom", "type": "upstream_error"})]
    with pytest.raises(ValueError):
        sse_to_chat_obj(chunks)


def test_empty_raises():
    with pytest.raises(ValueError):
        sse_to_chat_obj([b"data: [DONE]\n\n"])


def test_roundtrip_chat_obj_to_sse():
    src = {
        "id": "x", "object": "chat.completion", "created": 2, "model": "mm",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "testo"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2,
                  "total_tokens": 3},
    }
    obj = sse_to_chat_obj(chat_obj_to_sse(src))
    assert obj["choices"][0]["message"]["content"] == "testo"
    assert obj["choices"][0]["finish_reason"] == "stop"
    assert obj["usage"]["total_tokens"] == 3
