"""Regola utente: il 400 "tool_config.include_server_side_tool_invocations"
(rifiuto della COMBINAZIONE built-in tools + function calling, tipico di
Google/Gemini 3 anche via proxy OpenAI-compat come requesty) deve:

1. far RUOTARE la richiesta (quando un altro deployment accetta lo stesso
   payload, la richiesta DEVE riuscire);
2. NON penalizzare il deployment (nessun cooldown, fail_streak intatto);
3. NON essere consegnato grezzo al client a catena esaurita (non e'
   "actionable" -> il chiamante risponde 503 RETRYABLE).
"""
import asyncio
import os
import tempfile

import httpx

from app.config import GatewayConfig
from app.forwarder import Forwarder, UpstreamError, classify_error_class
from app.forwarder import _PAYLOAD_SCHEMA_RE, tool_combo_signature
from app.policy import Policy
from app.router import Router

# body reale osservato in produzione (requesty -> google/gemma-4-31b-it)
_TOOL_COMBO_BODY = (
    '{"error":{"origin":"provider","message":"Please enable '
    'tool_config.include_server_side_tool_invocations to use Built-in tools '
    'with Function calling."}}'
)


def _mk_router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write("commento,modello,provider,endpoint,data,context,max_input,"
                "priority,scrocco-llm-test,caps\n"
                "a,broken,requesty,https://rq.test/v1,paid,128,8000,5,K1,\n"
                "a,good,groq,https://ok.test/v1,paid,128,8000,5,K2,text\n")
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    pol.cooldown_jitter_ratio = 0
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, pol)
    os.unlink(path)
    return router


# --------------------------------------------------------- 1. firma ----

def test_tool_combo_signature_regex():
    assert tool_combo_signature(_TOOL_COMBO_BODY)
    assert tool_combo_signature("Please enable tool_config.include_server_side_"
                                "tool_invocations to use Built-in tools")
    assert tool_combo_signature("missing include_server_side_tool_invocations")
    # NON deve scattare su errori diversi (payload schema, quota, contenuto)
    assert not tool_combo_signature("")
    assert not tool_combo_signature(None)
    assert not tool_combo_signature("Function call is missing a "
                                    "thought_signature in functionCall parts.")
    assert not tool_combo_signature("No such model: gemma-4-31b-it")
    assert not tool_combo_signature("Monthly usage limit reached")


def test_tool_combo_not_payload_schema():
    """La firma e' DEDICATA: NON deve essere assorbita da _PAYLOAD_SCHEMA_RE,
    che e' "actionable" e a catena esaurita consegnerebbe il 400 al client."""
    assert not _PAYLOAD_SCHEMA_RE.search(_TOOL_COMBO_BODY)


def test_classify_error_class_tool_combo():
    assert classify_error_class(-400, _TOOL_COMBO_BODY) == "tool_combo"


# --------------------------------------------------- 2. rotazione ----

def test_tool_combo_400_rotates_without_cooldown():
    """Rotazione sul dep buono, 400 non consegnato, nessuna penalita'."""
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    good = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    route = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        route["n"] += 1
        if '"model":"broken"' in request.read().decode():
            return httpx.Response(400, text=_TOOL_COMBO_BODY)
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
    assert not router.is_cooled_down(broken["unique"])
    assert router.stats_for(broken["unique"]).fail_streak == 0


def test_tool_combo_400_no_alternative_is_not_actionable():
    """Catena esaurita: si solleva il vero -400 (per il trail), ma la firma
    NON e' actionable -> il chiamante risponde 503 RETRYABLE, mai il 400."""
    from app.main import _actionable_upstream_error
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    other = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=_TOOL_COMBO_BODY)

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}
    router.fallback_next = lambda *a, **k: other
    try:
        asyncio.run(fwd.call_with_fallback(router, "test", broken, payload))
        assert False, "doveva sollevare UpstreamError"
    except UpstreamError as err:
        assert err.status == -400
        assert "include_server_side_tool_invocations" in err.detail
        assert not _actionable_upstream_error(err)     # -> 503, non 400
    assert not router.is_cooled_down(broken["unique"])
    assert not router.is_cooled_down(other["unique"])
