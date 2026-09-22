"""Regressioni: errori upstream di MODALITA' (vision/immagine) e 5xx
non-standard non devono MAI essere consegnati al client.

Osservato in produzione (var/gateway.log, path STREAMING):
  [fallback] stream <dep> 400 PASS-TRANSPORT al client (non deployment-side)
per un body {"error":{"message":"Model '...' does not support vision input.",
"type":"invalid_request_error","code":"unsupported_model_feature"}}.

Un rifiuto di modalita' NON e' un errore della richiesta: un altro deployment
multimodale accetta lo stesso payload -> si ruota, mai pass-through.
"""
import asyncio
import os
import tempfile

from app.forwarder import (Forwarder, UpstreamError, RETRYABLE_STATUS,
                           _PROVIDER_TRANSIENT_RE, _MODEL_MISSING_RE,
                           _UNKNOWN_FIELD_RE, media_reject_signature)
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

VISION_400 = ('{"error":{"message":"Model \'minimax-m2.7\' does not support '
              'vision input.","type":"invalid_request_error","param":null,'
              '"code":"unsupported_model_feature"}}')
UPSTREAM_AUTH_400 = ('{"error":{"message":"Upstream provider authentication '
                     'failed.","type":"upstream_authentication_failed",'
                     '"param":null,"code":"upstream_authentication_failed"}}')

_CSV = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "a,broken,openrouter,https://openrouter.ai/api/v1,paid,128,8000,5,K1,vision\n"
        "a,good,groq,https://ok.test/v1,paid,128,8000,5,K2,vision\n")


def _mk():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    router = Router(cfg, pol)
    os.unlink(path)
    grp = next(g for g, deps in cfg.groups.items()
               if any(d["api_key"] == "K1" for d in deps))
    broken = next(d for d in cfg.groups[grp] if d["api_key"] == "K1")
    good = next(d for d in cfg.groups[grp] if d["api_key"] == "K2")
    return cfg, router, broken, good


# --------------------------------------------------------------- classificatori
def test_5xx_are_retryable():
    # 524 (Cloudflare timeout) e ogni 5xx devono essere ritriabili: se no
    # finiscono nel ramo status-negativo e possono fare pass-through.
    for code in (500, 502, 503, 504, 520, 522, 524, 599):
        assert code in RETRYABLE_STATUS
    assert 400 not in RETRYABLE_STATUS
    assert 404 not in RETRYABLE_STATUS


def test_upstream_auth_failure_is_provider_side():
    assert _PROVIDER_TRANSIENT_RE.search(UPSTREAM_AUTH_400)
    assert _PROVIDER_TRANSIENT_RE.search("Upstream provider authentication failed.")


def test_media_reject_signature_matches_vision():
    assert media_reject_signature(VISION_400)
    assert media_reject_signature(
        '{"error":{"message":"Model \'DeepSeek-V4-Flash-0731\' does not '
        'support vision input."}}')


def test_real_client_errors_are_not_misclassified():
    """Falsi positivi: un vero errore del client NON deve entrare nei
    classificatori deployment-side (altrimenti non ruoterebbe mai e, peggio,
    marcerebbe come rotte chiavi sane)."""
    client = ('{"error":{"type":"invalid_request_error","message":'
              '"messages required"}}')
    assert not media_reject_signature(client)
    assert not _MODEL_MISSING_RE.search("messages required")
    assert not _PROVIDER_TRANSIENT_RE.search("messages required")


# --------------------------------------------------------------- non-streaming
def test_nonstream_media_reject_rotates():
    import httpx

    cfg, router, broken, good = _mk()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(400, content=VISION_400.encode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        router.fallback_next = lambda *a, **k: good
        return await fwd.call_with_fallback(
            router, "test", broken,
            {"model": "x", "messages": [{"role": "user", "content": "x"}]})

    data, used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == good["unique"]


# ------------------------------------------------------------------ streaming
async def _drain(resp):
    out = b""
    async for c in resp.body_iterator:
        out += c
    return out


def test_streaming_media_reject_never_passthrough(monkeypatch):
    """Il path del log incriminato: con un 400 di rifiuto modalita' e
    un'alternativa disponibile si RUOTA; mai un JSONResponse 400 al client."""
    import app.main as M
    from fastapi.responses import JSONResponse, StreamingResponse

    cfg, router, broken, good = _mk()
    router.fallback_next = lambda *a, **k: good
    seen = []

    class _Fwd:
        async def stream_response(self, d, payload, **kwargs):
            seen.append(d["unique"])
            if d["unique"] == broken["unique"]:
                raise UpstreamError(-400, VISION_400)

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
        payload = {"model": broken["model"],
                   "messages": [{"role": "user", "content": "x"}]}
        return await M._stream_with_fallback(
            "test", broken, payload, need=frozenset({"vision"}), scope="chain")

    resp = asyncio.run(_run())
    assert not isinstance(resp, JSONResponse)      # MAI il 400 al client
    assert isinstance(resp, StreamingResponse)
    assert b"ok" in asyncio.run(_drain(resp))
    assert broken["unique"] in seen
    assert good["unique"] in seen


# --------------------------- "vision" fuorviante su richiesta di solo testo
def _vision_400_stream_harness(monkeypatch):
    import app.main as M
    from fastapi.responses import JSONResponse, StreamingResponse

    cfg, router, broken, good = _mk()
    router.fallback_next = lambda *a, **k: good
    seen = []

    class _Fwd:
        async def stream_response(self, d, payload, **kwargs):
            seen.append(d["unique"])
            if d["unique"] == broken["unique"]:
                raise UpstreamError(-400, VISION_400)

            async def _g():
                yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield (b'data: {"choices":[{"delta":{},'
                       b'"finish_reason":"stop"}]}\n\n')
                yield b"data: [DONE]\n\n"
            return _g()

    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "config", cfg)
    monkeypatch.setattr(M, "forwarder", _Fwd())
    return M, router, broken, good, seen, JSONResponse, StreamingResponse


def test_vision_fuorviante_su_testo_va_in_cooldown(monkeypatch):
    """llm7/Cloudflare risponde "does not support vision input" a una
    richiesta di PURO TESTO: non e' un rifiuto di modalita', il deployment
    e' rotto per QUESTA richiesta -> cooldown normale (prima restava vivo e
    veniva ritentato a ogni richiesta)."""
    M, router, broken, good, seen, JSONResponse, StreamingResponse = \
        _vision_400_stream_harness(monkeypatch)

    async def _run():
        payload = {"model": broken["model"],
                   "messages": [{"role": "user", "content": "x"}]}
        return await M._stream_with_fallback(
            "test", broken, payload, need=frozenset({"text"}), scope="chain")

    resp = asyncio.run(_run())
    assert isinstance(resp, StreamingResponse)
    assert b"ok" in asyncio.run(_drain(resp))
    assert broken["unique"] in router._cooldown      # KO vero -> cooldown


def test_vision_vera_non_punisce_il_deployment(monkeypatch):
    """Con media REALE nella richiesta il rifiuto di modalita' resta un
    rifiuto di modalita': si ruota senza cooldown."""
    M, router, broken, good, seen, JSONResponse, StreamingResponse = \
        _vision_400_stream_harness(monkeypatch)

    async def _run():
        payload = {"model": broken["model"],
                   "messages": [{"role": "user", "content": "x"}]}
        return await M._stream_with_fallback(
            "test", broken, payload, need=frozenset({"vision"}), scope="chain")

    resp = asyncio.run(_run())
    assert isinstance(resp, StreamingResponse)
    assert b"ok" in asyncio.run(_drain(resp))
    assert broken["unique"] not in router._cooldown


def test_vision_fuorviante_ripara_il_reasoning_e_riprova():
    """Ipotesi (utente): llm7/Cloudflare maschera col messaggio 'vision' il
    solito problema del reasoning mancante. Il gateway prova la riparazione
    sullo STESSO dep: se il 2o tentativo passa, nessun cooldown."""
    import httpx
    import json

    cfg, router, broken, good = _mk()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(400, content=VISION_400.encode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    payload = {"model": "x",
               "messages": [
                   {"role": "user", "content": "x"},
                   {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "t1", "type": "function",
                                    "function": {"name": "f",
                                                 "arguments": "{}"}}]},
                   {"role": "tool", "tool_call_id": "t1", "content": "r"}]}

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(
            router, "test", broken, payload, need=frozenset({"text"}))

    data, used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == broken["unique"]        # stesso dep, riprovato
    assert len(seen) == 2
    asst = [m for m in seen[1]["messages"] if m.get("tool_calls")][0]
    assert asst.get("reasoning_content")             # riparato PRIMA dell'invio
    assert broken["unique"] not in router._cooldown  # nessun cooldown


def test_vision_fuorviante_nonstream_va_in_cooldown():
    import httpx

    cfg, router, broken, good = _mk()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(400, content=VISION_400.encode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        router.fallback_next = lambda *a, **k: good
        return await fwd.call_with_fallback(
            router, "test", broken,
            {"model": "x", "messages": [{"role": "user", "content": "x"}]},
            need=frozenset({"text"}))

    data, used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == good["unique"]
    assert broken["unique"] in router._cooldown


# ------------------------------------------------- stream_options non-stream
def test_nonstream_toglie_stream_options():
    """Osservato in produzione: il client manda `stream_options` in una
    richiesta non-stream e il provider (opencode zen / Console Go) risponde
    400 'stream_options should be set along with stream = true'. In non-stream
    il gateway NON deve inoltrarlo."""
    import httpx
    import json

    cfg, router, broken, good = _mk()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return await fwd.call_with_fallback(
            router, "test", broken,
            {"model": "x", "stream": False,
             "stream_options": {"include_usage": True},
             "messages": [{"role": "user", "content": "x"}]})

    data, _used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert "stream_options" not in seen["body"]
    assert seen["body"].get("stream") in (None, False)


# ------------------- 400 "modello non servito" / "campo sconosciuto"
BYNARA_MODEL_400 = ('{"error":{"type":"bad_request","message":"The requested '
                    'model is not available.","request_id":"x"}}')
GOOGLE_STORE_400 = (
    '[{\n  "error": {\n    "code": 400,\n    "message": "Invalid JSON '
    'payload received. Unknown name \\"store\\": Cannot find field.",\n    '
    '"status": "INVALID_ARGUMENT"\n  }\n}]')


def test_bynara_model_not_available_is_provider_side():
    assert _PROVIDER_TRANSIENT_RE.search(BYNARA_MODEL_400)


def test_google_unknown_field_is_payload_schema():
    assert _UNKNOWN_FIELD_RE.search(GOOGLE_STORE_400)
    assert not media_reject_signature(GOOGLE_STORE_400)


def _client_4xx_stream_harness(monkeypatch, error_body):
    """Harness: il primo dep risponde col 400 dato, il secondo risponde ok."""
    import app.main as M
    from fastapi.responses import JSONResponse, StreamingResponse

    cfg, router, broken, good = _mk()
    router.fallback_next = lambda *a, **k: good
    seen = []

    class _Fwd:
        async def stream_response(self, d, payload, **kwargs):
            seen.append(d["unique"])
            if d["unique"] == broken["unique"]:
                raise UpstreamError(-400, error_body)

            async def _g():
                yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield (b'data: {"choices":[{"delta":{},'
                       b'"finish_reason":"stop"}]}\n\n')
                yield b"data: [DONE]\n\n"
            return _g()

    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "config", cfg)
    monkeypatch.setattr(M, "forwarder", _Fwd())
    return M, router, broken, good, seen, JSONResponse, StreamingResponse


def test_streaming_bynara_model_not_available_rotates(monkeypatch):
    M, router, broken, good, seen, JSONResponse, StreamingResponse = \
        _client_4xx_stream_harness(monkeypatch, BYNARA_MODEL_400)

    async def _run():
        payload = {"model": broken["model"],
                   "messages": [{"role": "user", "content": "x"}]}
        return await M._stream_with_fallback(
            "test", broken, payload, need=frozenset({"text"}), scope="chain")

    resp = asyncio.run(_run())
    assert not isinstance(resp, JSONResponse)      # MAI il 400 al client
    assert isinstance(resp, StreamingResponse)
    assert b"ok" in asyncio.run(_drain(resp))
    assert broken["unique"] in router._cooldown    # modello giu' -> cooldown
    assert good["unique"] in seen


def test_streaming_google_unknown_field_rotates(monkeypatch):
    M, router, broken, good, seen, JSONResponse, StreamingResponse = \
        _client_4xx_stream_harness(monkeypatch, GOOGLE_STORE_400)

    async def _run():
        payload = {"model": broken["model"],
                   "messages": [{"role": "user", "content": "x"}]}
        return await M._stream_with_fallback(
            "test", broken, payload, need=frozenset({"text"}), scope="chain")

    resp = asyncio.run(_run())
    assert not isinstance(resp, JSONResponse)      # MAI il 400 al client
    assert isinstance(resp, StreamingResponse)
    assert b"ok" in asyncio.run(_drain(resp))
    assert broken["unique"] not in router._cooldown  # incompat. -> no cooldown
    assert good["unique"] in seen


# ----------------------- REGOLA UTENTE: QUALSIASI non-200 ruota
def test_streaming_generic_4xx_rotates_never_passthrough(monkeypatch):
    """Regola utente: un 400 generico (nessun classificatore provider-side)
    NON deve piu' passare al client: si ruota sul successivo e solo a catena
    esaurita si risponde l'errore."""
    import app.main as M
    from fastapi.responses import JSONResponse, StreamingResponse

    cfg, router, broken, good = _mk()
    router.fallback_next = lambda *a, **k: good
    seen = []

    class _Fwd:
        async def stream_response(self, d, payload, **kwargs):
            seen.append(d["unique"])
            if d["unique"] == broken["unique"]:
                raise UpstreamError(-400, "generic upstream error")

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
        payload = {"model": broken["model"],
                   "messages": [{"role": "user", "content": "x"}]}
        return await M._stream_with_fallback(
            "test", broken, payload, need=frozenset({"text"}), scope="chain")

    resp = asyncio.run(_run())
    assert not isinstance(resp, JSONResponse)      # MAI il 400 al client
    assert isinstance(resp, StreamingResponse)
    assert b"ok" in asyncio.run(_drain(resp))
    assert broken["unique"] in seen
    assert good["unique"] in seen


def test_nonstream_generic_4xx_rotates_never_passthrough():
    """Stessa regola nel path non-stream: 400 generico -> rotazione sul
    successivo, mai l'errore nudo al client."""
    import httpx

    cfg, router, broken, good = _mk()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(400, content=b"generic upstream error")
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        router.fallback_next = lambda *a, **k: good
        return await fwd.call_with_fallback(
            router, "test", broken,
            {"model": "x", "messages": [{"role": "user", "content": "x"}]},
            need=frozenset({"text"}))

    data, used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == good["unique"]
