"""stream_response: se la connessione cade MENTRE si legge il body d'errore
(resp.aread), NON deve propagare un errore httpx grezzo (che 500-erebbe la
richiesta) -> _safe_aread torna b"" e si alza un UpstreamError con lo status
che gia' conosciamo."""
import asyncio

import httpx
import pytest

from app.forwarder import Forwarder, UpstreamError, _safe_aread


class _BoomStream:
    """resp-like: headers/status ok, ma aread() esplode (conn caduta)."""
    def __init__(self, status=502, ctype="application/json"):
        self.status_code = status
        self.headers = {"content-type": ctype}

    async def aread(self):
        raise httpx.RemoteProtocolError("peer closed connection")

    async def aclose(self):
        pass


def test_safe_aread_swallows_httpx_error():
    r = asyncio.run(_safe_aread(_BoomStream()))
    assert r == b""


def test_safe_aread_timeout():
    class _Slow:
        async def aread(self):
            await asyncio.sleep(5)
            return b"late"
    r = asyncio.run(_safe_aread(_Slow(), timeout=0.05))
    assert r == b""


def test_stream_response_retryable_body_read_fails_raises_upstreamerror():
    fwd = Forwarder(client=httpx.AsyncClient())

    async def _fake_send(req, stream=True):
        return _BoomStream(status=503)
    fwd.client.send = _fake_send    # type: ignore

    with pytest.raises(UpstreamError) as ei:
        asyncio.run(fwd.stream_response(
            {"model": "x", "api_base": "https://u/v1", "api_key": "k"},
            {"model": "x", "messages": [], "stream": True}))
    assert ei.value.status == 503                 # status noto anche senza body


def test_stream_response_4xx_body_read_fails_raises_upstreamerror():
    fwd = Forwarder(client=httpx.AsyncClient())

    async def _fake_send(req, stream=True):
        return _BoomStream(status=400)
    fwd.client.send = _fake_send    # type: ignore

    with pytest.raises(UpstreamError) as ei:
        asyncio.run(fwd.stream_response(
            {"model": "x", "api_base": "https://u/v1", "api_key": "k"},
            {"model": "x", "messages": [], "stream": True}))
    assert ei.value.status == -400
