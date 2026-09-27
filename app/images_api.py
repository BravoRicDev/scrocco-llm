"""Endpoint /v1/images/generations, /v1/images/edits, /v1/images/files.
Estratti verbatim da `app/main.py` (cluster C7, Round 4 Clean Code).
Gli oggetti condivisi (`policy`, `authn`, `router`, `config`, `log`,
`forwarder`: STATO runtime) sono raggiunti DENTRO il corpo
delle funzioni tramite `import app.main as M`: a livello di modulo si
creerebbe un ciclo di import (main include questo router a fine file, dopo
aver definito tutto).
"""
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import imagestore, metrics
from .http_responses import unauthorized as _unauthorized
from .http_responses import forbidden as _forbidden
from .suppressed import report_suppressed
from .auth import AuthResult
from .chat_helpers import (
    _client_attribution,
    _client_ip,
    _emit_summary,
    _opencode_session,
    _session_id,
    _set_opencode_gate,
    _strike_hook,
)
from .forwarder import (
    UpstreamError,
    _MODEL_MISSING_RE,
    chat_only_image_error,
    dep_image_via,
    extract_chat_images,
    image_chat_fallback_signature,
    image_chat_payload,
    image_refs_from_payload,
    images_dual,
    media_reject_signature,
)
from .image_helpers import _data_uri, _images_chat_loop, _images_pick_dep, _localize_images
from .policy import refill_out_budget

router = APIRouter()


@router.post("/v1/images/generations")
async def images_generations(request: Request):
    """Endpoint OpenAI-compatibile per la generazione immagini.

    Instrada SOLO verso deployment con capacità image_gen (model_capabilities).
    Su 404/provider-4xx dall'endpoint /images/generations e con
    capability_routing.images_chat_fallback=true, ritenta via chat/completions
    (modelli immagine esposti come chat, es. gemini-image).
    """
    import app.main as M

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400, content={"error": {"message": "invalid JSON body", "type": "invalid_request_error"}}
        )

    raw_model = payload.get("model") or ""
    if not str(payload.get("prompt") or "").strip():
        return JSONResponse(
            status_code=400, content={"error": {"message": "'prompt' è obbligatorio", "type": "invalid_request_error"}}
        )

    model = M.policy.canonicalize(raw_model)

    auth: AuthResult = M.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not M.authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"image_gen"}) if M.router.policy.routing_active() else frozenset()
    refs = image_refs_from_payload(payload)
    if refs:
        need = need | {"image_edit"}
    _set_opencode_gate(request)
    session_id = _session_id(request, payload)
    scope = "group" if M.router.is_explicit(model) else "chain"

    # Con immagini di riferimento la generazione va SEMPRE via chat multimodale:
    # i modelli image-edit (Gemini/nano-banana) ricevono la reference solo così.
    if refs:
        dep, profile, scope, err = _images_pick_dep(auth.profile, model, raw_model, session_id, need, payload)
        if err:
            return err
        custom_key = M.router.resolve_alias_key(raw_model, model)
        if custom_key:
            dep = {**dep, "api_key": custom_key}
        return await _images_chat_loop(
            request,
            payload=payload,
            refs=refs,
            raw_model=raw_model,
            model=model,
            need=need,
            scope=scope,
            dep=dep,
            profile=profile,
            session_id=session_id,
        )

    group_or_explicit = M.router.resolve_group_for_request(model, [], session_id, need, profile=auth.profile)
    if group_or_explicit is None:
        return JSONResponse(
            status_code=400 if need else 404,
            content={
                "error": {
                    "message": (
                        "nessun deployment dichiara image_gen: configura "
                        "capability_routing.model_capabilities in gateway.yaml"
                        if need
                        else f"model '{model}' not managed by {M.policy.service_name}"
                    ),
                    "type": "invalid_request_error",
                }
            },
        )

    dep = M.router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        dep = M.router.pick_deployment(group_or_explicit, need)
    if dep is None and auth.profile:
        dep = M.router.fallback_after(auth.profile, None, need, out_tokens=refill_out_budget(payload, M.router.policy))
    if dep is None:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "nessun deployment disponibile per image_gen", "type": "server_error"}},
        )

    custom_key = M.router.resolve_alias_key(raw_model, model)
    if custom_key:
        dep = {**dep, "api_key": custom_key}

    _sess = _opencode_session(request) or session_id
    _cip = _client_ip(request)
    _attr = _client_attribution(request)

    profile = auth.profile or M.config.profile_of_base(model.split("__")[0]) or M.config.profile_of_base(model)

    metrics.inc("nx_images_total", (dep["group"], "attempt"))
    M.log.info("[images] %s -> %s (prompt=%d chars)", model, dep["unique"], len(str(payload.get("prompt") or "")))

    tried: set[str] = set()
    # Marker "chat gia' tentata" SEPARATI da `tried`: cosi' non consumano il
    # budget di tentativi e la catena arriva davvero all'ultimo deployment
    # (inclusi i fallback a pagamento).
    chat_tried: set[str] = set()
    attempts: list[str] = []
    t_req = time.monotonic()
    last_err: UpstreamError | None = None

    async def _attempt_via_chat(cur: str, t0: float):
        """Genera via chat multimodale sul deployment `cur`.

        Ritorna la risposta finale, oppure solleva UpstreamError se la chat
        fallisce. Usata sia come fallback del nativo (firma "endpoint non
        supportato") sia come PRIMA scelta quando `image_via="chat"` dice che
        il provider espone il modello solo in chat."""
        chat_payload = image_chat_payload(payload, raw_model)
        data = await M.forwarder.call(dep, chat_payload, session=_sess, client_ip=_cip, attribution=_attr)
        M.router.note_result(cur, (time.monotonic() - t0) * 1000)
        metrics.inc("nx_images_total", (dep["group"], "ok_chat"))
        # normalizza: estrae le immagini dal messaggio se presenti
        if not isinstance(data, dict):
            out = data
        else:
            out = dict(data)
            out["nx_deployment"] = cur
            out["via"] = "chat"
            imgs = await _localize_images(request, images_dual(extract_chat_images(data)))
            if imgs:
                out["data"] = imgs
                out.setdefault("created", int(time.time()))
            else:
                M.log.warning("[images] chat su %s: nessuna immagine riconosciuta nella risposta", cur)
        _emit_summary(
            ses=session_id or "-",
            req=raw_model,
            grp=dep["group"],
            dep=cur,
            tries=len(attempts),
            fb=len(attempts) - 1,
            dur_ms=int((time.monotonic() - t_req) * 1000),
            stream=False,
            qc=False,
            wd=None,
            usage=None,
            kind="images",
            via="chat",
        )
        return out

    while dep is not None and len(tried) < 64:
        cur = dep["unique"]
        _was_dormant = M.router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        M.router.note_start(cur)
        t0 = time.monotonic()
        # `image_via` dichiara COME il provider espone i modelli immagine:
        # "chat" salta il nativo (farebbe 400 "not supported on /v1/images"),
        # risparmiando una chiamata persa e uno strike. Dichiarazione assente
        # ("both") = comportamento storico nativo-prima, poi chat.
        _via = dep_image_via(dep)
        if _via == "chat":
            try:
                return await _attempt_via_chat(cur, t0)
            except UpstreamError as chat_err:
                last_err = chat_err
                M.router.note_end(cur)
                M.router.mark_failed(
                    cur, seconds=chat_err.retry_after, status=abs(chat_err.status) if chat_err.status else None
                )
                metrics.inc("nx_images_total", (dep["group"], "retry"))
                nxt = (
                    M.router.fallback_next(
                        profile, dep, need, scope, tried=tried, out_tokens=refill_out_budget(payload, M.router.policy)
                    )
                    if profile
                    else None
                )
                if nxt is None:
                    break
                dep = nxt
                continue
        try:
            data = await M.forwarder.call_images(
                dep, payload, profile=profile or "", client_ip=_cip, session=_sess, attribution=_attr
            )
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                data["data"] = await _localize_images(request, images_dual(data["data"]))
            M.router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                M.router.clear_cooldown(cur)
            metrics.inc("nx_images_total", (dep["group"], "ok"))
            if isinstance(data, dict):
                data["nx_deployment"] = cur
            _emit_summary(
                ses=session_id or "-",
                req=raw_model,
                grp=dep["group"],
                dep=cur,
                tries=len(attempts),
                fb=len(attempts) - 1,
                dur_ms=int((time.monotonic() - t_req) * 1000),
                stream=False,
                qc=False,
                wd=None,
                usage=None,
                kind="images",
            )
            return data
        except UpstreamError as err:
            M.router.note_end(cur)
            last_err = err
            detail = err.detail or ""
            status = err.status if err.status is not None else 0
            deployment_side = (
                status > 0  # retryable (429/5xx/timeout)
                # chiave senza crediti / chiave rifiutata (401) / progetto
                # negato / endpoint o schema non gestiti: condizioni del
                # DEPLOYMENT, non del client -> ruota (la catena porta al
                # gruppo -image_gen-fallback, es. le chiavi OpenRouter a
                # pagamento). 401 = deployment-side perche' il client si e'
                # gia' autenticato verso il gateway (vedi TTS/video).
                or -status in (401, 402, 403, 404, 405, 415, 422)
                or _MODEL_MISSING_RE.search(detail)  # "No such model" stile CF
                # "unknown provider/model for model X": il deployment non ha
                # l'account o il modello (es. cli-proxy-api senza login) -> e'
                # una condizione del DEPLOYMENT, quindi si RUOTA verso il
                # successivo provider che serve lo stesso modello.
                or M.router.policy.images_chat_fallback
                and (image_chat_fallback_signature(err.status, detail) or chat_only_image_error(detail))
                or (
                    M.router.policy.images_chat_fallback
                    and -status == 400
                    and ("openai_error" in detail or "bad_response_status_code" in detail)
                )
            )
            if not deployment_side:
                metrics.inc("nx_images_total", (dep["group"], "client_error"))
                st = abs(status) if status else 502
                return JSONResponse(
                    status_code=st if st >= 400 else 502,
                    content={"error": {"message": err.detail, "type": "upstream_error"}},
                )
            # images.chat_fallback: prova via chat SOLO quando l'errore indica
            # che l'endpoint nativo e' assente o lo schema non e' riconosciuto.
            # Un 403/402 (permessi/crediti) non migliora via chat: ruota e basta.
            _chat_useful = (
                chat_only_image_error(detail)
                or image_chat_fallback_signature(err.status, detail)
                or (-status in (400, 403) and ("openai_error" in detail or "bad_response_status_code" in detail))
            )
            if M.router.policy.images_chat_fallback and _chat_useful and cur not in chat_tried:
                chat_tried.add(cur)
                M.log.info(
                    "[images] %s: /images/generations non disponibile (status=%s): ritenta via chat",
                    cur,
                    -status or "?",
                )
                try:
                    return await _attempt_via_chat(cur, t0)
                except UpstreamError as chat_err:
                    M.log.warning("[images] fallback chat su %s fallito: %s", cur, chat_err.detail[:120])
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    report_suppressed("images_api.images_generations@312")
            if _was_dormant:
                M.router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                M.router.mark_failed(cur, seconds=err.retry_after, status=abs(err.status) if err.status else None)
            metrics.inc("nx_images_total", (dep["group"], "retry"))
            nxt = (
                M.router.fallback_next(
                    profile, dep, need, scope, tried=tried, out_tokens=refill_out_budget(payload, M.router.policy)
                )
                if profile
                else None
            )
            if nxt is None:
                break
            dep = nxt
        finally:
            if cur in tried:
                M.router.note_end(cur)

    status = abs(last_err.status) if last_err and last_err.status else 502
    return JSONResponse(
        status_code=status if status >= 400 else 502,
        content={
            "error": {
                "message": (last_err.detail if last_err else "nessun deployment image_gen disponibile"),
                "type": "upstream_error",
            }
        },
    )


# --------------------------------------------------------------- image edits
@router.post("/v1/images/edits")
async def images_edits(request: Request):
    """Endpoint OpenAI-compatibile per l'EDIT di immagini (image-to-image).

    Accetta multipart/form-data (come OpenAI: campo `image`, uno o più file,
    anche `image[]`) oppure JSON con `image`/`images`/`reference_images`
    (data-URI base64 o URL). Le reference vengono inviate al deployment
    nell'adattamento giusto per il provider: endpoint nativo
    `/v1/images/edits` (multipart) per i modelli image-native, oppure chat
    multimodale (`messages` + `modalities:["image"]`) per i modelli chat-only.
    Il campo `mask` (PNG con alpha) viene inoltrato al provider nativo.
    """
    import app.main as M

    ctype = (request.headers.get("content-type") or "").lower()
    payload: dict = {}
    refs: list[str] = []
    if "multipart/form-data" in ctype or "application/x-www-form-urlencoded" in ctype:
        try:
            form = await request.form()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "multipart/form-data non valido", "type": "invalid_request_error"}},
            )
        payload["prompt"] = str(form.get("prompt") or "")
        if form.get("model"):
            payload["model"] = str(form.get("model"))
        if form.get("n") is not None:
            try:
                payload["n"] = int(str(form.get("n")))
            except (TypeError, ValueError):
                pass
        for k in ("size", "response_format", "quality", "user", "seed"):
            v = form.get(k)
            if v is not None and str(v).strip():
                payload[k] = str(v)
        mask_up = form.get("mask")
        if mask_up is not None:
            if isinstance(mask_up, str) and mask_up.strip():
                payload["mask"] = mask_up.strip()  # data-URI/URL
            else:
                try:
                    mraw = await mask_up.read()
                except Exception:
                    mraw = b""
                if mraw:
                    payload["mask"] = _data_uri(mraw, getattr(mask_up, "content_type", "") or "")
                else:
                    M.log.warning("[images] campo 'mask' presente ma vuoto")
        for field in ("image", "image[]", "images"):
            for up in form.getlist(field):
                if isinstance(up, str):
                    if up.strip():
                        refs.append(up.strip())
                    continue
                try:
                    raw = await up.read()
                except Exception:
                    raw = b""
                if raw:
                    refs.append(_data_uri(raw, getattr(up, "content_type", "") or ""))
    else:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400, content={"error": {"message": "invalid JSON body", "type": "invalid_request_error"}}
            )
        refs = image_refs_from_payload(payload)

    raw_model = payload.get("model") or ""
    if not str(payload.get("prompt") or "").strip():
        return JSONResponse(
            status_code=400, content={"error": {"message": "'prompt' è obbligatorio", "type": "invalid_request_error"}}
        )
    if not refs:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "'image' (immagine di riferimento) è obbligatorio",
                    "type": "invalid_request_error",
                }
            },
        )

    model = M.policy.canonicalize(raw_model)
    auth: AuthResult = M.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not M.authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"image_gen", "image_edit"}) if M.router.policy.routing_active() else frozenset()
    _set_opencode_gate(request)
    session_id = _session_id(request, payload)
    dep, profile, scope, err = _images_pick_dep(auth.profile, model, raw_model, session_id, need, payload)
    if err:
        return err
    custom_key = M.router.resolve_alias_key(raw_model, model)
    if custom_key:
        dep = {**dep, "api_key": custom_key}
    return await _images_chat_loop(
        request,
        payload=payload,
        refs=refs,
        raw_model=raw_model,
        model=model,
        need=need,
        scope=scope,
        dep=dep,
        profile=profile,
        session_id=session_id,
        endpoint="edits",
    )


@router.get("/v1/images/files/{file_id}")
async def images_files(file_id: str):
    """Download di un'immagine generata/edita (URL restituito da /v1/images/*).

    Pubblico (serve per `<img>`/download al volo): l'id e' un token casuale
    unguessable e la entry scade col TTL della policy `images.store_ttl_sec`."""
    got = imagestore.get(file_id)
    if not got:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": "immagine non trovata o scaduta", "type": "invalid_request_error"}},
        )
    content, mime = got
    ext = imagestore.ext_for_mime(mime)
    return Response(
        content=content,
        media_type=mime,
        headers={
            "Cache-Control": f"private, max-age={max(0, imagestore.ttl_sec())}",
            "Content-Disposition": f'inline; filename="image.{ext}"',
        },
    )

