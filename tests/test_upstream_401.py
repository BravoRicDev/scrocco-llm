"""401 upstream: la NOSTRA chiave e' rifiutata dal provider (assente/
invalidata/revocata). E' SEMPRE deployment-side perche' il client si e' gia'
autenticato verso il gateway: il 401 NON va mai passato al client finche'
esiste un'alternativa -> si ruota con cooldown lungo.

Regressione del bug osservato con la chiave Bynara/TokenHarbor vuota: un 401
con envelope OpenAI `{"error":{"type":"unauthorized",...}}` finiva in
PASS-THROUGH perche' non era classificato provider-side (solo il 403 e
l'envelope Anthropic `{"type":"error"}` lo erano).
"""
import asyncio
import os
import tempfile
import time

import httpx

from app.forwarder import (Forwarder, UpstreamError,
                           PERMISSION_DENIED_COOLDOWN_S)
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

# Body del 401 realmente osservato (Bynara).
BYNARA_401 = ('{"error":{"type":"unauthorized","message":'
              '"A valid API key is required.","request_id":"x"}}')

_CSV = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "a,broken,openrouter,https://openrouter.ai/api/v1,paid,128,8000,5,K1,\n"
        "a,good,groq,https://ok.test/v1,paid,128,8000,5,K2,text\n")


def _mk_router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_CSV)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, pol)
    os.unlink(path)
    return router


def test_cooldown_is_long():
    assert PERMISSION_DENIED_COOLDOWN_S >= 3600


# ----------------------------------------------------------------- non-stream
def test_401_rotates_never_reaches_client():
    """Un 401 (envelope OpenAI) deve RUOTARE sul deployment successivo, non
    passare al client."""
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    good = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    route = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        route["n"] += 1
        if request.url.host == "openrouter.ai":
            return httpx.Response(401, content=BYNARA_401.encode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}
    router.fallback_next = lambda *a, **k: good
    data, used = asyncio.run(
        fwd.call_with_fallback(router, "test", broken, payload))
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == good["unique"]
    assert route["n"] == 2
    # chiave rifiutata -> cooldown lungo (>= 1h)
    assert (router._cooldown[broken["unique"]] - time.time()
            >= 0.9 * PERMISSION_DENIED_COOLDOWN_S)


# ------------------------------------------------------------------ streaming
async def _drain(resp):
    out = b""
    async for c in resp.body_iterator:
        out += c
    return out


def test_streaming_401_rotates_never_passthrough(monkeypatch):
    """Path streaming (quello del log incriminato): con un'alternativa
    disponibile il 401 ruota; NON viene restituito un JSONResponse 401 al
    client."""
    import app.main as M
    from fastapi.responses import JSONResponse, StreamingResponse

    dep = next(iter(next(deps for deps in M.config.groups.values() if deps)))
    M.router._cooldown.pop(dep["unique"], None)
    first = dep["unique"]
    seen = []
    cooldown_before = set(M.router._cooldown)

    async def _resp(d, payload, **kwargs):
        seen.append(d["unique"])
        if d["unique"] == first:
            raise UpstreamError(-401, BYNARA_401)

        async def _g():
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield (b'data: {"choices":[{"delta":{},'
                   b'"finish_reason":"stop"}]}\n\n')
            yield b"data: [DONE]\n\n"
        return _g()

    monkeypatch.setattr(M.forwarder, "stream_response", _resp)

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await M._stream_with_fallback("test", dep, payload, scope="chain")

    try:
        resp = asyncio.run(_run())
        # MAI un 401 pass-through al client
        assert not (isinstance(resp, JSONResponse) and resp.status_code == 401)
        if isinstance(resp, StreamingResponse):
            assert b"ok" in asyncio.run(_drain(resp))
        else:
            assert resp.status_code == 503
        assert first in M.router._cooldown
        assert first in seen
    finally:
        for k in list(M.router._cooldown):
            if k not in cooldown_before:
                M.router._cooldown.pop(k, None)
