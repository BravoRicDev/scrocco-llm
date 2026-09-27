"""Endpoint video async (submit, poll status, download content).
Estratti da `app/main.py` (cluster C9, Round 4 Clean Code).

Lo stato runtime condiviso (router, config, policy, forwarder, ...) si legge
da `app.state` (`gw_state.<nome>`), popolato da `app/main.py` all'avvio;
il logger e' quello di main (`nx.main`), cosi' i record restano identici.
`gw_state._videos_jobs` e' il dict dei job (mutato, mai riassegnato) che il
watcher di pulizia scorre.
"""
import logging
import asyncio
import time
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import metrics
from . import state as gw_state
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
from .forwarder import UpstreamError, _MODEL_MISSING_RE, media_reject_signature
from .policy import refill_out_budget

# Stesso logger di app.main: i record (nome "nx.main") restano identici.
log = logging.getLogger("nx.main")

router = APIRouter()


@router.post("/v1/videos/generations")
async def videos_generations(request: Request):
    """Generazione video ASINCRONA (stile OR): submit -> {id,status,polling_url}.

    Instrada SOLO su deployment con capacità video_gen. Il client polla
    GET /v1/videos/generations/{job_id} e scarica via .../{job_id}/content.
    """

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

    model = gw_state.policy.canonicalize(raw_model)
    auth: AuthResult = gw_state.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not gw_state.authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"video_gen"}) if gw_state.router.policy.routing_active() else frozenset()
    # i2v / reference-to-video: le immagini d'input esigono il token vision
    # nell'inner-filter (solo modelli che accettano frame/reference)
    if payload.get("frame_images") or payload.get("input_references"):
        need = need | {"vision"}
    scope = "group" if gw_state.router.is_explicit(model) else "chain"
    _set_opencode_gate(request)
    session_id = _session_id(request, payload)

    group_or_explicit = gw_state.router.resolve_group_for_request(
        model,
        [],
        session_id,
        need | {"vision"} if (need and (payload.get("frame_images") or payload.get("input_references"))) else need,
        profile=auth.profile,
    )
    if group_or_explicit is None:
        for c in sorted(need or {"video_gen"}):
            metrics.inc("nx_caps_unroutable_total", (c,))
        return JSONResponse(
            status_code=400 if need else 404,
            content={
                "error": {
                    "message": (
                        "nessun deployment dichiara 'video_gen': configura "
                        "capability_routing.model_capabilities / colonna caps"
                        if need
                        else f"model '{model}' non gestito"
                    ),
                    "type": "invalid_request_error",
                }
            },
        )

    explicit_req = gw_state.router.is_explicit(model)
    dep = gw_state.router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        dep = gw_state.router.initial_pick(
            auth.profile,
            group_or_explicit,
            None if explicit_req else need,
            out_tokens=refill_out_budget(payload, gw_state.policy),
        )
    if dep is None and auth.profile and not explicit_req:
        dep = gw_state.router.fallback_after(auth.profile, None, need, out_tokens=refill_out_budget(payload, gw_state.router.policy))
    if dep is None:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "nessun deployment video_gen disponibile", "type": "server_error"}},
        )

    metrics.inc("nx_videos_total", (dep["group"], "attempt"))
    log.info("[videos] %s -> %s (prompt=%d chars)", model, dep["unique"], len(str(payload.get("prompt") or "")))

    tried: set[str] = set()
    attempts: list[str] = []
    _sess = _opencode_session(request) or session_id
    _cip = _client_ip(request)
    _attr = _client_attribution(request)
    _prof = auth.profile or gw_state.config.profile_of_base(model.split("__")[0]) or gw_state.config.profile_of_base(model)
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
            envelope = await gw_state.forwarder.submit_video(
                dep, payload, profile=_prof or "", client_ip=_cip, session=_sess, attribution=_attr
            )
            gw_state.router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                gw_state.router.clear_cooldown(cur)
            metrics.inc("nx_videos_total", (dep["group"], "ok"))
            job_id = str(envelope.get("id") or "")
            gw_state._videos_jobs[job_id] = {
                "api_base": dep["api_base"],
                "api_key": dep["api_key"],
                "group": dep["group"],
                "created": time.time(),
                "_sess": _sess,
                "_cip": _cip,
                "_prof": _prof or "",
            }
            qm = f"?model={urllib.parse.quote(raw_model)}"
            out = dict(envelope)
            out["nx_deployment"] = cur
            out["eta_seconds"] = 90
            # ENVELOPE AUTO-DESCRIBENTE per agenti: le URL portano il model
            # embedded -> poll/content STATELESS, sopravvivono ai restart
            out["poll"] = {"url": f"/v1/videos/generations/{job_id}{qm}", "interval_s": 15}
            out["content"] = {"url": f"/v1/videos/generations/{job_id}/content{qm}"}
            out["polling_url"] = out["poll"]["url"]
            out["content_url"] = out["content"]["url"]

            # --- wait mode (per tool-agent semplici): il gateway polla ---
            wait_s = int(payload.get("wait_seconds") or 0)
            if wait_s > 0:
                wait_s = min(wait_s, 240)
                deadline = time.time() + wait_s
                while time.time() < deadline:
                    await asyncio.sleep(5)
                    try:
                        st = await gw_state.forwarder.poll_video(
                            dep,
                            job_id,
                            profile=_prof or gw_state._videos_jobs.get(job_id, {}).get("_prof", ""),
                            client_ip=_cip or gw_state._videos_jobs.get(job_id, {}).get("_cip", ""),
                            session=_sess or gw_state._videos_jobs.get(job_id, {}).get("_sess"),
                            attribution=_attr,
                        )
                        log.debug(
                            "[video-wait] job=%s t=%ds status=%s",
                            job_id,
                            wait_s - int(deadline - time.time()),
                            st.get("status"),
                        )
                    except UpstreamError:
                        continue  # blip upstream: riprova fino a deadline
                    if st.get("status") == "completed":
                        out.update(
                            {"status": "completed", "usage": st.get("usage"), "unsigned_urls": st.get("unsigned_urls")}
                        )
                        break
                    if st.get("status") in ("failed", "cancelled", "expired"):
                        out.update({"status": st.get("status"), "error": st.get("error")})
                        break
                else:
                    out["note"] = "wait_seconds esaurito col job ancora in corso: prosegui con poll.url"
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
                usage=_usage_of(out),
                kind="videos",
                job=job_id,
                status=out.get("status"),
            )
            return JSONResponse(out)
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
                metrics.inc("nx_videos_total", (dep["group"], "client_error"))
                st = abs(status) if status else 502
                return JSONResponse(
                    status_code=st if st >= 400 else 502,
                    content={"error": {"message": err.detail, "type": "upstream_error"}},
                )
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    report_suppressed("videos_api.videos_generations@239")
            if _was_dormant:
                gw_state.router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                gw_state.router.mark_failed(cur, seconds=err.retry_after, status=abs(err.status) if err.status else None)
            metrics.inc("nx_videos_total", (dep["group"], "retry"))
            nxt = (
                gw_state.router.fallback_next(
                    profile, dep, need, scope, tried=tried, out_tokens=refill_out_budget(payload, gw_state.router.policy)
                )
                if (
                    profile := auth.profile
                    or gw_state.config.profile_of_base(model.split("__")[0])
                    or gw_state.config.profile_of_base(model)
                )
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
                "message": (last_err.detail if last_err else "nessun deployment video_gen disponibile"),
                "type": "upstream_error",
            }
        },
    )


def _job_deps(job_id: str, model: str | None):
    """Risolve i deployment per poll/content di un job video.

    STATELESS-FIRST: con ?model= (nome/alias/gruppo) si ricostruisce la
    lista dei candidati dal CSV/policy — sopravvive ai restart del gateway
    e funziona da qualsiasi replica. Senza model, si usa il mapping in
    memoria della submit (fast-path, perso su restart).
    Ritorna (deps_list | None, error_response | None)."""

    if model:
        canonical = gw_state.policy.canonicalize(model)
        deps = gw_state.router.video_gen_candidates(canonical)
        if deps:
            return deps, None
        return None, JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"nessun gruppo video_gen per '{model}' (colonna caps / capability_groups)",
                    "type": "invalid_request_error",
                }
            },
        )
    snap = gw_state._videos_jobs.get(job_id)
    if snap is None:
        return None, JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"job '{job_id}' sconosciuto o scaduto (TTL 24h / "
                    f"restart): ripassa ?model=<modello> per il lookup "
                    f"stateless, oppure rifai la submit",
                    "type": "invalid_request_error",
                }
            },
        )
    return [{"api_base": snap["api_base"], "api_key": snap["api_key"], "model": "", "unique": job_id}], None


@router.get("/v1/videos/generations/{job_id}")
async def videos_status(job_id: str, request: Request, model: str | None = None):

    auth = gw_state.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    deps, err = _job_deps(job_id, model or request.query_params.get("model"))
    if err:
        return err
    _prof = model or request.query_params.get("model") or ""
    _prof = gw_state.config.profile_of_base(_prof.split("__")[0]) or ""
    try:
        status = await gw_state.forwarder.poll_video_any(
            deps,
            job_id,
            profile=_prof,
            client_ip=_client_ip(request),
            session=_opencode_session(request),
            attribution=_client_attribution(request),
        )
    except UpstreamError as e:
        st = abs(e.status) if e.status and e.status > 0 else 502
        return JSONResponse(status_code=st if st >= 400 else 502, content={"error": {"message": e.detail}})
    st_val = status.get("status") if isinstance(status, dict) else "?"
    if st_val in ("completed", "failed", "cancelled", "expired"):
        log.info("[video-poll] job=%s -> %s", job_id, st_val)
    else:
        log.debug("[video-poll] job=%s status=%s", job_id, st_val)
    qm = request.query_params.get("model")
    if isinstance(status, dict):
        status.setdefault("polling_url", f"/v1/videos/generations/{job_id}" + (f"?model={qm}" if qm else ""))
        if status.get("unsigned_urls"):
            status["content_url"] = f"/v1/videos/generations/{job_id}/content" + (f"?model={qm}" if qm else "")
    return JSONResponse(status)


@router.get("/v1/videos/generations/{job_id}/content")
async def videos_content(job_id: str, request: Request, model: str | None = None):

    auth = gw_state.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    deps, err = _job_deps(job_id, model or request.query_params.get("model"))
    if err:
        return err
    _prof = model or request.query_params.get("model") or ""
    _prof = gw_state.config.profile_of_base(_prof.split("__")[0]) or ""
    try:
        t_dl = time.monotonic()
        content, ctype = await gw_state.forwarder.download_video_any(
            deps,
            job_id,
            profile=_prof,
            client_ip=_client_ip(request),
            session=_opencode_session(request),
            attribution=_client_attribution(request),
        )
        log.info(
            "[video-content] job=%s bytes=%d ctype=%s dur=%.1fs", job_id, len(content), ctype, time.monotonic() - t_dl
        )
    except UpstreamError as e:
        st = abs(e.status) if e.status and e.status > 0 else 502
        return JSONResponse(status_code=st if st >= 400 else 502, content={"error": {"message": e.detail}})
    return Response(content=content, media_type=ctype)

