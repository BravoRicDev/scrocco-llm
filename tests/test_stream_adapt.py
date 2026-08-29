"""forwarder._json_to_sse: upstream che IGNORA stream:true (risponde JSON
intero) viene ADATTATO a SSE invece di scartare un deployment funzionante."""
import json

from app.forwarder import _json_to_sse


def _join(chunks):
    return b"".join(chunks)


def test_adapts_plain_completion():
    body = json.dumps({
        "id": "x", "created": 1, "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant",
                                             "content": "ciao mondo"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                  "total_tokens": 5},
    }).encode()
    out = _json_to_sse(body)
    assert out is not None
    blob = _join(out)
    assert b'"content": "ciao mondo"' in blob or b'"content":"ciao mondo"' in blob
    assert b'"finish_reason": "stop"' in blob or b'"finish_reason":"stop"' in blob
    assert blob.rstrip().endswith(b"data: [DONE]")
    assert b'"usage"' in blob


def test_adapts_tool_calls_without_text():
    body = json.dumps({
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "t1", "type": "function",
                            "function": {"name": "f", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}],
    }).encode()
    out = _json_to_sse(body)
    assert out is not None
    assert b"tool_calls" in _join(out)


def test_rejects_empty_body():
    body = json.dumps({"choices": [{"message": {"role": "assistant",
                                                "content": "  "}}]}).encode()
    assert _json_to_sse(body) is None


def test_rejects_provider_error_envelope():
    assert _json_to_sse(b'{"type":"error","error":{"message":"nope"}}') is None
    assert _json_to_sse(b'{"error":{"message":"nope"}}') is None


def test_rejects_non_json():
    assert _json_to_sse(b"<html>bad gateway</html>") is None
