"""Regressione: prefissi provider e "no such model".

1. infer_model_prefix NON deve piu' aggiungere prefissi mistral/cloudflare:
   il forwarder chiama gli upstream direttamente via httpx e il nome col
   prefisso generava 400 "No such model" su TUTTI quei deployment
   (verificato live: stesso account+chiave risponde 200 senza prefisso).
2. Un 400 con "No such model" nel body (formato cloudflare, senza firme
   litellm "openai_error") deve RUOTARE sulla catena, mai passare al client.
"""
import asyncio
import os
import tempfile
import time

import httpx

from app.config import GatewayConfig, infer_model_prefix
from app.forwarder import Forwarder, MODEL_MISSING_COOLDOWN_S, UpstreamError
from app.policy import Policy
from app.router import Router

# ------------------------------------------------- 1. prefix inference ----


def test_mistral_model_untouched():
    model, needs = infer_model_prefix(
        "mistral-medium-latest", "https://api.mistral.ai/v1")
    assert model == "mistral-medium-latest"       # NIENTE "mistral/" davanti
    assert needs is False


def test_cloudflare_model_untouched():
    model, _ = infer_model_prefix(
        "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "https://api.cloudflare.com/client/v4/accounts/X/ai/v1")
    assert model == "@cf/meta/llama-3.3-70b-instruct-fp8-fast"


def test_openrouter_vendor_prefix_preserved():
    # il namespace vendor/model di openrouter e' ATTESO dall'upstream: intatto
    model, _ = infer_model_prefix("mistralai/mistral-medium",
                                  "https://openrouter.ai/api/v1")
    assert model == "mistralai/mistral-medium"


def test_groq_openai_prefixed_untouched():
    model, _ = infer_model_prefix("openai/gpt-oss-120b",
                                  "https://api.groq.com/openai/v1")
    assert model == "openai/gpt-oss-120b"


def test_nvidia_needs_provider_flag():
    model, needs = infer_model_prefix(
        "meta/llama-3.3-70b-instruct", "https://integrate.api.nvidia.com/v1")
    assert model == "meta/llama-3.3-70b-instruct" and needs is True


# ------------------------------------- 2. rotazione su "no such model" ----

def _mk_router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write("commento,modello,provider,endpoint,data,context,max_input,"
                "priority,scrocco-llm-test,caps\n"
                "a,broken,cloudflare,https://cf.test/v1,paid,128,8000,5,K1,\n"
                "a,good,groq,https://ok.test/v1,paid,128,8000,5,K2,text\n")
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, pol)
    os.unlink(path)
    return router


def test_no_such_model_400_rotates_never_reaches_client():
    """400 cloudflare 'No such model' (niente firme litellm): rotazione."""
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    good = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    route = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        route["n"] += 1
        if request.url.host == "cf.test":
            assert request.read().decode().find('"model":"broken"') >= 0
            return httpx.Response(400, json={"errors": [{
                "message": "AiError: No such model: No such model broken",
                "code": 5007}]})
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}
    # fallback_next guidato: dal broken si va sul good
    router.fallback_next = lambda *a, **k: good
    data, used = asyncio.run(
        fwd.call_with_fallback(router, "test", broken, payload))
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == good["unique"]
    assert route["n"] == 2                        # entrambe le chiamate fatte
    # modello inesistente/giu' -> cooldown 24h fisso (non escalation da 600s)
    assert router._cooldown[broken["unique"]] - time.time() > 0.9 * MODEL_MISSING_COOLDOWN_S


def test_model_missing_regex():
    from app.forwarder import _MODEL_MISSING_RE as RE
    assert RE.search("AiError: No such model: x")
    assert RE.search('{"error":"model_not_found"}')
    assert RE.search("Unknown model foo")
    # opencode-zen: {"type":"ModelError","message":"Model X is not supported"}
    assert RE.search('{"type":"error","error":{"type":"ModelError",'
                     '"message":"Model x-preview-f-free is not supported"}}')
    assert RE.search("Model gpt-foo is not supported")
    # "Model is (currently) unavailable" (opencode-zen / llm7)
    assert RE.search('{"error":{"type":"server_error","message":"Error from '
                     'provider (Console): Upstream request failed: Model is '
                     'unavailable."}}')
    assert RE.search("Model 'DeepSeek-V4-Flash-0731' is currently unavailable.")
    assert not RE.search("invalid api key")
    assert not RE.search("the service is temporarily unavailable")  # niente "model"
    # rifiuto di MODALITA' (non modello): resta sul percorso media-strike
    assert not RE.search("this model does not support vision input")
    assert not RE.search('{"message":"image input is not supported"}')


def test_thought_signature_regex():
    from app.forwarder import _THOUGHT_SIG_RE as RE
    assert RE.search("Function call is missing a thought_signature in "
                     "functionCall parts.")
    assert RE.search("missing thought signature")
    assert not RE.search("invalid api key")


def test_is_provider_error_body():
    """Fallback diretto SOLO per il body che INIZIA con {"type":"error",...}."""
    from app.forwarder import is_provider_error_body as F
    # SI: envelope in testa (anche con spazi o prefisso SSE `data: `)
    assert F('{"type":"error","error":{"type":"AuthError","message":"Invalid API key."}}')
    assert F('  {"type": "error", "error": {"type": "SomethingNew"}}')
    assert F('data: {"type":"error","error":{"type":"CreditsError"}}')
    # NO: qualsiasi altra forma con "error" -> solo audit, mai skip
    assert not F('{"error":{"type":"server_error","message":"Model is unavailable."}}')
    assert not F('{"error":{"message":"model_not_found"}}')
    assert not F('{"errors":[{"message":"AiError: No such model"}]}')
    assert not F('{"choices":[{"message":{"content":"gestione {\\"type\\":\\"error\\"} in JS"}}]}')
    assert not F('{"id":"x","choices":[]}')
    assert not F('')


def test_auth_error_envelope_rotates_never_reaches_client():
    """{"type":"error","error":{"type":"AuthError",...}} (401): rotazione
    sulla catena + cooldown del deployment, l'errore non raggiunge il client."""
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    good = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    def handler(request: httpx.Request) -> httpx.Response:
        if '"model":"broken"' in request.read().decode():
            return httpx.Response(401, json={"type": "error", "error": {
                "type": "AuthError", "message": "Invalid API key."}})
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
    assert router.is_cooled_down(broken["unique"])       # chiave morta -> cooldown


def test_thought_signature_400_rotates_without_cooldown():
    """400 Gemini 3 'missing thought_signature' (niente firme litellm):
    rotazione sulla catena, il 400 non raggiunge il client E la key Gemini
    NON va in cooldown (non e' rotta in generale, solo tool-replay)."""
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    good = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    route = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        route["n"] += 1
        if request.read().decode().find('"model":"broken"') >= 0:
            return httpx.Response(400, json={"error": {
                "code": 400,
                "message": "Function call is missing a thought_signature in "
                           "functionCall parts. This is required for tools to "
                           "work correctly.",
                "status": "INVALID_ARGUMENT"}})
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
    assert not router.is_cooled_down(broken["unique"])   # nessun cooldown
    assert router.stats_for(broken["unique"]).fail_streak == 0


def test_thought_signature_400_delivered_when_no_alternative():
    """Catena tutta Gemini 3: dopo aver ruotato una volta si consegna il 400
    vero (azionabile dall'agente), non un 502 generico."""
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    other = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {
            "message": "missing a thought_signature in functionCall parts",
            "status": "INVALID_ARGUMENT"}})

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}
    # prima rotazione -> other; poi fallback_next lo ripropone (in `tried`)
    router.fallback_next = lambda *a, **k: other
    try:
        asyncio.run(fwd.call_with_fallback(router, "test", broken, payload))
        assert False, "doveva sollevare UpstreamError"
    except UpstreamError as err:
        assert err.status == -400
        assert "thought_signature" in err.detail
    assert not router.is_cooled_down(broken["unique"])
    assert not router.is_cooled_down(other["unique"])


def test_length_truncated_empty_raises_503_no_rotation():
    """Contenuto vuoto + finish_reason='length' (rotate_on_length_empty=False):
    NON ruota, NON raffredda, ma NEMMENO consegna un turno vuoto -> UpstreamError
    503 RETRYABLE (l'agente ritenta; mai un turno finto o vuoto)."""
    import pytest
    from app.forwarder import UpstreamError
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    dep = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{
            "message": {"content": ""}, "finish_reason": "length"}]})

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}
    router.fallback_next = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("NON deve ruotare"))
    with pytest.raises(UpstreamError) as ei:
        asyncio.run(fwd.call_with_fallback(
            router, "test", dep, payload, collect_qc_failures=True))
    assert ei.value.status == 503
    assert calls["n"] == 1
    assert dep["unique"] not in router._cooldown   # nessun cooldown


def test_stream_injects_include_usage_only_for_supported_providers():
    """stream_options.include_usage: iniettato su groq/openrouter, MAI sugli
    altri (uno 400 a stream_options sconosciuto costerebbe la rotazione)."""
    import json as _json
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(_json.loads(request.read().decode()))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b'data: [DONE]\n\n')

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [], "stream": True}

    async def _run(base):
        dep = {"unique": "u", "group": "g", "model": "m",
               "api_base": base, "api_key": "k"}
        gen = await fwd.stream_response(dep, dict(payload))
        async for _ in gen:
            pass

    asyncio.run(_run("https://api.groq.com/openai/v1"))
    asyncio.run(_run("https://openrouter.ai/api/v1"))
    asyncio.run(_run("https://api.cloudflare.com/client/v4/accounts/A/ai/v1"))
    asyncio.run(_run("https://api.mistral.ai/v1"))
    g, o, c, m = bodies
    assert g.get("stream_options", {}).get("include_usage") is True
    assert o.get("stream_options", {}).get("include_usage") is True
    assert "stream_options" not in c
    assert "stream_options" not in m


def test_video_gen_candidates_by_raw_model_name():
    """?model=<modello grezzo> nel poll stateless: la risoluzione profilo
    fallisce ma i candidati video_gen devono comunque tornare (fallback su
    tutti i profili), non un 404."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write("commento,modello,provider,endpoint,data,context,max_input,"
                "priority,scrocco-llm-test,caps\n"
                "a,s1,openrouter,https://or.test/v1,paid,0,0,5,SV1,video_gen\n"
                "a,s2,openrouter,https://or.test/v1,paid,0,0,5,SV2,video_gen\n")
    from app.policy import Policy as P
    from app.config import GatewayConfig as GC
    from app.router import Router as R
    pol = P.from_dict({"capability_routing": {"model_capabilities": {
        "*s1*": ["video_gen"],
        "*s2*": ["video_gen"],
    }}})
    cfg = GC(path, proxy_prefix="scrocco-llm-", seed=1)
    r = R(cfg, pol)
    os.unlink(path)
    # nome grezzo che non risolve nessun profilo:
    got = r.video_gen_candidates("bytedance/seedance-sconosciuto-x")
    keys = {d["api_key"] for d in got}
    assert keys == {"SV1", "SV2"}, keys


def test_model_missing_regex_consistency():
    """La regex usata nei 5 punti (chat, stream, images, tts/stt, videos)
    copre le varianti provider senza firme litellm."""
    from app.forwarder import _MODEL_MISSING_RE as RE
    cases = [
        '{"errors":[{"message":"AiError: No such model: cloudflare/@cf/x"}]}',
        '{"error":{"message":"model_not_found","code":404}}',
        "Unknown model: foo-bar",
        "The model `x` does not exist",
    ]
    for c in cases:
        assert RE.search(c), c


# --- CF Workers AI: rifiuto di SCHEMA del payload (content array vs string) ---

_CF_SCHEMA_BODY = (
    '{"errors":[{"message":"AiError: Bad input: Error: oneOf at \'/\' not met, '
    "0 matches: required properties at '/' are 'prompt', Type mismatch of "
    "'/messages/0/content', 'array' not in 'string', Type mismatch of "
    "'/messages/17/content', 'array' not in 'string', required properties at "
    '\'/messages/17\' are \'role,content\'","code":5006}],'
    '"success":false,"result":{},"messages":[]}'
)


def test_payload_schema_regex():
    from app.forwarder import _PAYLOAD_SCHEMA_RE as RE
    assert RE.search(_CF_SCHEMA_BODY)
    assert RE.search("Type mismatch of '/messages/3/content', 'array' not in 'string'")
    assert RE.search("oneOf at '/' not met")
    # NON deve scattare su contenuto reale di un modello che parla di array/string
    assert not RE.search("Per convertire un array in string usa join().")
    assert not RE.search('{"choices":[{"message":{"content":"gestione errori"}}]}')


def test_payload_schema_400_rotates_without_cooldown():
    """CF Workers AI rifiuta il payload (content come array): rotazione sulla
    catena, il 400 NON raggiunge il client e la key NON va in cooldown."""
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    good = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    route = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        route["n"] += 1
        if '"model":"broken"' in request.read().decode():
            return httpx.Response(400, text=_CF_SCHEMA_BODY)
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


def test_payload_schema_400_delivered_when_no_alternative():
    """Nessuna alternativa non ancora provata: si consegna il 400 vero
    (azionabile: 'messages/17' malformato), non un 502/503 generico."""
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    other = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=_CF_SCHEMA_BODY)

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}
    router.fallback_next = lambda *a, **k: other
    try:
        asyncio.run(fwd.call_with_fallback(router, "test", broken, payload))
        assert False, "doveva sollevare UpstreamError"
    except UpstreamError as err:
        assert err.status == -400
        assert "AiError" in err.detail or "oneOf" in err.detail
    assert not router.is_cooled_down(broken["unique"])
    assert not router.is_cooled_down(other["unique"])


# --- nuovi pattern mappati: EOL 410, reasoning_content, body d'errore vuoto ---

def test_model_missing_regex_eol_410():
    from app.forwarder import _MODEL_MISSING_RE as RE
    cases = [
        '{"type":"about:blank","title":"Gone","status":410,"detail":"The model '
        "'nvidia/llama-3.3-nemotron-super-49b-v1' has reached [end of life]\"}",
        "The model x is no longer available to new users",
        "This model has been deprecated",
        "models/gemini-2.5-flash is no longer available",
    ]
    for c in cases:
        assert RE.search(c), c
    assert not RE.search("a normal answer about life and death and endings")


def test_payload_schema_regex_reasoning_content():
    from app.forwarder import _PAYLOAD_SCHEMA_RE as RE
    assert RE.search("'messages.2' : for 'role:assistant' the following must be "
                     "satisfied[('messages.2' : property 'reasoning_content' is "
                     "unsupported)]")
    assert RE.search('property \'reasoning_content\' is unsupported')
    assert RE.search("reasoning_content is unsupported")
    assert not RE.search("the model reasoned carefully about the content")


def test_empty_error_body_rotates_not_passthrough():
    """4xx con body illeggibile (stream appeso): ruota + cooldown corto, mai
    pass-through nudo. Catena esaurita -> 503."""
    from app.forwarder import UpstreamError, PROVIDER_TRANSIENT_COOLDOWN_S
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    good = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    def handler(request: httpx.Request) -> httpx.Response:
        if '"model":"broken"' in request.read().decode():
            return httpx.Response(400, text="")          # body VUOTO
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}
    router.fallback_next = lambda *a, **k: good
    data, used = asyncio.run(fwd.call_with_fallback(router, "test", broken, payload))
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == good["unique"]
    assert router.is_cooled_down(broken["unique"])      # cooldown corto applicato
