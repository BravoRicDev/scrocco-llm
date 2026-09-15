"""P1-5: skipPlatforms per-richiesta — un errore PROVIDER-LEVEL (5xx,
timeout, transport, transient) su un host fa saltare QUELL'host per il
resto della richiesta, invece di bruciare un hop per ogni chiave gemella.

Vincolo: il filtro non deve MAI lasciare la richiesta senza candidati —
se l'host saltato e' l'unico disponibile si ritorna comunque un suo dep
(meglio riprovare che rispondere 503 per un filtro).
"""
import asyncio
import os
import tempfile

import pytest

from app.forwarder import (Forwarder, UpstreamError, dep_host,
                           is_provider_level, PROVIDER_LEVEL_CLASSES)
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

_CSV = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "a,p1h-a,openrouter,https://openrouter.ai/api/v1,free,128,8000,5,K-A1,\n"
        "a,p1h-a,openrouter,https://openrouter.ai/api/v1,free,128,8000,5,K-A2,\n"
        "a,p1h-b,groq,https://api.groq.com/openai/v1,free,128,8000,5,K-B1,\n")

_CSV_SOLO_A = ("commento,modello,provider,endpoint,data,context,max_input,"
               "priority,scrocco-llm-test,caps\n"
               "a,p1h-a,openrouter,https://openrouter.ai/api/v1,free,128,8000,5,K-A1,\n"
               "a,p1h-a2,openrouter,https://openrouter.ai/api/v1,free,128,8000,5,K-A2,\n")


def _mk(csv_text=_CSV):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_text)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    router = Router(cfg, pol)
    os.unlink(path)
    by_key = {}
    for deps in cfg.groups.values():
        for d in deps:
            by_key[d["api_key"]] = d
    return cfg, router, by_key


# --------------------------------------------------------------------- unit
def test_dep_host_e_classi_provider_level():
    cfg, router, by_key = _mk()
    assert dep_host(by_key["K-A1"]) == "openrouter.ai"
    assert dep_host(by_key["K-B1"]) == "api.groq.com"
    assert dep_host({}) == ""
    assert is_provider_level("upstream_error") is True
    assert is_provider_level("timeout") is True
    assert is_provider_level("rate_limited") is False
    assert is_provider_level("auth") is False
    assert PROVIDER_LEVEL_CLASSES == {"upstream_error", "provider_transient",
                                      "host_transient", "timeout", "network"}


# ------------------------------------------------------------------- e2e ns
def test_nonstream_salta_le_chiavi_gemelle_dell_host_morto():
    import httpx

    cfg, router, by_key = _mk()
    a1, a2, b1 = by_key["K-A1"], by_key["K-A2"], by_key["K-B1"]
    hosts_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts_seen.append(request.url.host)
        if request.url.host == "openrouter.ai":
            return httpx.Response(500, content=b'{"error":"boom"}')
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(
            router, "test", a1,
            {"model": "x", "messages": [{"role": "user", "content": "x"}]})

    data, used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == b1["unique"]
    # il gemello a2 NON e' mai stato tentato: l'host e' stato saltato
    assert hosts_seen == ["openrouter.ai", "api.groq.com"]


def test_nonstream_se_host_unico_si_riprova_comunque():
    """Guard: se l'host saltato e' l'unico, il filtro non deve far fallire
    la richiesta per assenza di candidati — si ritorna il dep salvato."""
    import httpx

    cfg, router, by_key = _mk(_CSV_SOLO_A)
    a1 = by_key["K-A1"]
    hosts_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts_seen.append(request.url.host)
        return httpx.Response(500, content=b"")

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(
            router, "test", a1,
            {"model": "x", "messages": [{"role": "user", "content": "x"}]})

    with pytest.raises(UpstreamError):
        asyncio.run(_run())
    # entrambe le chiavi dell'host sono state tentate (nessun vicolo cieco)
    assert hosts_seen == ["openrouter.ai", "openrouter.ai"]


# -------------------------------------------------------------- e2e stream
def test_streaming_salta_le_chiavi_gemelle_dell_host_morto(monkeypatch):
    import app.main as M
    from fastapi.responses import StreamingResponse

    cfg, router, by_key = _mk()
    a1, a2, b1 = by_key["K-A1"], by_key["K-A2"], by_key["K-B1"]
    seen = []

    class _Fwd:
        async def stream_response(self, d, payload, **kwargs):
            seen.append(d["unique"])
            if dep_host(d) == "openrouter.ai":
                raise UpstreamError(-500, "")        # 5xx senza body

            async def _g():
                yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield (b'data: {"choices":[{"delta":{},'
                       b'"finish_reason":"stop"}]}\n\n')
                yield b"data: [DONE]\n\n"
            return _g()

    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "config", cfg)
    monkeypatch.setattr(M, "forwarder", _Fwd())

    async def _run():
        payload = {"model": a1["model"],
                   "messages": [{"role": "user", "content": "x"}]}
        return await M._stream_with_fallback(
            "test", a1, payload, need=frozenset({"text"}), scope="chain")

    resp = asyncio.run(_run())
    assert isinstance(resp, StreamingResponse)
    assert seen[0] == a1["unique"]
    assert a2["unique"] not in seen          # gemella saltata
    assert b1["unique"] in seen
