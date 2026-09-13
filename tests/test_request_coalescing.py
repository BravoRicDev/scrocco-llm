"""Inflight request coalescing: richieste identiche in volo condividono una
sola chiamata upstream (solo non-streaming)."""
import asyncio
import os
import tempfile

import pytest

import app.main as M
from app.main import _coalesce_key, _forward_coalesced
from app.policy import Policy


@pytest.fixture(autouse=True)
def _clear():
    M._inflight_coalesce.clear()
    yield
    M._inflight_coalesce.clear()


def test_coalesce_key_deterministic_and_order_independent():
    a = {"b": 1, "a": [1, 2, {"c": 3}]}
    b = {"a": [1, 2, {"c": 3}], "b": 1}
    assert _coalesce_key(a) == _coalesce_key(b)
    assert _coalesce_key(a, "prof") != _coalesce_key(a, "other")
    assert _coalesce_key({"a": 1}) != _coalesce_key({"a": 2})


def test_identical_payload_coalesced_once():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {"answer": 42}

    async def main():
        pol = Policy.from_dict({})
        payload = {"messages": [{"role": "user", "content": "hi"}],
                   "stream": False}
        return await asyncio.gather(
            _forward_coalesced(pol, payload, "p", factory),
            _forward_coalesced(pol, payload, "p", factory))

    r1, r2 = asyncio.run(main())
    assert calls["n"] == 1
    assert r1 == r2 == {"answer": 42}


def test_different_payload_not_coalesced():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return {"ok": True}

    async def main():
        pol = Policy.from_dict({})
        await asyncio.gather(
            _forward_coalesced(pol, {"x": 1, "stream": False}, "p", factory),
            _forward_coalesced(pol, {"x": 2, "stream": False}, "p", factory))

    asyncio.run(main())
    assert calls["n"] == 2


def test_exception_propagates_to_waiters():
    async def bad():
        raise ValueError("boom")

    async def main():
        pol = Policy.from_dict({})
        p = {"stream": False, "x": 1}
        return await asyncio.gather(
            _forward_coalesced(pol, p, "k", bad),
            _forward_coalesced(pol, p, "k", bad),
            return_exceptions=True)

    res = asyncio.run(main())
    assert all(isinstance(x, ValueError) for x in res)


def test_disabled_does_not_coalesce():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return {"ok": True}

    async def main():
        pol = Policy.from_dict({"request_coalescing_enabled": False})
        p = {"stream": False, "x": 1}
        await asyncio.gather(
            _forward_coalesced(pol, p, "p", factory),
            _forward_coalesced(pol, p, "p", factory))

    asyncio.run(main())
    assert calls["n"] == 2


def test_stream_bypasses_coalescing():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return {"ok": True}

    async def main():
        pol = Policy.from_dict({})
        p = {"stream": True, "x": 1}
        await asyncio.gather(
            _forward_coalesced(pol, p, "p", factory),
            _forward_coalesced(pol, p, "p", factory))

    asyncio.run(main())
    assert calls["n"] == 2
