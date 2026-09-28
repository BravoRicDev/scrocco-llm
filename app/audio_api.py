"""Endpoint audio (TTS, systemone/Jev, STT transcriptions/translations).
Estratti da `app/main.py` (cluster C8, Round 4 Clean Code).

Lo stato runtime condiviso (router, config, policy, forwarder, ...) si legge
da `app.state` (`gw_state.<nome>`), popolato da `app/main.py` all'avvio;
il logger e' quello di main (`nx.main`), cosi' i record restano identici.
"""
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from . import metrics, sttscrub
from .offload import request_json
from . import state as gw_state
from .http_responses import invalid_json_body
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
    _usage_of,
)
from .forwarder import (
    QUESTIONS_LIMIT_COOLDOWN_S,
    UpstreamError,
    _MODEL_MISSING_RE,
    _PROVIDER_TRANSIENT_RE,
    _QUESTIONS_LIMIT_RE,
    classify_error_class,
    media_reject_signature,
)
from .image_helpers import _cap_chain_pick
from .policy import refill_out_budget
from .stream_verdicts import _exhausted, _retry_at_ms

# Stesso logger di app.main: i record (nome "nx.main") restano identici.
log = logging.getLogger("nx.main")

router = APIRouter()


def _audio_route(profile: str | None, model: str, raw_model: str, session_id: str | None, need: frozenset[str]):
    """Routing condiviso degli endpoint audio: risolve il primo deployment
    capace (o explicit pass-through) oppure ritorna una JSONResponse d'errore.
    Ritorna (dep, profile, error_response)."""

    group_or_explicit = gw_state.router.resolve_group_for_request(model, [], session_id, need, profile=profile)
    if group_or_explicit is None:
        capname = sorted(need)[0] if need else model
        for c in sorted(need):
            metrics.inc("nx_caps_unroutable_total", (c,))
        return (
            None,
            profile,
            JSONResponse(
                status_code=400 if need else 404,
                content={
                    "error": {
                        "message": (
                            f"nessun deployment dichiara la capacità "
                            f"'{capname}': configura "
                            f"capability_routing.model_capabilities in "
                            f"gateway.yaml"
                            if need
                            else f"model '{model}' not managed by {gw_state.policy.service_name}"
                        ),
                        "type": "invalid_request_error",
                    }
                },
            ),
        )

    dep = gw_state.router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        dep = gw_state.router.pick_deployment(group_or_explicit, need)
    if dep is None and profile:
        dep = gw_state.router.fallback_after(profile, None, need, out_tokens=refill_out_budget({}, gw_state.router.policy))
    if dep is None:
        return (
            None,
            profile,
            JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": f"nessun deployment disponibile per {sorted(need) or model}",
                        "type": "server_error",
                    }
                },
            ),
        )

    custom_key = gw_state.router.resolve_alias_key(raw_model, model)
    if custom_key:
        dep = {**dep, "api_key": custom_key}
    return dep, profile, None


@router.post("/v1/audio/speech")
async def audio_speech(request: Request):
    """TTS OpenAI-compatibile: instrada SOLO su deployment con capacità tts."""

    try:
        payload = await request_json(request)
    except Exception:
        return invalid_json_body()
    raw_model = payload.get("model") or ""
    if not str(payload.get("input") or "").strip():
        return JSONResponse(
            status_code=400, content={"error": {"message": "'input' è obbligatorio", "type": "invalid_request_error"}}
        )
    model = gw_state.policy.canonicalize(raw_model)
    auth: AuthResult = gw_state.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not gw_state.authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"tts"}) if gw_state.router.policy.routing_active() else frozenset()
    scope = "group" if gw_state.router.is_explicit(model) else "chain"
    _set_opencode_gate(request)
    dep, profile, err = _audio_route(auth.profile, model, raw_model, _session_id(request, payload), need)
    if err:
        return err

    metrics.inc("nx_tts_total", (dep["group"], "attempt"))
    log.info("[tts] %s -> %s (input=%d chars)", model, dep["unique"], len(str(payload.get("input") or "")))

    tried: set[str] = set()
    attempts: list[str] = []
    session_id = _session_id(request, payload)
    _sess = _opencode_session(request) or session_id
    _cip = _client_ip(request)
    _attr = _client_attribution(request)
    t_req = time.monotonic()
    last_err: UpstreamError | None = None
    while dep is not None and len(tried) < 32:
        cur = dep["unique"]
        _was_dormant = gw_state.router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        gw_state.router.note_start(cur)
        t0 = time.monotonic()
        try:
            content, ctype = await gw_state.forwarder.call_speech(
                dep, payload, profile=profile or "", client_ip=_cip, session=_sess, attribution=_attr
            )
            gw_state.router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                gw_state.router.clear_cooldown(cur)
            metrics.inc("nx_tts_total", (dep["group"], "ok"))
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
                kind="tts",
                bytes=len(content),
                ctype=ctype,
            )
            return Response(content=content, media_type=ctype, headers={"x-nx-deployment": cur})
        except UpstreamError as err:
            gw_state.router.note_end(cur)
            last_err = err
            detail = err.detail or ""
            status = err.status if err.status is not None else 0
            deployment_side = (
                # 401 = la NOSTRA chiave upstream e' rifiutata: SEMPRE
                # deployment-side (il client si e' gia' autenticato verso il
                # gateway), quindi si RUOTA come il 403/404/402. Criterio
                # gia' presente e testato sul path chat
                # (tests/test_upstream_401.py).
                status > 0
                or -status in (401, 402, 404)
                or _MODEL_MISSING_RE.search(detail)  # "No such model" stile CF
                or (-status in (400, 403) and ("openai_error" in detail or "bad_response_status_code" in detail))
            )
            if not deployment_side:
                metrics.inc("nx_tts_total", (dep["group"], "client_error"))
                st = abs(status) if status else 502
                return JSONResponse(
                    status_code=st if st >= 400 else 502,
                    content={"error": {"message": err.detail, "type": "upstream_error"}},
                )
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    report_suppressed("audio_api.audio_speech")
            if _was_dormant:
                gw_state.router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                gw_state.router.mark_failed(cur, seconds=err.retry_after, status=abs(err.status) if err.status else None)
            metrics.inc("nx_tts_total", (dep["group"], "retry"))
            nxt = (
                gw_state.router.fallback_next(
                    profile, dep, need, scope, tried=tried, out_tokens=refill_out_budget(payload, gw_state.router.policy)
                )
                if profile
                else None
            )
            if nxt is None:
                break
            dep = nxt

    status = abs(last_err.status) if last_err and last_err.status else 502
    return JSONResponse(
        status_code=status if status >= 400 else 502,
        content={
            "error": {
                "message": (last_err.detail if last_err else "nessun deployment tts disponibile"),
                "type": "upstream_error",
            }
        },
    )


# --------------------------------------------------------- systemone (Jev)
@router.post("/v1/systemone")
async def systemone(request: Request):
    """Jev / TypeSafe System One: decisione strutturata.

    Body nativo `{model, state, questions}` -> risposta nativa
    `{model, answers, usage}` con `nx_deployment`/`nx_provider` aggiunti.
    Instrada SOLO su deployment con capacità `decision`. Non-streaming, nessuna
    traduzione di protocollo, niente tool/reasoning/hold.
    """

    try:
        payload = await request_json(request)
    except Exception:
        return invalid_json_body()
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "il body deve essere un oggetto JSON", "type": "invalid_request_error"}},
        )
    raw_model = payload.get("model") or ""
    if "state" not in payload:
        return JSONResponse(
            status_code=400, content={"error": {"message": "'state' è obbligatorio", "type": "invalid_request_error"}}
        )
    questions = payload.get("questions")
    if not isinstance(questions, dict) or not questions:
        return JSONResponse(
            status_code=400,
            content={
                "error": {"message": "'questions' deve essere un oggetto non vuoto", "type": "invalid_request_error"}
            },
        )

    model = gw_state.policy.canonicalize(raw_model)
    auth: AuthResult = gw_state.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not gw_state.authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"decision"}) if gw_state.router.policy.routing_active() else frozenset()
    scope = "group" if gw_state.router.is_explicit(model) else "chain"
    _set_opencode_gate(request)
    session_id = _session_id(request, payload)
    # Ruota anche con master key: il profilo si ricava dal nome richiesto,
    # altrimenti la rotazione resterebbe spenta (bug dei path audio).
    _prof = auth.profile or gw_state.config.profile_of_base(model.split("__")[0]) or gw_state.config.profile_of_base(model)

    group_or_explicit = gw_state.router.resolve_group_for_request(model, [], session_id, need, profile=_prof)
    if group_or_explicit is None:
        for c in sorted(need):
            metrics.inc("nx_caps_unroutable_total", (c,))
        return JSONResponse(
            status_code=400 if need else 404,
            content={
                "error": {
                    "message": (
                        "nessun deployment dichiara la capacità 'decision': "
                        "configura capability_routing.model_capabilities o la "
                        "colonna caps"
                        if need
                        else f"model '{model}' non gestito da {gw_state.policy.service_name}"
                    ),
                    "type": "invalid_request_error",
                }
            },
        )

    explicit_req = gw_state.router.is_explicit(model)
    dep = gw_state.router.config.deployment_by_unique(group_or_explicit)
    _cap = gw_state.router.config.group_caps.get(group_or_explicit)
    # Gruppo capacità con primario VUOTO (es. righe Jev a pagamento tutte
    # `fallback`): la catena capability attraversa free -> -go -> -fallback e
    # trova i deployment che il solo gruppo primario non ha.
    if dep is None and _cap is not None and not explicit_req:
        dep = _cap_chain_pick(_prof, need)
    if dep is None:
        dep = gw_state.router.initial_pick(
            _prof, group_or_explicit, None if explicit_req else need, out_tokens=refill_out_budget(payload, gw_state.policy)
        )
    if dep is None and _prof and not explicit_req:
        dep = _cap_chain_pick(_prof, need) or gw_state.router.fallback_after(
            _prof, None, need, out_tokens=refill_out_budget(payload, gw_state.router.policy)
        )
    if dep is None:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "nessun deployment systemone disponibile", "type": "server_error"}},
        )

    metrics.inc("nx_systemone_total", (dep["group"], "attempt"))
    log.info("[systemone] %s -> %s (questions=%d)", model, dep["unique"], len(questions))

    tried: set[str] = set()
    attempts: list[str] = []
    trail: list = []
    _sess = _opencode_session(request) or session_id
    _cip = _client_ip(request)
    _attr = _client_attribution(request)
    t_req = time.monotonic()
    last_err: UpstreamError | None = None
    while dep is not None and len(tried) < 32:
        cur = dep["unique"]
        _was_dormant = gw_state.router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        gw_state.router.note_start(cur)
        t0 = time.monotonic()
        try:
            out = await gw_state.forwarder.call_systemone(
                dep, payload, profile=_prof or "", client_ip=_cip, session=_sess, attribution=_attr
            )
            # QC: ogni chiave richiesta in `questions` deve comparire in
            # `answers`; una risposta incompleta e' un problema del deployment
            # -> rotazione (502 positivo = ritriabile).
            _answers = out.get("answers") if isinstance(out, dict) else None
            _missing = [k for k in questions if not (isinstance(_answers, dict) and k in _answers)]
            if _missing:
                raise UpstreamError(502, f"risposta systemone incompleta: chiavi mancanti {_missing}")
            gw_state.router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                gw_state.router.clear_cooldown(cur)
            metrics.inc("nx_systemone_total", (dep["group"], "ok"))
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
                usage=_usage_of(out),
                kind="systemone",
            )
            result = dict(out)
            result["nx_deployment"] = cur
            result["nx_provider"] = dep.get("provider")
            return JSONResponse(result, headers={"x-nx-deployment": cur})
        except UpstreamError as err:
            last_err = err
            detail = err.detail or ""
            status = err.status if err.status is not None else 0
            # status==0 (timeout/rete/non-JSON) e' transiente del deployment:
            # DEVE ruotare (i path audio lo consegnano invece come 502).
            questions_limit_hit = bool(_QUESTIONS_LIMIT_RE.search(detail))
            deployment_side = (
                status >= 0
                or -status in (401, 402, 403, 404, 405, 415, 422)
                or _MODEL_MISSING_RE.search(detail)
                or _PROVIDER_TRANSIENT_RE.search(detail)
                or questions_limit_hit
            )
            trail.append(
                {
                    "ord": len(trail) + 1,
                    "dep": cur,
                    "group": dep.get("group"),
                    "model": dep.get("model"),
                    "cls": classify_error_class(abs(status) if status else 0, detail),
                    "status": abs(status) if status else None,
                    "ms": int((time.monotonic() - t0) * 1000),
                }
            )
            if not deployment_side:
                metrics.inc("nx_systemone_total", (dep["group"], "client_error"))
                st = abs(status) if status else 502
                return JSONResponse(
                    status_code=st if st >= 400 else 502,
                    content={"error": {"message": err.detail, "type": "upstream_error"}},
                )
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    report_suppressed("audio_api.systemone")
            if _was_dormant:
                gw_state.router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                cooldown_s = QUESTIONS_LIMIT_COOLDOWN_S if questions_limit_hit else err.retry_after
                gw_state.router.mark_failed(cur, seconds=cooldown_s, status=abs(err.status) if err.status else None)
            metrics.inc("nx_systemone_total", (dep["group"], "retry"))
            nxt = (
                gw_state.router.fallback_next(
                    _prof, dep, need, scope, tried=tried, out_tokens=refill_out_budget(payload, gw_state.router.policy)
                )
                if _prof
                else None
            )
            if nxt is None:
                break
            dep = nxt
        finally:
            gw_state.router.note_end(cur)

    return _exhausted(
        len(attempts), last_err.detail if last_err else None, trail=trail, retry_at_ms=_retry_at_ms(gw_state.router, trail)
    )


# ----------------------------------------------------------------- audio STT
async def _audio_transcribe(request: Request, path: str):
    """Handler condiviso transcriptions/translations (multipart form).

    Form OpenAI: file (binario), model, language?, prompt?, response_format?
    (json|text|srt|verbose_json|vtt), temperature?. Instrada SOLO su
    deployment con capacità stt.
    """

    try:
        form = await request.form()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "multipart/form-data non valido", "type": "invalid_request_error"}},
        )

    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "'file' (audio) è obbligatorio", "type": "invalid_request_error"}},
        )
    filename = getattr(upload, "filename", "") or "audio"
    fcontent = getattr(upload, "content_type", "") or "application/octet-stream"
    try:
        file_bytes = await upload.read()
    except Exception:
        return JSONResponse(
            status_code=400, content={"error": {"message": "lettura del file fallita", "type": "invalid_request_error"}}
        )
    if not file_bytes:
        return JSONResponse(
            status_code=400, content={"error": {"message": "file audio vuoto", "type": "invalid_request_error"}}
        )

    data_fields = {
        k: v
        for k in ("language", "prompt", "response_format", "temperature", "hotwords", "vad_filter")
        if (v := form.get(k)) is not None
    }
    raw_model = str(form.get("model") or "")
    response_format = str(data_fields.get("response_format") or "json").lower()

    model = gw_state.policy.canonicalize(raw_model)
    auth: AuthResult = gw_state.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not gw_state.authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"stt"}) if gw_state.router.policy.routing_active() else frozenset()
    scope = "group" if gw_state.router.is_explicit(model) else "chain"
    _set_opencode_gate(request)
    session_id = _session_id(request, {})
    dep, profile, err = _audio_route(auth.profile, model, raw_model, session_id, need)
    if err:
        return err

    metrics.inc("nx_stt_total", (dep["group"], "attempt"))
    log.info("[stt] %s -> %s (%s, %d bytes, via /%s)", model, dep["unique"], filename, len(file_bytes), path)

    tried: set[str] = set()
    attempts: list[str] = []
    _sess = _opencode_session(request) or session_id
    _cip = _client_ip(request)
    _attr = _client_attribution(request)
    t_req = time.monotonic()
    last_err: UpstreamError | None = None
    while dep is not None and len(tried) < 32:
        cur = dep["unique"]
        _was_dormant = gw_state.router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        gw_state.router.note_start(cur)
        t0 = time.monotonic()
        try:
            result = await gw_state.forwarder.transcribe(
                dep,
                data_fields,
                file_bytes,
                filename,
                fcontent,
                path=path,
                profile=profile or "",
                client_ip=_cip,
                session=_sess,
                attribution=_attr,
            )
            gw_state.router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                gw_state.router.clear_cooldown(cur)
            metrics.inc("nx_stt_total", (dep["group"], "ok"))
            _emit_summary(
                ses=_session_id(request, {}) or "-",
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
                kind="stt",
                path=path,
            )
            result, _scrubbed = sttscrub.scrub_payload(result)
            if _scrubbed:
                log.info("[stt-scrub] %s: rimosse %d allucinazioni credit", cur, _scrubbed)
                metrics.inc("nx_stt_scrubbed_total", (dep["group"],))
            if isinstance(result, dict):
                result.setdefault("nx_deployment", cur)
                return JSONResponse(result)
            # formati text/srt/vtt: passthrough testo + header di disclosure
            return PlainTextResponse(result, headers={"x-nx-deployment": cur})
        except UpstreamError as err:
            gw_state.router.note_end(cur)
            last_err = err
            detail = err.detail or ""
            status = err.status if err.status is not None else 0
            deployment_side = (
                # 401 = la NOSTRA chiave upstream e' rifiutata: SEMPRE
                # deployment-side (il client si e' gia' autenticato verso il
                # gateway), quindi si RUOTA come il 403/404/402. Criterio
                # gia' presente e testato sul path chat
                # (tests/test_upstream_401.py).
                status > 0
                or -status in (401, 402, 404)
                or _MODEL_MISSING_RE.search(detail)  # "No such model" stile CF
                or (-status in (400, 403) and ("openai_error" in detail or "bad_response_status_code" in detail))
            )
            if not deployment_side:
                metrics.inc("nx_stt_total", (dep["group"], "client_error"))
                st = abs(status) if status else 502
                return JSONResponse(
                    status_code=st if st >= 400 else 502,
                    content={"error": {"message": err.detail, "type": "upstream_error"}},
                )
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    report_suppressed("audio_api._audio_transcribe")
            if _was_dormant:
                gw_state.router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                gw_state.router.mark_failed(cur, seconds=err.retry_after, status=abs(err.status) if err.status else None)
            metrics.inc("nx_stt_total", (dep["group"], "retry"))
            nxt = (
                gw_state.router.fallback_next(
                    profile, dep, need, scope, tried=tried, out_tokens=refill_out_budget({}, gw_state.router.policy)
                )
                if profile
                else None
            )
            if nxt is None:
                break
            dep = nxt

    status = abs(last_err.status) if last_err and last_err.status else 502
    return JSONResponse(
        status_code=status if status >= 400 else 502,
        content={
            "error": {
                "message": (last_err.detail if last_err else "nessun deployment stt disponibile"),
                "type": "upstream_error",
            }
        },
    )


@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request):
    """STT OpenAI-compatibile: audio -> testo nella lingua originale."""
    return await _audio_transcribe(request, "transcriptions")


@router.post("/v1/audio/translations")
async def audio_translations(request: Request):
    """STT OpenAI-compatibile: audio -> testo tradotto in inglese."""
    return await _audio_transcribe(request, "translations")
