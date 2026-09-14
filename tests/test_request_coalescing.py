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
    M._coalesce_cache.clear()
    yield
    M._inflight_coalesce.clear()
    M._coalesce_cache.clear()


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


# ----------------------------------------------------------- CACHE POST-RISPOSTA
def _pol_cache(sec):
    return Policy.from_dict({"request_coalescing_enabled": True,
                             "request_coalescing_cache_sec": sec})


def test_cache_off_by_default_second_call_hits_factory():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return {"answer": calls["n"]}

    async def main():
        pol = Policy.from_dict({})
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        a = await _forward_coalesced(pol, payload, "p", factory)
        b = await _forward_coalesced(pol, payload, "p", factory)
        return a, b

    a, b = asyncio.run(main())
    assert calls["n"] == 2 and a != b          # comportamento storico


def test_cache_window_serves_repeat_without_factory():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return {"answer": 42, "data": ["x"]}

    async def main():
        pol = _pol_cache(30.0)
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        a = await _forward_coalesced(pol, payload, "p", factory)
        b = await _forward_coalesced(pol, payload, "p", factory)
        return a, b

    a, b = asyncio.run(main())
    assert calls["n"] == 1
    assert a == b
    assert a is not b                          # deepcopy: niente aliasing


def test_cache_does_not_follow_leader_mutations():
    """Il leader dopo il ritorno muta il suo dict (model/nx_deployment): la
    cache deve restare pulita (deepcopy al put)."""
    async def factory():
        return {"choices": [{"message": {"content": "ciao"}}]}

    async def main():
        pol = _pol_cache(30.0)
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        a = await _forward_coalesced(pol, payload, "p", factory)
        a["model"] = "MUTATO"                  # come fa il main post-fwd
        b = await _forward_coalesced(pol, payload, "p", factory)
        return b

    b = asyncio.run(main())
    assert "model" not in b


def test_cache_expiry_rehits_factory():
    calls = {"n": 0}

    async def factory():
        calls["n"] += 1
        return {"n": calls["n"]}

    async def main():
        pol = _pol_cache(0.01)
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        await _forward_coalesced(pol, payload, "p", factory)
        await asyncio.sleep(0.05)
        return await _forward_coalesced(pol, payload, "p", factory)

    r = asyncio.run(main())
    assert calls["n"] == 2 and r["n"] == 2


def test_cache_caps_size():
    for i in range(80):
        M._coalesce_cache_put(f"k{i}", {"i": i}, 1e12)
    assert len(M._coalesce_cache) <= M._COALESCE_CACHE_MAX
