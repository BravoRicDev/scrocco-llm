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
import uuid as _uuid

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
from .ctxcompact import compact_tool_outputs, ctxcompact_config_from_policy, frontier_boundary, should_compact
from .effort import effort_from_request, set_effort
from .forwarder import UpstreamError, _client_attribution
from .histnorm import hist_config_from_policy, normalize_messages
from .http_responses import forbidden as _forbidden
from .http_responses import invalid_json_body
from .http_responses import unauthorized as _unauthorized
from .image_helpers import _image_chat_intercept
from .offload import request_json
from .policy import refill_out_budget
from .protocols import sse_to_chat_obj
from .qc import annotate_reasoning
from .router import _prompt_chars, estimate_tokens, inject_identity, set_current_session
from .runtime_persistence import _forward_coalesced, _nonstream_hold_redirect
from .sampling import apply_sampling_defaults, sampling_config_from_policy
from .schemaout import maybe_inject_response_format, schemaout_config_from_policy
from .stream_verdicts import _actionable_upstream_error, _exhausted, _retry_at_ms
from .suppressed import report_suppressed
from .thought_sig import has_unsigned_tool_calls, reset_request_flags, set_avoid_gemini, set_dummy_fill

log = logging.getLogger("nx.main")

router = APIRouter()




class _ChatCompletion:
    """Una richiesta /v1/chat/completions (method object: le fasi dell'endpoint condividono lo stato su self)."""

    def __init__(self, request, response):
        self.request = request
        self.response = response

    async def run(self):
        _resp = await self._read_payload()
        if _resp is not None:
            return _resp

        self._init_request_context()

        _resp = self._authorize()
        if _resp is not None:
            return _resp

        await self._prepare_session_and_needs()

        _resp = await self._maybe_serve_images()
        if _resp is not None:
            return _resp
        self._estimate_context()

        _resp = self._resolve_group()
        if _resp is not None:
            return _resp

        self._apply_session_bucket_rules()

        _resp = self._pick_first_deployment()
        if _resp is not None:
            return _resp
        _resp = self._reject_without_deployment()
        if _resp is not None:
            return _resp

        self._bind_deployment()

        self._normalize_history()
        self._compact_context()
        self._audit_prefix()
        self._prepare_upstream()

        _resp = await self._serve_stream()
        if _resp is not None:
            return _resp

        _resp = await self._forward_nonstream()
        if _resp is not None:
            return _resp
        return self._finish_nonstream()

    async def _read_payload(self):
        """Legge il body JSON; 400 se non valido."""
        self._api_log = logging.getLogger("nx.api")
        try:
            self.payload = await request_json(self.request)
        except Exception as exc:
            self._api_log.warning("[api] invalid JSON body: %s", exc)
            return invalid_json_body()

    def _init_request_context(self):
        """Stato per-richiesta (effort, flag, firme Gemini) e nome modello canonico."""
        # EFFORT/reasoning: `reasoning_effort` (o alias `effort`) nel body, oppure
        # header `x-effort`. Lo stato vive in una ContextVar legata al task della
        # richiesta: il router lo usa per il bias di intelligence, il forwarder per
        # iniettare/rimuovere reasoning_effort e per l'override di temperatura.
        set_effort(
            effort_from_request(self.payload, self.request.headers),
            temp_enabled=gw_state.policy.enable_effort_temperature_override,
            temp_overrides=gw_state.policy.effort_temperature_overrides,
        )

        self.raw_model = self.payload.get("model") or ""
        self.messages = self.payload.get("messages") or []
        self.stream = bool(self.payload.get("stream"))
        # id breve per correlare input/output nel file di debug-sniff

        self._rid = _uuid.uuid4().hex[:12]

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
        set_avoid_gemini(has_unsigned_tool_calls(self.messages) and not gw_state.policy.thought_sig_dummy_fill)
        set_dummy_fill(gw_state.policy.thought_sig_dummy_fill, gw_state.policy.thought_sig_dummy_value)

        # --- normalizzazione del nome richiesto:
        #     1) prefisso STORICO -> prefisso corrente (compatibilità client)
        #     2) alias (gateway.yaml) -> nome canonico
        self.model = gw_state.policy.canonicalize(self.raw_model)

    def _authorize(self):
        """Autenticazione e autorizzazione del modello richiesto (401/403)."""
        # --- auth ---
        self.auth: AuthResult = gw_state.authn.authenticate(self.request.headers.get("authorization"))
        if not self.auth.ok:
            return _unauthorized(self.auth.error)

        # --- autorizzazione modello (whitelist tre livelli, sul nome canonico) ---
        if not gw_state.authn.authorize_model(self.auth, self.model):
            return _forbidden(self.model, self.auth.profile)

    async def _prepare_session_and_needs(self):
        """Sessione, ponte STT per l'audio, capacita' richieste, guardie di sessione."""
        # --- routing ---
        _set_opencode_gate(self.request)
        self.session_id = _session_id(self.request, self.payload)

        # --- STT-BRIDGE: audio in chat -> testo (PRIMA di ogni altra cosa) ---
        # Va fatto PRIMA del capability detection: con STT sempre l'audio non
        # deve piu' chiedere la capacita' `audio` al routing, altrimenti finirebbe
        # nel gruppo -audio (73 deployment che ho verificato non trascrivere
        # l'audio in modo affidabile) invece che nel pool testo normale. Dopo
        # questa riga `messages` non ha piu' audio.
        if count_audio_parts(self.payload.get("messages") or []):
            self.payload = await _stt_bridge(self.request, self.payload, self.auth, self.session_id, self.model, self.raw_model)
            self.messages = self.payload.get("messages") or []

        # --- capability detection ---
        # capacità richieste dal payload; OGNI chat produce testo -> "text" è sempre
        # implicita: i modelli solo-tts/stt/image_gen escono dal pool chat automatico
        # (gli espliciti passano comunque; kill-switch: capability_routing.enabled=false)
        if gw_state.router.policy.routing_active():
            self.need = required_caps(self.payload) | {"text"}
        else:
            self.need = frozenset()

        # SESSION-DEP GUARD: la sessione corrente dev'essere nota GIA' durante
        # initial_pick/pick_deployment (guardia anti-usurpazione cross-sessione),
        # non solo dopo il pick come in passato.

        set_current_session(self.session_id)
        # SESSION-DEP GUARD: la sessione ha USATO il servizio -> rinnova
        # l'ownership di tutti i suoi deployment (restano suoi finché e' viva;
        # 15 min di silenzio e l'intero set torna libero).
        gw_state.router.note_session_activity(self.session_id)
        # RATE per-sessione (SOLO chat): alimenta warm_ready_min adattivo.
        gw_state.router.note_session_request(self.session_id)

    async def _maybe_serve_images(self):
        """Richiesta di immagini in output verso un modello image-native: la serve la macchina immagini."""
        # --- ADATTAMENTO chat -> /images/* ---
        # Il client ha chiesto immagini in output (modalities:["image"]): se il
        # deployment scelto e' image-native lo si serve con la macchina immagini
        # (body OpenAI images adattato dal body chat, risposta riconvertita in
        # chat.completion). Ritorna None per i modelli chat-native, che seguono il
        # motore chat normale qui sotto.
        if wants_image_output(self.payload):
            self._img_resp = await _image_chat_intercept(
                self.request, payload=self.payload, model=self.model, raw_model=self.raw_model, auth=self.auth, session_id=self.session_id
            )
            if self._img_resp is not None:
                return self._img_resp

    def _estimate_context(self):
        """Stima del contesto (per-sessione se calibrata, euristica al primo turno)."""
        _sniff_headers(
            self.request,
            logger=self._api_log,
            body_size=len(self.request._body) if hasattr(self.request, "_body") else 0,
            session_id=self.session_id,
        )
        self._img_est = getattr(gw_state.router.policy, "image_token_estimate", 0) or 0
        # Base di stima = payload con la sola histnorm (preview deterministica,
        # PRIMA di ctxcompact): la STESSA base su cui si apprende e si applica il
        # rapporto per-sessione, cosi' i char contati coincidono tra hook usage e
        # routing.
        self._est_msgs = self.messages
        try:

            self._est_msgs, _ = normalize_messages(
                self.messages, hist_config_from_policy(gw_state.router.policy), tail_floor=gw_state.router.ctx_boundary_floor(self.session_id)
            )
        except Exception:  # noqa: BLE001
            self._est_msgs = self.messages
        self._est_chars_pre = _prompt_chars(self._est_msgs, self.payload.get("tools"))
        self._cpt = gw_state.router.session_chars_per_token(self.session_id)
        if self._cpt:
            # Dal 2o turno, DUE stime dalla stessa base pre-compressione:
            #  ctx_dim = token POST-compressione previsti -> scelta della dim;
            #  ctx_est = token PRE-compressione -> sicurezza (ctxcompact/overflow).
            self.ctx_dim, _ = gw_state.router.estimate_for_session(
                self.session_id, self._est_msgs, gw_state.router.policy.estimate_divisor, self._img_est, tools=self.payload.get("tools"), pre=True
            )
            self.ctx_est, _ = gw_state.router.estimate_for_session(
                self.session_id, self._est_msgs, gw_state.router.policy.estimate_divisor, self._img_est, tools=self.payload.get("tools")
            )
            metrics.inc("nx_sess_est_used_total")
            log.info(
                "[estimate] sess cpt_pre=%.2f cpt_post=%.2f -> ctx_dim≈%d ctx_pre≈%d (chars_pre=%d, +%.0f%%)",
                gw_state.router.session_chars_per_token(self.session_id, pre=True),
                self._cpt,
                self.ctx_dim,
                self.ctx_est,
                self._est_chars_pre,
                (float(getattr(gw_state.router.policy, "session_estimate_margin", 1.05)) - 1.0) * 100.0,
            )
        else:
            # 1o turno della sessione: stima euristica basata sui soli char.
            self.ctx_est = estimate_tokens(self.messages, gw_state.router.policy.estimate_divisor, self._img_est, tools=self.payload.get("tools"))
            self.ctx_dim = self.ctx_est
            metrics.inc("nx_sess_est_fallback_total")

    def _resolve_group(self):
        """Gruppo (dims/capacita') o deployment esplicito della richiesta."""
        self.group_or_explicit = gw_state.router.resolve_group_for_request(
            self.model, self.messages, self.session_id, self.need, self.ctx_dim, profile=self.auth.profile
        )
        if self.group_or_explicit is None:
            if self.need:
                for cap in sorted(self.need):
                    metrics.inc("nx_caps_unroutable_total", (cap,))
                # Rifiuto ESPLICITO: il client ha chiesto un deployment preciso
                # (unique) che non dichiara una capacita' media necessaria. Il
                # messaggio generico ("configura model_capabilities in
                # gateway.yaml") sarebbe fuorviante: il deployment esiste, e' la
                # sua scheda a non avere la capacita'. Meglio un messaggio che
                # nomini modello e capacita' mancante: l'agente puo' scegliere da
                # solo al turno dopo invece di fare un giro di scoperta.
                self._missing = gw_state.router._missing_media_caps(self.model, self.need)
                if self._missing:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": {
                                "message": (
                                    f"il modello '{self.model}' non supporta: "
                                    f"{', '.join(self._missing)}. La richiesta contiene "
                                    f"media che quel modello non puo' elaborare; usa "
                                    f"un modello con capacita' "
                                    f"{'+'.join(self._missing)}."
                                ),
                                "type": "invalid_request_error",
                                "code": "model_capability_unsupported",
                                "missing_capabilities": self._missing,
                            }
                        },
                    )
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": f"nessun deployment dichiara le capacità richieste: {sorted(self.need)}. "
                            f"Configura capability_routing.model_capabilities in gateway.yaml",
                            "type": "invalid_request_error",
                        }
                    },
                )
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": f"model '{self.model}' not managed by {gw_state.policy.service_name}",
                        "type": "invalid_request_error",
                    }
                },
            )

    def _apply_session_bucket_rules(self):
        """Rimborso -go del turno e rilascio dello sticky sugli espliciti."""
        # RIMBORSO LATENZA: conta il turno della sessione (all'atterraggio) e, se
        # un "lento" ha regalato turni -go, atterra sul bucket -go come se il
        # client avesse chiamato scrocco-llm-<profilo>-go. Solo richieste di testo
        # su un dim (-Nk): media/cap, -go/-fallback e i unique espliciti restano
        # invariati. Ladder/selezione a valle sono INVARIATI.
        self._turn_go = False
        if self.session_id:
            try:
                self._turn_go = gw_state.router.note_session_turn(self.session_id)
            except Exception:  # noqa: BLE001
                self._turn_go = False
        self.group_or_explicit, self._refund_go = _apply_go_refund(gw_state.router, self.group_or_explicit, self.auth.profile, self._turn_go, self.session_id)

        self.explicit_req = gw_state.router.is_explicit(self.model)
        # Se il client chiama esplicitamente un gruppo diverso (es. -200k -> -1000k
        # o -go), rilascia lo sticky per-deployment cosi' la richiesta esplicita
        # atterra sul nuovo gruppo/key scelta dal routing, non resta incollata al
        # vecchio deployment dello sticky precedente.
        if (self.explicit_req or self._refund_go) and self.session_id:
            self.cur = gw_state.router.dep_sticky_get(self.session_id)
            self.sd = gw_state.router.config.deployment_by_unique(self.cur) if self.cur else None
            # Rilascia dep-sticky SOLO se il gruppo è cambiato o non c'è sticky:
            # se la richiesta esplicita punta allo stesso gruppo dello sticky,
            # lo preserviamo per la cache-preserving (misma key per sessione).
            if self.sd is None or self.sd.get("group") != self.group_or_explicit:
                gw_state.router.dep_sticky_release(self.session_id)
            # Per richieste esplicite su un dim (-Nk): riàncora lo sticky di
            # gruppo cosi' le successive NON-esplicite continuano nel contesto
            # scelto dall'utente (crescita cache-preserving). Per -go/-fallback/
            # unique: libera lo sticky di gruppo, altrimenti il traffico
            # automatico verrebbe parcheggiato in un bucket a pagamento.
            if re.search(r"-\d+k$", self.group_or_explicit):
                gw_state.router.sticky_set(self.session_id, self.group_or_explicit)
            else:
                gw_state.router.sticky_release(self.session_id)

    def _pick_first_deployment(self):
        """Primo deployment: esplicito, sticky/warm/holder o pick del gruppo (con fail-fast F31 sul contesto)."""
        self.dep = gw_state.router.config.deployment_by_unique(self.group_or_explicit)
        if self.dep is None:
            # F31 fail-fast ingresso: se il ctx non entra nel gruppo (max_input di
            # tutti i dep < ctx) si compatta FORZANDO il gate min_saved e si
            # ricalcola; se resta sopra si risponde 400 sintetico senza toccare
            # l'upstream. Evita 2-3 tentativi di catena e 10-15s di prefill inutile.
            self._max_grp = 0
            try:
                self._max_grp = max(
                    (int(d.get("max_input_tokens") or 0) for d in (gw_state.router.config.groups.get(self.group_or_explicit) or [])),
                    default=0,
                )
            except Exception:
                self._max_grp = 0
            self._zen_first = False
            try:
                self._zen_first = gw_state.router._zen_first_active()
            except Exception:  # noqa: BLE001
                self._zen_first = False
            # Zen-first: si tiene conto anche della RISERVA DI OUTPUT (il picker
            # richiede ctx+out <= max_input) e si compatta per rientrare nel tier
            # zen. Per i non-nativi resta lo storico 5% di margine sull'input.
            try:
                self._out_res = int(refill_out_budget(self.payload, gw_state.router.policy) or 0)
            except Exception:  # noqa: BLE001
                self._out_res = 0
            if self._zen_first:
                self._budget = max(1, self._max_grp - self._out_res) if self._max_grp else 0
                self._trig = bool(self.ctx_est and self.ctx_est > self._budget)
            else:
                self._budget = self._max_grp
                self._trig = bool(self.ctx_est and self.ctx_est > int(self._max_grp * 1.05))
            if self._max_grp > 0 and self._trig:
                self._up = None
                self._handled = False
                if self._zen_first:
                    # ZEN-FIRST (client opencode nativo): PRIMA di salire a una
                    # dim senza zen si prova a COMPATTARE per restare nel tier
                    # free; solo se il payload resta troppo grande si sale di dim.

                    self._ccf = ctxcompact_config_from_policy(gw_state.router.policy)
                    self._ccf.min_saved_tokens = 0
                    self._img = getattr(gw_state.router.policy, "image_token_estimate", 0) or 0
                    self._forced, self._frep = compact_tool_outputs(
                        self.payload.get("messages") or [],
                        self._ccf,
                        max_in=self._budget,
                        estimator=lambda ms: gw_state.router.estimate_for_session(
                            self.session_id, ms, gw_state.router.policy.estimate_divisor, self._img
                        )[0],
                    )
                    if self._frep.get("changed"):
                        self.payload["messages"] = self._forced
                        metrics.inc("nx_ctx_compacted_forced")
                        log.info(
                            "[ctx-overflow] compattazione forzata (zen) per %s: %s",
                            self.group_or_explicit,
                            {k: self._frep.get(k) for k in ("stubbed", "deduped", "args_trimmed", "saved_chars")},
                        )
                    self.ctx_est = gw_state.router.estimate_for_session(
                        self.session_id,
                        self.payload.get("messages") or [],
                        gw_state.router.policy.estimate_divisor,
                        self._img,
                        tools=self.payload.get("tools"),
                    )[0]
                    if self.ctx_est <= self._budget:
                        metrics.inc("nx_zen_dim_stay")
                        log.info(
                            "[zen-dim] compattato: ctx≈%d entra in %s (zen, budget=%d out=%d): resto nel tier free",
                            self.ctx_est,
                            self.group_or_explicit,
                            self._budget,
                            self._out_res,
                        )
                        self._handled = True
                if not self._handled:
                    self._climb = (self.ctx_est > self._budget) if self._zen_first else (self.ctx_est > self._max_grp)
                    if self._climb:
                        # SALITA DI DIM (regola dell'utente): la dim PIU' PICCOLA
                        # che contiene il payload (+ riserva output per lo zen).
                        self._up = gw_state.router.climb_dim_group(self.group_or_explicit, self.ctx_est + (self._out_res if self._zen_first else 0))
                    if self._up:
                        log.info(
                            "[dim] ctx≈%d non entra in %s (max %d): salgo a %s", self.ctx_est, self.group_or_explicit, self._max_grp, self._up
                        )
                        self.group_or_explicit = self._up
                        if self.explicit_req and self.session_id:
                            gw_state.router.sticky_set(self.session_id, self._up)
                    else:

                        # NB: CtxCompactConfig NON e' un dataclass -> niente
                        # `dataclasses.replace` (TypeError a runtime: era il bug di
                        # produzione). E' un'istanza fresca per chiamata: si muta il campo.
                        self._ccf = ctxcompact_config_from_policy(gw_state.router.policy)
                        self._ccf.min_saved_tokens = 0
                        self._img = getattr(gw_state.router.policy, "image_token_estimate", 0) or 0
                        self._forced, self._frep = compact_tool_outputs(
                            self.payload.get("messages") or [],
                            self._ccf,
                            max_in=self._max_grp,
                            estimator=lambda ms: gw_state.router.estimate_for_session(
                                self.session_id, ms, gw_state.router.policy.estimate_divisor, self._img
                            )[0],
                        )
                        if self._frep.get("changed"):
                            self.payload["messages"] = self._forced
                            metrics.inc("nx_ctx_compacted_forced")
                            log.info(
                                "[ctx-overflow] compattazione forzata per %s: %s",
                                self.group_or_explicit,
                                {k: self._frep.get(k) for k in ("stubbed", "deduped", "args_trimmed", "saved_chars")},
                            )
                        self.ctx_est = gw_state.router.estimate_for_session(
                            self.session_id,
                            self.payload.get("messages") or [],
                            gw_state.router.policy.estimate_divisor,
                            self._img,
                            tools=self.payload.get("tools"),
                        )[0]
                        if self.ctx_est > int(self._max_grp * 1.05):
                            metrics.inc("nx_ctx_overflow_total", (self.group_or_explicit,))
                            return JSONResponse(
                                status_code=400,
                                content={
                                    "error": {
                                        "code": "context_length_exceeded",
                                        "message": "ctx ~%d oltre il max_input %d del "
                                        "gruppo %s, anche dopo la "
                                        "compattazione" % (self.ctx_est, self._max_grp, self.group_or_explicit),
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
            self._grp_is_dim = gw_state.router.config.group_caps.get(self.group_or_explicit) is None and not gw_state.router._is_renewal_bucket(
                self.group_or_explicit
            )
            self._warm = (not self.explicit_req) or self._grp_is_dim
            # CACHE PAGATA: SOLO su richiesta esplicita a -go/-fallback testo:
            # riusa la stessa chiave della sessione (KV-cache calda) anche se sta
            # su un tier di rinnovo peggiore; al 429 il holder si esclude da solo
            # e la rotazione prosegue nell'ordine normale (crediti "sommati" un
            # account alla volta). Auto-routing ed escalation interne non lo usano.
            self._go_suf = gw_state.router.config.go_suffix or "-go"
            self._fb_suf = gw_state.router.config.fallback_suffix or "-fallback"
            self._paid_holder = self.explicit_req and (self.group_or_explicit.endswith(self._go_suf) or self.group_or_explicit.endswith(self._fb_suf))
            # PARITA' stream/non-stream sotto HOLD: se questa richiesta non-stream
            # sara' servita dal MOTORE STREAM (redirect hold, vedi _redirect sotto),
            # anche il pick iniziale deve ordinare il warm come lo stream
            # (prefer_fast=False). Qui `dep` non esiste ancora: l'intento si ricava
            # dalla policy (il flag per-deployment resta gestito dal ramo a valle).
            self._pre_redirect = _nonstream_hold_redirect(self.stream, None, gw_state.router.policy.qc_json, gw_state.router.policy)
            self.dep = gw_state.router.initial_pick(
                self.auth.profile,
                self.group_or_explicit,
                None if self.explicit_req else self.need,
                self.ctx_dim,
                session_id=self.session_id,
                warm=self._warm,
                prefer_holder=self._paid_holder,
                prefer_fast=(not self.stream) and not self._pre_redirect,
                out_tokens=refill_out_budget(self.payload, gw_state.router.policy),
            )

    def _reject_without_deployment(self):
        """Nessun deployment: 400 context_length_exceeded se e' overflow, altrimenti 503."""
        if self.dep is None:
            # F31: se il motivo e' l'overflow (tutti i dep del gruppo hanno
            # max_input < ctx) NON e' un disservizio ma un errore del client:
            # 400 context_length_exceeded invece del 503 "nessun deployment".
            try:
                self._mx = max(
                    (int(d.get("max_input_tokens") or 0) for d in (gw_state.router.config.groups.get(self.group_or_explicit) or [])),
                    default=0,
                )
            except Exception:
                self._mx = 0
            if self._mx > 0 and self.ctx_est and self.ctx_est > self._mx:
                metrics.inc("nx_ctx_overflow_total", (self.group_or_explicit,))
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "code": "context_length_exceeded",
                            "message": "ctx ~%d oltre il max_input %d del gruppo %s" % (self.ctx_est, self._mx, self.group_or_explicit),
                            "type": "invalid_request_error",
                        }
                    },
                )
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "nessun deployment disponibile"
                        + (" per le capacità richieste" if not self.explicit_req else ""),
                        "type": "server_error",
                    }
                },
            )

    def _bind_deployment(self):
        """Chiave alias, profilo, metriche, log di route, autoprobe, sticky e identita' nel payload."""
        # override chiave: alias GENERICO con chiave custom (policy.alias_keys):
        # sostituisce SOLO dep["api_key"]. Vale solo per il PRIMO tentativo —
        # i fallback successivi tornano al pool normale del profilo, così una
        # chiave rotta non blocca mai il servizio.
        self.custom_key = gw_state.router.resolve_alias_key(self.raw_model, self.model)
        if self.custom_key:
            self.dep = {**self.dep, "api_key": self.custom_key}

        self.profile = self.auth.profile or gw_state.config.profile_of_base(self.model.split("__")[0]) or gw_state.config.profile_of_base(self.model)

        if self.model != self.raw_model:
            log.info("[route] alias %r -> %r", self.raw_model, self.model)
        metrics.inc("nx_requests_total", (self.raw_model[:60], str(bool(self.payload.get("stream")))))
        metrics.inc("nx_group_total", (self.dep["group"],))
        for cap in sorted(self.need):
            metrics.inc("nx_caps_requests_total", (cap,))
        log.info(
            "[route] %s -> %s (ctx≈%d tok%s, need=%s, session=%s, stream=%s)",
            self.model,
            self.dep["group"],
            self.ctx_est,
            f"+{count_image_parts(self.messages)}img" if count_image_parts(self.messages) else "",
            sorted(self.need) if self.need else "-",
            self.session_id or "anonima",
            bool(self.payload.get("stream")),
        )

        # autoprobe cooldown (fire-and-forget: non entra nella risposta)
        autoprobe.maybe_spawn(gw_state.router, gw_state.forwarder, self.profile)

        # sticky session SOLO dal routing automatico (nome base): le richieste
        # esplicite (-Nk/-go/-fallback/__univoco) non leggono né scrivono sticky.
        # I bucket renewal (-go/-fallback) NON vengono mai salvati: -go si
        # raggiunge solo esplicitamente o a fine scala (free -> zen -> -go).
        if self.session_id and not gw_state.router.is_explicit(self.model) and not gw_state.router._is_renewal_bucket(self.group_or_explicit):
            gw_state.router.sticky_set(self.session_id, self.group_or_explicit)

        # iniezione identità + modello univoco nel payload upstream
        inject_identity(self.payload, self.dep, router=gw_state.router)

    def _normalize_history(self):
        """L1/L2: normalizzazione della coda della history (cache-safe)."""
        # ---- L1/L2 preprocessing (cache-safe: solo la coda) ----

        self._hn = hist_config_from_policy(gw_state.router.policy)
        self._sm = sampling_config_from_policy(gw_state.router.policy)
        self._so = schemaout_config_from_policy(gw_state.router.policy)
        self._orig_msgs = self.payload.get("messages")  # pre-normalizzazione
        self._orig_for_retry = None
        if self._hn.enabled:
            self._nm, self._nr = normalize_messages(self.payload.get("messages"), self._hn, tail_floor=gw_state.router.ctx_boundary_floor(self.session_id))
            if self._nr.get("changed"):
                self.payload["messages"] = self._nm
                metrics.inc("nx_histnorm_total", ("changed",))
                log.info(
                    "[histnorm] coda normalizzata: %s",
                    {k: self._nr.get(k) for k in ("shown_orphan_tool", "dangling_tool_calls", "empty_assistant", "dup_system")},
                )
            # Il taglio del reasoning e' un'ottimizzazione di TOKEN: la history
            # originale serve (a) ai deployment con `thinking_replay` per rimettere
            # il reasoning VERO prima dell'invio, (b) al retry una-tantum dopo un
            # errore "oscuro". Teniamo il riferimento sempre che esista.
            if self._orig_msgs:
                self._orig_for_retry = self._orig_msgs

    def _compact_context(self):
        """Cache-aware: detentore della sessione e compattazione del contesto."""
        # ---- cache-aware: detentore sessione + troncamento contesto ----

        self._cc = ctxcompact_config_from_policy(gw_state.router.policy)
        self._holder = gw_state.router.session_holder(self.session_id)
        self._max_in = int(self.dep.get("max_input_tokens") or 0)
        self._same_family = False
        if self._holder:
            self._hd = gw_state.router.config.deployment_by_unique(self._holder)
            if self._hd and self._hd.get("family") and self._hd.get("family") == self.dep.get("family"):
                self._same_family = True
        # F14: correzione per-deployment appresa dal VERO prompt_tokens upstream
        # (tokenizer diverso da chars/4): la decisione di compattazione non deve
        # lavorare su stime sballate.
        try:
            self._corr = gw_state.router.estimate_correction(self.dep.get("unique", ""))
            if self._corr != 1.0:
                self._ctx_corr = max(1, int(self.ctx_est * self._corr))
            else:
                self._ctx_corr = self.ctx_est
        except Exception:
            self._ctx_corr = self.ctx_est
        # H2: divisore chars/token CALIBRATO (F14) da usare per il budget della
        # frontiera: il 4 fisso sottostima i token sui tokenizer non-OpenAI.
        try:
            self._div_eff = gw_state.router.effective_divisor(self.dep.get("unique", ""))
        except Exception:
            self._div_eff = float(getattr(gw_state.router.policy, "estimate_divisor", 4) or 4)
        self._dec = should_compact(
            self._cc,
            self._ctx_corr,
            self._max_in,
            self._holder,
            self.dep.get("unique"),
            bool(self.session_id and gw_state.router.is_session_compact(self.session_id)),
            same_family=self._same_family,
            reasoning=bool(self.dep.get("effort_capable")),
        )
        self._do_compact = self._dec["compact"]
        if self._do_compact and self.session_id:
            gw_state.router.mark_session_compact(self.session_id)
        self._ctx_saved_hdr = 0
        if self._do_compact:
            self._cmsgs, self._crep = compact_tool_outputs(
                self.payload.get("messages"),
                self._cc,
                max_in=self._max_in,
                estimator=lambda ms: gw_state.router.estimate_for_session(
                    self.session_id,
                    ms,
                    self._div_eff,
                    getattr(gw_state.router.policy, "image_token_estimate", 0) or 0,
                    unique=self.dep.get("unique"),
                )[0],
                boundary_floor=gw_state.router.ctx_boundary_floor(self.session_id),
                divisor=self._div_eff,
            )
            if self._crep.get("changed"):
                self.payload["messages"] = self._cmsgs
                gw_state.router.note_compact_boundary(self.session_id, self._crep.get("boundary"))
                metrics.inc("nx_ctxcompact_total", ("stubbed",))
                for tool_name in (self._crep.get("tools") or {}):
                    metrics.inc("nx_ctxcompact_tool_total", (tool_name,))
                self._ctx_saved_hdr = int(self._crep.get("saved_tokens_est") or 0)
                log.info(
                    "[ctxcompact] ses=%s stubbed=%d dedup=%d args=%d saved≈%dtok reason=%s",
                    self.session_id,
                    self._crep["stubbed"],
                    self._crep.get("deduped", 0),
                    self._crep.get("args_trimmed", 0),
                    self._crep["saved_tokens_est"],
                    self._dec["reason"],
                )

    def _audit_prefix(self):
        """Audit del prefisso (F4) e log della richiesta instradata."""
        # AUDIT DEL PREFISSO (F4): prima di spendere la cache a monte, impronta
        # il prefisso [1:frontier] e dice perche' e' cambiato (se e' cambiato).
        # 'identity' = colpa nostra (system/inject), 'prefix' = ctxcompact,
        # histnorm o riscrittura del client. Osservabilita': nessun effetto sulla
        # scelta del deployment, ma F10 lo usa come breadcrumb sui 503.
        self._aud = None
        if getattr(gw_state.router.policy, "cache_prefix_audit", True) and self.session_id:
            self._bnd = self._crep.get("boundary") if (self._do_compact and self._crep.get("changed")) else None
            if self._bnd is None:
                try:
                    self._bnd = frontier_boundary(
                        self.payload.get("messages") or [], self._cc, self._max_in, gw_state.router.ctx_boundary_floor(self.session_id), self._div_eff
                    )
                except Exception:  # noqa: BLE001
                    self._bnd = None
            self._aud = gw_state.router.audit_prefix(self.session_id, self.payload.get("messages") or [], self._bnd)
            metrics.inc("nx_cache_audit_total", (self._aud,))
            if self._aud in ("identity", "prefix"):
                log.info(
                    "[cache-audit] ses=%s prefisso MUTATO (%s, boundary=%s): cache upstream riparte da li'",
                    self.session_id,
                    self._aud,
                    self._bnd,
                )
        log.info(
            "[cache] ses=%s holder=%s family=%s same_fam=%s compact=%s cold=%s reason=%s ctx≈%d max_in=%d",
            self.session_id,
            self._holder or "-",
            self.dep.get("family") or "-",
            self._same_family,
            self._do_compact,
            self._dec["cold"],
            self._dec["reason"] or "-",
            self.ctx_est,
            self._max_in,
        )

    def _prepare_upstream(self):
        """Default di sampling per lo stream, sessione/IP/attribuzione verso l'upstream."""
        if self.stream and self._sm.enabled:
            self._ap = apply_sampling_defaults(self.payload, self.dep, self._sm)
            if self._ap:
                log.debug("[sampling] %s: default %s", self.dep.get("unique"), self._ap)
            if maybe_inject_response_format(self.payload, self.dep, self._so):
                metrics.inc("nx_resp_format_injected_total", (self.dep.get("unique"),))
        self.t_req = time.monotonic()
        # sessione OpenCode: passthrough se il client la invia (x-opencode-session
        # oppure x-session-affinity/x-session-id nativi), altrimenti fallback alla
        # sessione del body; se manca del tutto l'header viene calcolato nel
        # forwarder (hash api_key+client_ip)
        self._sess = _opencode_session(self.request) or self.session_id
        self._cip = _client_ip(self.request)
        # attribuzione app OpenRouter: i modelli :free sono serviti SOLO agli
        # "agentic harness" riconosciuti; se il CLIENT si attribuisce
        # (HTTP-Referer/X-Title), quel valore vince sul default di policy.
        self._attr = _client_attribution(self.request)

    async def _serve_stream(self):
        """Richiesta stream: motore stream con fallback (app/chat_stream.py)."""
        if self.stream:
            self._sniffer = None
            if sniff.enabled(gw_state.router.policy):
                self._sniffer = sniff.begin(
                    self._rid,
                    {
                        "model": self.raw_model,
                        "canonical": self.model,
                        "profile": self.profile,
                        "session": self._sess or "-",
                        "client_ip": self._cip,
                        "need": sorted(self.need),
                        "group": self.group_or_explicit,
                        "dep": self.dep.get("unique"),
                        "stream": True,
                    },
                    self.payload,
                )
            self._sresp = await _stream_with_fallback(
                self.profile,
                self.dep,
                self.payload,
                self.need,
                hook=_strike_hook(self.explicit_req, self.need),
                scope="group" if self.explicit_req else "chain",
                ctx=self.ctx_dim,
                cold=bool(self._dec.get("cold")),
                prefix_reason=(self._aud if self._aud in ("identity", "prefix") else None),
                ses=self.session_id,
                req=self.raw_model,
                est_chars=self._est_chars_pre,
                session=self._sess,
                client_ip=self._cip,
                request=self.request,
                attribution=self._attr,
                requested_group=self.group_or_explicit,
                orig_messages=self._orig_for_retry,
                sniffer=self._sniffer,
            )
            if self._ctx_saved_hdr:
                self._sresp.headers["X-Ctxcompact-Saved"] = str(self._ctx_saved_hdr)
            return self._sresp

    async def _forward_nonstream(self):
        """Non-stream: fallback a catena (o redirect al motore stream sotto hold); 503 a catena esaurita."""
        self.qc_pol = gw_state.router.policy.qc_json
        self.attempts_box: list[str] = []
        # HOLD-UNTIL-FINISH + richiesta non-stream: esegui il MOTORE STREAM (sotto
        # hold bufferizza l'intera risposta) e restituisci non-stream. Un solo
        # motore per entrambi -> comportamento identico. Kill-switch:
        # policy.nonstream_hold_redirect.
        self._redirect = _nonstream_hold_redirect(self.stream, self.dep, self.qc_pol, gw_state.router.policy)

        try:
            if self._redirect:
                self.res = await _forward_coalesced(gw_state.router.policy, self.payload, self.profile, self._redirect_once)
            else:

                self._fwd_once = self._fwd_once

                self.res = await _forward_coalesced(gw_state.router.policy, self.payload, self.profile, self._fwd_once)
        except UpstreamError as _err_exc:
            # errore azionabile -> status vero; catena esaurita / nessun output
            # utile -> 503 RETRYABLE (mai un turno finto verso il client).
            self.err = _err_exc
            if _actionable_upstream_error(self.err) and self.err.status:
                self.st = abs(self.err.status)
                return JSONResponse(
                    status_code=self.st if self.st >= 400 else 502,
                    content={"error": {"message": self.err.detail, "type": "upstream_error"}},
                )
            # grp/dep coerenti: l'ULTIMO deployment tentato (dopo un'eventuale
            # escalation di gruppo), non quello iniziale.
            self._last_u = self.attempts_box[-1] if self.attempts_box else self.dep.get("unique")
            self._last_d = (gw_state.router.config.deployment_by_unique(self._last_u) if self._last_u else None) or self.dep
            _emit_summary(
                ses=self.session_id or "-",
                req=self.raw_model,
                grp=self._last_d.get("group"),
                dep=self._last_u,
                tries=max(1, len(self.attempts_box)),
                fb=max(0, len(self.attempts_box) - 1),
                dur_ms=int((time.monotonic() - self.t_req) * 1000),
                stream=False,
                qc=True,
                wd="chain-exhausted",
                usage=None,
            )
            self._trail = getattr(self.err, "trail", None)
            return _exhausted(
                len(self.attempts_box), self.err.detail, prefix_reason=self._aud, trail=self._trail, retry_at_ms=_retry_at_ms(gw_state.router, self._trail)
            )

    async def _fwd_once(self):
        return await gw_state.forwarder.call_with_fallback(
            gw_state.router,
            self.profile,
            self.dep,
            self.payload,
            collect_qc_failures=bool(self.qc_pol.enabled or gw_state.router.policy.qc_sanity.enabled),
            media_strike_hook=_strike_hook(self.explicit_req, self.need),
            need=self.need,
            scope="group" if self.explicit_req else "chain",
            ctx=self.ctx_dim,
            attempts_box=self.attempts_box,
            session=self._sess,
            ses=self.session_id,
            client_ip=self._cip,
            attribution=self._attr,
            orig_messages=self._orig_for_retry,
            requested_group=self.group_or_explicit,
        )

    async def _redirect_once(self):

        _sp = dict(self.payload)
        _sp["stream"] = True
        try:
            if self._sm.enabled:
                apply_sampling_defaults(_sp, self.dep, self._sm)
            maybe_inject_response_format(_sp, self.dep, self._so)
        except Exception:  # noqa: BLE001
            report_suppressed("main.chat_completions._redirect_once")
        _meta: dict = {}
        _sresp = await _stream_with_fallback(
            self.profile,
            self.dep,
            _sp,
            self.need,
            hook=_strike_hook(self.explicit_req, self.need),
            scope="group" if self.explicit_req else "chain",
            ctx=self.ctx_dim,
            cold=bool(self._dec.get("cold")),
            prefix_reason=(self._aud if self._aud in ("identity", "prefix") else None),
            ses=self.session_id,
            req=self.raw_model,
            est_chars=self._est_chars_pre,
            session=self._sess,
            client_ip=self._cip,
            request=self.request,
            attribution=self._attr,
            requested_group=self.group_or_explicit,
            orig_messages=self._orig_for_retry,
            sniffer=None,
            result_box=_meta,
            client_stream=False,
        )
        if isinstance(_sresp, StreamingResponse):
            _chunks = [c async for c in _sresp.body_iterator]
            self.attempts_box.extend(_meta.get("attempts") or [])
            try:
                _data = sse_to_chat_obj(_chunks)
            except ValueError as _ex:
                raise UpstreamError(503, "stream non assemblable: %s" % _ex, final=True) from _ex
            return (_data, _meta.get("dep") or self.dep)
        # errore PRE-BYTE: il motore stream ritorna gia' un JSONResponse
        # (503 retryable o status vero). Ricostruiamo l'errore per riusare
        # l'handler non-stream (status/trail/epiloghi identici).
        self.attempts_box.extend(_meta.get("attempts") or [])
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

    def _finish_nonstream(self):
        """Risposta non-stream: modello divulgato, note QC, stime, regalo -go, sniff, header."""
        self.data, self.used = self.res[0], self.res[1]
        self.qc_failed = self.res[2] if len(self.res) > 2 else []

        # Divulgazione del modello nel campo "model" della risposta
        # (policy.response_model, vedi app/policy.py): nx_deployment è SEMPRE
        # presente con il deployment univoco realmente usato.
        if isinstance(self.data, dict):
            self.disc = gw_state.router.policy.response_model
            if self.disc == "upstream":
                # nome ESATTO scritto dal provider nella sua risposta
                # (es. groq ritorna "meta-llama/llama-3.3-70b-instruct");
                # fallback al nome che noi inviamo se il campo manca/vuoto
                self.orig = self.data.get("model")
                self.data["model"] = self.orig if isinstance(self.orig, str) and self.orig.strip() else self.used["model"]
            elif self.disc == "deployment":
                self.data["model"] = self.used["unique"]
            else:  # requested (storico)
                self.data["model"] = self.raw_model
            self.data["nx_deployment"] = self.used["unique"]
        # nota QC nel reasoning (D3): solo se ci sono stati scarti e la policy
        # lo consente — il client che ignora reasoning_content non ne è toccato
        if self.qc_failed and self.qc_pol.annotate_reasoning and isinstance(self.data, dict):
            self.data = annotate_reasoning(self.data, self.qc_failed)
        self._u_f14 = _usage_of(self.data)
        # Sotto hold redirect (_redirect=True): il MOTORE STREAM ha GIA'
        # emesso _emit_summary (via _summary() in sse), note_estimate_error,
        # note_session_estimate e _note_fb_refund. Evitiamo duplicazione.
        if not self._redirect:
            try:
                if self._u_f14 and self._u_f14.get("prompt_tokens"):
                    gw_state.router.note_estimate_error(self.used["unique"], self.ctx_est, self._u_f14["prompt_tokens"])
                    # Stima per-sessione: char REALI del payload inviato a monte
                    # (post inject_identity/histnorm/ctxcompact) / prompt_tokens.
                    gw_state.router.note_session_estimate(
                        self.session_id,
                        self._est_chars_pre,
                        _prompt_chars(self.payload.get("messages"), self.payload.get("tools")),
                        self._u_f14["prompt_tokens"],
                    )
                    metrics.inc("nx_sess_est_samples_total")
            except Exception:
                report_suppressed("main.chat_completions")
            _emit_summary(
                ses=self.session_id or "-",
                req=self.raw_model,
                grp=self.used.get("group"),
                dep=self.used["unique"],
                tries=max(1, len(self.attempts_box)),
                fb=max(0, len(self.attempts_box) - 1),
                dur_ms=int((time.monotonic() - self.t_req) * 1000),
                stream=False,
                qc=bool(self.qc_failed),
                wd=None,
                usage=self._u_f14,
            )
        # Regalo -go per i fallback (#50): SOLO quando il non-stream ha servito
        # direttamente (con hold ON il motore stream ha gia' regalato: la richiesta
        # non-stream vi viene rediretta e il suo summary farebbe doppio regalo).
        if not self._redirect:
            _note_fb_refund(gw_state.router, self.session_id, max(0, len(self.attempts_box) - 1))
        if sniff.enabled(gw_state.router.policy):
            sniff.begin(
                self._rid,
                {
                    "model": self.raw_model,
                    "canonical": self.model,
                    "profile": self.profile,
                    "session": self._sess or "-",
                    "client_ip": self._cip,
                    "need": sorted(self.need),
                    "dep": self.used["unique"],
                    "stream": False,
                },
                self.payload,
            ).finish_json(self.data, {"status": "success", "tries": max(1, len(self.attempts_box)), "qc_failed": bool(self.qc_failed)})
        if self._ctx_saved_hdr:
            self.response.headers["X-Ctxcompact-Saved"] = str(self._ctx_saved_hdr)
        return self.data


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, response: Response):
    return await _ChatCompletion(request=request, response=response).run()
