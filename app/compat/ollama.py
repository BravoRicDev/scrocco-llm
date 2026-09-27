"""Endpoint di compatibilita' Ollama / llama.cpp.

Estratti da `app/main.py` (M2). Lo stato runtime condiviso (config, policy,
authn) si legge da `app.state` (`gw_state.<nome>`), popolato da `app/main.py`.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .. import capmeta
from .. import state as gw_state
from ..constants import GATEWAY_VERSION
from ..offload import request_json
from ..http_responses import unauthorized as _unauthorized
from ..models_and_health import _caps_and_deps
from ..models_and_health import _model_entry
from ..models_and_health import _names_for_auth
from ..models_and_health import _view_for
from ..models_and_health import _visible_model_names

router = APIRouter()


def _ollama_entry(name: str) -> dict:
    """Entry Ollama. `capabilities` esiste gia' nel contratto
    (`api/types.go` -> `ListModelResponse.Capabilities []model.Capability`, con
    `omitempty`): aggiungerlo e' additivo e non rompe i client."""

    caps, _deps = _caps_and_deps(name)
    entry = {
        "name": name,
        "model": name,
        "modified_at": "1970-01-01T00:00:00Z",
        "size": 0,
        "digest": "",
        "details": {
            "parent_model": "",
            "format": "gguf",
            "family": "",
            "families": None,
            "parameter_size": "",
            "quantization_level": "",
        },
    }
    if caps:
        entry["capabilities"] = capmeta.ollama_capabilities(caps)
    return entry


@router.get("/v1/models/{model_id:path}")
async def retrieve_model(model_id: str, request: Request):
    """OpenAI 'retrieve model'. 200 se il modello è gestito (nome diretto,
    alias, o canonicalizzabile a un gruppo/base noto), altrimenti 404.

    Restituisce le STESSE capability di /v1/models: un client che interroga un
    nome singolo deve vedere le stesse cose che vede in lista. Nota che sono
    accettati anche gli `uniques` (deployment): non sono stabili, ma restano
    chiamabili per non rompere chi li ha pinnati."""

    names, auth = _visible_model_names(request)
    if names is None:
        return _unauthorized(auth.error)
    canon = gw_state.policy.canonicalize(model_id)
    known = (
        model_id in names
        or canon in names
        or canon in gw_state.config.groups
        or canon in gw_state.config.alias_groups
        or gw_state.config.deployment_by_unique(canon) is not None
        or any(canon == a or canon == gw_state.policy.aliases.get(a) for a in gw_state.policy.aliases)
    )
    if not known:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"model '{model_id}' not found",
                    "type": "invalid_request_error",
                    "code": "model_not_found",
                }
            },
        )
    return _model_entry(model_id)


@router.get("/api/tags")
async def ollama_tags(request: Request):
    """Ollama: lista modelli (nomi stabili: base + gruppi + alias)."""

    names, auth = _visible_model_names(request)
    if names is None:
        return _unauthorized(auth.error)
    return {"models": [_ollama_entry(n) for n in names]}


@router.get("/api/v1/models")
async def api_v1_models(request: Request):
    """Alias non-standard di /v1/models usato da alcuni client."""

    auth = gw_state.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    view = _view_for(request, auth)
    rich = view == "stable"
    names = sorted(set(_names_for_auth(auth, view)))
    return {"object": "list", "data": [_model_entry(n, rich=rich) for n in names]}


@router.get("/v1/model/info")
async def model_info(request: Request):
    """Dettaglio capability per modello, stile LiteLLM `/model/info`.

    Vive su un endpoint dedicato (non dentro /v1/models) perche' e' la forma piu'
    ricca in circolazione e aggiungerla allo standard OpenAI lo inquinerebbe.
    Stessa segregazione di /v1/models: `?view=uniques|stable`, e una chiave di
    profilo vede solo i nomi stabili del proprio profilo."""

    auth = gw_state.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    view = _view_for(request, auth)
    rich = view == "stable"
    data = []
    for name in sorted(set(_names_for_auth(auth, view))):
        caps, deps = _caps_and_deps(name)
        if not caps:
            continue
        info = capmeta.litellm_style(caps)
        ctx_max, ctx_min = capmeta.context_lengths(deps)
        if ctx_max:
            info["max_input_tokens"] = ctx_max
            info["min_input_tokens"] = ctx_min
        if rich:
            info["architecture"] = capmeta.modalities(caps)
            info["supported_parameters"] = capmeta.supported_parameters(caps)
            info["capabilities_sx"] = capmeta.structured_style(caps)
        data.append({"id": name, "object": "model_info", "model_info": info})
    return {"object": "list", "data": data}


@router.post("/api/show")
async def ollama_show(request: Request):
    """Ollama: dettagli modello. Capabilities REALI del nome richiesto.

    Prima restituiva sempre `["completion", "chat"]`: un client che chiedeva
    "cosa sa fare scrocco-llm-fissone?" riceveva una risposta fissa, e quindi
    non poteva sapere che lo stesso nome instrada anche le chiamate Jev
    (`/v1/systemone`). `model_info` (il posto che Ollama riserva ai metadati
    arbitrari del modello) riporta capability, endpoint e un esempio d'uso.
    """

    names, auth = _visible_model_names(request)
    if names is None:
        return _unauthorized(auth.error)
    try:
        body = await request_json(request)
    except Exception:
        body = {}
    name = (body.get("name") or body.get("model") or "") if isinstance(body, dict) else ""
    canon = gw_state.policy.canonicalize(name) if name else ""
    known = bool(name) and (
        name in names or canon in names or canon in gw_state.config.groups or gw_state.config.deployment_by_unique(canon) is not None
    )
    if name and not known:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": f"model '{name}' not found", "type": "invalid_request_error"}},
        )
    target = name or gw_state.policy.service_name
    caps, deps = _caps_and_deps(target)
    ctx_max, ctx_min = capmeta.context_lengths(deps)
    model_info: dict = {}
    if caps:
        model_info["scrocco.caps"] = sorted(caps)
        model_info["scrocco.capabilities_ollama"] = capmeta.ollama_capabilities(caps)
        model_info["scrocco.endpoints"] = {
            c: capmeta.supported_endpoints({c})[0] for c in sorted(caps) if capmeta.supported_endpoints({c})
        }
        model_info["scrocco.architecture"] = capmeta.modalities(caps)
        model_info["scrocco.supported_parameters"] = capmeta.supported_parameters(caps)
        if ctx_max:
            model_info["scrocco.context_length"] = ctx_max
            model_info["scrocco.max_input_tokens"] = ctx_max
            model_info["scrocco.min_input_tokens"] = ctx_min
        _hint = capmeta.usage_hint(caps)
        if _hint:
            model_info["scrocco.usage"] = _hint
    return {
        "license": "",
        "modelfile": "",
        "parameters": "",
        "template": "",
        "details": _ollama_entry(target)["details"],
        "model_info": model_info,
        "capabilities": (capmeta.ollama_capabilities(caps) if caps else ["completion"]),
    }


@router.get("/api/version")
async def ollama_version():

    return {"version": GATEWAY_VERSION}


@router.get("/version")
async def llamacpp_version():

    return {"version": GATEWAY_VERSION}


@router.get("/props")
async def llamacpp_props():
    """llama.cpp server props: stub minimo ma valido."""

    return {
        "default_generation_settings": {"n_ctx": 0},
        "total_slots": 1,
        "chat_template": "",
        "model_path": gw_state.policy.service_name,
        "build_info": f"nx {GATEWAY_VERSION}",
    }


@router.get("/v1/props")
async def llamacpp_props_v1():
    """llama.cpp server props (prefisso /v1): stesso stub di /props."""

    return {
        "default_generation_settings": {"n_ctx": 0},
        "total_slots": 1,
        "chat_template": "",
        "model_path": gw_state.policy.service_name,
        "build_info": f"nx {GATEWAY_VERSION}",
    }
