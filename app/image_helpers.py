"""Helper immagini condivisi da /v1/images/* e dall'adattamento chat->images.
Estratti da `app/main.py` (cluster C6, Round 4 Clean Code).

Lo stato runtime condiviso (router, config, policy, forwarder, ...) si legge
da `app.state` (`gw_state.<nome>`), popolato da `app/main.py` all'avvio;
il logger e' quello di main (`nx.main`), cosi' i record restano identici.
"""
import logging
import base64
import time
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from . import metrics
from . import state as gw_state
from .suppressed import report_suppressed
from . import imagestore
from .auth import AuthResult
from .config import CAP_PRIORITY_ORDER
from .policy import refill_out_budget
from .capabilities import refs_max_for, wants_image_output
from .chat_helpers import (
    _client_attribution,
    _client_ip,
    _emit_summary,
    _opencode_session,
    _strike_hook,
)
from .forwarder import (
    UpstreamError,
    _MODEL_MISSING_RE,
    _split_data_uri,
    chat_only_image_error,
    chat_prompt_and_refs,
    dep_image_via,
    extract_chat_images,
    image_chat_fallback_signature,
    image_chat_payload,
    image_item_dual,
    images_dual,
    images_payload_from_chat,
    images_response_to_chat,
    media_reject_signature,
    truncate_refs,
)

# Stesso logger di app.main: i record (nome "nx.main") restano identici.
log = logging.getLogger("nx.main")


def _data_uri(data: bytes, content_type: str = "") -> str:
    """Bytes -> data-URI base64 (per le reference ricevute in multipart)."""
    mime = (content_type or "image/png").split(";")[0].strip() or "image/png"
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def _public_base_url(request: Request) -> str:
    """Base URL pubblico per i download immagine.

    Usa la policy `images.url_base` se impostata; altrimenti la deriva dalla
    request (X-Forwarded-Proto/Host, poi Host): i client raggiungono il gateway
    direttamente (LAN/VPN) o via reverse proxy che inoltra quegli header."""

    cfg = (getattr(gw_state.router.policy, "images_url_base", "") or "").strip()
    if cfg:
        return cfg.rstrip("/")
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip() or (
        request.headers.get("host") or ""
    ).strip()
    if not proto:
        proto = "https" if request.url.scheme == "https" else "http"
    if host:
        return f"{proto}://{host}"
    return f"{request.url.scheme}://{request.url.netloc}"


def _b64decode(s):
    """base64 tollerante (padding aggiunto); None se indecifrabile."""
    try:
        data = str(s or "")
        return base64.b64decode(data + "=" * (-len(data) % 4))
    except Exception:  # noqa: BLE001
        return None


async def _download_remote_image(url: str, *, timeout: float, max_bytes: int) -> tuple[bytes, str] | None:
    """Scarica un'immagine da un URL http(s) del provider (mirror locale).

    Ritorna (bytes, content_type) oppure None su errore/superamento del cap."""

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cli:
            async with cli.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    log.debug("[images] mirror %s: status %s", url, resp.status_code)
                    return None
                ctype = resp.headers.get("content-type") or ""
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if max_bytes and total > max_bytes:
                        log.warning("[images] mirror %s: supera %d byte", url, max_bytes)
                        return None
                    chunks.append(chunk)
                if not chunks:
                    return None
                return b"".join(chunks), ctype
    except Exception as exc:  # noqa: BLE001
        log.warning("[images] mirror %s fallito: %s", url, exc, exc_info=True)
        return None


async def _localize_images(request: Request, items):
    """Espone per ogni immagine un `url` NOSTRO + `b64_json`.

    - data-URI / b64_json -> salvata nello store locale (url del gateway);
    - url http(s) del provider -> scaricata e ri-ospitata (mirror);
    - store disabilitato o download fallito -> item invariato (url provider).
    """

    if not isinstance(items, list) or not items:
        return items
    if not bool(getattr(gw_state.router.policy, "images_store_enabled", True)):
        return items
    mirror = bool(getattr(gw_state.router.policy, "images_mirror_remote", True))
    base = _public_base_url(request)
    tmo = float(getattr(gw_state.router.policy, "images_remote_timeout_sec", 60) or 60)
    rmax = int(getattr(gw_state.router.policy, "images_remote_max_bytes", 20971520) or 0)
    out: list[dict] = []
    for it in items:
        dual = image_item_dual(it) if isinstance(it, dict) else None
        if not isinstance(dual, dict):
            out.append(it)
            continue
        url = dual.get("url")
        b64 = dual.get("b64_json")
        mime = None
        data_bytes: bytes | None = None
        if isinstance(url, str) and url.startswith("data:"):
            parsed = _split_data_uri(url)
            if parsed:
                mime = parsed[0]
                data_bytes = _b64decode(parsed[1])
        elif b64:
            data_bytes = _b64decode(b64)
        elif mirror and isinstance(url, str) and url.startswith(("http://", "https://")):
            got = await _download_remote_image(url, timeout=tmo, max_bytes=rmax)
            if got:
                data_bytes, mime = got
        if not data_bytes:
            out.append(dual)
            continue
        file_id = imagestore.put(data_bytes, mime)
        if not file_id:
            out.append(dual)
            continue
        ext = imagestore.ext_for_mime(mime)
        new = dict(dual)
        new["url"] = f"{base}/v1/images/files/{file_id}"
        new["b64_json"] = base64.b64encode(data_bytes).decode()
        new["mime_type"] = imagestore.normalize_mime(mime)
        new["file_name"] = f"image.{ext}"
        out.append(new)
    return out


def _images_group_for_base(prof: str, need: frozenset[str]) -> str | None:
    """Nome del gruppo-capacita' immagine per un modello BASE (generico).

    Un modello generico (`scrocco-llm-fissone`) o con un suffisso DIM
    (`-200k`, `-262k`) non nomina alcuna capacita': `resolve_group_for_request`
    lo manda quindi nel mondo TESTO (e per il sizing) e il dep restituito non
    genera immagini mai. Qui si sceglie direttamente il gruppo immagine del
    profilo: `{prefix}{prof}-image_gen`, con lo stesso ordine di preferenza
    del routing (free, poi -go, poi -fallback). Ritorna None se il profilo non
    ha quel gruppo."""

    base = f"{gw_state.router.config.proxy_prefix}{prof}"
    for suffix in (
        "",
        getattr(gw_state.router.policy, "go_suffix", "-go"),
        getattr(gw_state.router.policy, "fallback_suffix", "-fallback"),
    ):
        g = f"{base}-image_gen{suffix}"
        if g in gw_state.router.config.groups:
            return g
    return None


def _cap_chain_pick_all(prof: str, need: frozenset[str]) -> list[dict]:
    """Deployment UTILI alla catena capability del profilo, in ordine di rotazione.

    E' il cuore del comportamento "camaleontico" che chiede l'utente: la
    richiesta va capita A MONTE e il modello richiesto non deve essere un
    vincolo, ma un punto di partenza. Se il nome chiesto (generico, o con
    suffisso dim `-200k`) non nomina la capacita', il gateway la sale lui
    attraversando TUTTI i deployment del profilo per quella capacita', con lo
    stesso ordine del routing (free -> -go -> -fallback).

    Le capacita' si valutano in `CAP_PRIORITY_ORDER` (image_gen prima di
    image_edit non per caso: se un deployment soddisfa entrambe la catena lo
    prende al primo posto) e si pretende che il deployment soddisfi TUTTE le
    capacita' richieste. Restituisce la lista completa perche' l'intercettore
    ruoti su piu' deployment, esattamente come fanno gli endpoint /images/*."""

    chains = getattr(gw_state.router.config, "chains_cap", {}).get(prof) or {}
    out: list[dict] = []
    seen: set[str] = set()
    for cap in CAP_PRIORITY_ORDER:
        if cap not in need:
            continue
        for u in chains.get(cap) or ():
            if u in seen:
                continue
            d = gw_state.router.config.deployment_by_unique(u)
            if d is not None and need <= set(d.get("caps") or ()):
                seen.add(u)
                out.append(d)
    return out


def _cap_chain_pick(prof: str, need: frozenset[str]) -> dict | None:
    """Primo deployment UTILE dalla catena capability (vedi
    `_cap_chain_pick_all`)."""
    got = _cap_chain_pick_all(prof, need)
    return got[0] if got else None


def _images_pick_dep(
    profile: str | None, model: str, raw_model: str, session_id: str | None, need: frozenset[str], payload: dict
):
    """Risoluzione del primo deployment per gli endpoint immagini.

    Ritorna (dep, profile_effettivo, scope, error_response). `need` vuoto =
    nessun filtro capacità (routing disattivato)."""

    scope = "group" if gw_state.router.is_explicit(model) else "chain"
    prof = profile or gw_state.config.profile_of_base(model.split("__")[0]) or gw_state.config.profile_of_base(model)
    # Modello BASE o con suffisso DIM (`-200k`, `-262k`): il nome non porta
    # una capacita'. La risoluzione generica li manderebbe nel mondo TESTO (per
    # il sizing del contesto) e il dep scelto non genererebbe immagini. La
    # richiesta pero' e' di immagine: si sale direttamente nella capacita'
    # richiesta, esattamente come il routing sale di dim quando il contesto
    # non entra. Prima si prova la catena capability (che attraversa free/go/
    # fallback di tutte le righe image del profilo), e solo se non porta nulla
    # si ripiega sul gruppo image_gen.
    dim_or_base = bool(prof) and model.rstrip("/").startswith(f"{gw_state.router.config.proxy_prefix}{prof}")
    if dim_or_base and need:
        dep_cap = _cap_chain_pick(prof, need)
        if dep_cap is not None:
            return dep_cap, prof, scope, None
        _cap_g = _images_group_for_base(prof, need)
        group_or_explicit = _cap_g if _cap_g else None
    else:
        group_or_explicit = gw_state.router.resolve_group_for_request(model, [], session_id, need, profile=prof)
    if group_or_explicit is None:
        # Due cause diverse, due messaggi diversi:
        #  a) il MODELLO ESPLICITO esiste ma non dichiara la capacita' ->
        #     nomino modello e capacita' mancante, l'agente sceglie da solo;
        #  b) il PROFILO non ha deployment con quella capacita' -> il rimando
        #     alla configurazione e' corretto.
        _missing = gw_state.router._missing_media_caps(model, need)
        if _missing:
            return (
                None,
                prof,
                scope,
                JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": (
                                f"il modello '{model}' non supporta: "
                                f"{', '.join(_missing)}. Usa un modello "
                                f"con capacita' {'+'.join(_missing)}."
                            ),
                            "type": "invalid_request_error",
                            "code": "model_capability_unsupported",
                            "missing_capabilities": _missing,
                        }
                    },
                ),
            )
        return (
            None,
            prof,
            scope,
            JSONResponse(
                status_code=400 if need else 404,
                content={
                    "error": {
                        "message": (
                            f"nessun deployment dichiara "
                            f"{'+'.join(sorted(need)) or 'image_gen'}: configura "
                            "capability_routing.model_capabilities in gateway.yaml"
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
    if dep is None and prof:
        dep = gw_state.router.fallback_after(prof, None, need, out_tokens=refill_out_budget(payload, gw_state.router.policy))
    if dep is None:
        return (
            None,
            prof,
            scope,
            JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": (
                            f"nessun deployment disponibile che dichiari {'+'.join(sorted(need)) or 'image_gen'}"
                        ),
                        "type": "invalid_request_error",
                    }
                },
            ),
        )
    return dep, prof, scope, None


# ------------------------------------------------ chat -> images (adattamento)
# Un client puo' chiedere un'immagine con una CHAT (modalities:["image"]).
# Scrocco-llm e' client-agnostico: se il deployment scelto e' IMAGE-NATIVE
# (image_via="images", es. gpt-image-*) la chat viene adattata in una chiamata
# /images/generations (o /images/edits se il client ha mandato reference) e la
# risposta nativa viene riconvertita in un oggetto chat.completion. Viceversa,
# un deployment CHAT-ONLY (image_via="chat", es. gemini-image) resta servito dal
# motore chat. Con image_via="both" si prova la strada naturale (chat) e si
# adatta sull'errore "only supported on /v1/images" se il provider lo segnala.
async def _chat_via_images(
    request: Request,
    *,
    dep: dict,
    payload: dict,
    refs: list[str],
    session_id: str | None,
    raw_model: str,
    profile: str | None,
    model: str,
) -> dict | None:
    """Chiamata nativa /images/* per un deployment image-native; ritorna il
    body chat.completion con le immagini dentro `choices[0].message.images`
    (None se l'upstream non ha restituito immagini)."""

    endpoint = "edits" if refs else "generations"
    body = images_payload_from_chat(payload, refs=refs)
    data = await gw_state.forwarder.call_images(
        dep,
        body,
        profile=profile or "",
        client_ip=_client_ip(request),
        session=_opencode_session(request) or session_id,
        attribution=_client_attribution(request),
        endpoint=endpoint,
        refs=refs,
    )
    return images_response_to_chat(data, model)


def _profile_of_request(model: str, auth_profile: str | None) -> str | None:
    """Profililo del modello richiesto, anche se il nome porta un suffisso.

    `profile_of_base` riconosce solo il nome NUDO (`scrocco-llm-fissone`), ma
    un agente puo' chiedere una dim (`scrocco-llm-fissone-200k`) o il bucket
    `-go`: il profilo va allora tolto dal suffisso, come fa il routing. Senza
    questo la richiesta di immagine su un nome con suffisso non trovava la
    catena capability e rispondeva "nessun deployment dichiara image_gen"."""

    if auth_profile:
        return auth_profile
    for cand in (model.split("__")[0], model):
        p = gw_state.config.profile_of_base(cand)
        if p:
            return p
    # ultima risorsa: il nome richiesto puo' gia' essere un suffissato ->
    # prova a toglierli uno a uno (dim, -go, -fallback, -C).
    base = model.split("__")[0]
    if base.startswith(gw_state.config.proxy_prefix):
        for suf in sorted(gw_state.config.known_suffixes(), key=len, reverse=True) + [""]:
            stem = base[: -len(suf)] if suf and base.endswith(suf) else base
            p = gw_state.config.profile_of_base(stem)
            if p:
                return p
    return None


async def _image_chat_intercept(
    request: Request, *, payload: dict, model: str, raw_model: str, auth: AuthResult, session_id: str | None
):
    """Gestisce una richiesta CHAT che vuole immagini in output.

    Ritorna una Response JSON (chat.completion con immagini) se il deployment
    scelto richiede l'adattamento chat->/images; `None` per lasciare proseguire
    il motore chat normale (deployment chat-native o `both` senza certezza)."""

    if not wants_image_output(payload):
        return None
    if not gw_state.router.policy.routing_active():
        return None
    need = frozenset({"image_gen"})
    if payload.get("messages"):
        # se il client ha mandate immagini di input, serve anche image_edit
        if any(
            isinstance(p, dict) and p.get("type") in ("image_url", "input_image")
            for m in payload["messages"]
            if isinstance(m, dict)
            for p in ((m.get("content") or []) if isinstance(m.get("content"), list) else [])
        ):
            need = need | {"image_edit"}
    prompt, refs = chat_prompt_and_refs(payload.get("messages") or [])
    if not prompt.strip():
        return None  # niente prompt: non e' una richiesta immagine
    prof = _profile_of_request(model, auth.profile)
    # Il nome richiesto e' generico o con suffisso DIM? Allora il dep va
    # cercato nella catena CAPABILITY del profilo, che attraversa free, -go e
    # -fallback: lo stesso "camaleontismo" con cui il routing sale di dim
    # quando il contesto non entra. Un modello ESPLICITO di immagine
    # (`gemini31-image`) resta invece sul suo gruppo-alias.
    forced = bool(prof) and model.rstrip("/").startswith(f"{gw_state.router.config.proxy_prefix}{prof}")

    async def _serve(dep: dict, profile: str | None):
        """Serve UNA richiesta immagine sul deployment `dep`; ritorna il body
        chat.completion, oppure solleva UpstreamError per far ruotare."""
        via = dep_image_via(dep)
        if via == "images":
            return await _chat_via_images(
                request,
                dep=dep,
                payload=payload,
                refs=refs,
                session_id=session_id,
                raw_model=raw_model,
                profile=profile,
                model=model,
            )
        # via "chat" (modello chat-only, es. gemini/nano-banana) o "both":
        # si chiama la chat multimodale e, se il provider rimanda che il
        # modello sta solo su /images/*, si adatta al nativo.
        chat_payload = image_chat_payload(payload, raw_model, refs=refs)
        data = await gw_state.forwarder.call(
            dep,
            chat_payload,
            session=_opencode_session(request) or session_id,
            client_ip=_client_ip(request),
            attribution=_client_attribution(request),
        )
        imgs = images_dual(extract_chat_images(data)) if isinstance(data, dict) else []
        if not imgs:
            raise UpstreamError(502, "risposta chat senza immagini")
        return images_response_to_chat({"data": imgs}, model)

    # --- scelta dei candidati, in ordine di rotazione ---
    if forced:
        candidates = [d for d in (_cap_chain_pick_all(prof, need) or [])]
        if not candidates:
            dep, profile, scope, err = _images_pick_dep(auth.profile, model, raw_model, session_id, need, payload)
            if err:
                return err
            candidates = [dep]
    else:
        dep, profile, scope, err = _images_pick_dep(auth.profile, model, raw_model, session_id, need, payload)
        if err:
            return err
        if dep_image_via(dep) == "chat":
            return None  # modello immagine esplicito: motore chat
        candidates = [dep]

    last_err: UpstreamError | None = None
    t_req = time.monotonic()
    tried: set[str] = set()
    for dep in candidates:
        cur = dep["unique"]
        if cur in tried:
            continue
        tried.add(cur)
        custom_key = gw_state.router.resolve_alias_key(raw_model, model)
        d_use = {**dep, "api_key": custom_key} if custom_key else dep
        via = dep_image_via(d_use)
        if via == "chat" and not forced:
            return None
        metrics.inc("nx_images_total", (d_use["group"], "attempt_chat_adapt"))
        gw_state.router.note_start(cur)
        t0 = time.monotonic()
        try:
            out = await _serve(d_use, prof)
            if not out:
                raise UpstreamError(502, "risposta senza immagini")
        except UpstreamError as err_:
            gw_state.router.note_end(cur)
            last_err = err_
            # errore di DEPLOYMENT (quota, endpoint, provider assente...): si
            # segna e si prova il successivo, che e' esattamente la catena
            # capability. Non si risponde al client finche' non e' finita.
            gw_state.router.mark_failed(cur, seconds=err_.retry_after, status=abs(err_.status) if err_.status else None)
            metrics.inc("nx_images_total", (d_use["group"], "chat_adapt_error"))
            continue
        imgs = await _localize_images(request, images_dual(out.get("data") or []))
        out["data"] = imgs
        msg = out["choices"][0]["message"]
        # `images[]` nella forma che i modelli image-as-chat restituiscono:
        # ogni elemento e' {"type":"image_url","image_url":{url,b64_json,...}}.
        msg["images"] = [{"type": "image_url", "image_url": it} for it in imgs]
        if not msg.get("content"):
            msg["content"] = "\n".join(str(it.get("url") or "") for it in imgs if it.get("url")) or None
        out["nx_deployment"] = cur
        out.setdefault("via", "images")
        gw_state.router.note_result(cur, (time.monotonic() - t0) * 1000)
        metrics.inc("nx_images_total", (d_use["group"], "ok_chat_adapt"))
        _emit_summary(
            ses=session_id or "-",
            req=raw_model,
            grp=d_use["group"],
            dep=cur,
            tries=len(tried),
            fb=len(tried) - 1,
            dur_ms=int((time.monotonic() - t_req) * 1000),
            stream=False,
            qc=False,
            wd=None,
            usage=None,
            kind="images",
            via="chat->images",
        )
        return JSONResponse(out)

    # nessun deployment ha prodotto un'immagine


async def _try_native_image_edit(
    request: Request,
    *,
    owner: Request,
    dep: dict,
    payload: dict,
    refs: list[str],
    session_id: str | None,
    raw_model: str,
    profile: str | None,
    kind: str = "images",
) -> JSONResponse | None:
    """Tenta l'EDIT nativo OpenAI `/images/edits` (multipart) sul deployment.

    Ritorna la JSONResponse finale se il provider lo supporta e risponde con
    immagini; `None` se l'endpoint non e' supportato (firma tipica
    "campo/endpoint non riconosciuto" o 404/405/415) cosi' il chiamante puo'
    ritentare via chat multimodale. Errori non-schema (auth/crediti/quota)
    diventano un UpstreamError propagato al loop per la rotazione normale."""

    try:
        data = await gw_state.forwarder.call_images(
            dep,
            payload,
            profile=profile or "",
            client_ip=_client_ip(request),
            session=_opencode_session(request) or session_id,
            attribution=_client_attribution(request),
            endpoint="edits",
            refs=refs,
        )
    except UpstreamError as err:
        detail = err.detail or ""
        status = err.status if err.status is not None else 0
        if image_chat_fallback_signature(err.status, detail) or -status in (404, 405, 415):
            log.info(
                "[images] %s: /images/edits non disponibile (status=%s): ritento via chat",
                dep["unique"],
                -status or "?",
            )
            return None
        raise
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    data["data"] = await _localize_images(request, images_dual(data["data"]))
    data["nx_deployment"] = dep["unique"]
    data.setdefault("via", "images")
    metrics.inc("nx_images_total", (dep["group"], "ok_native_edit"))
    _emit_summary(
        ses=session_id or "-",
        req=raw_model,
        grp=dep["group"],
        dep=dep["unique"],
        tries=1,
        fb=0,
        dur_ms=0,
        stream=False,
        qc=False,
        wd=None,
        usage=None,
        kind=kind,
        via="images_edits",
    )
    gw_state.router.note_result(dep["unique"], 0.0)
    return JSONResponse(data)


async def _images_chat_loop(
    request: Request,
    *,
    payload: dict,
    refs: list[str],
    raw_model: str,
    model: str,
    need: frozenset[str],
    scope: str,
    dep: dict,
    profile: str | None,
    session_id: str | None,
    kind: str = "images",
    endpoint: str = "generations",
):
    """Generazione/editing immagini via chat multimodale, con rotazione.

    Usata sia da /v1/images/edits sia da /v1/images/generations quando il body
    porta immagini di riferimento. Le reference sono troncate per-deployment
    (modelli single-ref: solo la prima) e inviate come parti `image_url`."""

    _sess = _opencode_session(request) or session_id
    _cip = _client_ip(request)
    _attr = _client_attribution(request)
    hard_max = int(getattr(gw_state.router.policy, "image_refs_hard_max", 16) or 16)
    metrics.inc("nx_images_total", (dep["group"], "attempt"))
    log.info(
        "[images] %s -> %s (%s, prompt=%d chars, refs=%d)",
        model,
        dep["unique"],
        endpoint,
        len(str(payload.get("prompt") or "")),
        len(refs),
    )
    tried: set[str] = set()
    # Marker "nativo gia' tentato" SEPARATO da `tried`: non consuma il budget
    # di tentativi del deployment (si prova nativo -> chat sullo stesso dep).
    native_tried: set[str] = set()
    attempts: list[str] = []
    t_req = time.monotonic()
    last_err: UpstreamError | None = None
    while dep is not None and len(tried) < 64:
        cur = dep["unique"]
        _was_dormant = gw_state.router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        gw_state.router.note_start(cur)
        t0 = time.monotonic()
        try:
            declared = gw_state.router.policy.caps_for(dep.get("model", "")) | (dep.get("caps") or frozenset())
            use_refs = truncate_refs(refs, refs_max_for(declared, hard_max))
            # PROVIDER-AGNOSTICO: l'ordine dei tentativi dipende da COME il
            # provider espone i modelli immagine (colonna `image_via`):
            #   "chat"   -> si va DIRETTAMENTE in chat multimodale (il nativo
            #               risponderebbe 400 "not supported on /v1/images"):
            #               si risparmia una chiamata persa e uno strike.
            #   "images" -> si prova il NATIVO per primo (l'endpoint giusto).
            #   "both"   -> nativo prima, poi chat (comportamento storico,
            #               salvagente per i CSV che non dichiarano nulla).
            # In ogni caso, se l'errore ha la firma "endpoint non supportata"
            # si ritenta sull'altra strada: il client resta ignaro.
            chat_payload = image_chat_payload(payload, raw_model, refs=use_refs)
            _via = dep_image_via(dep)
            _native_try = cur not in native_tried
            if _via != "chat" and _native_try:
                native_tried.add(cur)
                _nres = await _try_native_image_edit(
                    request,
                    owner=request,
                    dep=dep,
                    payload=payload,
                    refs=use_refs,
                    session_id=session_id,
                    raw_model=raw_model,
                    profile=profile,
                    kind=kind,
                )
                if _nres is not None:
                    return _nres
            data = await gw_state.forwarder.call(dep, chat_payload, session=_sess, client_ip=_cip, attribution=_attr)
            imgs = (
                await _localize_images(request, images_dual(extract_chat_images(data)))
                if isinstance(data, dict)
                else []
            )
            if not imgs:
                raise UpstreamError(502, "risposta chat senza immagini")
            gw_state.router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                gw_state.router.clear_cooldown(cur)
            out = {"created": int(time.time()), "data": imgs, "via": "chat", "nx_deployment": cur}
            metrics.inc("nx_images_total", (dep["group"], "ok_chat"))
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
                kind=kind,
                via="chat",
            )
            return JSONResponse(out)
        except UpstreamError as err:
            gw_state.router.note_end(cur)
            last_err = err
            detail = err.detail or ""
            status = err.status if err.status is not None else 0
            deployment_side = (
                status > 0
                # 401: la NOSTRA chiave upstream e' rifiutata -> SEMPRE
                # deployment-side (il client si e' gia' autenticato verso il
                # gateway). Sul path chat lo stesso criterio e' gia' presente
                # e testato: forwarder.call_with_fallback ruota su 401 prima
                # di propagarlo, quindi il 401 che ARRIVA qui e' quello a
                # catena esaurita e va comunque consegnato col suo status
                # vero (_actionable_upstream_error: 401 actionable, 403 no).
                # Questi endpoint non usano call_with_fallback -> senza questa
                # riga il 401 finiva al client senza provare il dep successivo.
                or -status in (401, 402, 403, 404, 405, 415, 422)
                or _MODEL_MISSING_RE.search(detail)
                or chat_only_image_error(detail)
                or image_chat_fallback_signature(err.status, detail)
                or (-status == 400 and ("openai_error" in detail or "bad_response_status_code" in detail))
            )
            if not deployment_side:
                metrics.inc("nx_images_total", (dep["group"], "client_error"))
                st = abs(status) if status else 502
                return JSONResponse(
                    status_code=st if st >= 400 else 502,
                    content={"error": {"message": err.detail, "type": "upstream_error"}},
                )
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    report_suppressed("image_helpers._images_chat_loop")
            if _was_dormant:
                gw_state.router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                gw_state.router.mark_failed(cur, seconds=err.retry_after, status=abs(err.status) if err.status else None)
            metrics.inc("nx_images_total", (dep["group"], "retry"))
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
        finally:
            if cur in tried:
                gw_state.router.note_end(cur)
    status = abs(last_err.status) if last_err and last_err.status else 502
    return JSONResponse(
        status_code=status if status >= 400 else 502,
        content={
            "error": {
                "message": (last_err.detail if last_err else "nessun deployment image disponibile"),
                "type": "upstream_error",
            }
        },
    )

