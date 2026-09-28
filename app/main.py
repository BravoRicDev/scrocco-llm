"""scrocco-llm — assemblaggio dell'app FastAPI (gateway LLM OpenAI-compatible).

[IT] Qui si costruisce il processo, non si serve nessuna richiesta:
- logging (console colorata + file, rotazione solo sul leader in cluster);
- stato runtime condiviso (app/state.py): config dal CSV, policy, router,
  forwarder, auth, ledger, keyhealth, persistenza;
- `lifespan`: carica lo stato da disco, entra nel cluster (app/cluster.py),
  avvia watcher/heartbeat/health/nightly; allo stop drena e salva;
- middleware (ammissione, trace ID, metriche) e handler di errore;
- le rotte, ognuna nel suo modulo: chat completions (app/chat_completions.py),
  admin, bootstrap, ollama, models/health/metrics, immagini, audio, video.

La pipeline di una chat (auth -> alias -> stima contesto -> gruppo ->
pick -> fallback a catena -> QC/watchdog -> risposta) e' descritta in
app/chat_completions.py e in docs/ARCHITECTURE.md.

[EN] FastAPI app assembly: logging, shared runtime state, lifespan
(load/save state, cluster join, background tasks), middleware, error
handlers and route registration. Request handling lives in the route modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .admin import admin_api
from .suppressed import report_suppressed
from .bootstrap import bootstrap_api
from .auth import AuthManager, gateway_env
from . import cluster
from .config import GatewayConfig
from . import sniff
from .probes import (
    _drain_probe_tasks,
)
from . import repairlog
from .forwarder import (
    Forwarder,
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
)
from .health import health_loop
from .policy import Policy
from .router import Router, configure_estimate
from .caution import background_cautious_enabled
from .errors import AppError

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




# ------------------------------------------------------- PROBE (warm-refill)
# Il perdente di una gara non viene MAI cancellato: finisce la risposta in
# volo come PROBE REALE. Se consegna una risposta piena e pulita entra nel
# warm della sessione (note_warm_owner, senza holder/reputazione); se sbaglia
# va in cooldown CON LE SOLITE LOGICHE (timeout -> lungo, errore/stream rotto
# -> corto), mentre il vuoto-pulito/length da budget resta senza penale come
# per il tentativo servito. Costo doppio accettato: la cascata pesca SOLO nei
# free-dims.
gw_state._PROBE_TASKS = set()


# POST /v1/chat/completions (app/chat_completions.py): registrata QUI, nello
# stesso punto in cui c'era il decoratore, cosi' l'ordine delle rotte resta
# quello di prima.
from .chat_completions import router as _chat_router  # noqa: E402

app.include_router(_chat_router)
from .compat.ollama import router as _ollama_router  # noqa: E402 (local import to break cycle)
from .models_and_health import router as _models_and_health_router  # noqa: E402 (local import to break cycle)
from .images_api import router as _images_api_router  # noqa: E402 (local import to break cycle)
from .audio_api import router as _audio_api_router  # noqa: E402 (local import to break cycle)
from .videos_api import router as _videos_api_router  # noqa: E402 (local import to break cycle)

# Persistenza e task periodici usati dal lifespan (i test li sostituiscono
# qui, dove il lifespan li cerca).
from .runtime_persistence import (  # noqa: E402
    _bootstrap_runtime_from_logs,
    _load_adaptive_stats,
    _load_cooldowns,
    _load_routing_state,
    _load_thought_sigs,
    _maybe_save_adaptive_stats,
    _maybe_save_all,
    _maybe_save_cooldowns,
    _maybe_save_thought_sigs,
    _nightly_scheduler,
    _watcher,
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
