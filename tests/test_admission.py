"""Porta di ammissione del processo (app/admission.py)."""
import asyncio

from app.admission import AdmissionGate, AdmissionLimits, AdmissionMiddleware, _looks_streaming


def test_stream_detection():
    sc = {"headers": [(b"content-type", b"application/json")]}
    assert _looks_streaming(sc, b'{"model":"x","stream": true}')
    assert not _looks_streaming(sc, b'{"model":"x","stream":false}')
    assert not _looks_streaming({"headers": [(b"content-type", b"multipart/form-data")]}, b'"stream":true')


def test_gate_queues_and_releases_fifo_ish():
    async def main():
        g = AdmissionGate()
        lim = AdmissionLimits(max_inflight=2, max_streams=1, queue_timeout_sec=5)
        assert await g.acquire(True, lim)            # 1 stream
        assert await g.acquire(False, lim)           # tetto totale raggiunto
        waiter = asyncio.create_task(g.acquire(False, lim))
        await asyncio.sleep(0.05)
        assert not waiter.done() and g.snapshot()["waiting"] == 1
        g.release(False)
        assert await asyncio.wait_for(waiter, 1) is True
        assert g.snapshot() == {"inflight": 2, "streams": 1, "waiting": 0}
        # un secondo stream attende anche se il totale lo permetterebbe
        g.release(False)
        s2 = asyncio.create_task(g.acquire(True, lim))
        await asyncio.sleep(0.05)
        assert not s2.done()
        g.release(True)
        assert await asyncio.wait_for(s2, 1) is True
    asyncio.run(main())


def test_gate_times_out():
    async def main():
        g = AdmissionGate()
        lim = AdmissionLimits(max_inflight=1, max_streams=0, queue_timeout_sec=0.1)
        assert await g.acquire(False, lim)
        assert await g.acquire(False, lim) is False
        assert g.snapshot() == {"inflight": 1, "streams": 0, "waiting": 0}
    asyncio.run(main())


def test_middleware_503_only_after_queue_timeout_and_passthrough_body():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    class Pol:
        admission_max_inflight = 1
        admission_max_streams = 1
        admission_queue_timeout_sec = 0.2

    app = FastAPI()
    app.add_middleware(AdmissionMiddleware, policy_getter=lambda: Pol)

    @app.post("/v1/echo")
    async def echo(request: Request):
        return {"body": (await request.body()).decode()}

    @app.get("/healthz")
    async def hz():
        return {"ok": True}

    import app.admission as adm
    with TestClient(app) as c:
        r = c.post("/v1/echo", content=b'{"a":1}', headers={"content-type": "application/json"})
        assert r.status_code == 200 and r.json() == {"body": '{"a":1}'}
        adm.GATE.inflight += 1                    # porta piena
        try:
            r = c.post("/v1/echo", json={"stream": True})
            assert r.status_code == 503 and r.headers["retry-after"] == "2"
            assert r.json()["error"]["code"] == "gateway_busy"
            assert c.get("/healthz").status_code == 200       # mai in coda
        finally:
            adm.GATE.inflight -= 1
