"""POST /v1/chat/completions: dall'ingresso della richiesta alla risposta.

[IT] Pipeline: auth -> canonicalizzazione alias -> stima del contesto ->
gruppo dims/capacita' -> pick adattivo -> forwarder con fallback a catena ->
QC/watchdog -> risposta (stream o JSON), con una riga [summary] per richiesta.
Le decisioni chiave:
  - routing per CONTESTO STIMATO (-32k..-1000k): nei free-tier le finestre
    sono piccole; serve il modello minimo che CI STA, non il migliore
    assoluto (che rifiuterebbe o taglierebbe);
  - gruppi capacita' strutturali (-vision/-tts/...): un fallback di scopo
    non atterra mai su un modello senza la capacita' richiesta;
  - sticky session SOLO dal routing automatico: gli espliciti sono legge;
  - watchdog passivo sullo stream (tier1 vuoto/error, tier2 no-[DONE]):
    non tocca i byte, rileva solo upstream mezzi morti.

Struttura: questo modulo fa ingresso, routing iniziale e il motore
non-stream; lo stream con fallback e' in app/chat_stream.py, la gara di
hedge in app/chat_hedge.py, i media (tetto immagini, ponte STT) in
app/chat_media.py. Log sul logger "nx.main", come quando tutto stava in
app/main.py. Ciclo completo: docs/ARCHITECTURE.md.

[EN] Chat completions endpoint: auth -> alias -> ctx estimate -> capability
group -> adaptive pick -> chained fallback -> QC/watchdog -> response.
Minimum-context routing beats best-model routing on free tiers;
purpose-aware fallback; passive stream watchdog; per-request summary logs.
"""
from __future__ import annotations

import json
import logging
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import autoprobe, metrics, sniff
from . import state as gw_state
from .auth import AuthResult
from .capabilities import count_audio_parts, count_image_parts, required_caps, wants_image_output
from .chat_helpers import (
    _apply_go_refund,
    _client_ip,
    _emit_summary,
    _note_fb_refund,
    _opencode_session,
    _session_id,
    _set_opencode_gate,
    _sniff_headers,
    _strike_hook,
    _usage_of,
)
from .chat_media import _stt_bridge
from .chat_stream import _stream_with_fallback
from .effort import effort_from_request, set_effort
from .forwarder import UpstreamError, _client_attribution
from .http_responses import forbidden as _forbidden
from .http_responses import unauthorized as _unauthorized
from .image_helpers import _image_chat_intercept
from .offload import request_json
from .policy import refill_out_budget
from .qc import annotate_reasoning
from .router import _prompt_chars, estimate_tokens, inject_identity
from .runtime_persistence import _forward_coalesced, _nonstream_hold_redirect
from .stream_verdicts import _actionable_upstream_error, _exhausted, _retry_at_ms
from .suppressed import report_suppressed
from .thought_sig import has_unsigned_tool_calls, reset_request_flags, set_avoid_gemini, set_dummy_fill

log = logging.getLogger("nx.main")

router = APIRouter()




@router.post("/v1/chat/completions")
async def chat_completions(request: Request, response: Response):
    _api_log = logging.getLogger("nx.api")
    try:
        payload = await request_json(request)
    except Exception as exc:
        # FIX: Log error details for debugging; previously blank exception handler
        _api_log.warning("[api] invalid JSON body: %s", exc)
        return JSONResponse(
            status_code=400, content={"error": {"message": "invalid JSON body", "type": "invalid_request_error"}}
        )

    # EFFORT/reasoning: `reasoning_effort` (o alias `effort`) nel body, oppure
    # header `x-effort`. Lo stato vive in una ContextVar legata al task della
    # richiesta: il router lo usa per il bias di intelligence, il forwarder per
    # iniettare/rimuovere reasoning_effort e per l'override di temperatura.
    set_effort(
        effort_from_request(payload, request.headers),
        temp_enabled=gw_state.policy.enable_effort_temperature_override,
        temp_overrides=gw_state.policy.effort_temperature_overrides,
    )

    raw_model = payload.get("model") or ""
    messages = payload.get("messages") or []
    stream = bool(payload.get("stream"))
    # id breve per correlare input/output nel file di debug-sniff
    import uuid as _uuid

    _rid = _uuid.uuid4().hex[:12]

    # I6: azzera i flag per-request prima di QUALSIASI return anticipato
    # (401/403/400/413): un percorso uscito senza reset non deve inquinare la
    # richiesta successiva che riusa lo stesso contesto ContextVar.
    reset_request_flags()

    # Gemini 3: se la history contiene tool_call prive di thought_signature
    # (conversazione passata per modelli non-Google) Gemini risponderebbe 400
    # INVALID_ARGUMENT sul replay. Con `thought_sig_dummy_fill` attivo il
    # forwarder inietta la firma DUMMY ufficiale sui tool_call non firmati del
    # turno corrente: Gemini resta quindi eleggibile come qualunque provider.
    # Solo con la dummy-fill DISATTIVATA lo escludiamo A MONTE dalla selezione
    # (come un cap mancante, senza salti o tentativi finti).
    set_avoid_gemini(has_unsigned_tool_calls(messages) and not gw_state.policy.thought_sig_dummy_fill)
    set_dummy_fill(gw_state.policy.thought_sig_dummy_fill, gw_state.policy.thought_sig_dummy_value)

    # --- normalizzazione del nome richiesto:
    #     1) prefisso STORICO -> prefisso corrente (compatibilità client)
    #     2) alias (gateway.yaml) -> nome canonico
    model = gw_state.policy.canonicalize(raw_model)

    # --- auth ---
    auth: AuthResult = gw_state.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)

    # --- autorizzazione modello (whitelist tre livelli, sul nome canonico) ---
    if not gw_state.authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    # --- routing ---
    _set_opencode_gate(request)
    session_id = _session_id(request, payload)

    # --- STT-BRIDGE: audio in chat -> testo (PRIMA di ogni altra cosa) ---
    # Va fatto PRIMA del capability detection: con STT sempre l'audio non
    # deve piu' chiedere la capacita' `audio` al routing, altrimenti finirebbe
    # nel gruppo -audio (73 deployment che ho verificato non trascrivere
    # l'audio in modo affidabile) invece che nel pool testo normale. Dopo
    # questa riga `messages` non ha piu' audio.
    if count_audio_parts(payload.get("messages") or []):
        payload = await _stt_bridge(request, payload, auth, session_id, model, raw_model)
        messages = payload.get("messages") or []

    # --- capability detection ---
    # capacità richieste dal payload; OGNI chat produce testo -> "text" è sempre
    # implicita: i modelli solo-tts/stt/image_gen escono dal pool chat automatico
    # (gli espliciti passano comunque; kill-switch: capability_routing.enabled=false)
    if gw_state.router.policy.routing_active():
        need = required_caps(payload) | {"text"}
    else:
        need = frozenset()

    # SESSION-DEP GUARD: la sessione corrente dev'essere nota GIA' durante
    # initial_pick/pick_deployment (guardia anti-usurpazione cross-sessione),
    # non solo dopo il pick come in passato.
    from .router import set_current_session

    set_current_session(session_id)
    # SESSION-DEP GUARD: la sessione ha USATO il servizio -> rinnova
    # l'ownership di tutti i suoi deployment (restano suoi finché e' viva;
    # 15 min di silenzio e l'intero set torna libero).
    gw_state.router.note_session_activity(session_id)
    # RATE per-sessione (SOLO chat): alimenta warm_ready_min adattivo.
    gw_state.router.note_session_request(session_id)

    # --- ADATTAMENTO chat -> /images/* ---
    # Il client ha chiesto immagini in output (modalities:["image"]): se il
    # deployment scelto e' image-native lo si serve con la macchina immagini
    # (body OpenAI images adattato dal body chat, risposta riconvertita in
    # chat.completion). Ritorna None per i modelli chat-native, che seguono il
    # motore chat normale qui sotto.
    if wants_image_output(payload):
        _img_resp = await _image_chat_intercept(
            request, payload=payload, model=model, raw_model=raw_model, auth=auth, session_id=session_id
        )
        if _img_resp is not None:
            return _img_resp
    _sniff_headers(
        request,
        logger=_api_log,
        body_size=len(request._body) if hasattr(request, "_body") else 0,
        session_id=session_id,
    )
    _img_est = getattr(gw_state.router.policy, "image_token_estimate", 0) or 0
    # Base di stima = payload con la sola histnorm (preview deterministica,
    # PRIMA di ctxcompact): la STESSA base su cui si apprende e si applica il
    # rapporto per-sessione, cosi' i char contati coincidono tra hook usage e
    # routing.
    _est_msgs = messages
    try:
        from .histnorm import hist_config_from_policy, normalize_messages

        _est_msgs, _ = normalize_messages(
            messages, hist_config_from_policy(gw_state.router.policy), tail_floor=gw_state.router.ctx_boundary_floor(session_id)
        )
    except Exception:  # noqa: BLE001
        _est_msgs = messages
    _est_chars_pre = _prompt_chars(_est_msgs, payload.get("tools"))
    _cpt = gw_state.router.session_chars_per_token(session_id)
    if _cpt:
        # Dal 2o turno, DUE stime dalla stessa base pre-compressione:
        #  ctx_dim = token POST-compressione previsti -> scelta della dim;
        #  ctx_est = token PRE-compressione -> sicurezza (ctxcompact/overflow).
        ctx_dim, _ = gw_state.router.estimate_for_session(
            session_id, _est_msgs, gw_state.router.policy.estimate_divisor, _img_est, tools=payload.get("tools"), pre=True
        )
        ctx_est, _ = gw_state.router.estimate_for_session(
            session_id, _est_msgs, gw_state.router.policy.estimate_divisor, _img_est, tools=payload.get("tools")
        )
        metrics.inc("nx_sess_est_used_total")
        log.info(
            "[estimate] sess cpt_pre=%.2f cpt_post=%.2f -> ctx_dim≈%d ctx_pre≈%d (chars_pre=%d, +%.0f%%)",
            gw_state.router.session_chars_per_token(session_id, pre=True),
            _cpt,
            ctx_dim,
            ctx_est,
            _est_chars_pre,
            (float(getattr(gw_state.router.policy, "session_estimate_margin", 1.05)) - 1.0) * 100.0,
        )
    else:
        # 1o turno della sessione: stima euristica basata sui soli char.
        ctx_est = estimate_tokens(messages, gw_state.router.policy.estimate_divisor, _img_est, tools=payload.get("tools"))
        ctx_dim = ctx_est
        metrics.inc("nx_sess_est_fallback_total")

    group_or_explicit = gw_state.router.resolve_group_for_request(
        model, messages, session_id, need, ctx_dim, profile=auth.profile
    )
    if group_or_explicit is None:
        if need:
            for c in sorted(need):
                metrics.inc("nx_caps_unroutable_total", (c,))
            # Rifiuto ESPLICITO: il client ha chiesto un deployment preciso
            # (unique) che non dichiara una capacita' media necessaria. Il
            # messaggio generico ("configura model_capabilities in
            # gateway.yaml") sarebbe fuorviante: il deployment esiste, e' la
            # sua scheda a non avere la capacita'. Meglio un messaggio che
            # nomini modello e capacita' mancante: l'agente puo' scegliere da
            # solo al turno dopo invece di fare un giro di scoperta.
            _missing = gw_state.router._missing_media_caps(model, need)
            if _missing:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": (
                                f"il modello '{model}' non supporta: "
                                f"{', '.join(_missing)}. La richiesta contiene "
                                f"media che quel modello non puo' elaborare; usa "
                                f"un modello con capacita' "
                                f"{'+'.join(_missing)}."
                            ),
                            "type": "invalid_request_error",
                            "code": "model_capability_unsupported",
                            "missing_capabilities": _missing,
                        }
                    },
                )
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": f"nessun deployment dichiara le capacità richieste: {sorted(need)}. "
                        f"Configura capability_routing.model_capabilities in gateway.yaml",
                        "type": "invalid_request_error",
                    }
                },
            )
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"model '{model}' not managed by {gw_state.policy.service_name}",
                    "type": "invalid_request_error",
                }
            },
        )

    # RIMBORSO LATENZA: conta il turno della sessione (all'atterraggio) e, se
    # un "lento" ha regalato turni -go, atterra sul bucket -go come se il
    # client avesse chiamato scrocco-llm-<profilo>-go. Solo richieste di testo
    # su un dim (-Nk): media/cap, -go/-fallback e i unique espliciti restano
    # invariati. Ladder/selezione a valle sono INVARIATI.
    _turn_go = False
    if session_id:
        try:
            _turn_go = gw_state.router.note_session_turn(session_id)
        except Exception:  # noqa: BLE001
            _turn_go = False
    group_or_explicit, _refund_go = _apply_go_refund(gw_state.router, group_or_explicit, auth.profile, _turn_go, session_id)

    explicit_req = gw_state.router.is_explicit(model)
    # Se il client chiama esplicitamente un gruppo diverso (es. -200k -> -1000k
    # o -go), rilascia lo sticky per-deployment cosi' la richiesta esplicita
    # atterra sul nuovo gruppo/key scelta dal routing, non resta incollata al
    # vecchio deployment dello sticky precedente.
    if (explicit_req or _refund_go) and session_id:
        cur = gw_state.router.dep_sticky_get(session_id)
        sd = gw_state.router.config.deployment_by_unique(cur) if cur else None
        # Rilascia dep-sticky SOLO se il gruppo è cambiato o non c'è sticky:
        # se la richiesta esplicita punta allo stesso gruppo dello sticky,
        # lo preserviamo per la cache-preserving (misma key per sessione).
        if sd is None or sd.get("group") != group_or_explicit:
            gw_state.router.dep_sticky_release(session_id)
        # Per richieste esplicite su un dim (-Nk): riàncora lo sticky di
        # gruppo cosi' le successive NON-esplicite continuano nel contesto
        # scelto dall'utente (crescita cache-preserving). Per -go/-fallback/
        # unique: libera lo sticky di gruppo, altrimenti il traffico
        # automatico verrebbe parcheggiato in un bucket a pagamento.
        if re.search(r"-\d+k$", group_or_explicit):
            gw_state.router.sticky_set(session_id, group_or_explicit)
        else:
            gw_state.router.sticky_release(session_id)

    dep = gw_state.router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        # F31 fail-fast ingresso: se il ctx non entra nel gruppo (max_input di
        # tutti i dep < ctx) si compatta FORZANDO il gate min_saved e si
        # ricalcola; se resta sopra si risponde 400 sintetico senza toccare
        # l'upstream. Evita 2-3 tentativi di catena e 10-15s di prefill inutile.
        _max_grp = 0
        try:
            _max_grp = max(
                (int(d.get("max_input_tokens") or 0) for d in (gw_state.router.config.groups.get(group_or_explicit) or [])),
                default=0,
            )
        except Exception:
            _max_grp = 0
        _zen_first = False
        try:
            _zen_first = gw_state.router._zen_first_active()
        except Exception:  # noqa: BLE001
            _zen_first = False
        # Zen-first: si tiene conto anche della RISERVA DI OUTPUT (il picker
        # richiede ctx+out <= max_input) e si compatta per rientrare nel tier
        # zen. Per i non-nativi resta lo storico 5% di margine sull'input.
        try:
            _out_res = int(refill_out_budget(payload, gw_state.router.policy) or 0)
        except Exception:  # noqa: BLE001
            _out_res = 0
        if _zen_first:
            _budget = max(1, _max_grp - _out_res) if _max_grp else 0
            _trig = bool(ctx_est and ctx_est > _budget)
        else:
            _budget = _max_grp
            _trig = bool(ctx_est and ctx_est > int(_max_grp * 1.05))
        if _max_grp > 0 and _trig:
            _up = None
            _handled = False
            if _zen_first:
                # ZEN-FIRST (client opencode nativo): PRIMA di salire a una
                # dim senza zen si prova a COMPATTARE per restare nel tier
                # free; solo se il payload resta troppo grande si sale di dim.
                from .ctxcompact import compact_tool_outputs, ctxcompact_config_from_policy

                _ccf = ctxcompact_config_from_policy(gw_state.router.policy)
                _ccf.min_saved_tokens = 0
                _img = getattr(gw_state.router.policy, "image_token_estimate", 0) or 0
                _forced, _frep = compact_tool_outputs(
                    payload.get("messages") or [],
                    _ccf,
                    max_in=_budget,
                    estimator=lambda ms: gw_state.router.estimate_for_session(
                        session_id, ms, gw_state.router.policy.estimate_divisor, _img
                    )[0],
                )
                if _frep.get("changed"):
                    payload["messages"] = _forced
                    metrics.inc("nx_ctx_compacted_forced")
                    log.info(
                        "[ctx-overflow] compattazione forzata (zen) per %s: %s",
                        group_or_explicit,
                        {k: _frep.get(k) for k in ("stubbed", "deduped", "args_trimmed", "saved_chars")},
                    )
                ctx_est = gw_state.router.estimate_for_session(
                    session_id,
                    payload.get("messages") or [],
                    gw_state.router.policy.estimate_divisor,
                    _img,
                    tools=payload.get("tools"),
                )[0]
                if ctx_est <= _budget:
                    metrics.inc("nx_zen_dim_stay")
                    log.info(
                        "[zen-dim] compattato: ctx≈%d entra in %s (zen, budget=%d out=%d): resto nel tier free",
                        ctx_est,
                        group_or_explicit,
                        _budget,
                        _out_res,
                    )
                    _handled = True
            if not _handled:
                _climb = (ctx_est > _budget) if _zen_first else (ctx_est > _max_grp)
                if _climb:
                    # SALITA DI DIM (regola dell'utente): la dim PIU' PICCOLA
                    # che contiene il payload (+ riserva output per lo zen).
                    _up = gw_state.router.climb_dim_group(group_or_explicit, ctx_est + (_out_res if _zen_first else 0))
                if _up:
                    log.info(
                        "[dim] ctx≈%d non entra in %s (max %d): salgo a %s", ctx_est, group_or_explicit, _max_grp, _up
                    )
                    group_or_explicit = _up
                    if explicit_req and session_id:
                        gw_state.router.sticky_set(session_id, _up)
                else:
                    from .ctxcompact import compact_tool_outputs, ctxcompact_config_from_policy

                    # NB: CtxCompactConfig NON e' un dataclass -> niente
                    # `dataclasses.replace` (TypeError a runtime: era il bug di
                    # produzione). E' un'istanza fresca per chiamata: si muta il campo.
                    _ccf = ctxcompact_config_from_policy(gw_state.router.policy)
                    _ccf.min_saved_tokens = 0
                    _img = getattr(gw_state.router.policy, "image_token_estimate", 0) or 0
                    _forced, _frep = compact_tool_outputs(
                        payload.get("messages") or [],
                        _ccf,
                        max_in=_max_grp,
                        estimator=lambda ms: gw_state.router.estimate_for_session(
                            session_id, ms, gw_state.router.policy.estimate_divisor, _img
                        )[0],
                    )
                    if _frep.get("changed"):
                        payload["messages"] = _forced
                        metrics.inc("nx_ctx_compacted_forced")
                        log.info(
                            "[ctx-overflow] compattazione forzata per %s: %s",
                            group_or_explicit,
                            {k: _frep.get(k) for k in ("stubbed", "deduped", "args_trimmed", "saved_chars")},
                        )
                    ctx_est = gw_state.router.estimate_for_session(
                        session_id,
                        payload.get("messages") or [],
                        gw_state.router.policy.estimate_divisor,
                        _img,
                        tools=payload.get("tools"),
                    )[0]
                    if ctx_est > int(_max_grp * 1.05):
                        metrics.inc("nx_ctx_overflow_total", (group_or_explicit,))
                        return JSONResponse(
                            status_code=400,
                            content={
                                "error": {
                                    "code": "context_length_exceeded",
                                    "message": "ctx ~%d oltre il max_input %d del "
                                    "gruppo %s, anche dopo la "
                                    "compattazione" % (ctx_est, _max_grp, group_or_explicit),
                                    "type": "invalid_request_error",
                                }
                            },
                        )
        # ESPLICITO: nessun filtro (la lettera della richiesta vince); il retry
        # ruota solo nel gruppo. BASE: need+ctx con catena del mondo scelta da
        # initial_pick (dims per testo, cap-chain per -C).
        # WARM POOL: attivo sul routing automatico e sui dim espliciti (m0204:
        # il client vuole ANCHE la cache calda, ma con floor della dim
        # richiesta); NON su -go/-fallback (escalation deliberata a pagamento).
        # F27: niente regex sul nome (fragile: "llama-70b" non matcha,
        # "qwen-32k" matcha per caso). Verita' canonica = config.group_caps:
        # se il gruppo NON e' una capacita' ed e' un bucket -dim/apice, il warm
        # resta valido anche esplicito.
        _grp_is_dim = gw_state.router.config.group_caps.get(group_or_explicit) is None and not gw_state.router._is_renewal_bucket(
            group_or_explicit
        )
        _warm = (not explicit_req) or _grp_is_dim
        # CACHE PAGATA: SOLO su richiesta esplicita a -go/-fallback testo:
        # riusa la stessa chiave della sessione (KV-cache calda) anche se sta
        # su un tier di rinnovo peggiore; al 429 il holder si esclude da solo
        # e la rotazione prosegue nell'ordine normale (crediti "sommati" un
        # account alla volta). Auto-routing ed escalation interne non lo usano.
        _go_suf = gw_state.router.config.go_suffix or "-go"
        _fb_suf = gw_state.router.config.fallback_suffix or "-fallback"
        _paid_holder = explicit_req and (group_or_explicit.endswith(_go_suf) or group_or_explicit.endswith(_fb_suf))
        # PARITA' stream/non-stream sotto HOLD: se questa richiesta non-stream
        # sara' servita dal MOTORE STREAM (redirect hold, vedi _redirect sotto),
        # anche il pick iniziale deve ordinare il warm come lo stream
        # (prefer_fast=False). Qui `dep` non esiste ancora: l'intento si ricava
        # dalla policy (il flag per-deployment resta gestito dal ramo a valle).
        _pre_redirect = _nonstream_hold_redirect(stream, None, gw_state.router.policy.qc_json, gw_state.router.policy)
        dep = gw_state.router.initial_pick(
            auth.profile,
            group_or_explicit,
            None if explicit_req else need,
            ctx_dim,
            session_id=session_id,
            warm=_warm,
            prefer_holder=_paid_holder,
            prefer_fast=(not stream) and not _pre_redirect,
            out_tokens=refill_out_budget(payload, gw_state.router.policy),
        )
    if dep is None:
        # F31: se il motivo e' l'overflow (tutti i dep del gruppo hanno
        # max_input < ctx) NON e' un disservizio ma un errore del client:
        # 400 context_length_exceeded invece del 503 "nessun deployment".
        try:
            _mx = max(
                (int(d.get("max_input_tokens") or 0) for d in (gw_state.router.config.groups.get(group_or_explicit) or [])),
                default=0,
            )
        except Exception:
            _mx = 0
        if _mx > 0 and ctx_est and ctx_est > _mx:
            metrics.inc("nx_ctx_overflow_total", (group_or_explicit,))
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "ctx ~%d oltre il max_input %d del gruppo %s" % (ctx_est, _mx, group_or_explicit),
                        "type": "invalid_request_error",
                    }
                },
            )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "nessun deployment disponibile"
                    + (" per le capacità richieste" if not explicit_req else ""),
                    "type": "server_error",
                }
            },
        )

    # override chiave: alias GENERICO con chiave custom (policy.alias_keys):
    # sostituisce SOLO dep["api_key"]. Vale solo per il PRIMO tentativo —
    # i fallback successivi tornano al pool normale del profilo, così una
    # chiave rotta non blocca mai il servizio.
    custom_key = gw_state.router.resolve_alias_key(raw_model, model)
    if custom_key:
        dep = {**dep, "api_key": custom_key}

    profile = auth.profile or gw_state.config.profile_of_base(model.split("__")[0]) or gw_state.config.profile_of_base(model)

    if model != raw_model:
        log.info("[route] alias %r -> %r", raw_model, model)
    metrics.inc("nx_requests_total", (raw_model[:60], str(bool(payload.get("stream")))))
    metrics.inc("nx_group_total", (dep["group"],))
    for c in sorted(need):
        metrics.inc("nx_caps_requests_total", (c,))
    log.info(
        "[route] %s -> %s (ctx≈%d tok%s, need=%s, session=%s, stream=%s)",
        model,
        dep["group"],
        ctx_est,
        f"+{count_image_parts(messages)}img" if count_image_parts(messages) else "",
        sorted(need) if need else "-",
        session_id or "anonima",
        bool(payload.get("stream")),
    )

    # autoprobe cooldown (fire-and-forget: non entra nella risposta)
    autoprobe.maybe_spawn(gw_state.router, gw_state.forwarder, profile)

    # sticky session SOLO dal routing automatico (nome base): le richieste
    # esplicite (-Nk/-go/-fallback/__univoco) non leggono né scrivono sticky.
    # I bucket renewal (-go/-fallback) NON vengono mai salvati: -go si
    # raggiunge solo esplicitamente o a fine scala (free -> zen -> -go).
    if session_id and not gw_state.router.is_explicit(model) and not gw_state.router._is_renewal_bucket(group_or_explicit):
        gw_state.router.sticky_set(session_id, group_or_explicit)

    # iniezione identità + modello univoco nel payload upstream
    inject_identity(payload, dep, router=gw_state.router)

    # ---- L1/L2 preprocessing (cache-safe: solo la coda) ----
    from .histnorm import hist_config_from_policy, normalize_messages
    from .sampling import apply_sampling_defaults, sampling_config_from_policy
    from .schemaout import maybe_inject_response_format, schemaout_config_from_policy

    _hn = hist_config_from_policy(gw_state.router.policy)
    _sm = sampling_config_from_policy(gw_state.router.policy)
    _so = schemaout_config_from_policy(gw_state.router.policy)
    _orig_msgs = payload.get("messages")  # pre-normalizzazione
    _orig_for_retry = None
    if _hn.enabled:
        _nm, _nr = normalize_messages(payload.get("messages"), _hn, tail_floor=gw_state.router.ctx_boundary_floor(session_id))
        if _nr.get("changed"):
            payload["messages"] = _nm
            metrics.inc("nx_histnorm_total", ("changed",))
            log.info(
                "[histnorm] coda normalizzata: %s",
                {k: _nr.get(k) for k in ("shown_orphan_tool", "dangling_tool_calls", "empty_assistant", "dup_system")},
            )
        # Il taglio del reasoning e' un'ottimizzazione di TOKEN: la history
        # originale serve (a) ai deployment con `thinking_replay` per rimettere
        # il reasoning VERO prima dell'invio, (b) al retry una-tantum dopo un
        # errore "oscuro". Teniamo il riferimento sempre che esista.
        if _orig_msgs:
            _orig_for_retry = _orig_msgs
    # ---- cache-aware: detentore sessione + troncamento contesto ----
    from .ctxcompact import compact_tool_outputs, ctxcompact_config_from_policy, frontier_boundary, should_compact

    _cc = ctxcompact_config_from_policy(gw_state.router.policy)
    _holder = gw_state.router.session_holder(session_id)
    _max_in = int(dep.get("max_input_tokens") or 0)
    _same_family = False
    if _holder:
        _hd = gw_state.router.config.deployment_by_unique(_holder)
        if _hd and _hd.get("family") and _hd.get("family") == dep.get("family"):
            _same_family = True
    # F14: correzione per-deployment appresa dal VERO prompt_tokens upstream
    # (tokenizer diverso da chars/4): la decisione di compattazione non deve
    # lavorare su stime sballate.
    try:
        _corr = gw_state.router.estimate_correction(dep.get("unique", ""))
        if _corr != 1.0:
            _ctx_corr = max(1, int(ctx_est * _corr))
        else:
            _ctx_corr = ctx_est
    except Exception:
        _ctx_corr = ctx_est
    # H2: divisore chars/token CALIBRATO (F14) da usare per il budget della
    # frontiera: il 4 fisso sottostima i token sui tokenizer non-OpenAI.
    try:
        _div_eff = gw_state.router.effective_divisor(dep.get("unique", ""))
    except Exception:
        _div_eff = float(getattr(gw_state.router.policy, "estimate_divisor", 4) or 4)
    _dec = should_compact(
        _cc,
        _ctx_corr,
        _max_in,
        _holder,
        dep.get("unique"),
        bool(session_id and gw_state.router.is_session_compact(session_id)),
        same_family=_same_family,
        reasoning=bool(dep.get("effort_capable")),
    )
    _do_compact = _dec["compact"]
    if _do_compact and session_id:
        gw_state.router.mark_session_compact(session_id)
    _ctx_saved_hdr = 0
    if _do_compact:
        _cmsgs, _crep = compact_tool_outputs(
            payload.get("messages"),
            _cc,
            max_in=_max_in,
            estimator=lambda ms: gw_state.router.estimate_for_session(
                session_id,
                ms,
                _div_eff,
                getattr(gw_state.router.policy, "image_token_estimate", 0) or 0,
                unique=dep.get("unique"),
            )[0],
            boundary_floor=gw_state.router.ctx_boundary_floor(session_id),
            divisor=_div_eff,
        )
        if _crep.get("changed"):
            payload["messages"] = _cmsgs
            gw_state.router.note_compact_boundary(session_id, _crep.get("boundary"))
            metrics.inc("nx_ctxcompact_total", ("stubbed",))
            for _tn, _tcnt in (_crep.get("tools") or {}).items():
                metrics.inc("nx_ctxcompact_tool_total", (_tn,))
            _ctx_saved_hdr = int(_crep.get("saved_tokens_est") or 0)
            log.info(
                "[ctxcompact] ses=%s stubbed=%d dedup=%d args=%d saved≈%dtok reason=%s",
                session_id,
                _crep["stubbed"],
                _crep.get("deduped", 0),
                _crep.get("args_trimmed", 0),
                _crep["saved_tokens_est"],
                _dec["reason"],
            )
    # AUDIT DEL PREFISSO (F4): prima di spendere la cache a monte, impronta
    # il prefisso [1:frontier] e dice perche' e' cambiato (se e' cambiato).
    # 'identity' = colpa nostra (system/inject), 'prefix' = ctxcompact,
    # histnorm o riscrittura del client. Osservabilita': nessun effetto sulla
    # scelta del deployment, ma F10 lo usa come breadcrumb sui 503.
    _aud = None
    if getattr(gw_state.router.policy, "cache_prefix_audit", True) and session_id:
        _bnd = _crep.get("boundary") if (_do_compact and _crep.get("changed")) else None
        if _bnd is None:
            try:
                _bnd = frontier_boundary(
                    payload.get("messages") or [], _cc, _max_in, gw_state.router.ctx_boundary_floor(session_id), _div_eff
                )
            except Exception:  # noqa: BLE001
                _bnd = None
        _aud = gw_state.router.audit_prefix(session_id, payload.get("messages") or [], _bnd)
        metrics.inc("nx_cache_audit_total", (_aud,))
        if _aud in ("identity", "prefix"):
            log.info(
                "[cache-audit] ses=%s prefisso MUTATO (%s, boundary=%s): cache upstream riparte da li'",
                session_id,
                _aud,
                _bnd,
            )
    log.info(
        "[cache] ses=%s holder=%s family=%s same_fam=%s compact=%s cold=%s reason=%s ctx≈%d max_in=%d",
        session_id,
        _holder or "-",
        dep.get("family") or "-",
        _same_family,
        _do_compact,
        _dec["cold"],
        _dec["reason"] or "-",
        ctx_est,
        _max_in,
    )
    if stream and _sm.enabled:
        _ap = apply_sampling_defaults(payload, dep, _sm)
        if _ap:
            log.debug("[sampling] %s: default %s", dep.get("unique"), _ap)
        if maybe_inject_response_format(payload, dep, _so):
            metrics.inc("nx_resp_format_injected_total", (dep.get("unique"),))
    t_req = time.monotonic()
    # sessione OpenCode: passthrough se il client la invia (x-opencode-session
    # oppure x-session-affinity/x-session-id nativi), altrimenti fallback alla
    # sessione del body; se manca del tutto l'header viene calcolato nel
    # forwarder (hash api_key+client_ip)
    _sess = _opencode_session(request) or session_id
    _cip = _client_ip(request)
    # attribuzione app OpenRouter: i modelli :free sono serviti SOLO agli
    # "agentic harness" riconosciuti; se il CLIENT si attribuisce
    # (HTTP-Referer/X-Title), quel valore vince sul default di policy.
    _attr = _client_attribution(request)

    if stream:
        _sniffer = None
        if sniff.enabled(gw_state.router.policy):
            _sniffer = sniff.begin(
                _rid,
                {
                    "model": raw_model,
                    "canonical": model,
                    "profile": profile,
                    "session": _sess or "-",
                    "client_ip": _cip,
                    "need": sorted(need),
                    "group": group_or_explicit,
                    "dep": dep.get("unique"),
                    "stream": True,
                },
                payload,
            )
        _sresp = await _stream_with_fallback(
            profile,
            dep,
            payload,
            need,
            hook=_strike_hook(explicit_req, need),
            scope="group" if explicit_req else "chain",
            ctx=ctx_dim,
            cold=bool(_dec.get("cold")),
            prefix_reason=(_aud if _aud in ("identity", "prefix") else None),
            ses=session_id,
            req=raw_model,
            est_chars=_est_chars_pre,
            session=_sess,
            client_ip=_cip,
            request=request,
            attribution=_attr,
            requested_group=group_or_explicit,
            orig_messages=_orig_for_retry,
            sniffer=_sniffer,
        )
        if _ctx_saved_hdr:
            _sresp.headers["X-Ctxcompact-Saved"] = str(_ctx_saved_hdr)
        return _sresp

    qc_pol = gw_state.router.policy.qc_json
    attempts_box: list[str] = []
    # HOLD-UNTIL-FINISH + richiesta non-stream: esegui il MOTORE STREAM (sotto
    # hold bufferizza l'intera risposta) e restituisci non-stream. Un solo
    # motore per entrambi -> comportamento identico. Kill-switch:
    # policy.nonstream_hold_redirect.
    _redirect = _nonstream_hold_redirect(stream, dep, qc_pol, gw_state.router.policy)

    async def _redirect_once():
        from .protocols import sse_to_chat_obj

        _sp = dict(payload)
        _sp["stream"] = True
        try:
            if _sm.enabled:
                apply_sampling_defaults(_sp, dep, _sm)
            maybe_inject_response_format(_sp, dep, _so)
        except Exception:  # noqa: BLE001
            report_suppressed("main.chat_completions._redirect_once")
        _meta: dict = {}
        _sresp = await _stream_with_fallback(
            profile,
            dep,
            _sp,
            need,
            hook=_strike_hook(explicit_req, need),
            scope="group" if explicit_req else "chain",
            ctx=ctx_dim,
            cold=bool(_dec.get("cold")),
            prefix_reason=(_aud if _aud in ("identity", "prefix") else None),
            ses=session_id,
            req=raw_model,
            est_chars=_est_chars_pre,
            session=_sess,
            client_ip=_cip,
            request=request,
            attribution=_attr,
            requested_group=group_or_explicit,
            orig_messages=_orig_for_retry,
            sniffer=None,
            result_box=_meta,
            client_stream=False,
        )
        if isinstance(_sresp, StreamingResponse):
            _chunks = [c async for c in _sresp.body_iterator]
            attempts_box.extend(_meta.get("attempts") or [])
            try:
                _data = sse_to_chat_obj(_chunks)
            except ValueError as _ex:
                raise UpstreamError(503, "stream non assemblable: %s" % _ex, final=True) from _ex
            return (_data, _meta.get("dep") or dep)
        # errore PRE-BYTE: il motore stream ritorna gia' un JSONResponse
        # (503 retryable o status vero). Ricostruiamo l'errore per riusare
        # l'handler non-stream (status/trail/epiloghi identici).
        attempts_box.extend(_meta.get("attempts") or [])
        _st = int(getattr(_sresp, "status_code", 503) or 503)
        try:
            _detail = json.loads(bytes(getattr(_sresp, "body", b"") or b"")).get("error", {}).get("message")
        except Exception:  # noqa: BLE001
            _detail = None
        # Il trail (quali hop e con quale classe) arriva da _ret() nel
        # result_box: senza questo l'handler non-stream ricostruiva un
        # UpstreamError SENZA trail e il 503 finale usciva con attempts=[].
        _err = UpstreamError(_st, _detail or "upstream error (redirect stream)")
        _err.trail = _meta.get("trail")
        raise _err

    try:
        if _redirect:
            res = await _forward_coalesced(gw_state.router.policy, payload, profile, _redirect_once)
        else:

            async def _fwd_once():
                return await gw_state.forwarder.call_with_fallback(
                    gw_state.router,
                    profile,
                    dep,
                    payload,
                    collect_qc_failures=bool(qc_pol.enabled or gw_state.router.policy.qc_sanity.enabled),
                    media_strike_hook=_strike_hook(explicit_req, need),
                    need=need,
                    scope="group" if explicit_req else "chain",
                    ctx=ctx_dim,
                    attempts_box=attempts_box,
                    session=_sess,
                    ses=session_id,
                    client_ip=_cip,
                    attribution=_attr,
                    orig_messages=_orig_for_retry,
                    requested_group=group_or_explicit,
                )

            res = await _forward_coalesced(gw_state.router.policy, payload, profile, _fwd_once)
    except UpstreamError as err:
        # errore azionabile -> status vero; catena esaurita / nessun output
        # utile -> 503 RETRYABLE (mai un turno finto verso il client).
        if _actionable_upstream_error(err) and err.status:
            st = abs(err.status)
            return JSONResponse(
                status_code=st if st >= 400 else 502,
                content={"error": {"message": err.detail, "type": "upstream_error"}},
            )
        # grp/dep coerenti: l'ULTIMO deployment tentato (dopo un'eventuale
        # escalation di gruppo), non quello iniziale.
        _last_u = attempts_box[-1] if attempts_box else dep.get("unique")
        _last_d = (gw_state.router.config.deployment_by_unique(_last_u) if _last_u else None) or dep
        _emit_summary(
            ses=session_id or "-",
            req=raw_model,
            grp=_last_d.get("group"),
            dep=_last_u,
            tries=max(1, len(attempts_box)),
            fb=max(0, len(attempts_box) - 1),
            dur_ms=int((time.monotonic() - t_req) * 1000),
            stream=False,
            qc=True,
            wd="chain-exhausted",
            usage=None,
        )
        _trail = getattr(err, "trail", None)
        return _exhausted(
            len(attempts_box), err.detail, prefix_reason=_aud, trail=_trail, retry_at_ms=_retry_at_ms(gw_state.router, _trail)
        )
    data, used = res[0], res[1]
    qc_failed = res[2] if len(res) > 2 else []

    # Divulgazione del modello nel campo "model" della risposta
    # (policy.response_model, vedi app/policy.py): nx_deployment è SEMPRE
    # presente con il deployment univoco realmente usato.
    if isinstance(data, dict):
        disc = gw_state.router.policy.response_model
        if disc == "upstream":
            # nome ESATTO scritto dal provider nella sua risposta
            # (es. groq ritorna "meta-llama/llama-3.3-70b-instruct");
            # fallback al nome che noi inviamo se il campo manca/vuoto
            orig = data.get("model")
            data["model"] = orig if isinstance(orig, str) and orig.strip() else used["model"]
        elif disc == "deployment":
            data["model"] = used["unique"]
        else:  # requested (storico)
            data["model"] = raw_model
        data["nx_deployment"] = used["unique"]
    # nota QC nel reasoning (D3): solo se ci sono stati scarti e la policy
    # lo consente — il client che ignora reasoning_content non ne è toccato
    if qc_failed and qc_pol.annotate_reasoning and isinstance(data, dict):
        data = annotate_reasoning(data, qc_failed)
    _u_f14 = _usage_of(data)
    # Sotto hold redirect (_redirect=True): il MOTORE STREAM ha GIA'
    # emesso _emit_summary (via _summary() in sse), note_estimate_error,
    # note_session_estimate e _note_fb_refund. Evitiamo duplicazione.
    if not _redirect:
        try:
            if _u_f14 and _u_f14.get("prompt_tokens"):
                gw_state.router.note_estimate_error(used["unique"], ctx_est, _u_f14["prompt_tokens"])
                # Stima per-sessione: char REALI del payload inviato a monte
                # (post inject_identity/histnorm/ctxcompact) / prompt_tokens.
                gw_state.router.note_session_estimate(
                    session_id,
                    _est_chars_pre,
                    _prompt_chars(payload.get("messages"), payload.get("tools")),
                    _u_f14["prompt_tokens"],
                )
                metrics.inc("nx_sess_est_samples_total")
        except Exception:
            report_suppressed("main.chat_completions")
        _emit_summary(
            ses=session_id or "-",
            req=raw_model,
            grp=used.get("group"),
            dep=used["unique"],
            tries=max(1, len(attempts_box)),
            fb=max(0, len(attempts_box) - 1),
            dur_ms=int((time.monotonic() - t_req) * 1000),
            stream=False,
            qc=bool(qc_failed),
            wd=None,
            usage=_u_f14,
        )
    # Regalo -go per i fallback (#50): SOLO quando il non-stream ha servito
    # direttamente (con hold ON il motore stream ha gia' regalato: la richiesta
    # non-stream vi viene rediretta e il suo summary farebbe doppio regalo).
    if not _redirect:
        _note_fb_refund(gw_state.router, session_id, max(0, len(attempts_box) - 1))
    if sniff.enabled(gw_state.router.policy):
        sniff.begin(
            _rid,
            {
                "model": raw_model,
                "canonical": model,
                "profile": profile,
                "session": _sess or "-",
                "client_ip": _cip,
                "need": sorted(need),
                "dep": used["unique"],
                "stream": False,
            },
            payload,
        ).finish_json(data, {"status": "success", "tries": max(1, len(attempts_box)), "qc_failed": bool(qc_failed)})
    if _ctx_saved_hdr:
        response.headers["X-Ctxcompact-Saved"] = str(_ctx_saved_hdr)
    return data
