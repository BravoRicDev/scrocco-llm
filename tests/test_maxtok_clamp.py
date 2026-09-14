"""Clamp di max_tokens: input stimato + output richiesto non deve superare il
context window del deployment (altrimenti l'upstream risponde -400/-413)."""
from app.forwarder import clamp_max_tokens
from app.router import estimate_tokens


def _dep(mi=32768):
    return {"unique": "d1", "max_input_tokens": mi}


def _ctx(msgs):
    return estimate_tokens(msgs)


def test_clamp_reduces_when_room_is_tighter():
    msgs = [{"role": "user", "content": "x" * 4000}]
    body = {"messages": msgs, "max_tokens": 32000}
    clamp_max_tokens(body, _dep())
    assert body["max_tokens"] == max(1, 32768 - _ctx(msgs))
    assert body["max_tokens"] < 32000


def test_clamp_noop_when_fits():
    body = {"messages": [{"role": "user", "content": "ciao"}],
            "max_tokens": 100}
    clamp_max_tokens(body, _dep())
    assert body["max_tokens"] == 100


def test_clamp_uses_max_completion_tokens():
    msgs = [{"role": "user", "content": "x" * 4000}]
    body = {"messages": msgs, "max_completion_tokens": 50000}
    clamp_max_tokens(body, _dep())
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] == max(1, 32768 - _ctx(msgs))


def test_clamp_floor_is_one():
    body = {"messages": [{"role": "user", "content": "x" * 200000}],
            "max_tokens": 50000}
    clamp_max_tokens(body, _dep())
    assert body["max_tokens"] == 1


def test_clamp_skips_unknown_max_input():
    body = {"messages": [{"role": "user", "content": "x" * 4000}],
            "max_tokens": 50000}
    clamp_max_tokens(body, {"unique": "d1", "max_input_tokens": 0})
    assert body["max_tokens"] == 50000


def test_clamp_skips_when_no_limit_field():
    body = {"messages": [{"role": "user", "content": "x" * 4000}]}
    clamp_max_tokens(body, _dep())
    assert "max_tokens" not in body
