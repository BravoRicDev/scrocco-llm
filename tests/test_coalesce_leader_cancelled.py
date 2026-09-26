"""B4 - la cancellazione del LEADER non deve uccidere i waiter vivi.

`except BaseException as exc` in `app/main.py::_forward_coalesced` faceva
`entry["future"].set_exception(exc)`: se `exc` era un `asyncio.CancelledError`
(BaseException in py3.11+) la MORTE DEL LEADER veniva propagata a tutti i
waiter, che hanno connessioni vive e meritavano la risposta. Trigger reale:
uvicorn cancella i task in-flight al graceful shutdown (e su disconnect/
timeout esterni).

Contratto verificato qui:
  - leader cancellato        -> il waiter RIPROVA da solo (factory propria);
  - waiter PROPRIAMENTE cancellato (client andato via) -> CancelledError
    propagato, NESSUN ritentativo (niente lavoro fantasma);
  - errore NORMALE           -> resta condiviso ai waiter (regressione:
    il fix non deve trasformare ogni errore in un retry);
  - timeout del waiter       -> continua a ritentare (regressione).
"""
from __future__ import annotations

import asyncio

import pytest

import app.main as M
from app.main import _forward_coalesced
from app.policy import Policy

POL = Policy.from_dict({})
PAYLOAD = {"stream": False, "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture(autouse=True)
def _clear():
    M._inflight_coalesce.clear()
    yield
    M._inflight_coalesce.clear()


def test_leader_cancelled_waiter_survives_and_retries():
    """Il waiter sopravvive alla cancel del leader: ritenta da solo e
    riceve la risposta (prima moriva con CancelledError)."""
    calls: list[str] = []
    leader_started = asyncio.Event()

    async def leader_factory():
        calls.append("leader")
        leader_started.set()
        await asyncio.sleep(10)            # non finisce mai da solo
        return {"never": True}

    async def waiter_factory():
        calls.append("waiter")
        return {"answer": "waiter ha risposto"}

    async def main():
        leader = asyncio.create_task(
            _forward_coalesced(POL, PAYLOAD, "k", leader_factory))
        await leader_started.wait()
        await asyncio.sleep(0)             # il waiter si registra come waiter
        waiter = asyncio.create_task(
            _forward_coalesced(POL, PAYLOAD, "k", waiter_factory))
        await asyncio.sleep(0.01)
        assert calls == ["leader"]         # condivisione: un solo tentativo
        # shutdown / disconnect del LEADER
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        return await asyncio.wait_for(waiter, 2)

    out = asyncio.run(main())
    assert out == {"answer": "waiter ha risposto"}
    assert calls == ["leader", "waiter"]
    # nessuna entry pendente lasciata nel registro degli in-flight
    assert not M._inflight_coalesce


def test_waiter_properly_cancelled_does_not_retry():
    """Se il TASK DEL WAITER e' davvero cancellato (cancelling() > 0) il
    CancelledError viene propagato e la factory NON viene ritentata: niente
    lavoro fantasma su una connessione morta."""
    calls: list[str] = []
    leader_started = asyncio.Event()

    async def leader_factory():
        calls.append("leader")
        leader_started.set()
        await asyncio.sleep(10)
        return {"never": True}

    async def waiter_factory():
        calls.append("waiter")
        return {"answer": 42}

    async def main():
        leader = asyncio.create_task(
            _forward_coalesced(POL, PAYLOAD, "k", leader_factory))
        await leader_started.wait()
        await asyncio.sleep(0)
        waiter = asyncio.create_task(
            _forward_coalesced(POL, PAYLOAD, "k", waiter_factory))
        await asyncio.sleep(0.01)
        waiter.cancel()                    # il CLIENT e' andato via
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.sleep(0.01)
        assert calls == ["leader"]         # NESSUN ritentativo
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader

    asyncio.run(main())


def test_normal_error_still_shared_with_waiters():
    """Regressione: un errore NORMALE continua a essere condiviso (nessun
    retry duplicato verso l'upstream)."""
    calls: list[str] = []

    async def bad():
        calls.append("leader")
        await asyncio.sleep(0.01)
        raise ValueError("boom")

    async def waiter_factory():
        calls.append("waiter")
        return {"answer": 42}

    async def main():
        leader = asyncio.create_task(
            _forward_coalesced(POL, PAYLOAD, "k", bad))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(
            _forward_coalesced(POL, PAYLOAD, "k", waiter_factory))
        res = await asyncio.gather(leader, waiter, return_exceptions=True)
        return res

    res = asyncio.run(main())
    assert isinstance(res[0], ValueError) and isinstance(res[1], ValueError)
    assert calls == ["leader"]


def test_timeout_path_still_retries():
    """La via TimeoutError (waiter lento oltre il TTL) continua a ritentare:
    il nuovo ramo CancelledError non deve averla sostituita."""
    calls: list[str] = []

    async def slow():
        calls.append("leader")
        await asyncio.sleep(5)
        return {"never": True}

    async def waiter_factory():
        calls.append("waiter")
        return {"answer": 42}

    pol = Policy.from_dict({"request_coalescing_ttl_sec": 0.05})

    async def main():
        leader = asyncio.create_task(_forward_coalesced(pol, PAYLOAD, "k", slow))
        await asyncio.sleep(0)
        return await asyncio.wait_for(
            _forward_coalesced(pol, PAYLOAD, "k", waiter_factory), 2)

    assert asyncio.run(main()) == {"answer": 42}
    assert calls == ["leader", "waiter"]
