"""scrocco-llm — gateway LLM OpenAI-compatible multi-provider (FastAPI :4001).

[EN] WHAT: exposes /v1/chat/completions (+ images/tts/stt/videos, models,
metrics) and routes each request to the best deployment across dozens of
provider accounts. HOW: auth -> alias canonicalization -> context estimate ->
dims/capability group -> adaptive pick -> forwarder with chain fallback ->
QC/watchdog -> response (stream or JSON). Every request emits a [summary] log
line. Full lifecycle: docs/ARCHITECTURE.md.

[IT] COSA: espone /v1/chat/completions (+ images/tts/stt/videos, models,
metrics) e instrada ogni richiesta al deployment migliore tra decine di
account/provider. HOW: auth -> canonicalize alias -> stima contesto ->
gruppo dims/capacita -> pick adattivo -> forwarder con fallback a catena
-> QC/watchdog -> risposta (stream o JSON). WHY le decisioni chiave:
  - routing per CONTESTO STIMATO (-32k..-1000k): nei free-tier le finestre
    sono piccole; serve il modello minimo che CI STA, non il migliore
    assoluto (che rifiuterebbe o taglierebbe).
  - gruppi capacita strutturali (-vision/-tts/...): un fallback di scopo
    non deve mai atterrare su un modello senza la capac richiesta.
  - sticky session SOLO dal routing automatico: gli espliciti sono legge.
  - watchdog passivo sullo stream (tier1 vuoto/error, tier2 no-[DONE]):
    non tocca i byte, rileva solo upstream mezzi morti.
  - [summary] per-richiesta: osservabilita senza grep sparsi.
Doc vivente: docs/AGENT.md (day-2), docs/BOOTSTRAP.md (setup),
GET /admin/guide e GET /bootstrap (serviti dal gateway stesso).

[EN] WHAT: OpenAI-compatible gateway fanning requests across many provider
accounts. HOW: auth -> alias -> ctx estimate -> capability group ->
adaptive pick -> chained fallback -> QC/watchdog. WHY: minimum-context
routing beats best-model routing on free tiers; purpose-aware fallback;
passive stream watchdog; per-request summary logs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .admin import admin_api
from .suppressed import report_suppressed
from .bootstrap import bootstrap_api
from .auth import AuthManager, AuthResult, gateway_env
from . import cluster
from . import metrics
from . import imagestore  # noqa: F401 - ri-esportato
from . import capmeta  # noqa: F401 - ri-esportato
from .config import GatewayConfig
from . import sniff
from .chat_helpers import (
    _apply_go_refund,
    _cached_tokens_of,
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
from .probes import (
    _drain_probe_tasks,
    _probe_late_open,
    _spawn_probe,
    _spawn_wake_sweep,
)
from .stream_verdicts import (
    _actionable_upstream_error,
    _discard_stream,
    _exhausted,
    _parachute_verdict,
    _payload_text_empty,
    _retry_at_ms,
    _soft_cd,
)
from .image_helpers import (
    _download_remote_image,  # noqa: F401 - ri-esportato
    _image_chat_intercept,
    _profile_of_request,
)
from . import repairlog
from . import autoprobe
from . import sttscrub
from . import sttchat
from . import forwarder as fwd
from .forwarder import (
    Forwarder,
    UpstreamError,
    StreamLoopDetected,
    _MODEL_MISSING_RE,
    _PAYLOAD_SCHEMA_RE,
    _UNKNOWN_FIELD_RE,
    _length_truncated_should_fail,
    _CONTENT_ARRAY_RE,
    tool_combo_signature,
    _PROVIDER_TRANSIENT_RE,
    _THOUGHT_SIG_RE,
    is_provider_error_body,
    is_provider_fault_body,
    media_reject_signature,
    media_input_needed,
    media_modality_signature,
    _looks_context_limit,
    extract_requested_tokens,
    _client_attribution,
    _QUOTA_EXHAUSTED_RE,
    parse_quota_reset_seconds,
    _QUOTA_RESET_RE,
    set_retry_after_floors,
    set_stream_stall_sec,
    set_strip_client_fields,
    set_adaptive_timeout,
    set_latency_lookup,
    set_reasoning_reserve,
    apply_cooldown_policy,
    set_estimate_defaults,
    set_ttft_lookup,
    set_nonstream_hook,
    set_stall_bucket,
    set_schemaout_config,
    maybe_quarantine_ban,
    maybe_host_transient_cooldown,
    note_context_limit,
    repair_reasoning_error,
    reasoning_err_kind,
    classify_error_class,
    dep_host,
    is_provider_level,
    restore_reasoning,
    is_unclear_error,
    maybe_account_quota_cooldown,
)
from .csvlearn import learn_thinking_replay, learn_strip_reasoning, learn_no_thinking, learn_content_string
from .health import health_loop
from .policy import Policy, refill_out_budget
from .histnorm import flatten_text_content
from .qc import annotate_reasoning
from .thought_sig import has_unsigned_tool_calls, reset_request_flags, set_avoid_gemini, set_dummy_fill
from .router import Router, inject_identity, estimate_tokens, configure_estimate, _prompt_chars
from .caution import background_cautious_enabled
from .opencode_gate import (
    opencode_cautious_request,
    is_opencode_zen_dep,
)
from .capabilities import (
    required_caps,
    count_image_parts,
    wants_image_output,
    _is_image_part,
    count_audio_parts,
)
from .effort import set_effort, effort_from_request
from .errors import AppError

from .sse_utils import (
    _sse_data_objs,
    _merge_qc_tool_calls,
    _strip_sse_content,
    _collapse_sse_field,
    _collapse_sse_content,
    _rewrite_sse_tool_calls,
    _delta_has_content,  # noqa: F401 - ri-esportato
    _answer_chars,
    _delta_has_answer,  # noqa: F401 - ri-esportato
    _chunk_finish_reason,  # noqa: F401 - ri-esportato
    _tool_calls_sse,
    _buffered_answer_text,
    _peek_stream,
)

# Logging strutturato: [auth] [route] [vigile] [identity] [fallback] [cooldown]
# basicConfig è no-op se root ha già handler (es. sotto pytest/caplog).
# Formato con colori per terminali (ANSI escape codes)
from app.terminal_logging import (
    setup_colored_logging,
)

console_handler = setup_colored_logging()

logging.basicConfig(level=logging.INFO, handlers=[console_handler], force=True)  # Override any existing basicConfig
log = logging.getLogger("nx.main")

from .constants import GATEWAY_VERSION  # noqa: E402
from .toolrepair import create_tool_repair_config  # noqa: E402 - usato da _stream_with_fallback
from .fakecall import fake_config_from_policy, is_escalation_group, looks_like_fake_tool_call, TemplateTokenStripper  # noqa: E402 - usato da _stream_with_fallback
from .texttoolparse import text_config_from_policy, parse_text_toolcalls, strip_toolid_markup, truncation_config_from_policy  # noqa: E402 - usato da _stream_with_fallback
from .sampling import sampling_config_from_policy  # noqa: E402 - usato da _stream_with_fallback
from .schemaout import enforce_response, schemaout_config_from_policy  # noqa: E402 - usato da _stream_with_fallback
from .forwarder import _corrective_note, _corrective_kind  # noqa: E402 - usato da _stream_with_fallback
from .protocols import sse_to_chat_obj as _sse2obj  # noqa: E402 - usato da _stream_with_fallback
from .toolrepair import repair_tool_calls as _rep_tc, sanitize_response as _san_resp  # noqa: E402 - usato da _stream_with_fallback
from .qc import check_response, check_sanity  # noqa: E402 - usato da _stream_with_fallback
from . import state as gw_state  # noqa: E402 - stato runtime condiviso (vedi app/state.py)

BASE_DIR = Path(__file__).resolve().parent.parent
# I DATI (credenziali + policy) vivono in var/: directory bind-montata nel
# container Docker, così l'admin API scrive i file VERO dell'host.
gw_state.VAR_DIR = BASE_DIR / "var"
gw_state.CSV_PATH = Path(os.environ.get("GATEWAY_CSV", gw_state.VAR_DIR / "keys_rotation.csv"))
gw_state.POLICY_PATH = Path(os.environ.get("GATEWAY_POLICY", gw_state.VAR_DIR / "gateway.yaml"))
gw_state.PORT = int(os.environ.get("GATEWAY_PORT", "4001"))
# 127.0.0.1 di default (loopback-only); nel container vale 0.0.0.0
HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
WATCH_SECONDS = float(os.environ.get("GATEWAY_WATCH_SECONDS", "5"))


def _install_file_logging() -> None:
    """Handler su FILE oltre allo stdout (docker logs resta invariato).

    - var/gateway.log  : tutto il log INFO (bind-montato -> sopravvive al
      redeploy del container, dove lo stdout viene perso).
    - var/error-audit.log : SOLO i body upstream con "error" (logger
      nx.erroraudit, alimentato da forwarder.UpstreamError + le righe
      PASS-THROUGH). File LOCALE, gitignored (var/*), da rivedere ogni tanto.
    Fail-safe: se un path non e' scrivibile si prosegue col solo stdout.
    Saltato sotto pytest (PYTEST_CURRENT_TEST) per non sporcare il repo.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    from logging.handlers import RotatingFileHandler, WatchedFileHandler

    def _file_handler(path: str) -> logging.Handler:
        # Multi-worker: stessi file per tutti i processi, ma ruota SOLO il
        # leader; gli altri appendono e riaprono il file quando e' stato
        # ruotato (WatchedFileHandler), senza rinominarlo in parallelo.
        if cluster.enabled() and not cluster.is_leader():
            return WatchedFileHandler(path, encoding="utf-8")
        return RotatingFileHandler(path, maxBytes=mb * 1024 * 1024, backupCount=bk, encoding="utf-8")

    # Same format as console for consistency
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    mb = int(os.environ.get("GATEWAY_LOG_MAX_MB", "20"))
    bk = int(os.environ.get("GATEWAY_LOG_BACKUPS", "5"))
    main_path = os.environ.get("GATEWAY_LOG_FILE", str(gw_state.VAR_DIR / "gateway.log"))
    audit_path = os.environ.get("GATEWAY_ERROR_LOG_FILE", str(gw_state.VAR_DIR / "error-audit.log"))
    try:
        h = _file_handler(main_path)
        h.setFormatter(logging.Formatter(fmt))
        h.setLevel(logging.INFO)
        logging.getLogger().addHandler(h)
    except OSError as exc:  # noqa: BLE001
        log.warning("[log] file %s non scrivibile (%s): solo stdout", main_path, exc)
    try:
        ah = _file_handler(audit_path)
        ah.setFormatter(logging.Formatter(fmt))
        ah.setLevel(logging.INFO)
        eaudit = logging.getLogger("nx.erroraudit")
        eaudit.addHandler(ah)
        eaudit.propagate = True  # va anche in gateway.log/stdout
    except OSError as exc:  # noqa: BLE001
        log.warning("[log] file %s non scrivibile (%s)", audit_path, exc)


_install_file_logging()

# La POLITICA (gateway.yaml) è separata dalle CREDENZIALI (keys_rotation.csv):
# file assente/corrotto -> default, il servizio parte comunque.
# I nomi pubblici usano policy.proxy_prefix: nessun nome è hardcodato qui.
gw_state.policy = Policy.load_or_default(gw_state.POLICY_PATH)
# Debug SNIFF: handler con rotazione oraria, default OFF. Registrato anche se
# disattivato (l'abilitazione e' live via policy/env) — nessun costo se spento.
sniff.configure(str(gw_state.VAR_DIR / "debug-sniff.log"), gw_state.policy.debug_sniff_retention_hours)
# Ledger persistente delle riparazioni tool-call (log a schermo + JSONL).
repairlog.configure(str(gw_state.VAR_DIR))
gw_state.config = GatewayConfig(
    gw_state.CSV_PATH,
    proxy_prefix=gw_state.policy.proxy_prefix,
    go_suffix=gw_state.policy.go_suffix,
    fallback_suffix=gw_state.policy.fallback_suffix,
    extra_prefixes=gw_state.policy.legacy_prefixes,
)
gw_state.router = Router(gw_state.config, gw_state.policy)
# provider callable: l'hot-reload della policy aggiorna anche le chiavi client
gw_state.authn = AuthManager(gw_state.config, client_keys_provider=lambda: gw_state.policy.client_keys)
gw_state.forwarder = Forwarder(keepalive_pool=gw_state.policy.http_keepalive_pool)
set_retry_after_floors(gw_state.policy.retry_after_min_sec, gw_state.policy.retry_after_floor_by_provider)
set_stream_stall_sec(gw_state.policy.stream_stall_sec)
set_strip_client_fields(gw_state.policy.strip_client_fields)
set_latency_lookup(lambda u, ctx=None: gw_state.router.bucket_latency_ms(u, ctx))
# F21: lo stall guard si calibra sul TTFT per bucket e sul moltiplicatore/
# tetto di policy; F20: divisore+immagini condivisi per le stime "senza router".
set_ttft_lookup(lambda u, ctx=None: gw_state.router.bucket_latency_ms(u, ctx, "ttft"))
set_stall_bucket(multiplier=gw_state.policy.stream_stall_ttft_mult, max_sec=gw_state.policy.stream_stall_max_sec)
set_estimate_defaults(gw_state.policy.estimate_divisor, getattr(gw_state.policy, "image_token_estimate", 0) or 0)


# P4: i dep che IGNORANO stream:true vengono annotati (json_fallback++) e poi
# esclusi dai canary: un non-streaming non puo' vincere la gara.
def _note_json_fallback(_u):
    try:
        if _u:
            gw_state.router.note_json_fallback(_u)
    except Exception:  # noqa: BLE001
        report_suppressed("main._note_json_fallback")


set_nonstream_hook(_note_json_fallback)
set_adaptive_timeout(
    enabled=gw_state.policy.adaptive_timeout_enabled,
    floor_sec=gw_state.policy.adaptive_timeout_floor_sec,
    multiplier=gw_state.policy.adaptive_timeout_multiplier,
    max_sec=gw_state.policy.adaptive_timeout_max_sec,
)
set_reasoning_reserve(1.0 - float(getattr(gw_state.policy, "cache_ctx_reasoning_headroom_ratio", 0.7) or 0.0))
apply_cooldown_policy(gw_state.policy)
from .schemaout import schemaout_config_from_policy as _so_cfg_from_policy

set_schemaout_config(_so_cfg_from_policy(gw_state.policy))
configure_estimate(
    adaptive=gw_state.policy.estimate_adaptive_enabled,
    shadow=gw_state.policy.estimate_adaptive_shadow,
    auto_enable=gw_state.policy.estimate_adaptive_auto_enable,
    auto_min_n=gw_state.policy.estimate_adaptive_auto_min_n,
    auto_max_delta_pct=gw_state.policy.estimate_adaptive_auto_max_delta_pct,
)

_watch_task: asyncio.Task | None = None
_health_task: asyncio.Task | None = None
_nightly_task: asyncio.Task | None = None
gw_state._stats_file = gw_state.VAR_DIR / "adaptive_stats.json"
gw_state._last_stats_save = 0.0
# Persistenza DEDICATA dei cooldown (var/cooldown_state.json): a differenza
# di adaptive_stats salva anche `since`/`full`, cosi' dopo un restart il
# probe/decay ripartono con l'eta' reale e la durata totale.
gw_state._cooldown_file = gw_state.VAR_DIR / "cooldown_state.json"
gw_state._last_cooldown_save = 0.0
# Throttle DEDICATO delle firme Gemini: condividere _last_stats_save (appena
# aggiornato dallo stesso tick del watcher) le faceva salvare solo allo shutdown.
gw_state._last_thought_sigs_save = 0.0
# WARM-START DI ROUTING (var/routing_state.json): holder cache, sticky,
# ownership warm, demote per-sessione, pin escalation e watermark ctxcompact.
# Senza questo, ogni deploy ripartiva freddo: ri-rotazioni, ri-escalations e
# — per il ctxcompact — frontiera regredita che ri-invalidava le cache.
# Multi-worker: stato per-sessione -> un file per worker (app/cluster.py).
gw_state._routing_file = cluster.per_worker_path(gw_state.VAR_DIR / "routing_state.json")
gw_state._last_routing_save = 0.0
# i TEST settano GATEWAY_PERSIST_ROUTING=0: nessuna contaminazione col live
gw_state.PERSIST_ROUTING = os.environ.get("GATEWAY_PERSIST_ROUTING", "1") != "0"
# job video asincroni (OR-style): job_id -> snapshot deployment per poll/content.
# MAPPING IN MEMORIA con TTL: al restart i job in corso si perdono -> 404 con hint.
gw_state._videos_jobs = {}
gw_state.VIDEO_JOB_TTL_SEC = 24 * 3600


# i TEST settano GATEWAY_PERSIST_STATS=0: nessuna contaminazione col live
gw_state.PERSIST_STATS = os.environ.get("GATEWAY_PERSIST_STATS", "1") != "0"

# Ledger usage/costi (Feature: /admin/insights). Stessa env dei test per non
# sporcare var/ reale durante la suite.
from .ledger import Ledger as _Ledger

gw_state.LEDGER = _Ledger(gw_state.VAR_DIR)

# Evidenza persistente salute chiavi (lifecycle dead/retired, no-delete).
from .keyhealth import KeyHealth as _KeyHealth

gw_state.KEYHEALTH = _KeyHealth(gw_state.VAR_DIR)

# --- Inflight request coalescing (payload identico, solo non-streaming) ---
# Se due richieste identiche (stesso payload + stesso profilo) sono in volo,
# solo la prima interroga l'upstream; le altre attendono e ricevono la stessa
# risposta (deepcopy). Un retry automatico identico al 100% non spreca quota.
gw_state._inflight_coalesce = {}
gw_state._inflight_lock = asyncio.Lock()
# Finestra POST-risposta (request_coalescing_cache_sec): lo stesso payload
# arrivato entro N secondi riceve la risposta gia' prodotta, senza ripetere
# la chiamata upstream. Cache SOLO successi non-stream, cap 64 entry.
gw_state._coalesce_cache = {}
gw_state._COALESCE_CACHE_MAX = 64


# Import statico (non late/break-cycle): _apply_misc_policy e' chiamata QUI
# a livello di modulo, prima che `app` esista.
from .runtime_persistence import _apply_misc_policy  # noqa: E402

_apply_misc_policy(gw_state.policy)


# --- thought_signature sidecar: persistenza firme Gemini 3 (tool calling) ----
gw_state._thought_sigs_file = cluster.per_worker_path(gw_state.VAR_DIR / "thought_sigs.json")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _watch_task, _health_task, _nightly_task
    # I tre task sono SEMPRE legati (None finche' non creati): il blocco
    # `finally` li referenzia anche in modalita' CAUTA, dove health/nightly
    # NON vengono avviati. Senza questo, `_health_task` resterebbe una locale
    # non associata -> UnboundLocalError allo shutdown.
    _watch_task = _health_task = _nightly_task = None
    # Fail-fast in produzione: master key reale + client_keys esplicite.
    # In development (default) e' un no-op.
    gw_state.authn.enforce_startup()
    log.info("[start] env=%s production=%s", gateway_env(), gw_state.authn.production)
    _load_adaptive_stats()  # F4: ripristino EMA/cooldown
    _load_cooldowns()  # cooldown NON scaduti (since/full)
    _load_routing_state()  # warm-start: holder/sticky/warm/pin/frontiere
    _bootstrap_runtime_from_logs()  # finestre 24h uso/probe dal log
    _load_thought_sigs()  # firme Gemini: sopravvivono al restart
    _maybe_save_adaptive_stats(force=True)  # baseline subito
    # Multi-worker: da qui le osservazioni globali si replicano sugli altri
    # worker (dopo i load: lo stato letto da disco e' gia' comune a tutti).
    await cluster.start(gw_state.router, gw_state.KEYHEALTH)
    _watch_task = asyncio.create_task(_watcher(WATCH_SECONDS))
    # Battito di vita per l'HEALTHCHECK Docker (vedi app/liveness.py): prova
    # che il loop gira anche quando e' troppo carico per rispondere a /healthz.
    _heartbeat_task = asyncio.create_task(heartbeat_loop())
    _cautious = background_cautious_enabled()
    if _cautious:
        log.warning(
            "[start] modalita' CAUTA generica (BACKGROUND_CAUTIOUS): probe/health/nightly automatici DISATTIVATI"
        )
    elif cluster.is_leader():  # multi-worker: lavori unici solo sul worker 0
        _health_task = asyncio.create_task(health_loop(gw_state.router, gw_state.policy.health_interval_sec))
        _nightly_task = asyncio.create_task(_nightly_scheduler())
    log.info(
        "[start] %s su %s:%d · profili=%s · deployment=%d",
        gw_state.policy.service_name,
        HOST,
        gw_state.PORT,
        ",".join(gw_state.config.profiles),
        sum(len(v) for v in gw_state.config.groups.values()),
    )
    try:
        yield
    finally:
        for task in (_watch_task, _health_task, _nightly_task, _heartbeat_task):
            if task:
                task.cancel()
        # Graceful shutdown: uvicorn ha gia' smesso di accettare nuove
        # richieste; attendiamo il drain di quelle in volo (best-effort, con
        # deadline) prima del flush finale, per non troncare risposte.
        try:
            _drain = float(getattr(gw_state.policy, "shutdown_drain_sec", 0.0) or 0.0)
            # (multi-worker: solo le richieste servite da QUESTO processo)
            _infl = cluster.local_inflight(gw_state.router)
            if _infl:
                log.info("[shutdown] drain di %d richieste in volo (max %.1fs)...", _infl, _drain)
            _deadline = time.monotonic() + _drain
            while cluster.local_inflight(gw_state.router) > 0 and time.monotonic() < _deadline:
                await asyncio.sleep(0.2)
            _left = cluster.local_inflight(gw_state.router)
            if _left:
                log.warning("[shutdown] drain scaduto: %d richieste ancora in volo", _left)
            elif _infl:
                log.info("[shutdown] drain completato")
        except Exception:  # noqa: BLE001
            report_suppressed("main.lifespan.drain")
        # Task di background (canary/probe/sveglie): vanno cancellati PRIMA di
        # chiudere il client httpx, altrimenti i probe in volo esplodono sul
        # client chiuso, sporcano lo shutdown e possono far saltare il
        # salvataggio atomico finale.
        try:
            _np = await _drain_probe_tasks()
            if _np:
                log.info("[shutdown] cancel di %d probe/sveglie in volo", _np)
        except Exception:  # noqa: BLE001
            report_suppressed("main.lifespan.probe_tasks")
        await gw_state.forwarder.aclose()
        await cluster.stop()
        _maybe_save_all(force=True)  # F26: stats+routing insieme
        _maybe_save_cooldowns(force=True)  # cooldown: salva allo shutdown
        _maybe_save_thought_sigs(force=True)  # firme Gemini: salva allo shutdown
        _rows = gw_state.LEDGER.flush_sync()  # ledger: nessuna riga persa
        log.info("[shutdown] ledger flush_sync: %d righe salvate", _rows)
        _rrows = repairlog.flush_sync()  # ledger riparazioni
        if _rrows:
            log.info("[shutdown] repair flush_sync: %d righe salvate", _rrows)


app = FastAPI(title=gw_state.policy.service_name, version=GATEWAY_VERSION, lifespan=lifespan)
app.include_router(admin_api)
app.include_router(bootstrap_api)

# --- Observability: Trace ID, JSON logging, Prometheus /metrics ---
from .observability import (
    setup_observability,
    setup_replay_endpoint,
    render_prometheus,  # noqa: F401 - ri-esportato
)

# Porta di ammissione del processo (tetto globale richieste/stream LLM):
# aggiunta PRIMA dell'osservabilita' -> il trace ID resta il middleware piu'
# esterno e copre anche un eventuale 503 di coda.
from .admission import AdmissionMiddleware  # noqa: E402
from .liveness import heartbeat_loop  # noqa: E402

app.add_middleware(AdmissionMiddleware, policy_getter=lambda: gw_state.router.policy)

_obs_enabled = os.environ.get("GATEWAY_OBSERVABILITY", "1").strip() != "0"
if _obs_enabled:
    setup_observability(
        app,
        enable_json_logging=os.environ.get("GATEWAY_JSON_LOGGING", "0").strip() == "1",
        enable_trace_id=True,
        enable_prometheus=True,
    )
    setup_replay_endpoint(app)


# ------------------------------------------------------------------ Exception handlers (Blocco 1: Refactoring errori globali)
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    """Gestione centralizzata delle eccezioni AppError e sottoclassi."""
    # 5xx -> ERROR (server-side, visibilita' massima), 4xx -> WARNING
    # (client-side ma anomalia). Mai DEBUG: a INFO il root logger e il
    # file handler silenziano completamente i log DEBUG.
    _log_fn = log.error if exc.status >= 500 else log.warning
    _log_fn(
        "[error-handler] %s %s -> %d %s: %s", request.method, request.url.path, exc.status, exc.error_type, exc.message
    )
    return JSONResponse(
        status_code=exc.status, content={"error": {"message": exc.message, "type": exc.error_type, "code": exc.code}}
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    """Gestione errori non catturati (fallback generico)."""
    log.warning("[error-handler] Unhandled error %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500, content={"error": {"message": "errore interno", "type": "server_error", "code": "500"}}
    )


from .http_responses import forbidden as _forbidden  # noqa: E402
from .http_responses import unauthorized as _unauthorized  # noqa: E402


# ---------------------------------------------------------- chat completions


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, response: Response):
    _api_log = logging.getLogger("nx.api")
    try:
        payload = await request.json()
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
    from .sampling import sampling_config_from_policy, apply_sampling_defaults
    from .schemaout import schemaout_config_from_policy, maybe_inject_response_format

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
    from .ctxcompact import ctxcompact_config_from_policy, compact_tool_outputs, should_compact, frontier_boundary

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


async def _hedge_peek(
    dep,
    gen,
    t_att,
    fc_ms,
    incl_reason,
    min_ch,
    hold_idle,
    hold_maxb,
    *,
    payload,
    profile,
    need,
    scope,
    ctx,
    tried_set,
    attempts,
    requested_group,
    session,
    client_ip,
    attribution,
    hedge_ms,
    _tr_cfg,
    _tct_cfg,
    k: int = 1,
    slow_race_ms: int = 0,
    slow_canary_ms: int = 0,
    fresh_only: bool = False,
    hold: bool = False,
    refill: bool = False,
    zen_only: bool = False,
    out_tokens: int | None = None,
    raced: dict | None = None,
):
    """HEDGE sul primo contenuto (stream, pre-commit).

    A e' gia' aperto; se dopo `hedge_ms` non ha ancora un verdetto si aprono
    fino a `k` canary **nuovi** (tier crescenti, mai bucket pagati, mai sotto
    il floor di dim richiesto; con `fresh_only=True` si escludono i caldi
    della sessione e si preferiscono i meno usati) e corrono tutti. Vince chi
    IMPEGNA contenuto.

    I perdenti NON vengono MAI cancellati (regola del warm-refill): restano
    in volo come PROBE REALI fino alla fine della generazione. Se consegnano
    una risposta piena e pulita entrano nel warm della sessione
    (`note_warm_owner`); altrimenti si scartano senza nessuna punizione. Il
    double-cost e' accettato per costruzione: solo free-dims.

    `refill=True` (warm-refill a cascata): invece dei canary cross-tier si
    lancia UN solo candidato nuovo (libero da qualsiasi sessione, api_key
    diversa, stesso tier prioritario, output assicurato) e la gara parte
    anche se A sta gia' streammando: con il hold il verdetto 'content' di A
    arriva solo a chiusura, quindi un canary che chiude prima e' davvero la
    risposta piu' veloce da consegnare.

    Ritorna i valori di (dep, gen, t_att, verdict, prebuf, pending, meta) del
    vincente."""

    def _peek(g, fcm, fb=None):
        return _peek_stream(
            g,
            fcm,
            incl_reason,
            min_ch,
            hold_until_finish=hold,
            hold_idle_ms=hold_idle,
            hold_max_bytes=hold_maxb,
            first_byte=fb,
        )

    firstA = asyncio.Event() if hold else None
    futA = asyncio.ensure_future(_peek(gen, fc_ms, firstA))
    waiter = asyncio.ensure_future(firstA.wait()) if hold else None
    done, _pending = await asyncio.wait(
        ({futA, waiter} if waiter is not None else {futA}), timeout=max(0.05, hedge_ms / 1000.0)
    )
    if futA in done:
        if waiter is not None:
            waiter.cancel()
        return dep, gen, t_att, *await futA
    # ---- DUE TRIGGER INDIPENDENTI --------------------------------------
    # (1) HEDGE CLASSICO (invariato): se A non ha ancora emesso NIENTE si
    #     aprono i canary classici/di refill; se A sta GIA' streammando lo si
    #     lascia finire (in hold e' la norma: risposta lunga, nessuna gara
    #     inutile). In refill si gareggia comunque (winner solo pre-byte).
    # (2) GARA LENTA: timer indipendente a `slow_race_ms` dall'inizio del
    #     TENTATIVO di A. Se A non ha ancora CONSEGNATO (hold: verdetto solo
    #     a chiusura) apre UN canario (fuori dal tetto per-sessione) e lo
    #     mette in gara. Nessuna penalita' per il "lento": A e i perdenti
    #     restano probe reali.
    _slow_dl = (t_att + slow_race_ms / 1000.0) if slow_race_ms > 0 else None
    # TIMING DEL CANARY separato dal FLAG lento: `slow_canary_ms` apre il
    # canario, `slow_race_ms` marca il dep "lento per la sessione". Con
    # `slow_canary_ms <= 0` il canario resta appeso alla soglia del flag
    # (storico: i due scattano insieme).
    _canary_dl = (t_att + slow_canary_ms / 1000.0) if slow_canary_ms > 0 else _slow_dl
    _a_streaming = bool(waiter is not None and waiter.done())
    # con A gia' in streaming (e fuori refill) NON si aprono canary classici:
    # si arma solo il timer lento.
    _skip_classic = bool(_a_streaming and not refill)
    if _a_streaming and not refill and _slow_dl is None and _canary_dl is None:
        # A ha emesso byte ma non ha chiuso e la gara lenta e' spenta: si
        # aspetta A (comportamento storico).
        return dep, gen, t_att, *await futA
    if waiter is not None:
        waiter.cancel()
    # ---- candidati NUOVI per la gara -----------------------------------
    _W = None
    try:
        if refill:
            _wk = gw_state.router.warm_api_keys(session, profile, requested_group or dep.get("group"))
            _xk = set((raced or {}).get("keys") or ()) | _wk
            _xk.add(str(dep.get("api_key") or ""))
            _ex_uniq = (raced or {}).get("uniq")
            _B = gw_state.router.warm_fill_canary(
                profile,
                dep,
                need,
                ctx,
                out_tokens,
                tried=tried_set,
                requested_group=requested_group,
                exclude_keys=_xk,
                exclude_uniq=_ex_uniq,
                only_zen=False,
            )
            if _B is not None:
                log.info(
                    "[refill] canario %s per %s (order=%s, chiavi warm+in-volo escluse=%d, out=%s)",
                    _B["unique"],
                    dep.get("unique"),
                    _B.get("order"),
                    len(_xk),
                    out_tokens,
                )
            else:
                log.info(
                    "[refill] %s: nessun canario free consegnabile (chiavi escluse=%d)", dep.get("unique"), len(_xk)
                )
            # TERZO canario: la SVEglia. Cerca un dep dormiente da un 429 da
            # ALMENO 1h (regola utente) e prova a rimetterlo caldo.
            if int(k) > 1:
                _exu2 = set(_ex_uniq or ())
                _xk2 = set(_xk)
                if _B is not None:
                    _exu2.add(_B["unique"])
                    _xk2.add(str(_B.get("api_key") or ""))
                try:
                    _age = float(getattr(gw_state.router.policy, "warm_refill_wake_min_cooldown_age_sec", 3600.0) or 3600.0)
                except Exception:
                    _age = 3600.0
                _W = gw_state.router.warm_wake_canary(
                    profile,
                    dep,
                    need,
                    ctx,
                    out_tokens,
                    tried=tried_set,
                    requested_group=requested_group,
                    exclude_keys=_xk2,
                    exclude_uniq=_exu2,
                    only_zen=False,
                    min_age_sec=_age,
                )
                if _W is not None:
                    log.info("[refill] sveglia %s (429 in cooldown da almeno %.0fs)", _W["unique"], _age)
                else:
                    log.info("[refill] nessuna sveglia 429 matura")
            # CANARY ZEN DEDICATO (client opencode nativo senza zen in warm):
            # affianca il canary normale e cerca gli zen in TUTTE le dim del
            # profilo (non solo in quella richiesta, che puo' essere senza zen).
            _Z = None
            if zen_only:
                _exu3 = set(_ex_uniq or ())
                _xk3 = set(_xk)
                for _c in (_B, _W):
                    if _c is not None:
                        _exu3.add(_c["unique"])
                        _xk3.add(str(_c.get("api_key") or ""))
                try:
                    _Z = gw_state.router.warm_fill_canary(
                        profile,
                        dep,
                        need,
                        ctx,
                        out_tokens,
                        tried=tried_set,
                        requested_group=requested_group,
                        exclude_keys=_xk3,
                        exclude_uniq=_exu3,
                        only_zen=True,
                    )
                except Exception:  # noqa: BLE001
                    _Z = None
                if _Z is None:
                    try:
                        _age_z = float(
                            getattr(gw_state.router.policy, "warm_refill_wake_min_cooldown_age_sec", 3600.0) or 3600.0
                        )
                    except Exception:  # noqa: BLE001
                        _age_z = 3600.0
                    try:
                        _Z = gw_state.router.warm_wake_canary(
                            profile,
                            dep,
                            need,
                            ctx,
                            out_tokens,
                            tried=tried_set,
                            requested_group=requested_group,
                            exclude_keys=_xk3,
                            exclude_uniq=_exu3,
                            only_zen=True,
                            min_age_sec=_age_z,
                        )
                    except Exception:  # noqa: BLE001
                        _Z = None
                if _Z is not None:
                    log.info("[refill] zen-wake %s (dim=%s, order=%s)", _Z["unique"], _Z.get("group"), _Z.get("order"))
                else:
                    log.info("[refill] %s: nessun canary zen consegnabile (ctx=%s)", dep.get("unique"), ctx)
            cands = [c for c in (_Z, _B, _W) if c is not None]
        else:
            _excl = set(gw_state.router._sess_deps().get(session, ())) if fresh_only else None
            cands = gw_state.router.hedge_canaries(
                profile,
                dep,
                need,
                ctx,
                tried_set,
                requested_group,
                k=max(1, int(k)),
                exclude=_excl,
                fresh_only=bool(fresh_only),
                out_tokens=out_tokens,
            )
    except Exception:
        cands = []
    if _skip_classic:
        # A gia' in streaming: nessun canario classico, solo il timer lento.
        cands = []
    # TETTO per-sessione: apri solo i canari che stanno nel tetto (gli
    # in-volo contano tutti: refill, legacy, A/loser staccati come probe).
    if refill and cands:
        try:
            _mx = int(getattr(gw_state.router.policy, "warm_refill_max_inflight", 6) or 6)
        except Exception:
            _mx = 6
        try:
            _free = max(0, _mx - int(gw_state.router.probes_in_flight(session)))
        except Exception:
            _free = _mx
        cands = cands[:_free] if _free > 0 else []
    if not cands:
        metrics.inc("nx_hedge_total", ("no_canary",))
        log.debug(
            "[hedge] %s: nessun candidato nuovo (%s)", dep.get("unique"), "warm-lento" if fresh_only else "cross-tier"
        )
        if _slow_dl is None and _canary_dl is None:
            return dep, gen, t_att, *await futA

    async def _open_canary(B, wake=False):
        """Apre un canary e ne ritorna il record (o None se non disponibile:
        in tal caso la chiave va in cooldown con le regole di sempre)."""
        _bu = B["unique"]
        tB = time.monotonic()
        p2 = dict(payload)
        inject_identity(p2, B, router=gw_state.router)

        def _hookB(_salvaged, _u=_bu, _m=B.get("model", "")):
            metrics.inc("nx_truncated_toolcall_total", (_u, "salvaged" if _salvaged else "dropped"))
            repairlog.note(
                "salvage_truncated",
                source="hedge",
                outcome="ok" if _salvaged else "fail",
                dep=_u,
                model=_m,
                detail="tag tool-call rotto (canary)",
            )
            gw_state.router.mark_failed(_u, seconds=_tct_cfg.cooldown_sec, reason="truncated_toolcall")

        gw_state.router.note_start(_bu, ctx)
        if refill and raced is not None:
            raced.setdefault("uniq", set()).add(_bu)
            raced.setdefault("keys", set()).add(str(B.get("api_key") or ""))
        genB = None
        try:
            genB = await gw_state.forwarder.stream_response(
                B,
                p2,
                profile=profile or "",
                ctx_est=ctx,
                client_ip=client_ip,
                session=session,
                attribution=attribution,
                tool_repair_config=_tr_cfg,
                truncation_config=_tct_cfg,
                truncation_hook=_hookB,
                rate_hook=lambda u, rl: gw_state.router.note_rate_limit(u, rl),
            )
            if gw_state.router.is_cooled_down(_bu):
                gw_state.router.clear_cooldown(_bu)
            futB = asyncio.ensure_future(_peek(genB, gw_state.router.first_content_deadline_ms(_bu, ctx)))
            with contextlib.suppress(Exception):
                gw_state.router.note_probe_started(session, _bu)
            return {"dep": B, "gen": genB, "t0": tB, "fut": futB, "wake": bool(wake)}
        except asyncio.CancelledError:
            if genB is not None:
                await _discard_stream(genB, None)
            with contextlib.suppress(Exception):
                gw_state.router.note_end(_bu, ctx)
            raise
        except BaseException as exc:
            if genB is not None:
                await _discard_stream(genB, None)
            with contextlib.suppress(Exception):
                gw_state.router.note_end(_bu, ctx)
            # REGE (utente): un errore durante il canary va in cooldown
            # COME AL SOLITO: 429/5xx transitori -> soft cooldown calibrato
            # sui fallimenti 24h (retry_after numerico ha priorita').
            # ECCEZIONE: il rifiuto "replay del reasoning" e' un problema del
            # PAYLOAD (tutte le chiavi del provider lo rifiutano): la chiave
            # e' sana -> nessuna penale (la richiesta principale ripara).
            _rk = reasoning_err_kind(str(exc))
            _qacct = 0
            with contextlib.suppress(Exception):
                _qacct = maybe_account_quota_cooldown(gw_state.router, B, getattr(exc, "status", None), str(exc))
            if _qacct:
                log.info(
                    "[hedge] canary %s: quota dell'account esaurita -> %d chiavi dell'account in pausa fino al reset",
                    _bu,
                    _qacct,
                )
            elif _rk is not None:
                log.info(
                    "[hedge] canary %s: payload della famiglia reasoning (%s) (chiave sana, nessuna penale)", _bu, _rk
                )
                with contextlib.suppress(Exception):
                    if _rk == "needs":
                        learn_thinking_replay(gw_state.router, B.get("model"))
                    elif _rk == "rejects":
                        learn_strip_reasoning(gw_state.router, B.get("model"))
                    elif _rk == "history":
                        learn_no_thinking(gw_state.router, B.get("model"))
            else:
                try:
                    _sec = getattr(exc, "retry_after", None)
                    if not (isinstance(_sec, (int, float)) and _sec > 0):
                        try:
                            _f24 = gw_state.router.stats_for(_bu).fail_count_24h
                        except Exception:
                            _f24 = 0
                        _sec = _soft_cd(_f24)
                    gw_state.router.mark_failed(_bu, seconds=_sec, reason="canary_error")
                except Exception:
                    report_suppressed("main._hedge_peek._open_canary")
            log.info("[hedge] canary %s non disponibile (%s) -> cooldown", _bu, type(exc).__name__)
            return None

    # Apertura dei canari in PARALLELO e NON bloccante: `_open_canary` fa
    # `await forwarder.stream_response` (attende le HEADERS dell'upstream) e
    # con l'apertura sequenziale un provider lento bloccava il loop della
    # gara, rimandando/saltando il timer della gara lenta (e il `break` sul
    # vincitore scavalcava il check del timer). Ora ogni apertura e' un TASK:
    # le risoluzioni vengono raccolte DENTRO il loop, cosi' il timer scatta
    # SEMPRE a `slow_race_ms`.
    canaries: list[dict] = []
    _tasks: dict = {}
    for B in cands:
        _tasks[asyncio.ensure_future(_open_canary(B, wake=bool(_W is not None and B is _W)))] = None
    if not cands:
        if _slow_dl is None and _canary_dl is None:
            metrics.inc("nx_hedge_total", ("no_canary",))
            return dep, gen, t_att, *await futA
    else:
        log.info(
            "[hedge] %s: nessun contenuto dopo %dms -> gara con %s",
            dep.get("unique"),
            hedge_ms,
            ",".join(B.get("unique", "") for B in cands),
        )
    futs: dict = {futA: None}
    for c in canaries:
        futs[c["fut"]] = c
    results: dict = {}
    winner = None
    running = set(futs) | set(_tasks)
    _canary_opened = _canary_dl is None
    _slow_marked = _slow_dl is None
    while running:
        _pending_dls = []
        if not _canary_opened and _canary_dl is not None:
            _pending_dls.append(_canary_dl)
        if not _slow_marked and _slow_dl is not None:
            _pending_dls.append(_slow_dl)
        _to = max(0.0, min(_pending_dls) - time.monotonic()) if _pending_dls else None
        completed, pending = await asyncio.wait(running, timeout=_to, return_when=asyncio.FIRST_COMPLETED)
        # NB: esaminare TUTTI i completati del tick (non solo uno): se piu'
        # canari finiscono insieme, gli altri resterebbero con l'eccezione
        # non recuperata e il loro verdetto andrebbe perso.
        running = set(pending)
        _add: set = set()
        for f in completed:
            if f in _tasks:
                # apertura di un canary conclusa: raccogli il record (None =
                # non disponibile) e metti in gara la sua attesa.
                _tasks.pop(f, None)
                with contextlib.suppress(BaseException):
                    _c = f.result()
                    if _c is not None:
                        canaries.append(_c)
                        futs[_c["fut"]] = _c
                        _add.add(_c["fut"])
                continue
            if f in results:
                continue
            try:
                results[f] = f.result()
            except BaseException:
                results[f] = ("error", [], None, {})
            if results[f][0] == "content":
                winner = f
                break
        running |= _add
        if winner is not None:
            break
        _now_sr = time.monotonic()
        # ---- FLAG LENTO: alla soglia `slow_race_ms` il dep e' lento per la
        # sessione (indipendente dall'esito della gara e dal canary). Per il
        # canary c'e' un timer PROPRIO (`slow_canary_ms`).
        if not _slow_marked and _slow_dl is not None and _now_sr >= _slow_dl:
            _slow_marked = True
            # R3: il dep che ha fatto scattare il timer e' lento per la
            # sessione, indipendentemente dall'esito della gara (anche se poi
            # vince). Auto-pulito al primo successo rapido.
            with contextlib.suppress(Exception):
                gw_state.router.mark_session_slow(session, dep.get("unique"))
        # ---- CANARY LENTO: timer proprio scaduto -> UN canario in piu' ----
        # INDIPENDENTE dal tetto per-sessione e dall'hedge classico: il
        # "lento" non prende nessuna penale (resta probe reale).
        if not _canary_opened and _canary_dl is not None and _now_sr >= _canary_dl:
            _canary_opened = True
            _shown_ms = slow_canary_ms if slow_canary_ms > 0 else slow_race_ms
            log.info(
                "[slow-race] %s in generazione da %.0fs (> %.0fs) -> canario in gara",
                dep.get("unique"),
                _now_sr - t_att,
                _shown_ms / 1000.0,
            )
            # R2: gate — il canary si apre solo se la sessione ha pochi warm
            # (need+ctx+output, prestati inclusi); a warm pieno riempirebbe
            # la lista di altri lenti.
            _allow = True
            try:
                _allow = bool(
                    gw_state.router.slow_race_allowed(session, profile, requested_group, need, ctx, out_tokens, tried_set)
                )
            except Exception:  # noqa: BLE001
                _allow = True
            if not _allow:
                metrics.inc("nx_slow_race_total", ("warm_full",))
                log.info(
                    "[slow-race] %s: warm gia' pieno (>=%s), niente canario",
                    dep.get("unique"),
                    getattr(gw_state.router.policy, "slow_race_max_warm", 6),
                )
            else:
                metrics.inc("nx_slow_race_total", ("open",))
                _lc: list[dict] = []
                with contextlib.suppress(Exception):
                    _lc = gw_state.router.hedge_canaries(
                        profile,
                        dep,
                        need,
                        ctx,
                        tried_set,
                        requested_group,
                        k=1,
                        exclude=None,
                        fresh_only=False,
                        out_tokens=out_tokens,
                    )
                _xu = set((raced or {}).get("uniq") or ())
                _xk2 = set((raced or {}).get("keys") or ())
                for _cc in canaries:
                    _xu.add(_cc["dep"]["unique"])
                    _xk2.add(str(_cc["dep"].get("api_key") or ""))
                _lc = [B for B in _lc if B["unique"] not in _xu and str(B.get("api_key") or "") not in _xk2]
                if not _lc:
                    metrics.inc("nx_slow_race_total", ("no_canary",))
                    log.info("[slow-race] %s: nessun canario libero (chiavi/uniq in gara escluse)", dep.get("unique"))
                else:
                    # Apertura NON bloccante anche qui: il canario lento entra
                    # in gara appena arrivano le headers (task in coda).
                    _t2 = asyncio.ensure_future(_open_canary(_lc[0]))
                    _tasks[_t2] = None
                    running.add(_t2)
                    if raced is not None:
                        raced.setdefault("uniq", set()).add(_lc[0]["unique"])
                        raced.setdefault("keys", set()).add(str(_lc[0].get("api_key") or ""))
                    log.info("[hedge] slow-race: %s in gara con A (fuori dal tetto)", _lc[0]["unique"])
    if winner is None:
        # fallback: risolvi prima le aperture ancora in corso, poi attendi
        # tutti i verdetti (A compreso).
        for f in list(running):
            if f in _tasks:
                _tasks.pop(f, None)
                running.discard(f)
                with contextlib.suppress(BaseException):
                    _c = f.result() if f.done() else await f
                    if _c is not None:
                        canaries.append(_c)
                        futs[_c["fut"]] = _c
                        running.add(_c["fut"])
        for f in list(running):
            if f in results:
                continue
            try:
                results[f] = await f
            except BaseException:
                results[f] = ("error", [], None, {})
        winner = futA

    # REGOLA UTENTE: mai bloccare in volo e mai buttare via un canary — le
    # aperture ancora in corso NON vengono annullate: restano in background e,
    # appena arrivano le headers, la loro attesa entra in gara come probe reale
    # (chi consegna va in warm, anche se lento).
    def _handover_late(race):
        for _t in list(_tasks):
            _tasks.pop(_t, None)
            _pt = asyncio.ensure_future(_probe_late_open(_t, session, ctx, hold, race))
            gw_state._PROBE_TASKS.add(_pt)
            _pt.add_done_callback(gw_state._PROBE_TASKS.discard)

    # ---------------------------------------------------------- A vince ----
    if winner is futA:
        if not canaries:
            metrics.inc("nx_hedge_total", ("no_canary",))
        metrics.inc("nx_hedge_total", ("won_a",))
        _race = (dep["unique"], max(0.0, (time.monotonic() - t_att) * 1000.0))
        _handover_late(_race)
        for c in canaries:
            _spawn_probe(
                c["dep"],
                c["gen"],
                c["fut"],
                results.get(c["fut"]),
                session,
                ctx,
                hold,
                wake=bool(c.get("wake")),
                t0=c.get("t0"),
                race=_race,
            )
        _rA = results.get(futA)
        if _rA is None:
            try:
                _rA = await futA
            except BaseException:
                _rA = ("timeout", [], None, {})
        return dep, gen, t_att, *_rA
    # ------------------------------------------------- canary vince ---------
    metrics.inc("nx_hedge_total", ("won_b",))
    w = futs[winner]
    with contextlib.suppress(Exception):
        gw_state.router.note_probe_done(session, w["dep"]["unique"])
    if w.get("wake"):
        with contextlib.suppress(Exception):
            gw_state.router.clear_cooldown(w["dep"]["unique"])  # sveglia riuscita
        log.info("[refill] sveglia riuscita: %s torna caldo (consegna la risposta)", w["dep"]["unique"])
    # A NON viene annullata: finisce la sua risposta in background come probe
    # reale (se consegna pulita entra in warm, altrimenti si scarta).
    _race = (w["dep"]["unique"], max(0.0, (time.monotonic() - w["t0"]) * 1000.0))
    _handover_late(_race)
    _spawn_probe(dep, gen, futA, results.get(futA), session, ctx, hold, t0=t_att, race=_race)
    for c in canaries:
        if c is w:
            continue
        _spawn_probe(
            c["dep"],
            c["gen"],
            c["fut"],
            results.get(c["fut"]),
            session,
            ctx,
            hold,
            wake=bool(c.get("wake")),
            t0=c.get("t0"),
            race=_race,
        )
    attempts.append(w["dep"]["unique"])
    tried_set.add(w["dep"]["unique"])
    log.info(
        "[hedge] vince %s (A=%s e %d altri in volo come probe, non puniti)",
        w["dep"]["unique"],
        dep.get("unique"),
        len(canaries) - 1,
    )
    return w["dep"], w["gen"], w["t0"], *results[winner]


# ------------------------------------------------------- PROBE (warm-refill)
# Il perdente di una gara non viene MAI cancellato: finisce la risposta in
# volo come PROBE REALE. Se consegna una risposta piena e pulita entra nel
# warm della sessione (note_warm_owner, senza holder/reputazione); se sbaglia
# va in cooldown CON LE SOLITE LOGICHE (timeout -> lungo, errore/stream rotto
# -> corto), mentre il vuoto-pulito/length da budget resta senza penale come
# per il tentativo servito. Costo doppio accettato: la cascata pesca SOLO nei
# free-dims.
gw_state._PROBE_TASKS = set()


def _trim_chat_images(payload: dict, max_images: int) -> tuple[dict, int]:
    """Tetto alle immagini INVIATE all'upstream, in una copia del payload.

    Tiene le `max_images` piu' RECENTI, dando priorita' alle immagini del turno
    corrente (l'utente): se il turno corrente ne ha piu' del tetto, si tiene la
    parte piu' recente di quelle. Le piu' vecchi restano nella history del
    CLIENT (che non perde nulla) ma non vengono reinviate.

    Restituisce (payload_out, n_rimosse). Con `max_images <= 0` o zero
    immagini non tocca nulla e restituisce l'ORIGINALE (nessuna copia inutile).
    La stima del contesto continua a contarle tutte: restare conservativi
    sull'overflow vale piu' di ottimizzare i token."""
    if not max_images or max_images <= 0:
        return payload, 0
    messages = payload.get("messages") or []
    if not messages:
        return payload, 0
    # indice (msg_index, part_index) di ogni immagine, in ordine di arrivo
    spots: list[tuple[int, int]] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for j, part in enumerate(content):
            if isinstance(part, dict) and _is_image_part(part):
                spots.append((i, j))
    if len(spots) <= max_images:
        return payload, 0
    # da tenere: le ultime N in ordine, con le immagini dell'ULTIMO messaggio
    # (turno corrente) davanti a tutto il resto a parita' di recency.
    last_msg = max((i for i, _j in spots), default=-1)
    current = [s for s in spots if s[0] == last_msg]
    older = [s for s in spots if s[0] != last_msg]
    keep = set()
    for s in reversed(current):  # turno corrente, piu' recente
        if len(keep) >= max_images:
            break
        keep.add(s)
    for s in reversed(older):  # poi il passato, dal piu' recente
        if len(keep) >= max_images:
            break
        keep.add(s)
    drop = set(spots) - keep

    out_msgs: list = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            out_msgs.append(msg)
            continue
        parts = [p for j, p in enumerate(msg["content"]) if (i, j) not in drop]
        if len(parts) == len(msg["content"]):
            out_msgs.append(msg)
            continue
        out_msgs.append({**msg, "content": parts})
    out = dict(payload)
    out["messages"] = out_msgs
    return out, len(drop)


async def _stt_bridge_transcribe(
    request: Request, chunk: bytes, profile: str | None, raw_model: str, session_id: str | None, used: set[str]
) -> str:
    """Trascrive un chunk di audio. Ritorna "" SOLO dopo aver provato TUTTI i
    deployment con la capacita' `stt` del profilo.

    Ordine di tentativi (e' la regola della flotta, non una scelta locale):
      1) free vivi          2) free in cooldown
      3) -go vivi           4) -go in cooldown
      5) -fallback
    Il fallimento si dichiara quando la lista e' esaurita, mai prima: un chunk
    senza trascrizione e' peggio di un tentativo in piu'.

    `used` raccoglie i deployment gia' occupati da altri chunk PARALLELI della
    stessa richiesta: dentro lo stesso tier si preferiscono quelli liberi, ma
    nessuno viene scartato. Il parallelismo e' un'ottimizzazione, la
    trascrizione e' un requisito.
    """
    need = frozenset({"stt"})

    def _tier(d: dict) -> int:
        """0=free, 1=-go, 2=-fallback (dal nome del gruppo)."""
        g = str(d.get("group") or "")
        if g.endswith(gw_state.config.fallback_suffix or "-fallback"):
            return 2
        if g.endswith(gw_state.router.policy.go_suffix or "-go"):
            return 1
        return 0

    # --- candidati: la CATENA CAPABILITY del profilo, che e' la lista
    #     completa dei deployment con cap `stt` (free + -go + -fallback).
    chains = getattr(gw_state.router.config, "chains_cap", {}).get(profile or "") or {}
    uniques = list(chains.get("stt") or ())
    cands: list[dict] = []
    seen_u: set[str] = set()
    for u in uniques:
        if u in seen_u:
            continue
        seen_u.add(u)
        d = gw_state.router.config.deployment_by_unique(u)
        if d is not None:
            cands.append(d)
    if not cands:
        # nessuna catena (routing spento o profilo senza stt): si prova il
        # gruppo -stt direttamente, se esiste.
        grp = None
        if profile:
            for cand in (f"{gw_state.config.proxy_prefix}{profile}-stt", f"{gw_state.config.proxy_prefix}{profile}-stt-fallback"):
                if cand in gw_state.router.config.groups:
                    grp = cand
                    break
        if grp is None:
            return ""
        d = gw_state.router.config.deployment_by_unique(grp) or gw_state.router.pick_deployment(grp, need)
        if d is None:
            return ""
        cands = [d]

    # --- ordine: tier, poi i VIVI prima dei raffreddati, poi i liberi prima
    #     di quelli gia' presi da un altro chunk.
    def _key(d: dict):
        return (_tier(d), 1 if gw_state.router.is_cooled_down(d["unique"]) else 0, 1 if d["unique"] in used else 0)

    cands.sort(key=_key)

    last_err: UpstreamError | None = None
    _sess = _opencode_session(request) or session_id
    _cip = _client_ip(request)
    _attr = _client_attribution(request)
    t_req = time.monotonic()
    tried: set[str] = set()
    for dep in cands:
        cur = dep["unique"]
        if cur in tried:
            continue
        tried.add(cur)
        used.add(cur)
        _was_dormant = gw_state.router.is_cooled_down(cur)
        gw_state.router.note_start(cur)
        t0 = time.monotonic()
        try:
            res = await gw_state.forwarder.transcribe(
                dep,
                {},
                chunk,
                "chunk.ogg",
                "audio/ogg",
                path="transcriptions",
                profile=profile or "",
                client_ip=_cip,
                session=_sess,
                attribution=_attr,
            )
            gw_state.router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                gw_state.router.clear_cooldown(cur)
            metrics.inc("nx_stt_total", (dep["group"], "ok"))
            res, _scrubbed = sttscrub.scrub_payload(res)
            if _scrubbed:
                log.info("[stt-bridge] %s: rimosse %d allucinazioni credit", cur, _scrubbed)
            _emit_summary(
                ses=session_id or "-",
                req=raw_model,
                grp=dep["group"],
                dep=cur,
                tries=len(tried),
                fb=len(tried) - 1,
                dur_ms=int((time.monotonic() - t_req) * 1000),
                stream=False,
                qc=False,
                wd=None,
                usage=None,
                kind="stt",
                path="transcriptions",
            )
            if isinstance(res, dict):
                return str(res.get("text") or "")
            return str(res or "")
        except UpstreamError as err:
            last_err = err
            detail = str(err.detail or "")
            gw_state.router.note_end(cur)
            st = abs(err.status) if err.status else 0
            if -err.status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:  # noqa: BLE001
                    report_suppressed("main._stt_bridge_transcribe")
            if _was_dormant:
                gw_state.router.mark_failed_double_residual(cur, reason=detail[:80], status=st or None)
            else:
                gw_state.router.mark_failed(cur, seconds=err.retry_after, status=st or None)
            metrics.inc("nx_stt_total", (dep["group"], "retry"))
            # NON si esce: si prosegue col candidato successivo. Solo a lista
            # esaurita si dichiara il fallimento (ritorno "").
    if last_err is not None:
        log.info(
            "[stt-bridge] esauriti %d/%d deployment stt: %s", len(tried), len(cands), (last_err.detail or "")[:100]
        )
    return ""


# --- STT-BRIDGE: audio in chat -> testo, prima di ogni altra logica ---
async def _stt_bridge(
    request: Request, payload: dict, auth: AuthResult, session_id: str | None, model: str, raw_model: str
) -> dict:
    """Trascrive l'audio nella history e lo sostituisce con testo.

    Va eseguita PRIMA di qualunque cosa che guardi la history: l'intercettore
    immagini, il tetto immagini, la stima del contesto e la compattazione. Se
    l'audio diventasse testo dopo, la stima conterrebbe byte che poi non
    esistono piu' e la compattazione taglierebbe a caso.

    Ritorna il payload (eventualmente identico): non solleva mai, perche' un
    audio non trascrivibile non deve portare via la richiesta.
    """
    if not count_audio_parts(payload.get("messages") or []):
        return payload
    # Il confine e' quello della COMPATTAZIONE: una sola nozione di "turno
    # protetto", cosi' trascrizione e compattazione non possono divergere.
    boundary = None
    try:
        from .ctxcompact import ctxcompact_config_from_policy, frontier_boundary  # noqa: PLC0415

        _cc = ctxcompact_config_from_policy(gw_state.router.policy)
        _b = frontier_boundary(payload.get("messages") or [], _cc, max_in=0, boundary_floor=0)
        boundary = _b
    except Exception:  # noqa: BLE001
        boundary = None
    used: set[str] = set()
    _prof = auth.profile or _profile_of_request(model, auth.profile)

    async def _one(chunk: bytes, idx: int) -> str:
        return await _stt_bridge_transcribe(request, chunk, _prof, raw_model, session_id, used)

    try:
        out = await sttchat.resolve_audio_in_payload(
            payload, boundary=boundary, transcript_one=_one, policy=gw_state.router.policy
        )
    except Exception as e:  # noqa: BLE001
        log.warning("[stt-bridge] errore inatteso: %s", e)
        return payload
    if out is not payload:
        _n = count_audio_parts(payload.get("messages") or []) - count_audio_parts(out.get("messages") or [])
        if _n:
            log.info("[stt-bridge] %d parti audio sostituite dal testo", _n)
            metrics.inc("nx_stt_total", (_prof or "-", "chat_bridge"))
    return out


class _ClientRelay:
    """Relay SSE verso il client di UNA richiesta streaming (method object di
    `_StreamFallback.sse`): emette il prebuffer e il resto dello stream upstream,
    sorveglia la disconnessione del client e, a fine stream, registra summary,
    usage ed eventuali penalita'. `self.fb` e' la pipeline di fallback."""

    def __init__(self, fb):
        self.fb = fb

    async def run(self):
        if self.fb._synth:
            for self._b in self.fb._synth:
                yield self._b
            _emit_summary(
                ses=self.fb.ses or "-",
                req=self.fb.req or "-",
                grp=self.fb.dep["group"],
                dep=self.fb.dep["unique"],
                tries=len(self.fb.attempts),
                fb=max(0, len(self.fb.attempts) - 1),
                dur_ms=int((time.monotonic() - self.fb.t_req) * 1000),
                stream=self.fb.client_stream,
                qc=False,
                wd="text-toolcall",
                ttfb_ms=self.fb.ttfb_ms,
                usage=None,
            )
            _note_fb_refund(gw_state.router, self.fb.ses, max(0, len(self.fb.attempts) - 1))
            return
        self._init_state()
        # il task che sta eseguendo QUESTO generator (sse): e' lui che va
        # cancellato per interrompere SUBITO l'attesa upstream. asyncio
        # current_task() qui restituisce proprio il task della StreamingResponse.
        self._sse_task = asyncio.current_task()


        try:
            self.monitor = asyncio.create_task(self._watch_disconnect())
            # STRIP dei marker di template (Nemotron/Ling): nessun marker di
            # tool-call testuale deve arrivare al client (anche sul bucket di
            # escalation, dove non ruotiamo).
            self._stripper = TemplateTokenStripper()
            # (D2/B) ordine: prima il prebuffer gia' letto da _peek_stream, poi
            # l'eventuale lettura rimasta in volo (`pending`), poi il resto.
            # OUTPUT STRUTTURATO (HOLD): se il content e' stato pulito/riparato
            # prima di inviare i byte, si emette il testo sanificato al posto
            # dell'originale (finish_reason/usage/[DONE] preservati).
            self._emit_chunks = _collapse_sse_content(self.fb.prebuf, self.fb._so_text) if self.fb._so_rewrite else self.fb.prebuf
            for self.chunk in self._emit_chunks:
                yield _strip_sse_content(self._ingest(self.chunk), self._stripper)
            if self.fb.pending is not None:
                try:
                    yield _strip_sse_content(self._ingest(await self.fb.pending), self._stripper)
                except StopAsyncIteration:
                    self.finished = True
                except Exception:
                    self.gen_broken = True  # upstream rotto a meta' frame
            if not self.finished and not self.gen_broken:
                async for self.chunk in self.fb.gen:
                    yield _strip_sse_content(self._ingest(self.chunk), self._stripper)
                self.finished = True  # StopAsyncIteration: stream chiuso
            if self._stripper.tail:
                log.debug("[strip-tokens] coda residua scartata a fine stream (len=%d)", len(self._stripper.tail))
        except (GeneratorExit, asyncio.CancelledError):
            # disconnessione client o aborted dal monitor: chiudi l'upstream e
            # non punire il deployment (e' il client che e' andato via).
            if not self.aborted:
                await _discard_stream(self.fb.gen, self.fb.pending)
            raise  # disconnessione client: non punire
        except Exception as _exc_exc:
            self.exc = _exc_exc
            self.gen_broken = True
            # anti-stall: StreamStallError e' un asyncio.TimeoutError -> danno
            # reale (upstream appeso), cooldown lungo invece del soft.
            self.gen_stall = isinstance(self.exc, asyncio.TimeoutError)
            self.gen_loop = isinstance(self.exc, StreamLoopDetected)
        finally:
            self._on_stream_end()

    def _init_state(self):
        """Contatori e flag del watchdog passivo (chunk, [DONE], finish_reason, usage, esito)."""
        self.sent_first = False
        self.chunks = 0
        self.seen_done = False
        self.seen_error = False
        self.finished = False
        self.wd: str | None = None
        self.usage_final: dict | None = None
        self.sum_sent = False
        self.answer_total = 0  # solo testo risposta (D2/C)
        self.req_has_input = not _payload_text_empty(self.fb.payload)  # D2/C
        self.finish_len = False  # finish_reason == "length" (D2/C)
        self.saw_finish_reason = False  # QUALSIASI finish_reason non nullo
        self.last_finish_reason: str | None = None  # ultimo finish_reason visto
        self.had_tool_calls = False  # tool_calls visti (D2/C)
        self.req_max_tokens = self.fb.payload.get("max_tokens") or self.fb.payload.get("max_completion_tokens")



        self.gen_broken = False
        self.gen_stall = False  # stall mid-stream rilevato (anti-stall)
        self.gen_loop = False  # loop degenere rilevato in streaming
        self.aborted = False  # client disconnesso durante lo stream
        self.monitor: asyncio.Task | None = None

    async def _watch_disconnect(self) -> None:
        """Se il client chiude la connessione, interrompe SUBITO il task di
        sse() (CancelledError) invece di lasciare l'upstream generare fino a
        fine stream: niente token sprecati sul provider e niente raffiche di
        'socket.send() raised exception' verso una socket morta.

        NB: cancellare il task di sse() chiude anche `gen` (il generator
        upstream esegue il suo finally -> resp.aclose()); aclose() diretto
        da un altro task NON interrompe un generator in pausa, quindi e'
        il task a dover essere cancellato."""
        pass
        try:
            while True:
                await asyncio.sleep(0.5)
                disconnected = False
                if self.fb.request is not None:
                    try:
                        disconnected = await self.fb.request.is_disconnected()
                    except Exception:
                        disconnected = False
                if disconnected:
                    self.aborted = True
                    if self._sse_task is not None:
                        self._sse_task.cancel()
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            report_suppressed("main._ClientRelay._watch_disconnect")

    # corpo del loop fattorizzato: aggiorna lo stato watchdog ed emette
    # il chunk invariato. Condiviso da prebuffer e dal flusso residuo.
    def _ingest(self, chunk: bytes) -> bytes:
        pass
        pass
        pass
        if self.fb.sniffer is not None:
            self.fb.sniffer.feed(chunk)
        self.chunks += 1
        if b"[DONE]" in chunk:
            self.seen_done = True
        if b'data: {"error"' in chunk:
            self.seen_error = True
        if self.usage_final is None and b'"usage"' in chunk and self.chunks > 1:  # parse best-effort del chunk usage
            try:
                line = next((ln for ln in chunk.split(b"\n") if ln.startswith(b"data:") and b'"usage"' in ln), None)
                if line:
                    obj = json.loads(line[5:].strip())
                    u = obj.get("usage")
                    if isinstance(u, dict):
                        self.usage_final = {
                            k: u[k]
                            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                            if u.get(k) is not None
                        }
                        _cached = _cached_tokens_of(u)
                        if _cached is not None:
                            self.usage_final["cached_tokens"] = _cached
                            if _cached > 0:
                                metrics.inc("nx_cache_hit_requests_total", ())
                        c = u.get("cost")
                        if isinstance(c, dict):
                            self.usage_final["cost"] = c.get("total_cost")
                        elif c is not None:
                            self.usage_final["cost"] = c
            except Exception:
                pass
        # F14: calibrazione closed-loop dell'estimator col prompt_tokens
        # reale del provider (stream: arriva nel chunk finale di usage).
        try:
            if isinstance(self.usage_final, dict) and self.usage_final.get("prompt_tokens"):
                gw_state.router.note_estimate_error(self.fb.dep["unique"], self.fb.ctx, self.usage_final["prompt_tokens"])
                # Stima per-sessione (stream): char REALI inviati a monte.
                gw_state.router.note_session_estimate(
                    self.fb.ses,
                    self.fb.est_chars,
                    _prompt_chars(self.fb.payload.get("messages"), self.fb.payload.get("tools")),
                    self.usage_final["prompt_tokens"],
                )
                metrics.inc("nx_sess_est_samples_total")
        except Exception:
            report_suppressed("main._ClientRelay._ingest")
        for o in _sse_data_objs(chunk):
            self.answer_total += _answer_chars(o)
            for ch in o.get("choices") or []:
                if not isinstance(ch, dict):
                    continue
                fr = ch.get("finish_reason")
                if fr:
                    self.saw_finish_reason = True
                    self.last_finish_reason = fr
                    if fr == "length":
                        self.finish_len = True
                d = ch.get("delta") or ch.get("message") or {}
                if isinstance(d, dict) and d.get("tool_calls"):
                    self.had_tool_calls = True
        if not self.sent_first:
            self.sent_first = True  # TTFB gia' presa agli header upstream
        return chunk

    def _on_stream_end(self):
        """Chiusura dello stream (sempre, dal finally): monitor, inflight, esito, summary e sniff."""
        if self.monitor is not None:
            self.monitor.cancel()
        self.dur_ms = int((time.monotonic() - self.fb.t_req) * 1000)
        gw_state.router.note_end(self.fb.dep["unique"], self.fb.ctx)
        if not self.aborted:
            # F1: durata TOTALE del tentativo vincente nel bucket di
            # contesto (il commit ha gia' registrato il TTFT).
            gw_state.router.note_stream_end(
                self.fb.dep["unique"],
                (time.monotonic() - self.fb.t_att) * 1000,
                self.fb.ctx,
                completion_tokens=(self.usage_final or {}).get("completion_tokens"),
            )
        self._judge_stream_outcome()
        self._summary(self.dur_ms)
        if self.fb.sniffer is not None:
            self.fb.sniffer.finish_stream(
                {
                    "status": "success" if (self.finished and not self.gen_broken) else ("aborted" if self.aborted else "broken"),
                    "wd": self.wd,
                    "chunks": self.chunks,
                    "answer_chars": self.answer_total,
                    "had_tool_calls": self.had_tool_calls,
                    "finish_reason_len": self.finish_len,
                    "saw_finish_reason": self.saw_finish_reason,
                    "seen_done": self.seen_done,
                    "usage": self.usage_final,
                    "dep_final": self.fb.dep.get("unique"),
                    "tries": len(self.fb.attempts),
                }
            )

    def _judge_stream_outcome(self):
        """Esito a fine stream: disconnessione del client, stream rotto/stallato/in loop o completo
        -> penalita' e successo del deployment (mai byte aggiuntivi al client)."""
        # NB (fix): il watchdog NON inietta mai nulla nello stream verso il
        # client (un `data:` non-conforme viene renderizzato come testo da
        # opencode & simili). L'unica reazione automatica e' il cooldown del
        # deployment, cosi' i retry del client / le richieste successive
        # evitano la chiave che ha scazzato.
        if self.aborted or self.finished or self.gen_broken:
            if self.aborted:
                # client disconnesso a meta' stream: NON e' colpa del
                # deployment -> nessun cooldown, solo log diagnostico.
                self.wd = "client-aborted"
                log.info("[watchdog] client disconnesso durante lo stream da %s (chunks=%d)", self.fb.dep["unique"], self.chunks)
            elif self.chunks == 0:
                self.wd = "tier1-empty"
                metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "empty"))
                log.warning("[watchdog] tier1 stream VUOTO da %s (chunks=0): cooldown", self.fb.dep["unique"])
                self.fb._fail(self.fb.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.fb.dep["unique"]).fail_count_24h))
            elif self.seen_error:
                self.wd = "tier1-error"
                metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "error"))
                log.warning("[watchdog] tier1 evento error esplicito da %s (chunks=%d)", self.fb.dep["unique"], self.chunks)
                self.fb._fail(self.fb.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.fb.dep["unique"]).fail_count_24h))
            elif self.gen_loop:
                # loop degenere: il modello streammava output ripetitivo,
                # il detector l'ha killato -> cooldown medio e riparti.
                self.wd = "loop-detected"
                metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "loop"))
                log.warning(
                    "[watchdog] stream in LOOP da %s (chunks=%d): kill precoce, cooldown %ds",
                    self.fb.dep["unique"],
                    self.chunks,
                    fwd.STREAM_LOOP_COOLDOWN_S,
                )
                self.fb._fail(self.fb.dep["unique"], seconds=fwd.STREAM_LOOP_COOLDOWN_S, reason="loop_detected")
            elif self.gen_broken or (not self.seen_done and not self.saw_finish_reason):
                # troncamento GENUINO: stream rotto a meta' oppure niente
                # [DONE] E niente finish_reason -> il modello ha scazzato.
                if self.gen_stall:
                    # upstream "congelato" a meta' stream (nessun byte per
                    # stream_stall_sec): danno REALE -> cooldown lungo.
                    self.wd = "tier2-stall"
                    metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "stall"))
                    log.warning(
                        "[watchdog] tier2 stream in STALLO da %s (chunk=%d, stall=%.0fs): cooldown",
                        self.fb.dep["unique"],
                        self.chunks,
                        float(getattr(gw_state.router.policy, "stream_stall_sec", 0) or 0),
                    )
                    self.fb._fail(self.fb.dep["unique"], reason="timeout")
                else:
                    self.wd = "tier2-truncated"
                    metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "truncated"))
                    log.warning(
                        "[watchdog] tier2 stream TRONCATO da %s (chunk=%d, finish_reason=%s): cooldown",
                        self.fb.dep["unique"],
                        self.chunks,
                        self.saw_finish_reason,
                    )
                    self.fb._fail(self.fb.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.fb.dep["unique"]).fail_count_24h))
            elif not self.seen_done:
                # c'e' un finish_reason ma manca [DONE]: risposta di fatto
                # completa, il provider omette solo il sentinel. Solo log.
                self.wd = "tier2-no-done"
                log.info(
                    "[watchdog] tier2 %s: finish_reason presente, nessun [DONE] (provider senza sentinel)",
                    self.fb.dep["unique"],
                )
            elif (
                self.finish_len
                and self.fb._maxtok.get("cap")
                and (self.usage_final or {}).get("completion_tokens") is not None
                and int((self.usage_final or {}).get("completion_tokens")) >= int(self.fb._maxtok["cap"]) - 2
            ):
                # Troncatura AUTO-INFLITTA: il gateway ha clampato
                # max_tokens e il modello ha esaurito ESATTAMENTE quel
                # budget (finish_reason=length). Non e' colpa del
                # deployment: nessuna penale, solo log (i byte sono gia'
                # partiti). Serve a non far scattare il cooldown
                # zero-answer/length-truncated su risposte monche nostre.
                self.wd = "clamp-truncated"
                log.info(
                    "[watchdog] %s: risposta troncata dal clamp "
                    "gateway (max_tokens %s->%s, completion=%s): "
                    "nessuna penale",
                    self.fb.dep["unique"],
                    self.fb._maxtok.get("old"),
                    self.fb._maxtok["cap"],
                    (self.usage_final or {}).get("completion_tokens"),
                )
            elif _length_truncated_should_fail(
                self.finish_len,
                self.answer_total,
                self.req_max_tokens,
                (self.usage_final or {}).get("completion_tokens"),
                gw_state.router.policy.qc_sanity.rotate_on_length_truncated,
            ):
                # risposta TRONCATA dal modello (finish_reason=length) ma
                # con contenuto: come un errore -> cooldown del dep, cosi'
                # le prossime richieste ruotano su un altro modello.
                # (La risposta corrente e' gia' partita: non e' ritraibile.)
                self.wd = "length-truncated"
                metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "length_truncated"))
                log.warning(
                    "[watchdog] risposta TRONCATA (finish_reason="
                    "length) da %s (chunk=%d, answer=%d, "
                    "completion=%s, req_max=%s): cooldown + "
                    "rotazione",
                    self.fb.dep["unique"],
                    self.chunks,
                    self.answer_total,
                    (self.usage_final or {}).get("completion_tokens"),
                    self.req_max_tokens,
                )
                self.fb._fail(self.fb.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.fb.dep["unique"]).fail_count_24h))
            elif (
                self.answer_total == 0
                and self.req_has_input
                and not self.had_tool_calls
                and not (self.finish_len and not gw_state.router.policy.qc_sanity.rotate_on_length_empty)
            ):
                # stream "completo" ma 0 testo di risposta con input reale:
                # fallimento silenzioso -> cooldown (nessun artefatto verso
                # il client: i byte, per quanto vuoti, sono gia' partiti).
                self.wd = "zero-answer"
                metrics.inc("nx_qc_watchdog_total", (self.fb.dep["unique"], "zero_answer"))
                log.warning(
                    "[watchdog] stream 0-answer da %s (input non vuoto, finish_len=%s): cooldown",
                    self.fb.dep["unique"],
                    self.finish_len,
                )
                self.fb._fail(self.fb.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.fb.dep["unique"]).fail_count_24h))

    def _summary(self, dur_ms: int) -> None:
        pass
        if self.sum_sent:
            return
        self.sum_sent = True
        _emit_summary(
            ses=self.fb.ses or "-",
            req=self.fb.req or "-",
            grp=self.fb.dep["group"],
            dep=self.fb.dep["unique"],
            tries=len(self.fb.attempts),
            fb=max(0, len(self.fb.attempts) - 1),
            dur_ms=dur_ms,
            stream=self.fb.client_stream,
            qc=False,
            wd=self.wd,
            ttfb_ms=self.fb.ttfb_ms,
            fr=self.last_finish_reason,
            usage=self.usage_final,
        )
        _note_fb_refund(gw_state.router, self.fb.ses, max(0, len(self.fb.attempts) - 1))


class _StreamFallback:
    """Method object di `_stream_with_fallback` (Replace Method with Method Object).

    Lo stato di UNA richiesta streaming (deployment corrente, tentativi, trail,
    rimedi gia' provati, buffer del peek...) vive negli attributi invece che
    in ~200 variabili locali: la pipeline e' divisa in metodi che condividono
    `self`. Il comportamento e' quello della funzione originale."""

    def __init__(self, profile, first_dep, payload, need, hook, scope, ctx, ses, est_chars, req, session, client_ip, request, attribution, requested_group, cold, prefix_reason, orig_messages, sniffer, result_box, client_stream):
        self.profile = profile
        self.first_dep = first_dep
        self.payload = payload
        self.need = need
        self.hook = hook
        self.scope = scope
        self.ctx = ctx
        self.ses = ses
        self.est_chars = est_chars
        self.req = req
        self.session = session
        self.client_ip = client_ip
        self.request = request
        self.attribution = attribution
        self.requested_group = requested_group
        self.cold = cold
        self.prefix_reason = prefix_reason
        self.orig_messages = orig_messages
        self.sniffer = sniffer
        self.result_box = result_box
        self.client_stream = client_stream

    async def run(self):
        self._init_request_state()
        # Gemini 3 tool replay: una history con tool_call prive di firma rende Gemini
        # inutilizzabile. L'esclusione avviene A MONTE nel router (set_avoid_gemini in
        # chat_completions -> _gemini_blocked in pick_deployment/_walk_chain), quindi
        # qui non serve più alcun salto o tentativo finto.
        while True:
            self._begin_attempt()
            try:
                await self._open_upstream_stream()
                self._load_attempt_settings()
                await self._peek_first_content()
                await self._resolve_verdict()
                self._apply_content_remedies()
                if self._commit_content():
                    break
                await self._close_rejected_attempt()
                self._penalize_verdict()
                _resp = self._last_resort_after_verdict()
                if _resp is not None:
                    return _resp
                self.dep = self.nxt
                inject_identity(self.payload, self.dep, router=gw_state.router)
                continue  # ri-entra nel while col nuovo dep
            except UpstreamError as _err_exc:
                self.err = _err_exc
                gw_state.router.note_end(self.dep["unique"], self.ctx)  # tentativo chiuso senza stream
                gw_state.router.key_lease_release(self._lease)  # P2-8
                self._lease = None
                self.detail = self.err.detail or ""
                if self._on_upstream_error():
                    continue
                _resp = self._last_resort_after_upstream_error()
                if _resp is not None:
                    return _resp
                if self.ses:
                    gw_state.router.sticky_handoff(self.ses, self.nxt)
                self.dep = self.nxt
                inject_identity(self.payload, self.dep, router=gw_state.router)
            except (GeneratorExit, asyncio.CancelledError):
                raise
            except Exception as _exc_exc:
                # qualsiasi errore IMPREVISTO nell'ottenere lo stream da questo
                # deployment (es. httpx che cade leggendo il body d'errore) -> NON
                # deve 500-are la richiesta: cooldown corto + rotazione, 503 solo
                # se non resta nulla.
                self.exc = _exc_exc
                self._on_unexpected_exception()
                _resp = self._last_resort_after_exception()
                if _resp is not None:
                    return _resp
                if self.ses:
                    gw_state.router.sticky_handoff(self.ses, self.nxt)
                self.dep = self.nxt
                inject_identity(self.payload, self.dep, router=gw_state.router)


        return self._ret(StreamingResponse(self.sse(), media_type="text/event-stream"))

    def _init_request_state(self):
        """Stato iniziale della richiesta: tetto immagini, contatori, configurazioni di rimedio da policy."""
        self.dep = self.first_dep
        # Tetto immagini: una volta sola, prima di qualsiasi tentativo, cosi' vale
        # per TUTTA la catena di fallback (niente righe per-deployment e nessuna
        # copia per tentativo). L'originale resta intatto: i rimedi che
        # ripristinano la history (reasoning replay) continuano a vedere tutto.
        try:
            self._imax = int(getattr(gw_state.router.policy, "chat_images_max", 0) or 0)
        except Exception:  # noqa: BLE001
            self._imax = 0
        if self._imax > 0 and count_image_parts(self.payload.get("messages") or []) > self._imax:
            self.payload, self._dropped = _trim_chat_images(self.payload, self._imax)
            if self._dropped:
                metrics.inc("nx_images_total", ((self.dep or {}).get("group", "-"), "chat_images_trimmed"))
                log.info(
                    "[images] tetto chat_images_max=%d: %d immagini non "
                    "inviate all'upstream (restano nella history del client)",
                    self._imax,
                    self._dropped,
                )
        # Gruppo ORIGINARIO della richiesta (es. -200k): serve al pin
        # escalation-winner per valere anche dopo la salita su altre dim.
        self.requested_group = self.requested_group or (self.first_dep or {}).get("group")
        self.tried = 0
        self.tried_set: set[str] = set()
        self._rsn_steps: dict[str, set] = {}  # rimedi reasoning per dep
        self._cstr_steps: dict[str, set] = {}  # rimedi content-string per dep
        self._rsn_restored = False  # history originale gia' riprovata
        self._max_tries = int(
            getattr(gw_state.router.policy, "max_fallback_tries", os.environ.get("GATEWAY_MAX_FALLBACK_TRIES", "128")) or 128
        )
        # Tool repair config per streaming

        self._tr_cfg = create_tool_repair_config(
            {
                "tool_repair": {
                    "enabled": gw_state.router.policy.tool_repair_enabled,
                    "default_level": gw_state.router.policy.tool_repair_default_level,
                    "disable_for_google": gw_state.router.policy.tool_repair_disable_for_google,
                    "max_args_size": gw_state.router.policy.tool_repair_max_args_size,
                },
            }
        )
        self._fc = fake_config_from_policy(gw_state.router.policy)

        self._tt = text_config_from_policy(gw_state.router.policy)
        self._tct_cfg = truncation_config_from_policy(gw_state.router.policy)

        self._sm = sampling_config_from_policy(gw_state.router.policy)

        self._so = schemaout_config_from_policy(gw_state.router.policy)
        # QC di contenuto (parita' col non-stream): attivi anche in hold.
        self.qc = gw_state.router.policy.qc_json
        self.san = gw_state.router.policy.qc_sanity
        self._synth: list[bytes] = []
        # OUTPUT STRUTTURATO in HOLD: la risposta bufferizzata viene trattata come
        # non-streaming -> pulizia/riparazione JSON prima di inviare i byte.
        self._so_corrected: set[str] = set()  # retry correttivo gia' provato per dep
        self._so_rewrite = False  # il content va riscritto in emissione
        self._so_text = ""  # content sanificato da inviare
        self.t_req = time.monotonic()
        try:
            self._hedge_ms = int(getattr(gw_state.router.policy.qc_json, "stream_hedge_delay_ms", 0) or 0)
        except Exception:
            self._hedge_ms = 0
        self.attempts: list[str] = []
        # ATTEMPT TRAIL (P0): per ogni hop fallito, PERCHE' e' stato scartato
        # (classe d'errore onesta). Finisce nel body/header del 503 finale.
        self.trail: list = []
        self.skip_hosts: set[str] = set()  # P1-5: host saltati (errore provider)
        self._lease = None  # P2-8: lease per chiave (opt-in)



        self._races_done = 0
        # WARM-REFILL a cascata: candidati gia' sonciati in QUESTA richiesta
        # (uniq + api_key) e round gia' consumati (budget per-richiesta =
        # warm_refill_max_inflight; il tetto GLOBALE e' il registro in volo per
        # sessione nel router).
        self._raced: dict = {}
        self._refill_rounds = 0
        self._wake_spawned = False  # la SVEglia parte una volta per richiesta
        self.ttfb_ms: int | None = None  # letta da sse()/_summary via closure
        # Clamp max_tokens GATEWAY-side dell'attempt corrente (via maxtok_hook):
        # se il modello esaurisce il NOSTRO budget ridotto, la troncatura e'
        # auto-inflitta -> il watchdog non deve punire il deployment.
        self._maxtok: dict = {}

    def _begin_attempt(self):
        """Apre il tentativo sul deployment corrente: contatori, stato di rimedio per-tentativo, note_start."""
        self.tried += 1
        self._maxtok.clear()
        self._so_rewrite = False
        self._so_text = ""
        self.attempts.append(self.dep["unique"])
        self.tried_set.add(self.dep["unique"])
        self._was_dormant = gw_state.router.is_cooled_down(self.dep["unique"])


        gw_state.router.note_start(self.dep["unique"], self.ctx)
        # qcp PRIMA del try: lo usano anche gli handler `except` (es.
        # stream_total_deadline_ms), quindi deve essere sempre definito anche se
        # `stream_response` solleva UpstreamError al primo invio (429/402 subito).
        self.qcp = gw_state.router.policy.qc_json

    async def _open_upstream_stream(self):
        """Apre lo stream upstream del tentativo (hook di troncatura, thinking replay, lease della chiave)."""
        self.t_att = time.monotonic()
        # hook: a fine stream, se il guard ha trovato un tag tool-call
        # rotto, declassa il deployment (cooldown breve). `salvaged` dice
        # se la chiamata e' stata recuperata o scartata.
        self._trunc_unique = self.dep["unique"]
        self._trunc_was_dormant = self._was_dormant

        def _trunc_hook(_salvaged, _u=self._trunc_unique, _was=self._trunc_was_dormant):
            metrics.inc("nx_truncated_toolcall_total", (_u, "salvaged" if _salvaged else "dropped"))
            repairlog.note(
                "salvage_truncated",
                source="stream",
                outcome="ok" if _salvaged else "fail",
                dep=_u,
                model=self.dep.get("model", ""),
                detail="tag tool-call rotto",
            )
            log.warning(
                "[truncation] stream %s: tag tool-call rotto (%s) -> declasso %ds",
                _u,
                "salvato" if _salvaged else "scartato",
                self._tct_cfg.cooldown_sec,
            )
            if _was:
                gw_state.router.mark_failed_double_residual(_u, reason="truncated_toolcall")
            else:
                gw_state.router.mark_failed(_u, seconds=self._tct_cfg.cooldown_sec, reason="truncated_toolcall")
        self._trunc_hook = _trunc_hook

        if self.dep.get("thinking_replay") and self.orig_messages:
            self._tpr = restore_reasoning(self.payload, self.orig_messages)
            if self._tpr:
                metrics.inc("nx_thinking_replay_total", ("proactive",))
                log.info(
                    "[thinking-replay] %s: %d campi reasoning rimessi PRIMA dell'invio (proattivo)",
                    self.dep["unique"],
                    self._tpr,
                )
        self._lease = gw_state.router.key_lease_acquire(self.dep)  # P2-8 (opt-in)
        # HOLD (parita' col non-stream): se la risposta sara' interamente
        # bufferizzata, la riparazione tool-call NON si fa nel filtro SSE
        # incrementale ma ALLA FINE sull'output GREZZO totale (stessa
        # riparazione del percorso non-streaming). Vedi blocco HOLD sotto.
        self._defer_tr = bool(self.dep.get("hold_until_finish")) or bool(
            getattr(gw_state.router.policy.qc_json, "stream_hold_until_finish", False)
        )
        self.gen = await gw_state.forwarder.stream_response(
            self.dep,
            self.payload,
            profile=self.profile or "",
            ctx_est=self.ctx,
            client_ip=self.client_ip,
            session=self.session,
            attribution=self.attribution,
            tool_repair_config=self._tr_cfg,
            truncation_config=self._tct_cfg,
            truncation_hook=_trunc_hook,
            maxtok_hook=lambda old, new: self._maxtok.update(cap=new, old=old),
            rate_hook=lambda u, rl: gw_state.router.note_rate_limit(u, rl),
            defer_tool_repair=self._defer_tr,
        )

    def _load_attempt_settings(self):
        """TTFB, qualita' e parametri di commit/hold/race letti per questo tentativo."""
        # la TTFB vera e' il tempo fino agli HEADER upstream
        # (send(stream=True) ritorna gia' col primo chunk bufferizzato:
        # misurarla sul primo yield darebbe sempre ~0ms e avvelenerebbe
        # l'EMA della rotazione adattiva con latenze nulle).
        self.ttfb_ms = int((time.monotonic() - self.t_att) * 1000)
        self._quality = 1.0
        if self._was_dormant:
            gw_state.router.clear_cooldown(self.dep["unique"])
        # ANTI-STALLO (1): lo stream verso il client NON parte finche' non
        # arriva CONTENUTO DI RISPOSTA reale. Entro stream_first_content_ms
        # un upstream vuoto/errore/lento viene ruotato in modo TRASPARENTE
        # (nessun byte inviato). Esaurita la catena -> risposta "notice".
        self.qcp = gw_state.router.policy.qc_json
        # ADATTIVO: deadline proporzionale alla latenza storica (EMA) del
        # dep scelto, con pavimento e tetto. Un dep normalmente veloce che
        # stalla non trattiene la richiesta per il cap; un dep lento ha un
        # margine proporzionato (mai oltre il cap). EMA ignota -> cap.
        self.fc_ms = gw_state.router.first_content_deadline_ms(self.dep["unique"], self.ctx)
        self.incl_reason = bool(getattr(self.qcp, "stream_commit_include_reasoning", False))
        self.min_ch = int(getattr(self.qcp, "stream_commit_min_chars", 40) or 0)
        # HOLD-UNTIL-FINISH: attesa della chiusura PULITA dello stream
        # prima di inviare byte (per-deployment dal CSV, o globale da
        # policy). Cosi' una risposta troncata non arriva MAI al client:
        # si ruota pre-byte come per gli altri errori.
        self.hold = bool(self.dep.get("hold_until_finish")) or bool(getattr(self.qcp, "stream_hold_until_finish", False))
        self.hold_idle = int(getattr(self.qcp, "stream_hold_idle_ms", 120000) or 120000)
        self.hold_maxb = int(getattr(self.qcp, "stream_hold_max_buffer_bytes", 52428800) or 52428800)
        # --- peek + HEDGE (F3) + WARM-REFILL a cascata ------------------
        # La gara parte quando: (legacy) il warm non puo' aiutare —
        # catena fredda, holder lento o gia' provato; oppure (REFILL) la
        # sessione ha MENO di warm_ready_min caldi che possono
        # EFFETTIVAMENTE consegnare questa richiesta (need + ctx + output
        # assicurato). Il refill ignora lentezza e warm utile: 2 alla
        # volta (A + 1 canary nuovo, free-only), a cascata a ogni
        # rotazione. I perdenti restano in volo come probe reali.
        self._h_ms = 0
        self._fresh_only = False
        self._legacy = False
        self._refill = False
        self._zen_hunt = False  # caccia canary zen-only (nativo)
        self._pol = gw_state.router.policy
        # Budget di output della richiesta: serve SEMPRE (non solo in
        # refill) — e' il criterio di "capace" per il gruppo warm (gate
        # della gara lenta e conteggi di prontezza). Senza questo il gate
        # conteggiava come caldi dep che non possono consegnare l'output.
        self._need_out = refill_out_budget(self.payload, self._pol)

    async def _peek_first_content(self):
        """Attende il primo contenuto utile (con gare warm/hedge/slow-race): produce `verdict`."""
        self._plan_warm_refill()
        self._plan_races()
        await self._run_peek()

    def _plan_warm_refill(self):
        """Modalita' degradata, bucket di escalation e piano di warm-refill a cascata."""
        # DEGRADED (P1-6): in un blackout upstream l'esplorazione
        # (cascata refill, hedge canary, sveglia) si sospende: spreca
        # rate-limit e chiavi. Resta la rotazione della ladder.
        try:
            self._degraded = gw_state.router.degraded_active()
        except Exception:
            self._degraded = False
        if self._degraded and not self._wake_spawned:
            self._wake_spawned = True  # evita ripetizioni nel loop
            log.info("[degraded] esplorazione sospesa per questa richiesta (%s)", self.dep.get("unique"))
        # Bucket di escalation (-go/-fallback): niente esplorazione
        # Solo se il gruppo RICHIESTO esplicitamente e' un bucket di
        # escalation (-go/-fallback): niente refill/canary/gara lenta/hedge
        # (i bucket a pagamento non usano il caldo, sonde sprecate). Se ci
        # si arriva via FALLBACK dal dim, la speculativa resta attiva per
        # tornare al caldo appena possibile.
        self._esc_grp = is_escalation_group(
            str(self.requested_group or ""), gw_state.router.config.go_suffix, gw_state.router.config.fallback_suffix
        )
        if (
            self.session
            and self.profile
            and not self._degraded
            and not self._esc_grp
            and not opencode_cautious_request()
            and bool(getattr(self._pol, "warm_refill_enabled", True))
            and bool(getattr(self._pol, "warm_pool_enabled", True))
        ):
            self._ready = gw_state.router.warm_ready_effective(self.session, self._pol)
            self._maxif = max(0, int(getattr(self._pol, "warm_refill_max_inflight", 6) or 0))
            try:
                self._fly = gw_state.router.probes_in_flight(self.session)
            except Exception:
                self._fly = 0
            if self._ready and self._refill_rounds < self._maxif and self._fly < self._maxif:
                try:
                    self._pool = gw_state.router.warm_valid_for(
                        self.session,
                        self.profile,
                        self.requested_group or self.dep.get("group"),
                        self.need,
                        self.ctx,
                        self._need_out,
                        tried=self.tried_set,
                        include_borrowed=True,
                    )
                    self._nv = len(self._pool)
                except Exception:
                    self._pool, self._nv = [], self._ready
                # Nativo opencode SENZA zen nel warm: caccia un canary
                # zen-only anche se il conteggio MISTO basta (basta 1 zen).
                self._zen_hunt = (
                    gw_state.router._zen_first_active()
                    and not any(is_opencode_zen_dep(d) for d in self._pool)
                    and gw_state.router.hunt_allowed(self.session, self.ctx)
                )
                self._refill = (self._nv < self._ready) or self._zen_hunt
                if self._zen_hunt:
                    gw_state.router.note_hunt(self.session, self.ctx, gained=False)
                    log.info(
                        "[refill] %s: 0 zen nel warm per client nativo -> caccia canary zen-only", self.dep.get("unique")
                    )
                if self._refill:
                    self._rpm = gw_state.router.session_rpm(self.session)
                    log.info(
                        "[refill] %s: warm validi %d/%d, in volo "
                        "%d/%d (ctx=%s, out=%s, rpm=%.1f) -> "
                        "canario extra in gara",
                        self.dep.get("unique"),
                        self._nv,
                        self._ready,
                        self._fly,
                        self._maxif,
                        self.ctx,
                        self._need_out,
                        self._rpm,
                    )
                    if not self._wake_spawned:
                        self._wake_spawned = True
                        _spawn_wake_sweep(
                            self.payload, self.profile, self.dep, self.need, self.ctx, self._need_out, self.requested_group, self.session, self._raced
                        )

    def _plan_races(self):
        """Parametri di gara: slow race, slow canary e hedge sul cache holder."""
        # GARA LENTA: se A non ha ancora CONSEGNATO dopo N ms si apre 1
        # canario SENZA buttare via la risposta (regola utente): vince
        # chi consegna prima, ma per il giro successivo e' eletto chi ha
        # impiegato meno nel proprio tentativo. E' INDIPENDENTE
        # dall'hedge classico (che resta attivo) e vale anche in refill;
        # il canario lento NON concorre al tetto per-sessione.
        # NB: il campo vive su Policy (non su qc_json): leggerlo da qcp
        # lo lasciava sempre a 0 (bug: la gara lenta non partiva mai).
        self._slow_ms = 0
        self._slow_canary_ms = 0
        if not self._degraded and not self._esc_grp:
            try:
                self._slow_ms = int(getattr(gw_state.router.policy, "stream_slow_race_after_ms", 0) or 0)
            except Exception:
                self._slow_ms = 0
            try:
                self._slow_canary_ms = int(getattr(gw_state.router.policy, "slow_canary_after_ms", 0) or 0)
            except Exception:
                self._slow_canary_ms = 0
        self._slow_only = bool((self._slow_ms > 0 or self._slow_canary_ms > 0) and not self._refill)
        if not self._degraded and not self._esc_grp and (self._hedge_ms > 0 or self._refill or self._slow_only):
            try:
                self._h_dep = gw_state.router.cache_holder(need=self.need, ctx=self.ctx)
                self._h_u = self._h_dep["unique"] if self._h_dep else None
            except Exception:
                self._h_u = None
            self._warm_useful = bool(self._h_u and self._h_u not in self.tried_set and self._h_u != self.dep["unique"])
            self._races_max = int(getattr(self.qcp, "stream_hedge_max_races", 0) or 0)
            self._legacy = (
                not self._warm_useful
                and (self._races_max == 0 or self._races_done < self._races_max)
                and gw_state.router.hunt_allowed(self.session, self.ctx)
            )
            if self._legacy or self._refill or self._slow_only:
                if self._refill:
                    # la cascata parte SUBITO e con il proprio picker:
                    # indipendente dalla lentezza di A (regola utente).
                    self._h_ms = 1
                else:
                    # HEDGE CLASSICO invariato (F13: ritardo calibrato sul
                    # bucket, TTFT fisiologico). La gara lenta NON lo
                    # sostituisce: e' un timer separato dentro _hedge_peek.
                    try:
                        self._h_ms = gw_state.router.hedge_delay_ms(self.dep["unique"], self.ctx)
                    except Exception:
                        self._h_ms = self._hedge_ms
                if self._h_ms <= 0 and self._slow_only:
                    # hedge classico spento ma la gara lenta va armata:
                    # _hedge_peek deve essere chiamato comunque.
                    self._h_ms = 1
                self._fresh_only = bool(self._h_u and self._h_u == self.dep["unique"])

    async def _run_peek(self):
        """Esegue il peek: con gara (hedge/refill/slow) oppure sul solo deployment corrente."""
        if not self._esc_grp and self._h_ms > 0:
            self._races_done += 1
            if self._refill:
                self._refill_rounds += 1
            self._dep_before = self.dep["unique"]
            if self._refill:
                self._hh_k = 2
            else:
                self._hh_k = (
                    max(1, int(getattr(self.qcp, "stream_hedge_tiers", 1) or 1))
                    if bool(getattr(self.qcp, "stream_hedge_cross_tier", True))
                    else 1
                )
            self._raced.setdefault("uniq", set()).add(self.dep["unique"])
            self._raced.setdefault("keys", set()).add(str(self.dep.get("api_key") or ""))
            (self.dep, self.gen, self.t_att, self.verdict, self.prebuf, self.pending, self.meta) = await _hedge_peek(
                self.dep,
                self.gen,
                self.t_att,
                self.fc_ms,
                self.incl_reason,
                self.min_ch,
                self.hold_idle,
                self.hold_maxb,
                payload=self.payload,
                profile=self.profile,
                need=self.need,
                scope=self.scope,
                ctx=self.ctx,
                tried_set=self.tried_set,
                attempts=self.attempts,
                requested_group=self.requested_group,
                session=self.session,
                client_ip=self.client_ip,
                attribution=self.attribution,
                hedge_ms=self._h_ms,
                _tr_cfg=self._tr_cfg,
                _tct_cfg=self._tct_cfg,
                k=self._hh_k,
                fresh_only=self._fresh_only,
                hold=self.hold,
                refill=self._refill,
                zen_only=self._zen_hunt,
                slow_race_ms=self._slow_ms,
                slow_canary_ms=self._slow_canary_ms,
                out_tokens=self._need_out or None,
                raced=self._raced,
            )
            if self._legacy:
                # backoff "il buono non esiste": solo la gara legacy
                # consuma il budget caccia; il refill ha il suo (round).
                gw_state.router.note_hunt(self.session, self.ctx, gained=(self.dep["unique"] != self._dep_before))
        else:
            self.verdict, self.prebuf, self.pending, self.meta = await _peek_stream(
                self.gen,
                self.fc_ms,
                self.incl_reason,
                self.min_ch,
                hold_until_finish=self.hold,
                hold_idle_ms=self.hold_idle,
                hold_max_bytes=self.hold_maxb,
            )

    async def _resolve_verdict(self):
        """Verdetto finale del peek: paracadute sotto hold e troncature da length."""
        # FIX paracadute: sulla catena -go/-fallback (ULTIMO scaglione del
        # ladder) il timeout sul primo contenuto NON deve produrre un 503:
        # li' non c'e' piu' nessuno dietro a cui ruotare, quindi si
        # consegna comunque quello che arriva (parametro opzionale
        # stream_parachute_no_timeout, default True). Sotto HOLD la
        # consegna e' SEMPRE bufferizzata (mai byte live): si scarta la
        # coda in volo cosi' il tool repair hold gira sul buffer parziale.
        self._pv = _parachute_verdict(self.verdict, self.qcp, self.dep, gw_state.router.policy, hold=self.hold, has_buffer=bool(self.prebuf))
        if self.hold and self.verdict == "timeout" and self._pv == "content":
            await _discard_stream(self.gen, self.pending)
            self.pending = None
        self.verdict = self._pv
        # HOLD: finish_reason=length -> risposta TRONCATA dal modello (non
        # dal cap del client): si ruota pre-byte, non si consegna il
        # parziale. Se invece il client ha chiesto max_tokens ed e' stato
        # raggiunto (stima answer_chars/4) la risposta e' voluta -> content.
        if self.verdict == "length_truncated":
            self._req_max = self.payload.get("max_tokens") or self.payload.get("max_completion_tokens")
            self._ans_chars = len(_buffered_answer_text(self.prebuf))
            self._capped = False
            try:
                if self._req_max and self._ans_chars > 0:
                    self._capped = (self._ans_chars / 4.0) >= float(self._req_max) - 2
            except (TypeError, ValueError):
                self._capped = False
            if self._capped:
                self.verdict = "content"

    def _apply_content_remedies(self):
        """Rimedi sul contenuto bufferizzato: tool call testuali/finte, tool repair, output strutturato e QC (hold)."""
        self._parse_text_tool_calls()
        self._reject_fake_tool_call()
        self._repair_tool_calls_on_hold()
        self._enforce_structured_output_on_hold()
        self._quality_check_on_hold()

    def _parse_text_tool_calls(self):
        """Tool call scritte come testo (formato non nativo): convertite in tool_calls vere."""
        if self.verdict == "content" and self._tt.enabled and self.payload.get("tools"):
            self._parsed = parse_text_toolcalls(_buffered_answer_text(self.prebuf), self.payload.get("tools"), self._tt)
            if self._parsed:
                self._synth.extend(_tool_calls_sse(self._parsed, self.dep.get("model")))
                metrics.inc("nx_text_toolcall_total", (self.dep["unique"], "parsed"))
                self._quality = 0.6
                repairlog.note(
                    "salvage_text",
                    source="stream",
                    outcome="ok",
                    dep=self.dep["unique"],
                    model=self.dep.get("model", ""),
                    detail="tool-call resi come testo",
                    count=len(self._parsed),
                )
                # OPZIONE A: il tool-call va RICOSTRUITO ma il testo
                # residuo (es. i marker <goal .../> del plugin) resta al
                # client: si rimuove SOLO il markup del tool-call.
                self._tt_txt = _buffered_answer_text(self.prebuf)
                self._tt_res = strip_toolid_markup(self._tt_txt)
                if self._tt_res != self._tt_txt:
                    self._so_text = self._tt_res
                    self._so_rewrite = True

    def _reject_fake_tool_call(self):
        """Tool call finte (JSON imitato nel testo) -> il verdetto diventa `fake_tool_call`."""
        if self.verdict == "content" and self._fc.enabled:
            self._pat = looks_like_fake_tool_call(_buffered_answer_text(self.prebuf), self._fc)
            if self._pat:
                metrics.inc("nx_fake_toolcall_total", (self.dep["unique"], "detected"))
                self._esc = is_escalation_group(self.dep.get("group"), gw_state.router.config.go_suffix, gw_state.router.config.fallback_suffix)
                if self._esc:
                    # sul bucket di escalation non c'e' dove ruotare senza
                    # loop: si logga e si lascia al sanitizzatore (strip dei
                    # marker), cosi' il client non li vede mai.
                    log.warning(
                        "[fake-tool-call] stream %s: tool-call reso "
                        "come testo (pattern=%s) su bucket di "
                        "escalation -> strip",
                        self.dep["unique"],
                        self._pat,
                    )
                else:
                    log.warning(
                        "[fake-tool-call] stream %s: tool-call reso come testo (pattern=%s), escalation",
                        self.dep["unique"],
                        self._pat,
                    )
                    self._quality = 0.3
                    self.verdict = "fake_tool_call"

    def _repair_tool_calls_on_hold(self):
        """Tool repair sull'output bufferizzato (hold): stessa riparazione del non-streaming."""
        # TOOL REPAIR (HOLD): la risposta e' INTERAMENTE bufferizzata ->
        # STESSA riparazione del percorso non-streaming, applicata
        # ALL'OUTPUT GREZZO totale. Sotto hold il filtro SSE incrementale
        # NON gira (defer_tool_repair), quindi l'intenzione del modello e'
        # intatta; qui si assembla, si ripara e si riscrive lo stream
        # bufferizzato (content + tool_calls) prima di inviare i byte.
        if self.verdict == "content" and self.hold and not self._synth:

            try:
                self._tr_obj = _sse2obj(self.prebuf)
            except ValueError:
                self._tr_obj = None
            if self._tr_obj is not None:
                self._tr_rep = _rep_tc(self._tr_obj, self.payload, self.dep, self._tr_cfg)
                self._tr_san = _san_resp(self._tr_obj)
                if self._tr_rep.get("repaired") or self._tr_san:
                    self._tr_msg = self._tr_obj["choices"][0]["message"]
                    if self._tr_san:
                        self._tr_c = self._tr_msg.get("content")
                        if isinstance(self._tr_c, str):
                            self.prebuf = _collapse_sse_field(self.prebuf, "content", self._tr_c)
                        self._tr_rc = self._tr_msg.get("reasoning_content")
                        if isinstance(self._tr_rc, str):
                            self.prebuf = _collapse_sse_field(self.prebuf, "reasoning_content", self._tr_rc)
                        metrics.inc("nx_content_sanitized_total", (self.dep["unique"],))
                    if self._tr_rep.get("repaired"):
                        self._tr_tcs = self._tr_msg.get("tool_calls")
                        if isinstance(self._tr_tcs, list):
                            self.prebuf = _rewrite_sse_tool_calls(self.prebuf, self._tr_tcs)
                        metrics.inc("nx_tool_repair_total", (self.dep["unique"], "ok"))
                        repairlog.note(
                            "repair_args",
                            source="stream",
                            outcome="ok",
                            dep=self.dep["unique"],
                            model=self.dep.get("model", ""),
                            detail="hold whole-output: moves=%s" % self._tr_rep.get("moves"),
                        )

    def _enforce_structured_output_on_hold(self):
        """Output strutturato (response_format) sull'output bufferizzato: pulizia/riparazione o retry correttivo."""
        # OUTPUT STRUTTURATO (HOLD): la risposta e' INTERAMENTE bufferizzata
        # -> la trattiamo come non-streaming. Pulizia (A) / riparazione
        # schema-driven (D) PRIMA di inviare qualunque byte: il client non
        # vede mai il JSON sporco, e rotazione/corrective restano
        # trasparenti. Solo con HOLD attivo (senza buffer completo non e'
        # possibile) e senza tool-call sintetizzate.
        if self.verdict == "content" and self.hold and self._so.enabled and not self._synth:
            self._so_txt = _buffered_answer_text(self.prebuf)
            self._so_tcs: list | None = None
            for self._o in _sse_data_objs(b"".join(self.prebuf)):
                for self._ch in (self._o.get("choices") or []) if isinstance(self._o, dict) else []:
                    self._d = self._ch.get("delta") if isinstance(self._ch, dict) else None
                    self._tc = self._d.get("tool_calls") if isinstance(self._d, dict) else None
                    if self._tc:
                        self._so_tcs = (self._so_tcs or []) + list(self._tc)
            self._so_data = {"choices": [{"message": {"content": self._so_txt, "tool_calls": self._so_tcs}}]}
            self._so_rep = enforce_response(self._so_data, self.payload, self._so)
            self._so_st = self._so_rep.get("status")
            if self._so_st in ("cleaned", "repaired"):
                self._so_new = self._so_data["choices"][0]["message"].get("content")
                if isinstance(self._so_new, str) and self._so_new != self._so_txt:
                    self._so_text = self._so_new
                    self._so_rewrite = True
                metrics.inc("nx_struct_out_total", (self.dep["unique"], self._so_st))
                log.info("[struct-out] stream %s: %s", self.dep["unique"], self._so_st)
                repairlog.note(
                    "struct_cleaned" if self._so_st == "cleaned" else "struct_repaired",
                    source="stream",
                    outcome="ok",
                    dep=self.dep["unique"],
                    model=self.dep.get("model", ""),
                    detail=",".join(self._so_rep.get("moves") or []) or self._so_st,
                )
            elif self._so_st == "invalid":
                self._r5 = self._so_rep.get("reason") or "schema"
                if getattr(gw_state.router.policy, "corrective_retry_enabled", True) and self.dep["unique"] not in self._so_corrected:
                    self._so_corrected.add(self.dep["unique"])
                    self.payload.setdefault("messages", []).append(
                        {"role": "system", "content": _corrective_note("schema")}
                    )
                    metrics.inc("nx_corrective_retry_total", (self.dep["unique"], "schema"))
                    log.warning(
                        "[retry] stream %s contenuto non conforme (%s): retry correttivo", self.dep["unique"], self._r5
                    )
                    repairlog.note(
                        "struct_corrective",
                        source="stream",
                        outcome="ok",
                        dep=self.dep["unique"],
                        model=self.dep.get("model", ""),
                        detail="schema",
                    )
                    self.verdict = "struct_corrective"
                else:
                    metrics.inc("nx_struct_out_total", (self.dep["unique"], "invalid"))
                    log.warning(
                        "[struct-out] stream %s non conforme (%s): ruoto senza cooldown", self.dep["unique"], self._r5
                    )
                    repairlog.note(
                        "struct_invalid",
                        source="stream",
                        outcome="fail",
                        dep=self.dep["unique"],
                        model=self.dep.get("model", ""),
                        detail=self._r5,
                    )
                    self.verdict = "struct_invalid"

    def _quality_check_on_hold(self):
        """QC di contenuto (JSON e anti-vuoto) sull'output bufferizzato, come nel non-streaming."""
        # QC DI CONTENUTO (HOLD): parita' col percorso non-streaming. La
        # risposta e' INTERAMENTE bufferizzata -> si applicano check_response
        # (JSON quando richiesto) e check_sanity (anti-vuoto) come nel
        # non-stream, con corrective JSON sullo stesso dep e rotazione senza
        # penale se non conforme. (D3 'meno peggio' non si applica: in hold
        # non si consegna mai un body rotto, si ruota fino al 503.)
        if self.verdict == "content" and self.hold and not self._synth and (self.qc.enabled or self.san.enabled):

            self._qc_txt = _buffered_answer_text(self.prebuf)
            self._qc_tcs = _merge_qc_tool_calls(b"".join(self.prebuf)) or None
            self._qc_obj = {"choices": [{"message": {"content": self._qc_txt, "tool_calls": self._qc_tcs}}]}
            # QC solo su contenuto REALE: i casi vuoti/zero-answer (e il
            # paracadute -go che trasmette senza contenuto) restano gestiti
            # dalla macchina a verdict, non dalla sanity.
            self._qc_reason = None
            if self._qc_txt.strip() or self._qc_tcs:
                self._qc_reason = check_response(self._qc_obj, self.payload, self.qc) if self.qc.enabled else None
                if not self._qc_reason and self.san.enabled:
                    self._qc_reason = check_sanity(self._qc_obj, self.payload, self.san)
            if self._qc_reason:
                self._ck = _corrective_kind(self._qc_reason)
                if getattr(gw_state.router.policy, "corrective_retry_enabled", True) and self.dep["unique"] not in self._so_corrected:
                    self._so_corrected.add(self.dep["unique"])
                    self.payload.setdefault("messages", []).append({"role": "system", "content": _corrective_note(self._ck)})
                    metrics.inc("nx_corrective_retry_total", (self.dep["unique"], self._ck))
                    log.warning(
                        "[retry] stream %s contenuto non conforme (%s): retry correttivo %s",
                        self.dep["unique"],
                        self._qc_reason,
                        self._ck,
                    )
                    repairlog.note(
                        "struct_corrective",
                        source="stream",
                        outcome="ok",
                        dep=self.dep["unique"],
                        model=self.dep.get("model", ""),
                        detail=self._ck,
                    )
                    self.verdict = "struct_corrective"
                else:
                    metrics.inc("nx_qc_discarded_total", (self.dep["unique"], str(self._qc_reason).split(" ")[0]))
                    log.warning(
                        "[qc] stream %s contenuto non conforme (%s): ruoto senza cooldown",
                        self.dep["unique"],
                        self._qc_reason,
                    )
                    repairlog.note(
                        "struct_invalid",
                        source="stream",
                        outcome="fail",
                        dep=self.dep["unique"],
                        model=self.dep.get("model", ""),
                        detail=str(self._qc_reason)[:60],
                    )
                    self.verdict = "struct_invalid"

    def _commit_content(self):
        """Contenuto valido: registra il successo e termina il ciclo (True)."""
        if self.verdict == "content":
            # risposta reale in arrivo: se questo deployment ha SERVITO in
            # salita (gruppo != richiesto), ricorda il winner come
            # scorciatoia per le prossime richieste di QUEL bucket.
            gw_state.router.note_result(
                self.dep["unique"], (time.monotonic() - self.t_att) * 1000, quality=self._quality, ctx_est=self.ctx, kind="ttft"
            )
            gw_state.router.record_escalation_win(self.requested_group, self.dep)
            gw_state.router.note_session_success(
                self.ses, self.dep["unique"], (time.monotonic() - self.t_att) * 1000, ctx_est=self.ctx, kind="ttft"
            )
            # P2-8: la gara e' decisa; la lease si libera qui (il cap
            # serve a non FAR PARTIRE nuovi tentativi su chiave satura).
            gw_state.router.key_lease_release(self._lease)
            self._lease = None
            return True  # risposta reale in arrivo: si parte

    async def _close_rejected_attempt(self):
        """Chiude il tentativo scartato: scarica lo stream e rilascia inflight e lease."""
        # --- nessun contenuto: rotazione PRE-BYTE ---
        await _discard_stream(self.gen, self.pending)
        gw_state.router.note_end(self.dep["unique"], self.ctx)
        gw_state.router.key_lease_release(self._lease)  # P2-8
        self._lease = None

    def _penalize_verdict(self):
        """Trail, cooldown e rimedi per un verdetto non consegnabile (vuoto, troncato, timeout, struttura)."""
        self._record_verdict_trail()
        self.fr = self.meta.get("finish_reason")
        self.rot_len = getattr(gw_state.router.policy.qc_sanity, "rotate_on_length_empty", False)
        # NON ruotare (e non punire) se il modello HA prodotto reasoning o
        # ha esaurito max_tokens: non e' rotto, ruotare non cambia nulla
        # (tutto il gruppo si comporterebbe uguale) -> 503 retryable diretto.
        self.no_rotate = self.verdict == "empty_eof" and self.meta.get("no_rotate") and not self.rot_len
        # Clamp GATEWAY-side: se il modello ha esaurito il max_tokens che
        # GLI ABBIAMO TAGLIATO NOI (fr=length + cap nostro), la troncatura
        # e' auto-inflitta: ruotare va bene (il dim dopo ha piu' spazio) ma
        # NON declassare il deployment (non e' colpa sua).
        self._gw_clamp_trunc = bool(self._maxtok.get("cap") and self.fr == "length")
        # 0 caratteri in hold (stop/[DONE] puliti senza risposta, o length
        # bruciato tutto in reasoning): il modello NON e' rotto, ha solo
        # finito il budget o risposto vuoto -> si ruota (ladder, poi
        # -go/-fallback) SENZA penale; la penale resta per gli stream
        # VAMENTE rotti (timeout, EOF sporco, length con mezzo answer).
        self._zero_empty = (self.verdict == "empty_eof" and bool(self.meta.get("empty_clean"))) or (
            self.verdict == "length_truncated" and not _buffered_answer_text(self.prebuf)
        )
        self._penalize_rejected_verdict()
        self.over_deadline = (time.monotonic() - self.t_req) * 1000 > int(
            getattr(self.qcp, "stream_total_deadline_ms", 90000) or 90000
        )
        self._on_structure_verdict()
        log.warning(
            "[fallback] stream %s pre-contenuto verdict=%s fr=%s no_rotate=%s -> %s",
            self.dep["unique"],
            self.verdict,
            self.fr,
            bool(self.no_rotate),
            self.nxt["unique"] if self.nxt else "503",
        )

    def _record_verdict_trail(self):
        """Aggiunge al trail l'hop del verdetto (classe e status) per il 503 finale."""
        # ATTEMPT TRAIL anche per i VERDETTI: senza questo hop un 503 con
        # catena esaurita per verdetti (empty_eof/length_truncated/
        # timeout/fake_tool_call/struct_invalid) riportava attempts=[] e
        # nessun X-Scrocco-Trail: il client non capiva QUANTI e QUALI
        # deployment erano stati scartati, e perche'.
        try:
            self._v_cls = (
                "timeout"
                if self.verdict == "timeout"
                else (
                    "struct_invalid"
                    if self.verdict in ("struct_corrective", "struct_invalid")
                    else (
                        self.verdict
                        if self.verdict in ("empty_eof", "length_truncated", "fake_tool_call")
                        else classify_error_class(502, self.verdict)
                    )
                )
            )
            self._v_st = (
                504
                if self.verdict == "timeout"
                else (422 if self.verdict in ("struct_corrective", "struct_invalid") else 502)
            )
            self.trail.append(
                {
                    "ord": len(self.trail) + 1,
                    "dep": self.dep.get("unique"),
                    "group": self.dep.get("group"),
                    "model": self.dep.get("model"),
                    "cls": self._v_cls,
                    "status": self._v_st,
                    "ms": int((time.monotonic() - self.t_att) * 1000),
                }
            )
        except Exception:  # noqa: BLE001
            report_suppressed("main._StreamFallback._record_verdict_trail")

    def _penalize_rejected_verdict(self):
        """Penale del deployment per il verdetto scartato: nessuna per chiusure pulite,
        clamp gateway, tool call finte e output strutturato; timeout lungo; altro soft."""
        if self._zero_empty:
            log.info(
                "[hold] %s: chiusura '%s' senza risposta (fr=%s): nessuna penale, ruoto su candidato piu' capace",
                self.dep["unique"],
                self.verdict,
                self.fr,
            )
        elif self._gw_clamp_trunc:
            log.info(
                "[maxtok] %s: stream vuoto perche' ha esaurito il clamp gateway (%s->%s): nessuna penale, ruoto",
                self.dep["unique"],
                self._maxtok.get("old"),
                self._maxtok.get("cap"),
            )
        elif not self.no_rotate:
            # TIMEOUT (upstream che appende): danno REALE (tempo perso) ->
            # cooldown lungo (timeout_cooldown_mult x classico). Vuoto/
            # troncato: fallimento SOFT -> cooldown corto con escalation
            # dolce sui fallimenti recenti (24h).
            if self.verdict == "timeout":
                self._fail(self.dep["unique"], reason="timeout")
            elif self.verdict == "fake_tool_call":
                # ROTAZIONE SENZA PENALITA' (richiesta esplicita): il modello
                # non e' rotto, ha solo reso la chiamata come testo ->
                # nessun cooldown/streak, si ruota e basta.
                log.info("[fake-tool-call] %s: rotazione senza cooldown", self.dep["unique"])
            elif self.verdict in ("struct_corrective", "struct_invalid"):
                # OUTPUT STRUTTURATO (HOLD): risposta gia' completa e non
                # conforme -> nessuna penale (no cooldown/streak): si
                # ritenta lo stesso dep (corrective) o si ruota.
                log.info("[struct-out] %s: %s senza cooldown", self.dep["unique"], self.verdict)
            else:
                self._fail(self.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.dep["unique"]).fail_count_24h))

    def _on_structure_verdict(self):
        """Output strutturato non conforme: retry correttivo sullo stesso dep o cooldown."""
        if self.verdict == "struct_corrective":
            # retry correttivo: STESSO deployment (la nota di sistema e'
            # gia' stata appesa al payload).
            self.nxt = self.dep
        elif self.verdict == "fake_tool_call":
            self.nxt = (
                gw_state.router.force_escalation(
                    self.dep, self.need, self.ctx, tried=self.tried_set, out_tokens=refill_out_budget(self.payload, gw_state.router.policy)
                )
                if self.profile
                else None
            )
            if self.nxt is None and self.profile and gw_state.router._is_renewal_bucket(str(self.dep.get("group") or "")):
                self.nxt = gw_state.router._free_last_resort(
                    self.dep, self.need, self.ctx, self.tried_set, refill_out_budget(self.payload, gw_state.router.policy), self.requested_group
                )
        else:
            # Su troncatura/risposta-vuota preferiamo un candidato PIU'
            # CAPACE (finestra > corrente, poi intelligence), perche' il
            # problema e'fisicamente lo spazio di output: la scala normale
            # (dim ascendente) resta il fallback se il picker non trova di
            # meglio.
            self._cap_pref = self.verdict == "length_truncated" or self._zero_empty
            self.nxt = (
                None
                if (self.no_rotate or self.over_deadline)
                else (
                    gw_state.router.fallback_next(
                        self.profile,
                        self.dep,
                        self.need,
                        self.scope,
                        ctx=self.ctx,
                        tried=self.tried_set,
                        requested_group=self.requested_group,
                        out_tokens=refill_out_budget(self.payload, gw_state.router.policy),
                        prefer_capable=self._cap_pref,
                    )
                    if self.profile
                    else None
                )
            )

    def _last_resort_after_verdict(self):
        """Nessun prossimo deployment dopo un verdetto scartato: ultima risorsa free, poi 503 retryable.

        Ritorna la risposta finale, oppure None se la catena prosegue."""
        if self.nxt is None or self.tried > self._max_tries:
            # ULTIMA RISORSA: bucket -go/-fallback esaurito -> scendi ai
            # free-dims (warm di chiunque cap-ok, poi canary, poi cooled)
            # pur di non consegnare un 503.
            self._flr = None
            if (
                self.nxt is None
                and self.profile
                and not self.over_deadline
                and gw_state.router._is_renewal_bucket(str(self.dep.get("group") or ""))
            ):
                self._flr = gw_state.router._free_last_resort(
                    self.dep, self.need, self.ctx, self.tried_set, refill_out_budget(self.payload, gw_state.router.policy), self.requested_group
                )
            if self._flr is not None:
                self.nxt = self._flr
            elif self.nxt is None or self.tried > self._max_tries:
                # nessun byte inviato al client -> errore RETRYABLE pulito
                _emit_summary(
                    ses=self.ses or "-",
                    req=self.req or "-",
                    grp=self.dep.get("group"),
                    dep=self.dep.get("unique"),
                    tries=len(self.attempts),
                    fb=max(0, len(self.attempts) - 1),
                    dur_ms=int((time.monotonic() - self.t_req) * 1000),
                    stream=self.client_stream,
                    qc=True,
                    wd="chain-exhausted",
                    ttfb_ms=self.ttfb_ms,
                    usage=None,
                )
                return self._ret(
                    _exhausted(
                        len(self.attempts),
                        "%s (%s)" % (self.verdict, self.fr) if self.fr else self.verdict,
                        prefix_reason=self.prefix_reason,
                        trail=self.trail,
                        retry_at_ms=_retry_at_ms(gw_state.router, self.trail),
                    )
                )

    def _on_upstream_error(self):
        """Dopo un UpstreamError: rimedi sul payload (True = ritenta lo stesso dep),
        classificazione dell'errore, cooldown e scelta del prossimo deployment."""
        if self._try_payload_repairs():
            return True
        self._classify_upstream_error()
        self._penalize_failed_dep()
        self._pick_next_after_error()

    def _try_payload_repairs(self):
        """Rimedi sul payload che ritentano lo STESSO deployment (reasoning replay, content
        array, history originale). True = ritenta."""
        if self._try_reasoning_repairs():
            return True
        if self._try_content_array_repair():
            return True
        if self._try_original_history():
            return True

    def _try_reasoning_repairs(self):
        """Firme media/reasoning e rimedi reasoning-replay sullo stesso deployment. True = ritenta."""
        # ATTEMPT TRAIL: registra l'hop fallito con la sua classe onesta
        # (anche quando il rimedio reasoning piu' sotto lo ritenta).
        try:
            self.trail.append(
                {
                    "ord": len(self.trail) + 1,
                    "dep": self.dep.get("unique"),
                    "group": self.dep.get("group"),
                    "model": self.dep.get("model"),
                    "cls": classify_error_class(self.err.status, self.detail),
                    "status": abs(int(self.err.status)) if self.err.status else None,
                    "ms": int((time.monotonic() - self.t_att) * 1000),
                }
            )
        except Exception:  # noqa: BLE001
            report_suppressed("main._StreamFallback._try_reasoning_repairs#1")
        # P1-5 skipPlatforms: errore PROVIDER-level (5xx/timeout/transport)
        # -> salta TUTTO l'host per questa richiesta.
        try:
            if is_provider_level(classify_error_class(self.err.status, self.detail)):
                self._h = dep_host(self.dep)
                if self._h and self._h not in self.skip_hosts:
                    self.skip_hosts.add(self._h)
                    log.info(
                        "[skip-host] %s: errore provider-level -> host %s saltato per questa richiesta",
                        self.dep["unique"],
                        self._h,
                    )
        except Exception:  # noqa: BLE001
            report_suppressed("main._StreamFallback._try_reasoning_repairs#2")
        # "does not support vision input" (llm7/Cloudflare) su richieste
        # di PURO TESTO: il proxy maschera spesso lo stesso problema del
        # reasoning mancante (i payload reali hanno decine di assistant
        # con tool_calls e zero reasoning_content). Quindi lo trattiamo
        # come candidato replay: prima si ripara e si ritenta LO STESSO
        # dep; se fallisce di nuovo -> cooldown (reason=model_feature).
        self._media_raw = bool(media_reject_signature(self.detail))
        self.media_sig = self._media_raw and media_input_needed(self.need)
        self._rsn_media = media_modality_signature(self.detail) and not self.media_sig and reasoning_err_kind(self.detail) is None
        # FAMIGLIA REASONING (needs/rejects/history): un rimedio per dep,
        # poi si ritenta LO STESSO deployment. Copre il replay del campo
        # `reasoning_content` (opencode zen / deepseek thinking), il
        # provider che lo RIFIUTA (Cloudflare "reasoning_content is
        # unsupported") e la history incoerente col thinking nativo
        # (Anthropic/Gemini). Ruotare non aiuta: tutte le chiavi dello
        # stesso provider rifiutano lo stesso payload.
        self._steps = self._rsn_steps.setdefault(self.dep["unique"], set())
        self._replim = int(getattr(gw_state.router.policy, "repair_exempt_streak_limit", 3) or 0)
        self._rexb = gw_state.router.repair_exempt_blocked(self.dep["unique"], self._replim)
        self._rr = (
            None
            if self._rexb
            else repair_reasoning_error(
                self.payload, self.detail, self.dep, self._steps, self.orig_messages, force_kind=("needs" if self._rsn_media else None)
            )
        )
        if self._rexb:
            log.warning(
                "[reasoning-exempt] %s: budget esenzione esaurito (%d) -> KO normale", self.dep["unique"], self._replim
            )
            # Booking NORMALE: le classi payload/schema da sole non
            # prevedono cooldown, quindi lo applichiamo qui (altrimenti
            # il dep verrebbe ritentato all'infinito su ogni richiesta).
            with contextlib.suppress(Exception):
                self._f24 = gw_state.router.stats_for(self.dep["unique"]).fail_count_24h
                gw_state.router.mark_failed(
                    self.dep["unique"],
                    seconds=_soft_cd(self._f24),
                    reason="repair_exempt_exhausted",
                    status=abs(int(self.err.status)) if self.err.status else None,
                )
        if self._rr == "downgraded":
            self.dep = dict(self.dep)
            self.dep["_no_thinking"] = True  # copia locale, non il CSV
        if self._rr:
            with contextlib.suppress(Exception):
                gw_state.router.note_repair_exempt(self.dep["unique"])
            metrics.inc("nx_reasoning_replay_total", (self._rr,))
            log.warning("[reasoning-%s] %s: rimedio applicato -> ritento lo stesso deployment", self._rr, self.dep["unique"])
            # IMPARA il flag corrispondente: d'ora in poi il CSV lo porta
            # per questo modello (tutti i gemelli) e parte corretto.
            with contextlib.suppress(Exception):
                if self._rr == "repaired":
                    learn_thinking_replay(gw_state.router, self.dep.get("model"))
                elif self._rr == "stripped":
                    learn_strip_reasoning(gw_state.router, self.dep.get("model"))
                elif self._rr == "downgraded":
                    learn_no_thinking(gw_state.router, self.dep.get("model"))
            return True

    def _try_content_array_repair(self):
        """Provider con schema stretto (content array -> stringa): ripara il payload. True = ritenta."""
        # CONTENT ARRAY -> STRING (provider schema stretto, es.
        # Cloudflare Workers AI): 400 "'array' not in 'string'" /
        # "required properties ... 'role,content'". Payload RIPARABILE:
        # impariamo `content_string` (gemelli del modello) e ritentiamo LO
        # STESSO deployment col payload appiattito (media-safe). Se la
        # bonifica non basta (array con media) o il flag c'e' gia', si
        # ricade sulla rotazione di _PAYLOAD_SCHEMA_RE piu' sotto.
        if _CONTENT_ARRAY_RE.search(self.detail):
            self._csteps = self._cstr_steps.setdefault(self.dep["unique"], set())
            # Solo se c'e' DAVVERO qualcosa da appiattire (altrimenti il
            # retry non aiuta: si ricade sulla rotazione piu' sotto).
            self._flat, self._fn = flatten_text_content((self.payload or {}).get("messages"))
            if self._fn and "flatten" not in self._csteps and not self.dep.get("content_string"):
                self._csteps.add("flatten")
                metrics.inc("nx_content_string_total", ("learned",))
                log.warning(
                    "[content-string] %s: 400 schema content-array "
                    "-> imparo content_string e ritento lo stesso "
                    "deployment (%d messaggi)",
                    self.dep["unique"],
                    self._fn,
                )
                with contextlib.suppress(Exception):
                    learn_content_string(gw_state.router, self.dep.get("model"))
                self.dep = dict(self.dep)
                self.dep["content_string"] = True  # copia locale (retry)
                return True

    def _try_original_history(self):
        """Errore oscuro dopo il taglio della history: riprova con la history originale. True = ritenta."""
        # ERRORE "OSCURO" su richiesta reasoning: il taglio del reasoning
        # (histnorm) e' un'ottimizzazione di token; se il provider non ci
        # da' una firma chiara, si ritenta UNA volta lo STESSO deployment
        # con la history ORIGINALE (reasoning intatto). Se l'errore e'
        # chiaro (quota/auth/ban/schema/...) il tentativo non serve.
        if self.orig_messages is not None and not self._rsn_restored and is_unclear_error(self.err.status, self.detail):
            self._nres = restore_reasoning(self.payload, self.orig_messages)
            if self._nres:
                self._rsn_restored = True
                metrics.inc("nx_reasoning_replay_total", ("restored",))
                log.warning(
                    "[reasoning-restore] %s: errore non chiaro (%s) "
                    "-> reasoning ripristinato (%d campi), ritento "
                    "lo stesso deployment",
                    self.dep["unique"],
                    (self.detail or "")[:90],
                    self._nres,
                )
                return True

    def _classify_upstream_error(self):
        """Classifica l'errore: quarantene/cooldown di host e contesto, firme (thought, schema, quota, 401/403...)."""
        # BAN/ToS dell'endpoint (ip_banned / policy_review / Terms of
        # Service): quarantena dell'HOST 24h, cosi' la rotazione non
        # brucia una chiave dietro l'altra dello stesso provider.
        maybe_quarantine_ban(gw_state.router, self.dep, self.err.status, self.detail)
        # 502/503 mid-stream di un aggregatore: e' l'HOST a essere
        # malato -> pausa BREVE dell'host invece di bruciare le chiavi
        # sorelle (elasticita' per un problema transitorio).
        maybe_host_transient_cooldown(gw_state.router, self.dep, self.err.status, self.detail)
        # 413/400 "context length": il provider ha rivelato il VERO
        # limite di input -> ridimensiona il deployment (regola utente).
        note_context_limit(gw_state.router, self.dep, self.err.status, self.detail, self.ctx)
        # QUOTA DI ACCOUNT (Cloudflare & co.): la quota e' dell'account,
        # non della chiave -> metti in pausa TUTTE le chiavi sorelle fino
        # al reset invece di ruotarle a vuoto una per una.
        with contextlib.suppress(Exception):
            maybe_account_quota_cooldown(gw_state.router, self.dep, self.err.status, self.detail)
        # D5 anche in STREAMING: 4xx deployment-side (firma provider-side,
        # modello inesistente oppure 404) -> fallback pre-byte invece di
        # pass-through. Gli altri 4xx restano errori del client.
        self.thought_sig = bool(_THOUGHT_SIG_RE.search(self.detail))
        # CF Workers AI & co.: rifiuto di SCHEMA (content array vs string,
        # messaggio senza content) -> stesso trattamento del
        # thought_signature: ruota SENZA cooldown, mai pass-through finche'
        # c'e' un'alternativa (un provider OpenAI-compatibile lo accetta).
        # Include anche i rifiuti "campo sconosciuto" dei provider severi
        # (Google: "Unknown name \"store\" ... Invalid JSON payload"):
        # incompatibilita' col provider, NON colpa della richiesta ->
        # ruota senza cooldown, mai pass-through del 400 al client (parita'
        # col path non-stream, forwarder._UNKNOWN_FIELD_RE).
        self.schema_sig = bool(_PAYLOAD_SCHEMA_RE.search(self.detail) or _UNKNOWN_FIELD_RE.search(self.detail))
        # Google/Gemini 3 (anche via proxy OpenAI-compat): rifiuto della
        # COMBINAZIONE built-in tools + function calling (il flag
        # tool_config non e' passabile). Stesso trattamento dello schema:
        # ruota SENZA cooldown, mai pass-through; a catena esaurita NON e'
        # "actionable" -> 503 RETRYABLE (il client non puo' farci nulla).
        self.tool_combo_sig = tool_combo_signature(self.detail)
        if self.schema_sig or self.tool_combo_sig:
            self.thought_sig = True  # riusa tutta la logica no-cooldown
        # Rifiuto di MODALITA' (vision/image/audio/…): il modello non e'
        # rotto, semplicemente non accetta quel tipo di input -> ruota
        # SENZA cooldown (un altro deployment multimodale lo accetta),
        # mai pass-through del 400 al client.
        # MA solo se la richiesta HA davvero media: alcuni proxy (llm7/
        # Cloudflare) rispondono "does not support vision input" a
        # richieste di puro testo -> in quel caso il dep e' rotto per
        # QUESTA richiesta e va in cooldown come un KO normale.
        # NB: `_media_raw`/`media_sig` sono gia' calcolati sopra (servono
        # anche al tentativo di replay reasoning).
        self.prov_err = is_provider_error_body(self.detail)  # body {"error":...} & co.
        self.prov_fault = is_provider_fault_body(self.detail)
        # QUOTA: la firma basta da sola. Alcuni provider (Cloudflare
        # Workers AI) usano un envelope {"errors":[{...}]} che NON passa
        # `prov_err`, ma il messaggio di quota e' inequivocabile.
        self.quota_exhausted = (
            bool(_QUOTA_EXHAUSTED_RE.search(self.detail)) if (self.prov_err or abs(int(self.err.status or 0)) == 429) else False
        )
        self.transient = bool(_PROVIDER_TRANSIENT_RE.search(self.detail))
        # 403 di qualsiasi tipo: chiave/progetto rifiutato dal provider ->
        # deployment-side (mai colpa della richiesta), ruota (mai al client).
        self.upstream403 = self.err.status == -403
        # 401 upstream: la NOSTRA chiave e' rifiutata dal provider
        # (assente/invalidata/revocata). E' SEMPRE deployment-side: il
        # client si e' gia' autenticato da noi, quindi non e' colpa sua.
        # Ruota come il 403, mai pass-through.
        self.upstream401 = self.err.status == -401
        self.openai_sig = "bad_response_status_code" in self.detail or "openai_error" in self.detail
        # 4xx con body d'errore ASSENTE/illeggibile (stream appeso ->
        # _safe_aread scaduto): non c'e' alcun messaggio azionabile per il
        # client -> NON e' un errore del client, e' infrastruttura ->
        # ruota + cooldown corto, mai pass-through (503 se catena esaurita).
        self.empty_body = not self.detail.strip() or "body non leggibile" in self.detail.lower() or len(self.detail.strip()) < 12

    def _penalize_failed_dep(self):
        """Cooldown/strike del deployment che ha fallito, secondo la classe dell'errore."""
        self._classify_failure_reason()
        self._classify_negative_status()
        self._cool_down_failed_dep()

    def _classify_failure_reason(self):
        """Motivo deployment-side del fallimento (loop, quota, schema, media...), per log e cooldown."""
        # motivo della classificazione deployment-side (per il log)
        if isinstance(self.err, StreamLoopDetected):
            self.reason = "loop_detected"
        elif self.quota_exhausted:
            # QUOTA prima dello schema: la quota va in cooldown (fino al
            # reset), non ruotata a vuoto senza cooldown.
            self.reason = "quota_exhausted"
        elif self.tool_combo_sig:
            self.reason = "tool_combo"
        elif self.schema_sig:
            self.reason = "payload_schema"
        elif self.media_sig:
            self.reason = "media_reject"
        elif self._media_raw:
            # falso rifiuto di modalita': niente media nella richiesta ->
            # modello rotto per questa richiesta, cooldown normale.
            self.reason = "model_feature"
        elif self.thought_sig:
            self.reason = "thought_signature"
        elif self.prov_err:
            self.reason = "provider_error_body"
        elif self.transient:
            self.reason = "provider_transient"
        elif _MODEL_MISSING_RE.search(self.detail):
            self.reason = "model_missing"
        elif gw_state.router.policy.qc_json.retry_provider_4xx and self.openai_sig:
            self.reason = "openai_error"
        elif self.err.status == -402:
            self.reason = "http_402"
        elif self.empty_body:
            self.reason = "empty_error_body"
        elif self.upstream403:
            self.reason = "upstream_403"
        elif self.upstream401:
            self.reason = "upstream_401"
        elif self.prov_fault:
            self.reason = "provider_fault"
        elif self.err.status == -429:
            # 429 esplicito: quota/chiave satura -> soft per-chiave (F7)
            # e NESSUNA penale reputazionale (record_failure class-aware).
            self.reason = "http_429"
        elif self.err.status is not None and self.err.status < 0:
            self.reason = "other_4xx"
        elif self.err.status is None and "upstream timeout" in self.detail.lower():
            # Upstream che APPENDE (read/connect timeout): danno reale ->
            # cooldown lungo via reason=timeout (timeout_cooldown_mult x).
            self.reason = "timeout"
        else:
            self.reason = "http_%s" % self.err.status if self.err.status else "network"

    def _classify_negative_status(self):
        """Status negativo (4xx del provider): colpa del deployment (ruota, mai pass-through)
        oppure della richiesta; il context length alza la soglia della sessione."""
        if self.err.status is not None and self.err.status < 0:
            self.provider_side = (
                (gw_state.router.policy.qc_json.retry_provider_4xx and self.openai_sig)
                or _MODEL_MISSING_RE.search(self.detail)
                or self.thought_sig
                or self.prov_err
                or self.transient
                or self.upstream403
                or self.upstream401
                or self.empty_body
                or self.prov_fault
                or self._media_raw
                or self.quota_exhausted
                # 429 (anche a status negativo, es. body non-standard di
                # un aggregatore): chiave/quota satura = deployment-side,
                # MAI un errore della richiesta -> ruota, mai pass-through.
                or self.err.status == -429
                or self.err.status == -402
            )
            # né thought_signature né il body d'errore provider né
            # il 403 sono rifiuti di modalita': non alimentano l'auto-
            # learn (hook).
            if (
                self.provider_side
                and self.hook
                and not self.thought_sig
                and not self.prov_err
                and not self.upstream403
                and not self.upstream401
                and not self.prov_fault
                and self.media_sig
            ):
                try:
                    self.hook(self.dep["model"], self.detail)
                except Exception:
                    report_suppressed("main._StreamFallback._classify_negative_status#1")
            if _looks_context_limit(-self.err.status, self.detail):
                # CONTEXT LENGTH: NON passiamo il 400 al client. Alziamo la
                # soglia minima della sessione (le richieste successive
                # partiranno da una dim che contiene il payload) e lasciamo
                # cadere nel flusso di fallback: `_fail` + `_next_filtered`
                # ruotano (e per i dim espliciti la ladder sale di dim).
                self._actual = extract_requested_tokens(self.detail)
                try:
                    gw_state.router.note_session_overflow(self.ses, self._actual or 0)
                except Exception:  # noqa: BLE001
                    report_suppressed("main._StreamFallback._classify_negative_status#2")
                log.warning(
                    "[fallback] stream %s context_length_exceeded (%.90s): alzo la soglia sessione (%s) e ruoto",
                    self.dep["unique"],
                    self.detail,
                    (">=%d tok" % self._actual) if self._actual else "ctx-sconosciuto",
                )

    def _cool_down_failed_dep(self):
        """Applica il cooldown al deployment secondo il motivo (quota fino al reset,
        transiente, 401/403 lunghi, modello assente...) e rilascia lo sticky."""
            # QUALSIASI altro non-200: ruota, mai pass-through al client.
            # (La rotazione termina solo a catena esaurita: a quel punto
            # _actionable_upstream_error consegna lo status vero oppure 503.)
        # Gemini 3 tool replay: ruota SENZA cooldown (vedi _THOUGHT_SIG_RE
        # nel forwarder) — la key Gemini resta sana per il traffico non-tool.
        # model_missing (inesistente/non servito/giu'): 24h fissi.
        if not self.thought_sig and not self.media_sig:
            self._prov_q = None
            if self.reason == "quota_exhausted":
                # Abbonamento flat esaurito: cooldown = tempo al reset
                # (es. "Resets in 9 days" -> ~9gg), non escalation.
                self._cd = parse_quota_reset_seconds(self.detail)
                # Provenienza: 'authoritative' SOLO se il provider ha
                # dichiarato il reset ("Resets in ..."); la nostra stima
                # (mezzanotte UTC) resta 'heuristic' -> la SVEglia puo'
                # comunque tentare il risveglio (regola utente).
                self._prov_q = "authoritative" if _QUOTA_RESET_RE.search(self.detail or "") else "heuristic"
                # Rilascia dep-sticky: questa key NON tornerà prima del
                # reset; la sessione deve ripartire su un'altra chiave.
                if self.ses:
                    self.cur = gw_state.router.dep_sticky_get(self.ses)
                    if self.cur and self.cur == self.dep["unique"]:
                        gw_state.router.dep_sticky_release(self.ses)
            elif self.reason in ("provider_transient", "empty_error_body"):
                self._cd = gw_state.router.escalate_cooldown(
                    fwd.PROVIDER_TRANSIENT_COOLDOWN_S, gw_state.router.stats_for(self.dep["unique"]).fail_count_24h
                )
            elif self.reason == "model_missing":
                self._cd = fwd.MODEL_MISSING_COOLDOWN_S
            elif self.reason == "upstream_403":
                # Key/progetto rifiutato dal provider: cooldown lungo +
                # rilascia lo sticky, la sessione riparte su un'altra key.
                self._cd = fwd.PERMISSION_DENIED_COOLDOWN_S
                if self.ses:
                    self.cur = gw_state.router.dep_sticky_get(self.ses)
                    if self.cur and self.cur == self.dep["unique"]:
                        gw_state.router.dep_sticky_release(self.ses)
            elif self.reason == "upstream_401":
                # Chiave assente/invalidata/revocata: stessa gestione del
                # 403 (cooldown lungo + rilascio sticky).
                self._cd = fwd.PERMISSION_DENIED_COOLDOWN_S
                if self.ses:
                    self.cur = gw_state.router.dep_sticky_get(self.ses)
                    if self.cur and self.cur == self.dep["unique"]:
                        gw_state.router.dep_sticky_release(self.ses)
            elif self.reason == "loop_detected":
                # Loop degenere in streaming: cooldown medio, si ruota
                # subito (un'altra chiave/modello puo' rispondere).
                self._cd = fwd.STREAM_LOOP_COOLDOWN_S
            else:
                self._cd = self.err.retry_after
            # reason propagato solo per il TIMEOUT (mark_failed applica il
            # moltiplicatore dedicato); per gli altri resta il comportamento
            # storico (seconds esplicito / default).
            # reason/status propagati SEMPRE: senza, sul path streaming
            # restavano morti key-soft 429, budget-guard learning,
            # _punish_concurrency e le classi di cooldown (F18).
            if fwd.is_insufficient_balance(self.detail):
                # BILANCIO ESAURITO: ritira il DEPLOYMENT (sblocco manuale).
                if self.ses:
                    self._cur = gw_state.router.dep_sticky_get(self.ses)
                    if self._cur and self._cur == self.dep["unique"]:
                        gw_state.router.dep_sticky_release(self.ses)
                self._fail(self.dep["unique"], reason="insufficient_balance", status=402, kind=fwd.ErrorKind.PERMANENT_DEAD)
                log.warning(
                    "[fallback] stream %s 402 'insufficient balance': DEPLOYMENT RITIRATO (sblocco manuale)",
                    self.dep["unique"],
                )
            else:
                self._fail(
                    self.dep["unique"],
                    seconds=self._cd,
                    reason=self.reason,
                    status=abs(self.err.status) if self.err.status else None,
                    provenance=self._prov_q,
                )

    def _pick_next_after_error(self):
        """Sceglie il prossimo deployment dopo l'errore (mai lo stesso per le firme thought)."""
        self.nxt = (
            self._next_filtered(self.profile, self.dep, self.need, self.scope, ctx=self.ctx, tried=self.tried_set, requested_group=self.requested_group)
            if self.profile
            else None
        )
        if self.nxt is not None and self.thought_sig and self.nxt["unique"] in self.attempts:
            self.nxt = None  # gruppo/catena tutto Gemini 3
        log.warning(
            "[fallback] stream %s %s motivo=%s -> %s :: %.120s",
            self.dep["unique"],
            self.err.status or "conn",
            self.reason,
            self.nxt["unique"] if self.nxt else "nessun alternativo",
            self.detail,
        )

    def _last_resort_after_upstream_error(self):
        """Nessun prossimo deployment dopo un UpstreamError: ultima risorsa free, errore
        azionabile col suo status oppure 503. Ritorna la risposta o None."""
        if self.nxt is None or self.tried > self._max_tries:
            # ULTIMA RISORSA free (solo se -go/-fallback esaurito).
            self._over_dl = (time.monotonic() - self.t_req) * 1000 > int(
                getattr(self.qcp, "stream_total_deadline_ms", 90000) or 90000
            )
            self._flr = None
            if self.nxt is None and self.profile and not self._over_dl and gw_state.router._is_renewal_bucket(str(self.dep.get("group") or "")):
                self._flr = gw_state.router._free_last_resort(
                    self.dep, self.need, self.ctx, self.tried_set, refill_out_budget(self.payload, gw_state.router.policy), self.requested_group
                )
            if self._flr is not None:
                self.nxt = self._flr
            else:
                # errori AZIONABILI (auth/credito/permessi/modello assente/
                # thought_signature) -> status vero. Il resto -> 503.
                if _actionable_upstream_error(self.err) and self.err.status:
                    return self._ret(
                        JSONResponse(
                            status_code=abs(self.err.status),
                            content={"error": {"message": self.err.detail, "type": "upstream_error"}},
                        )
                    )
                _emit_summary(
                    ses=self.ses or "-",
                    req=self.req or "-",
                    grp=self.dep.get("group"),
                    dep=self.dep.get("unique"),
                    tries=len(self.attempts),
                    fb=max(0, len(self.attempts) - 1),
                    dur_ms=int((time.monotonic() - self.t_req) * 1000),
                    stream=self.client_stream,
                    qc=True,
                    wd="chain-exhausted",
                    ttfb_ms=self.ttfb_ms,
                    usage=None,
                )
                return self._ret(
                    _exhausted(
                        len(self.attempts),
                        self.err.detail,
                        prefix_reason=self.prefix_reason,
                        trail=self.trail,
                        retry_at_ms=_retry_at_ms(gw_state.router, self.trail),
                    )
                )

    def _on_unexpected_exception(self):
        """Eccezione inattesa nel tentativo: cooldown soft del deployment e scelta del prossimo."""
        try:
            gw_state.router.note_end(self.dep["unique"], self.ctx)
        except Exception:
            report_suppressed("main._StreamFallback._on_unexpected_exception")
        self._fail(self.dep["unique"], seconds=_soft_cd(gw_state.router.stats_for(self.dep["unique"]).fail_count_24h))
        self.nxt = (
            self._next_filtered(self.profile, self.dep, self.need, self.scope, ctx=self.ctx, tried=self.tried_set, requested_group=self.requested_group)
            if self.profile
            else None
        )
        log.warning(
            "[fallback] stream %s errore imprevisto %r -> %s", self.dep["unique"], self.exc, self.nxt["unique"] if self.nxt else "503"
        )

    def _last_resort_after_exception(self):
        """Nessun prossimo deployment dopo un'eccezione inattesa: ultima risorsa free
        oppure 503. Ritorna la risposta o None."""
        if self.nxt is None or self.tried > self._max_tries:
            self._over_dl = (time.monotonic() - self.t_req) * 1000 > int(
                getattr(self.qcp, "stream_total_deadline_ms", 90000) or 90000
            )
            self._flr = None
            if self.nxt is None and self.profile and not self._over_dl and gw_state.router._is_renewal_bucket(str(self.dep.get("group") or "")):
                self._flr = gw_state.router._free_last_resort(
                    self.dep, self.need, self.ctx, self.tried_set, refill_out_budget(self.payload, gw_state.router.policy), self.requested_group
                )
            if self._flr is not None:
                self.nxt = self._flr
            else:
                _emit_summary(
                    ses=self.ses or "-",
                    req=self.req or "-",
                    grp=self.dep.get("group"),
                    dep=self.dep.get("unique"),
                    tries=len(self.attempts),
                    fb=max(0, len(self.attempts) - 1),
                    dur_ms=int((time.monotonic() - self.t_req) * 1000),
                    stream=self.client_stream,
                    qc=True,
                    wd="chain-exhausted",
                    ttfb_ms=self.ttfb_ms,
                    usage=None,
                )
                return self._ret(
                    _exhausted(
                        len(self.attempts),
                        repr(self.exc)[:160],
                        prefix_reason=self.prefix_reason,
                        trail=self.trail,
                        retry_at_ms=_retry_at_ms(gw_state.router, self.trail),
                    )
                )

    def _fail(self, u, *, seconds=None, reason=None, status=None, provenance=None, kind=None):
        if kind is not None:
            # errore che IMPONE una strategia (es. PERMANENT_DEAD ->
            # retirement): nessun cooldown, la decisione e' del lifecycle.
            return gw_state.router.mark_failed(u, reason=reason, status=status, kind=kind)
        if self._was_dormant:
            _r = gw_state.router.mark_failed_double_residual(u, reason=reason, status=status)
        else:
            _r = gw_state.router.mark_failed(u, seconds=seconds, reason=reason, status=status, provenance=provenance)
        # P1-4: 3 KO dello stesso MODELLO (anche su chiavi diverse) entro
        # la finestra -> bench del modello su tutte le sue chiavi.
        with contextlib.suppress(Exception):
            gw_state.router.note_model_failure(self.dep)
        return _r

    def _next_filtered(self, *a, **k):
        """fallback_next + P1-5: salta gli host che hanno gia' fallito a
        livello provider in QUESTA richiesta; se non ne restano, torna al
        candidato saltato (mai lasciare la richiesta senza risposta)."""
        k.setdefault("out_tokens", refill_out_budget(self.payload, gw_state.router.policy))
        _n = gw_state.router.fallback_next(*a, **k)
        if _n is None or dep_host(_n) not in self.skip_hosts:
            return _n
        _saved = _n
        for _ in range(8):
            self.tried_set.add(_saved["unique"])
            _c = gw_state.router.fallback_next(*a, **k)
            if _c is None:
                return _saved
            if dep_host(_c) not in self.skip_hosts:
                return _c
            _saved = _c
        return _saved

    def _ret(self, resp):
        """Registra dep/attempts/trail finali per il chiamante (redirect
        non-stream) e ritorna la risposta invariata."""
        if self.result_box is not None:
            try:
                self.result_box["dep"] = self.dep
                self.result_box["attempts"] = list(self.attempts)
                # il trail DEVE passare da qui: il redirect non-stream non
                # vede la closure e senza questo finiva con _tr=None -> 503
                # con attempts vuoti e nessun X-Scrocco-Trail.
                self.result_box["trail"] = list(self.trail)
            except Exception:  # noqa: BLE001
                report_suppressed("main._StreamFallback._ret")
        return resp

    def sse(self):
        # Watchdog PASSIVO (D4): conta chunk, rileva [DONE] ed eventi error.
        # NON modifica mai i byte verso il client. Due livelli:
        #   tier1 stream vuoto / evento "error" esplicito -> cooldown subito
        #   tier2 chiuso senza [DONE] -> solo log, cooldown se policy lo vuole
        # + (D2) ri-emissione del prebuffer e coda d'errore SSE finale (verdict C).
        return _ClientRelay(self).run()


async def _stream_with_fallback(
    profile: str | None,
    first_dep: dict,
    payload: dict,
    need: frozenset[str] = frozenset(),
    hook=None,
    scope: str = "chain",
    ctx: int | None = None,
    ses: str | None = None,
    est_chars: int = 0,
    req: str | None = None,
    session: str | None = None,
    client_ip: str = "",
    request: "Request | None" = None,
    attribution: dict | None = None,
    requested_group: str | None = None,
    cold: bool = False,
    prefix_reason: str | None = None,
    orig_messages: list | None = None,
    sniffer=None,
    result_box: dict | None = None,
    client_stream: bool = True,
):
    """Streaming SSE con fallback PRIMA del primo byte inviato al client.

    `result_box`, se fornito, riceve ('dep'/'attempts'/'trail') il deployment
    finale, i tentativi e l'attempt trail (quali hop e con quale classe):
    serve al redirect non-stream->stream sotto hold per la post-elaborazione
    non-stream e per propagare la provenienza al suo 503.
    `client_stream=False` etichetta summary/sniff come non-stream (il client
    reale ha chiesto non-stream)."""
    return await _StreamFallback(profile=profile, first_dep=first_dep, payload=payload, need=need, hook=hook, scope=scope, ctx=ctx, ses=ses, est_chars=est_chars, req=req, session=session, client_ip=client_ip, request=request, attribution=attribution, requested_group=requested_group, cold=cold, prefix_reason=prefix_reason, orig_messages=orig_messages, sniffer=sniffer, result_box=result_box, client_stream=client_stream).run()


from .compat.ollama import router as _ollama_router  # noqa: E402 (local import to break cycle)
from .models_and_health import router as _models_and_health_router  # noqa: E402 (local import to break cycle)
from .images_api import router as _images_api_router  # noqa: E402 (local import to break cycle)
from .audio_api import router as _audio_api_router  # noqa: E402 (local import to break cycle)
from .videos_api import router as _videos_api_router  # noqa: E402 (local import to break cycle)

# Re-export: `lifespan` (sopra, ESCLUSA dallo spostamento) chiama questi
# helper via nome nudo, e i test li patchano/importano via app.main.<simbolo>.
from .runtime_persistence import (  # noqa: E402 (re-export per lifespan + test)
    _bootstrap_runtime_from_logs,
    _coalesce_cache_put,  # noqa: F401 - ri-esportato
    _coalesce_key,  # noqa: F401 - ri-esportato
    _forward_coalesced,
    _load_adaptive_stats,
    _load_cooldowns,
    _load_routing_state,
    _load_thought_sigs,
    _maybe_save_adaptive_stats,
    _maybe_save_all,
    _maybe_save_cooldowns,
    _maybe_save_thought_sigs,
    _nightly_scheduler,
    _nonstream_hold_redirect,
    _watcher,
    seconds_to_midnight,  # noqa: F401 - ri-esportato
)

app.include_router(_ollama_router)
app.include_router(_models_and_health_router)
app.include_router(_images_api_router)
app.include_router(_audio_api_router)
app.include_router(_videos_api_router)


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=gw_state.PORT, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
