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
import base64
import contextlib
import copy
import hashlib
import json
import logging
import os
import random
import re
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from .admin import admin_api
from .bootstrap import bootstrap_api
from .auth import AuthManager, AuthResult, gateway_env
from . import journal, metrics
from . import imagestore
from . import audiostore
from . import capmeta
from .config import GatewayConfig, csv_mtime_ns, maybe_reload, CAP_PRIORITY_ORDER
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
    _cap_chain_pick,
    _data_uri,
    _download_remote_image,
    _image_chat_intercept,
    _images_chat_loop,
    _images_pick_dep,
    _localize_images,
    _profile_of_request,
)
from . import repairlog
from . import autoprobe
from . import sttscrub
from . import sttchat
from . import forwarder as fwd
from .forwarder import (
    Forwarder,
    MODEL_MISSING_COOLDOWN_S,
    PERMISSION_DENIED_COOLDOWN_S,
    PROVIDER_TRANSIENT_COOLDOWN_S,
    UpstreamError,
    StreamLoopDetected,
    STREAM_LOOP_COOLDOWN_S,
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
    is_embedded_provider_error,
    media_reject_signature,
    media_input_needed,
    media_modality_signature,
    image_chat_fallback_signature,
    image_chat_payload,
    extract_chat_images,
    image_refs_from_payload,
    truncate_refs,
    images_dual,
    image_item_dual,
    dep_image_via,
    chat_prompt_and_refs,
    images_payload_from_chat,
    images_response_to_chat,
    native_images_only_error,
    chat_only_image_error,
    _split_data_uri,
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
    repair_reasoning_replay,
    _REASONING_REPLAY_RE,
    repair_reasoning_error,
    reasoning_err_kind,
    classify_error_class,
    dep_host,
    is_provider_level,
    restore_reasoning,
    is_unclear_error,
    QUOTA_MIN_COOLDOWN_S,
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
    set_allow_opencode_zen,
    set_spoofing_request,
    set_zen_first,
    client_can_use_opencode_zen,
    client_is_opencode,
    spoof_enabled,
    opencode_cautious_request,
    is_opencode_zen_dep,
)
from .capabilities import (
    required_caps,
    count_image_parts,
    refs_max_for,
    wants_image_output,
    _is_image_part,
    count_audio_parts,
)
from .effort import set_effort, effort_from_request
from .errors import AppError, UnauthorizedError, NotFoundError, ForbiddenError

from .sse_utils import (
    _sse_data_objs,
    _merge_qc_tool_calls,
    _strip_sse_content,
    _collapse_sse_field,
    _collapse_sse_content,
    _rewrite_sse_tool_calls,
    _delta_has_content,
    _answer_chars,
    _obj_is_error,
    _delta_has_answer,
    _chunk_finish_reason,
    _tool_calls_sse,
    _buffered_answer_text,
    _peek_stream,
)

# Logging strutturato: [auth] [route] [vigile] [identity] [fallback] [cooldown]
# basicConfig è no-op se root ha già handler (es. sotto pytest/caplog).
# Formato con colori per terminali (ANSI escape codes)
from app.terminal_logging import (
    ColoredFormatter,
    setup_colored_logging,
    colorize_tag,
    colorize_level,
    is_terminal_stream,
)

console_handler = setup_colored_logging()

logging.basicConfig(level=logging.INFO, handlers=[console_handler], force=True)  # Override any existing basicConfig
log = logging.getLogger("nx.main")

BASE_DIR = Path(__file__).resolve().parent.parent
# I DATI (credenziali + policy) vivono in var/: directory bind-montata nel
# container Docker, così l'admin API scrive i file VERO dell'host.
VAR_DIR = BASE_DIR / "var"
CSV_PATH = Path(os.environ.get("GATEWAY_CSV", VAR_DIR / "keys_rotation.csv"))
POLICY_PATH = Path(os.environ.get("GATEWAY_POLICY", VAR_DIR / "gateway.yaml"))
PORT = int(os.environ.get("GATEWAY_PORT", "4001"))
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
    from logging.handlers import RotatingFileHandler

    # Same format as console for consistency
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    mb = int(os.environ.get("GATEWAY_LOG_MAX_MB", "20"))
    bk = int(os.environ.get("GATEWAY_LOG_BACKUPS", "5"))
    main_path = os.environ.get("GATEWAY_LOG_FILE", str(VAR_DIR / "gateway.log"))
    audit_path = os.environ.get("GATEWAY_ERROR_LOG_FILE", str(VAR_DIR / "error-audit.log"))
    try:
        h = RotatingFileHandler(main_path, maxBytes=mb * 1024 * 1024, backupCount=bk, encoding="utf-8")
        h.setFormatter(logging.Formatter(fmt))
        h.setLevel(logging.INFO)
        logging.getLogger().addHandler(h)
    except OSError as exc:  # noqa: BLE001
        log.warning("[log] file %s non scrivibile (%s): solo stdout", main_path, exc)
    try:
        ah = RotatingFileHandler(audit_path, maxBytes=mb * 1024 * 1024, backupCount=bk, encoding="utf-8")
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
policy = Policy.load_or_default(POLICY_PATH)
# Debug SNIFF: handler con rotazione oraria, default OFF. Registrato anche se
# disattivato (l'abilitazione e' live via policy/env) — nessun costo se spento.
sniff.configure(str(VAR_DIR / "debug-sniff.log"), policy.debug_sniff_retention_hours)
# Ledger persistente delle riparazioni tool-call (log a schermo + JSONL).
repairlog.configure(str(VAR_DIR))
config = GatewayConfig(
    CSV_PATH,
    proxy_prefix=policy.proxy_prefix,
    go_suffix=policy.go_suffix,
    fallback_suffix=policy.fallback_suffix,
    extra_prefixes=policy.legacy_prefixes,
)
router = Router(config, policy)
# provider callable: l'hot-reload della policy aggiorna anche le chiavi client
authn = AuthManager(config, client_keys_provider=lambda: policy.client_keys)
forwarder = Forwarder(keepalive_pool=policy.http_keepalive_pool)
set_retry_after_floors(policy.retry_after_min_sec, policy.retry_after_floor_by_provider)
set_stream_stall_sec(policy.stream_stall_sec)
set_strip_client_fields(policy.strip_client_fields)
set_latency_lookup(lambda u, ctx=None: router.bucket_latency_ms(u, ctx))
# F21: lo stall guard si calibra sul TTFT per bucket e sul moltiplicatore/
# tetto di policy; F20: divisore+immagini condivisi per le stime "senza router".
set_ttft_lookup(lambda u, ctx=None: router.bucket_latency_ms(u, ctx, "ttft"))
set_stall_bucket(multiplier=policy.stream_stall_ttft_mult, max_sec=policy.stream_stall_max_sec)
set_estimate_defaults(policy.estimate_divisor, getattr(policy, "image_token_estimate", 0) or 0)


# P4: i dep che IGNORANO stream:true vengono annotati (json_fallback++) e poi
# esclusi dai canary: un non-streaming non puo' vincere la gara.
def _note_json_fallback(_u):
    try:
        if _u:
            router.stats_for(_u).json_fallback += 1
    except Exception:  # noqa: BLE001
        pass


set_nonstream_hook(_note_json_fallback)
set_adaptive_timeout(
    enabled=policy.adaptive_timeout_enabled,
    floor_sec=policy.adaptive_timeout_floor_sec,
    multiplier=policy.adaptive_timeout_multiplier,
    max_sec=policy.adaptive_timeout_max_sec,
)
set_reasoning_reserve(1.0 - float(getattr(policy, "cache_ctx_reasoning_headroom_ratio", 0.7) or 0.0))
apply_cooldown_policy(policy)
from .schemaout import schemaout_config_from_policy as _so_cfg_from_policy

set_schemaout_config(_so_cfg_from_policy(policy))
configure_estimate(
    adaptive=policy.estimate_adaptive_enabled,
    shadow=policy.estimate_adaptive_shadow,
    auto_enable=policy.estimate_adaptive_auto_enable,
    auto_min_n=policy.estimate_adaptive_auto_min_n,
    auto_max_delta_pct=policy.estimate_adaptive_auto_max_delta_pct,
)

_watch_task: asyncio.Task | None = None
_health_task: asyncio.Task | None = None
_nightly_task: asyncio.Task | None = None
_stats_file = VAR_DIR / "adaptive_stats.json"
_last_stats_save = 0.0
# Persistenza DEDICATA dei cooldown (var/cooldown_state.json): a differenza
# di adaptive_stats salva anche `since`/`full`, cosi' dopo un restart il
# probe/decay ripartono con l'eta' reale e la durata totale.
_cooldown_file = VAR_DIR / "cooldown_state.json"
_last_cooldown_save = 0.0
# WARM-START DI ROUTING (var/routing_state.json): holder cache, sticky,
# ownership warm, demote per-sessione, pin escalation e watermark ctxcompact.
# Senza questo, ogni deploy ripartiva freddo: ri-rotazioni, ri-escalations e
# — per il ctxcompact — frontiera regredita che ri-invalidava le cache.
_routing_file = VAR_DIR / "routing_state.json"
_last_routing_save = 0.0
# i TEST settano GATEWAY_PERSIST_ROUTING=0: nessuna contaminazione col live
PERSIST_ROUTING = os.environ.get("GATEWAY_PERSIST_ROUTING", "1") != "0"
# job video asincroni (OR-style): job_id -> snapshot deployment per poll/content.
# MAPPING IN MEMORIA con TTL: al restart i job in corso si perdono -> 404 con hint.
_videos_jobs: dict[str, dict] = {}
VIDEO_JOB_TTL_SEC = 24 * 3600


def set_video_job_ttl_sec(value=None) -> None:
    """TTL dei job video in memoria (policy `video_job_ttl_sec`, default 24h)."""
    global VIDEO_JOB_TTL_SEC
    if value is not None:
        try:
            VIDEO_JOB_TTL_SEC = max(0, int(value))
        except (TypeError, ValueError):
            pass


# i TEST settano GATEWAY_PERSIST_STATS=0: nessuna contaminazione col live
PERSIST_STATS = os.environ.get("GATEWAY_PERSIST_STATS", "1") != "0"

# Ledger usage/costi (Feature: /admin/insights). Stessa env dei test per non
# sporcare var/ reale durante la suite.
from .ledger import Ledger as _Ledger

LEDGER = _Ledger(VAR_DIR)

# Evidenza persistente salute chiavi (lifecycle dead/retired, no-delete).
from .keyhealth import KeyHealth as _KeyHealth

KEYHEALTH = _KeyHealth(VAR_DIR)
from .atomic_store import load_json as _load_json, save_json as _save_json

# --- Inflight request coalescing (payload identico, solo non-streaming) ---
# Se due richieste identiche (stesso payload + stesso profilo) sono in volo,
# solo la prima interroga l'upstream; le altre attendono e ricevono la stessa
# risposta (deepcopy). Un retry automatico identico al 100% non spreca quota.
_inflight_coalesce: dict[str, dict] = {}
_inflight_lock = asyncio.Lock()
# Finestra POST-risposta (request_coalescing_cache_sec): lo stesso payload
# arrivato entro N secondi riceve la risposta gia' prodotta, senza ripetere
# la chiamata upstream. Cache SOLO successi non-stream, cap 64 entry.
_coalesce_cache: dict[str, dict] = {}
_COALESCE_CACHE_MAX = 64


def set_coalesce_cache_max(value=None) -> None:
    """Cap entry della cache di coalescing (policy `coalesce_cache_max`)."""
    global _COALESCE_CACHE_MAX
    if value is not None:
        try:
            _COALESCE_CACHE_MAX = max(0, int(value))
        except (TypeError, ValueError):
            pass


def _apply_misc_policy(pol) -> None:
    """Propaga i parametri di policy alle costanti runtime dei moduli minori.

    Chiamata all'avvio e ad ogni hot-reload. Non cambia la logica: i default
    restano identici alle costanti storiche dei moduli."""
    set_video_job_ttl_sec(getattr(pol, "video_job_ttl_sec", None))
    set_coalesce_cache_max(getattr(pol, "coalesce_cache_max", None))
    try:
        # Persistenza su disco: senza, gli URL /v1/images/files/{id} gia'
        # consegnati ai client vanno in 404 al riavvio del processo (restart,
        # deploy, reload) o con worker multipli.
        imagestore.configure(
            ttl_sec=getattr(pol, "images_store_ttl_sec", None),
            max_items=getattr(pol, "images_store_max_items", None),
            max_bytes=getattr(pol, "images_store_max_bytes", None),
            storage_dir=Path(VAR_DIR) / "images",
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        # Cache delle trascrizioni STT: TTL dalla policy, gli altri limiti
        # restano quelli del modulo (il budget e' in caratteri di testo, che
        # non ha un equivalente nella policy delle immagini).
        audiostore.configure(ttl_sec=getattr(pol, "stt_chat_cache_ttl_sec", None))
    except Exception:  # noqa: BLE001
        pass
    try:
        sniff.set_sniff_caps(
            max_b64_chars=getattr(pol, "sniff_max_b64_chars", None),
            max_str_chars=getattr(pol, "sniff_max_str_chars", None),
            max_sse_bytes=getattr(pol, "sniff_max_sse_bytes", None),
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        from .keyhealth import set_health_thresholds

        set_health_thresholds(
            streak_dead=getattr(pol, "keyhealth_streak_dead_threshold", None),
            success_ema_floor=getattr(pol, "keyhealth_success_ema_floor", None),
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        from .ctxcompact import set_min_protected_msgs

        set_min_protected_msgs(getattr(pol, "ctxcompact_min_protected_msgs", None))
    except Exception:  # noqa: BLE001
        pass
    try:
        from .toolrepair import set_max_unwrap_depth

        set_max_unwrap_depth(getattr(pol, "toolrepair_max_unwrap_depth", None))
    except Exception:  # noqa: BLE001
        pass
    try:
        fwd.set_upstream_http(
            connect=getattr(pol, "upstream_connect_timeout_sec", None),
            read=getattr(pol, "upstream_read_timeout_sec", None),
            write=getattr(pol, "upstream_write_timeout_sec", None),
            pool=getattr(pol, "upstream_pool_timeout_sec", None),
            max_keepalive=getattr(pol, "upstream_max_keepalive_connections", None),
            max_connections=getattr(pol, "upstream_max_connections", None),
            keepalive_expiry=getattr(pol, "upstream_keepalive_expiry_sec", None),
        )
        fwd.set_retryable_status(getattr(pol, "retryable_status_codes", None))
        fwd.set_effort_incompatible_hosts(getattr(pol, "effort_incompatible_hosts", None))
    except Exception:  # noqa: BLE001
        pass


_apply_misc_policy(policy)


def _coalesce_cache_take(key: str, now: float):
    ent = _coalesce_cache.get(key)
    if ent is None:
        return None
    if now >= ent["exp"]:
        _coalesce_cache.pop(key, None)
        return None
    return ent["res"]


def _coalesce_cacheable(res) -> bool:
    """NON mettere in cache le RISPOSTE D'ERRORE: un 4xx/5xx (o un envelope
    {"error": ...}) non deve avvelenare i retry identici in coda, che invece
    devono poter scalare sul fallback."""
    try:
        obj = res[0] if isinstance(res, tuple) and res else res
        sc = getattr(obj, "status_code", None)
        if isinstance(sc, int) and sc >= 400:
            return False
        if isinstance(obj, dict) and isinstance(obj.get("error"), dict):
            return False
    except Exception:  # noqa: BLE001
        return True
    return True


def _coalesce_cache_put(key: str, res, exp: float) -> None:
    if not _coalesce_cacheable(res):
        return
    _coalesce_cache[key] = {"res": res, "exp": exp}
    while len(_coalesce_cache) > _COALESCE_CACHE_MAX:
        oldest = min(_coalesce_cache, key=lambda k: _coalesce_cache[k]["exp"])
        _coalesce_cache.pop(oldest, None)


def _coalesce_key(payload: dict, extra: str = "") -> str:
    """SHA-256 del payload intero serializzato in modo deterministico."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    # NB: `extra` puo' essere None (profile assente): era un 500 trasparente
    # al client ("unsupported operand type(s) for +") su ogni non-stream.
    return hashlib.sha256(((extra or "") + "|" + raw).encode()).hexdigest()


def _nonstream_hold_redirect(stream: bool, dep: dict | None, qc_json, policy) -> bool:
    """True se una richiesta NON-stream va eseguita col MOTORE STREAM sotto
    hold (parita' stream/non-stream). Hold = flag per-deployment OR policy.
    Kill-switch: policy.nonstream_hold_redirect."""
    if stream:
        return False
    hold = bool((dep or {}).get("hold_until_finish")) or bool(getattr(qc_json, "stream_hold_until_finish", False))
    return hold and bool(getattr(policy, "nonstream_hold_redirect", True))


async def _forward_coalesced(policy_obj, payload: dict, extra_key: str, factory):
    """Coalescing delle richieste identiche in volo (solo non-streaming)."""
    if payload.get("stream"):
        return await factory()
    if not getattr(policy_obj, "request_coalescing_enabled", True):
        return await factory()
    ttl = float(getattr(policy_obj, "request_coalescing_ttl_sec", 60.0) or 60.0)
    max_waiters = int(getattr(policy_obj, "request_coalescing_max_waiters", 10) or 0)
    cache_sec = float(getattr(policy_obj, "request_coalescing_cache_sec", 0.0) or 0.0)
    key = _coalesce_key(payload, extra_key)
    now = time.time()
    if cache_sec > 0:
        hit = _coalesce_cache_take(key, now)
        if hit is not None:
            metrics.inc("nx_coalesce_total", ("hit",))
            return copy.deepcopy(hit)
    leader = False
    _bypass = False
    async with _inflight_lock:
        entry = _inflight_coalesce.get(key)
        if entry is None or entry["future"].done():
            fut = asyncio.get_running_loop().create_future()
            entry = {"future": fut, "waiters": 0}
            _inflight_coalesce[key] = entry
            leader = True
        elif max_waiters > 0 and entry["waiters"] >= max_waiters:
            _bypass = True
        else:
            entry["waiters"] += 1
    if _bypass:
        return await factory()
    if not leader:
        metrics.inc("nx_coalesce_total", ("inflight",))
    else:
        try:
            res = await factory()
        except asyncio.CancelledError:
            # Il LEADER e' stato cancellato (shutdown, disconnect, timeout
            # esterno): i waiter hanno connessioni vive e meritano la
            # risposta. set_exception(CancelledError) li ucciderebbe TUTTI
            # con un errore che non e' loro -> future.cancel() +
            # fallback sotto li fa riprovare da soli. cancel() e non
            # set_exception() perche' non trattiene la traceback.
            if entry["waiters"] > 0 and not entry["future"].done():
                entry["future"].cancel()
            _inflight_coalesce.pop(key, None)
            raise
        except BaseException as exc:  # noqa: BLE001
            if entry["waiters"] > 0 and not entry["future"].done():
                entry["future"].set_exception(exc)
            _inflight_coalesce.pop(key, None)
            raise
        if not entry["future"].done():
            entry["future"].set_result(res)
        _inflight_coalesce.pop(key, None)
        if cache_sec > 0:
            # deep copiamo SUBITO: il leader continuera' a mutare `data`
            # (model/nx_deployment/annotate) e la cache non deve seguirlo
            _coalesce_cache_put(key, copy.deepcopy(res), time.time() + cache_sec)
        return res
    try:
        res = await asyncio.wait_for(asyncio.shield(entry["future"]), ttl)
    except asyncio.TimeoutError:
        return await factory()
    except asyncio.CancelledError:
        # Il LEADER e' morto (cancellato): la nostra connessione e' viva,
        # quindi NON dobbiamo propagare la sua morte. Solo se il task
        # corrente e' stato davvero cancellato dall'esterno (client andato
        # via, shutdown) il CancelledError va propagato: cancelling() > 0.
        if asyncio.current_task().cancelling() > 0:
            raise
        return await factory()
    return copy.deepcopy(res)


def _load_adaptive_stats() -> None:
    """All'avvio: ripristina EMA/last_used/cooldown dal file (F4)."""
    if not PERSIST_STATS:
        return
    try:
        data = _load_json(_stats_file, dict)
        if data:
            router.load_stats(data)
            log.info("[stats] ripristinate da %s (%d deployment tracciati)", _stats_file.name, len(router._stats))
    except Exception as exc:  # mai bloccare lo startup
        log.warning("[stats] load fallito (%s): riparto pulito", exc)


# --- thought_signature sidecar: persistenza firme Gemini 3 (tool calling) ----
_thought_sigs_file = VAR_DIR / "thought_sigs.json"


def _load_thought_sigs() -> None:
    """All'avvio: ripristina le firme Gemini catturate (sopravvivono al restart)."""
    if not PERSIST_STATS:
        return
    try:
        data = _load_json(_thought_sigs_file, dict)
        if data:
            from .thought_sig import THOUGHT_SIGS

            THOUGHT_SIGS.load(data)
            log.info("[thought_sig] ripristinate %d firme da %s", len(THOUGHT_SIGS), _thought_sigs_file.name)
    except Exception as exc:  # mai bloccare lo startup
        log.warning("[thought_sig] load fallito (%s): riparto pulito", exc)


def _maybe_save_thought_sigs(force: bool = False) -> None:
    """Salvataggio atomico throttled (max ogni 60s) delle firme Gemini."""
    global _last_stats_save
    if not PERSIST_STATS:
        return
    now = time.time()
    if not force and now - _last_stats_save < 60:
        return
    from .thought_sig import THOUGHT_SIGS

    if not _save_json(_thought_sigs_file, THOUGHT_SIGS.dump()):
        log.warning("[thought_sig] save fallito")


def _maybe_save_adaptive_stats(force: bool = False) -> None:
    """Salvataggio atomico throttled (max ogni 60s) delle stats adattive."""
    global _last_stats_save
    if not PERSIST_STATS:
        return
    now = time.time()
    if not force and now - _last_stats_save < 60:
        return
    _last_stats_save = now
    if not _save_json(_stats_file, router.dump_stats()):
        log.error("[stats] save fallito")


def _maybe_save_all(force: bool = False) -> None:
    """F26: stats adattive (EMA per bucket, prefill-rate, calibration) e stato
    di routing (holder/sticky/warm/frontier) sono due viste DELLO STESSO
    istante. Salvandole con timer indipendenti, un crash nel mezzo lasciava
    holder freschi con EMA vecchie (o viceversa). Qui il throttle e' unico:
    o si flushano insieme, o nessuna delle due."""
    global _last_stats_save, _last_routing_save
    now = time.time()
    if not force and now - min(_last_stats_save, _last_routing_save) < 60:
        return
    _last_stats_save = now
    _last_routing_save = now
    _maybe_save_adaptive_stats(force=True)
    _maybe_save_routing_state(force=True)


def _load_cooldowns() -> None:
    """All'avvio: ripristina i cooldown NON scaduti dal file dedicato."""
    if not PERSIST_STATS:
        return
    try:
        data = _load_json(_cooldown_file, dict)
        if data:
            n = router.load_cooldowns(data)
            log.info("[cooldown] ripristinati %d cooldown da %s", n, _cooldown_file.name)
    except Exception as exc:  # mai bloccare lo startup
        log.warning("[cooldown] load fallito (%s): riparto pulito", exc)


def _maybe_save_routing_state(force: bool = False) -> None:
    """Snapshot throttled (max ogni 60s + force allo shutdown) dello stato di
    routing legato alle sessioni: il restart non deve azzerare il pool caldo."""
    global _last_routing_save
    if not PERSIST_ROUTING:
        return
    now = time.time()
    if not force and now - _last_routing_save < 60:
        return
    _last_routing_save = now
    _exc: BaseException | None = None
    try:
        ok = _save_json(_routing_file, router.dump_routing_state())
    except Exception as e:  # noqa: BLE001 - mai bloccare lo shutdown
        ok = False
        _exc = e
    if not ok:
        # save_json() e' best-effort: non solleva, logga gia' l'errore REALE
        # con traceback in atomic_store. Qui registriamo il fatto (stato di
        # routing perso -> warm pool perso al restart). Un solo call site per
        # i due rami: `exc_info` riceve l'eccezione VERA quando c'e', altrimenti
        # False (non None!) -> niente "NoneType: None", che e' un'evidenza
        # falsa in on-call.
        log.error("[warmstart] save fallito", exc_info=_exc or False)


def _load_routing_state() -> None:
    """All'avvio: ripristina lo stato di routing NON scaduto (validazione e
    TTL dentro load_routing_state)."""
    if not PERSIST_ROUTING:
        return
    try:
        data = _load_json(_routing_file, dict)
        if data:
            rep = router.load_routing_state(data)
            if any(rep.values()):
                log.info("[warmstart] ripristinato %s", rep)
    except Exception as exc:  # mai bloccare lo startup
        log.warning("[warmstart] load fallito (%s): riparto freddo", exc)


def _bootstrap_runtime_from_logs() -> None:
    """All'avvio: ricostruisce le finestre rolling-24h (uso + probe) dal log,
    cosi' il cold-spread e il moltiplicatore dell'autoprobe non ripartono
    'a freddo'. Scansiona solo `gateway.log` (nessun ruotato). La scansione e'
    in `app.logboot.scan_log`. Mai bloccare lo startup."""
    from . import autoprobe as _ap
    from .logboot import scan_log

    path = os.environ.get("GATEWAY_LOG_FILE", str(VAR_DIR / "gateway.log"))
    try:
        usage, probes = scan_log(path, time.time() - 86400.0)
    except Exception as exc:  # mai bloccare lo startup
        log.warning("[bootstrap] scan log fallito (%s)", exc)
        return
    for u, ts in usage:
        router.note_usage(u, ts)
    for u, ts in probes:
        _ap.note_probe_time(u, ts)
    if usage or probes:
        log.info("[bootstrap] finestre 24h da log: %d tentativi, %d probe", len(usage), len(probes))


def _maybe_save_cooldowns(force: bool = False) -> None:
    """Salvataggio atomico throttled (max ogni 60s) dei cooldown attivi."""
    global _last_cooldown_save
    if not PERSIST_STATS:
        return
    now = time.time()
    if not force and now - _last_cooldown_save < 60:
        return
    _last_cooldown_save = now
    if not _save_json(_cooldown_file, router.save_cooldowns()):
        log.error("[cooldown] save fallito")


def _all_uniques() -> set:
    """Set degli unique attualmente configurati (per il diff hot-reload)."""
    out: set = set()
    for _lst in config.groups.values():
        for _d in _lst:
            out.add(_d.get("unique"))
    return out


def _all_deps() -> dict:
    """unique -> dep dict di tutti i deployment configurati (per il drain
    hot-reload: serve il dep VECCHIO di quelli rimossi)."""
    out: dict = {}
    for _lst in config.groups.values():
        for _d in _lst:
            out[_d.get("unique")] = _d
    return out


async def _watcher(interval: float) -> None:
    """Ogni `interval` secondi controlla mtime di CSV (credenziali) e
    gateway.yaml (policy) e ricarica ciò che è cambiato.

    Un file corrotto/mancante NON abbatta il servizio: lo stato vecchio resta vivo.
    """
    last_csv: int | None = None
    last_yaml: int | None = None
    _prev_uniques = _all_uniques()
    _prev_deps = _all_deps()
    # Lock seriale del reload: un reload in corso NON viene sovrapposto ma
    # serializzato (insieme al suo post-processing probe/drain). Il lock
    # sincrono in config.reload() protegge invece il momento dello swap.
    _reload_lock = asyncio.Lock()
    while True:
        try:
            router.purge_expired()  # igiene: sticky/cooldown scaduti
            router.purge_draining()  # draining scaduti oltre il TTL
            _maybe_save_all()  # F26: stats+routing, stesso istante
            # giro giornaliero sui RITIRATI: parte al primo tick dopo
            # mezzanotte e li sonda con calma (un probe riuscito riabilita)
            if not background_cautious_enabled():  # cautela: nessun probe automatico
                autoprobe.maybe_spawn_retired(router, forwarder)
            _maybe_save_cooldowns()  # cooldown attivi su disco
            _maybe_save_thought_sigs()  # firme Gemini: persistite su disco
            await LEDGER.flush_async()  # ledger usage: offload su thread
            await repairlog.flush_async()  # ledger riparazioni: idem
            # keyhealth: osserva TUTTI i deployment con stats e aggiorna
            # l'evidenza su disco (throttled dal tick stesso)
            try:
                now = time.time()
                for u, s in list(router._stats.items()):
                    cooled = router._cooldown.get(u, 0) > now
                    KEYHEALTH.observe(
                        u,
                        fail_streak=s.fail_streak,
                        success_ema=s.success_ema,
                        is_cooled=cooled,
                        reason=getattr(s, "last_reason", None),
                        now=now,
                    )
                new_retired = KEYHEALTH.apply_retirement(policy.retire_after_days)
                if new_retired:
                    log.warning(
                        "[keyhealth] %d chiavi passate RETIRED: %s", len(new_retired), ", ".join(new_retired[:5])
                    )
                KEYHEALTH.save()
            except Exception:  # analytics non deve mai mordere
                log.warning("[keyhealth] tick error", exc_info=True)
            # purge job video scaduti (mapping in memoria, TTL 24h)
            now = time.time()
            expired = [j for j, m in _videos_jobs.items() if now - m.get("created", 0) > VIDEO_JOB_TTL_SEC]
            for j in expired:
                _videos_jobs.pop(j, None)
            # purge immagini scadute (store in memoria, TTL policy images.*)
            try:
                imagestore.sweep()
            except Exception:  # noqa: BLE001
                log.warning("[images] sweep error", exc_info=True)

            async with _reload_lock:
                new = maybe_reload(config, last_csv)
                if last_csv is not None and new != last_csv:
                    log.info(
                        "[config] CSV ricaricato: profili=%s deployment=%d",
                        ",".join(config.profiles),
                        sum(len(v) for v in config.groups.values()),
                    )
                    try:
                        router.apply_quirks()  # flag in-memory (P2-9)
                    except Exception:
                        pass
                    # Probe immediato dei deployment appena aggiunti: scoprono lo
                    # stato di salute PRIMA del traffico reale (vedi autoprobe).
                    _added = sorted(_all_uniques() - _prev_uniques)
                    _max_p = max(0, int(getattr(policy, "hotreload_probe_max", 20) or 0))
                    if _added and _max_p and not background_cautious_enabled():
                        autoprobe.spawn_hotreload_probe(router, forwarder, _added[:_max_p])
                        log.info("[hotreload] %d deployment nuovi: probe fire-and-forget", min(len(_added), _max_p))
                    # CONNECTION DRAINING: i deployment rimossi dal CSV con
                    # richieste in volo restano in config marcati draining
                    # (ignorati dal pick per le nuove richieste); l'inflight
                    # viene drenato in note_end, il TTL li forza comunque via.
                    _cur = _all_uniques()
                    for _u in sorted(set(_prev_deps) - _cur):
                        _inf = router.stats_for(_u).inflight
                        if _inf > 0:
                            router.start_draining(_u, _prev_deps[_u], _inf)
                            log.info(
                                "[drain] %s: rimosso dal CSV con %d richieste in volo -> draining (TTL %ds)",
                                _u,
                                _inf,
                                int(getattr(policy, "hotreload_drain_ttl_sec", 120) or 120),
                            )
                        else:
                            log.debug("[drain] %s: rimosso dal CSV, nessuna richiesta in volo -> drop immediato", _u)
                last_csv = new
                _prev_uniques = _all_uniques()
                _prev_deps = _all_deps()

            ym = csv_mtime_ns(POLICY_PATH)
            if ym is not None and last_yaml is not None and ym != last_yaml:
                try:
                    fresh = Policy.load(POLICY_PATH)
                except Exception as exc:
                    log.warning("[policy] reload FALLITO (%s): resta la precedente", exc)
                else:
                    router.policy = fresh  # swap atomico dei riferimenti
                    globals()["policy"] = fresh
                    set_retry_after_floors(fresh.retry_after_min_sec, fresh.retry_after_floor_by_provider)
                    set_stream_stall_sec(fresh.stream_stall_sec)
                    set_strip_client_fields(fresh.strip_client_fields)
                    set_ttft_lookup(lambda u, ctx=None: router.bucket_latency_ms(u, ctx, "ttft"))
                    set_stall_bucket(multiplier=fresh.stream_stall_ttft_mult, max_sec=fresh.stream_stall_max_sec)
                    set_estimate_defaults(fresh.estimate_divisor, getattr(fresh, "image_token_estimate", 0) or 0)
                    set_adaptive_timeout(
                        enabled=fresh.adaptive_timeout_enabled,
                        floor_sec=fresh.adaptive_timeout_floor_sec,
                        multiplier=fresh.adaptive_timeout_multiplier,
                        max_sec=fresh.adaptive_timeout_max_sec,
                    )
                    set_reasoning_reserve(1.0 - float(getattr(fresh, "cache_ctx_reasoning_headroom_ratio", 0.7) or 0.0))
                    apply_cooldown_policy(fresh)
                    _apply_misc_policy(fresh)
                    set_schemaout_config(_so_cfg_from_policy(fresh))
                    forwarder._keepalive_pool = fresh.http_keepalive_pool
                    configure_estimate(
                        adaptive=fresh.estimate_adaptive_enabled,
                        shadow=fresh.estimate_adaptive_shadow,
                        auto_enable=fresh.estimate_adaptive_auto_enable,
                        auto_min_n=fresh.estimate_adaptive_auto_min_n,
                        auto_max_delta_pct=(fresh.estimate_adaptive_auto_max_delta_pct),
                    )
                    log.info(
                        "[policy] ricaricata: step_up=%s%% aliases=%d per-profilo=%s",
                        fresh.step_up_pct,
                        len(fresh.aliases),
                        fresh.profile_step_up_pct or "-",
                    )
            if ym is not None and last_yaml is None:
                log.info("[policy] watcher: baseline %s", POLICY_PATH.name)
            last_yaml = ym
        except Exception as exc:  # mai far morire il watcher
            log.warning("[config] watcher error: %s", exc)
        await asyncio.sleep(interval)


def seconds_to_midnight(now: float | None = None) -> float:
    """Secondi alla PROSSIMA mezzanotte locale (helper puro, testabile)."""
    import datetime as _dt

    now_ts = time.time() if now is None else now
    local = _dt.datetime.fromtimestamp(now_ts)
    nxt = (local + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (nxt - local).total_seconds())


async def _nightly_scheduler():
    """Giro NOTTURNO dell'autoprobe: alle 00:00 locali (+ jitter 0-120s) un
    solo passaggio completo su tutti i profile testo. In modalita' 'request'
    il task resta vivo ma non fa nulla (puo' essere riattivato a caldo)."""
    while True:
        try:
            await asyncio.sleep(seconds_to_midnight() + random.uniform(0.0, 120.0))
            if (
                str(getattr(router.policy, "cooldown_autoprobe_schedule", "nightly")).lower() == "nightly"
                and autoprobe._cfg(router.policy)[0]
                and not background_cautious_enabled()
            ):
                await autoprobe.nightly_pass(router, forwarder)
        except asyncio.CancelledError:
            raise
        except Exception:  # il scheduler non muore MAI
            log.warning("[autoprobe] nightly scheduler: errore", exc_info=True)
            await asyncio.sleep(60.0)


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
    authn.enforce_startup()
    log.info("[start] env=%s production=%s", gateway_env(), authn.production)
    _load_adaptive_stats()  # F4: ripristino EMA/cooldown
    _load_cooldowns()  # cooldown NON scaduti (since/full)
    _load_routing_state()  # warm-start: holder/sticky/warm/pin/frontiere
    _bootstrap_runtime_from_logs()  # finestre 24h uso/probe dal log
    _load_thought_sigs()  # firme Gemini: sopravvivono al restart
    _maybe_save_adaptive_stats(force=True)  # baseline subito
    _watch_task = asyncio.create_task(_watcher(WATCH_SECONDS))
    _cautious = background_cautious_enabled()
    if _cautious:
        log.warning(
            "[start] modalita' CAUTA generica (BACKGROUND_CAUTIOUS): probe/health/nightly automatici DISATTIVATI"
        )
    else:
        _health_task = asyncio.create_task(health_loop(router, policy.health_interval_sec))
    if not _cautious:
        _nightly_task = asyncio.create_task(_nightly_scheduler())
    log.info(
        "[start] %s su %s:%d · profili=%s · deployment=%d",
        policy.service_name,
        HOST,
        PORT,
        ",".join(config.profiles),
        sum(len(v) for v in config.groups.values()),
    )
    try:
        yield
    finally:
        for task in (_watch_task, _health_task, _nightly_task):
            if task:
                task.cancel()
        # Graceful shutdown: uvicorn ha gia' smesso di accettare nuove
        # richieste; attendiamo il drain di quelle in volo (best-effort, con
        # deadline) prima del flush finale, per non troncare risposte.
        try:
            _drain = float(getattr(policy, "shutdown_drain_sec", 0.0) or 0.0)
            _infl = router.inflight_total()
            if _infl:
                log.info("[shutdown] drain di %d richieste in volo (max %.1fs)...", _infl, _drain)
            _deadline = time.monotonic() + _drain
            while router.inflight_total() > 0 and time.monotonic() < _deadline:
                await asyncio.sleep(0.2)
            _left = router.inflight_total()
            if _left:
                log.warning("[shutdown] drain scaduto: %d richieste ancora in volo", _left)
            elif _infl:
                log.info("[shutdown] drain completato")
        except Exception:  # noqa: BLE001
            pass
        # Task di background (canary/probe/sveglie): vanno cancellati PRIMA di
        # chiudere il client httpx, altrimenti i probe in volo esplodono sul
        # client chiuso, sporcano lo shutdown e possono far saltare il
        # salvataggio atomico finale.
        try:
            _np = await _drain_probe_tasks()
            if _np:
                log.info("[shutdown] cancel di %d probe/sveglie in volo", _np)
        except Exception:  # noqa: BLE001
            pass
        await forwarder.aclose()
        _maybe_save_all(force=True)  # F26: stats+routing insieme
        _maybe_save_cooldowns(force=True)  # cooldown: salva allo shutdown
        _maybe_save_thought_sigs(force=True)  # firme Gemini: salva allo shutdown
        _rows = LEDGER.flush_sync()  # ledger: nessuna riga persa
        log.info("[shutdown] ledger flush_sync: %d righe salvate", _rows)
        _rrows = repairlog.flush_sync()  # ledger riparazioni
        if _rrows:
            log.info("[shutdown] repair flush_sync: %d righe salvate", _rrows)


app = FastAPI(title=policy.service_name, version="0.2.0", lifespan=lifespan)
app.include_router(admin_api)
app.include_router(bootstrap_api)

# --- Observability: Trace ID, JSON logging, Prometheus /metrics ---
from .observability import (
    setup_observability,
    setup_replay_endpoint,
    render_prometheus,
)

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


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=401, content={"error": {"message": detail, "type": "auth_error", "param": None, "code": "401"}}
    )


def _forbidden(model: str, profile: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": f"Model '{model}' not allowed for this key" + (f" (profile '{profile}')" if profile else ""),
                "type": "permission_error",
                "param": None,
                "code": "403",
            }
        },
    )


# ------------------------------------------------------------------- health


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
        temp_enabled=policy.enable_effort_temperature_override,
        temp_overrides=policy.effort_temperature_overrides,
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
    set_avoid_gemini(has_unsigned_tool_calls(messages) and not policy.thought_sig_dummy_fill)
    set_dummy_fill(policy.thought_sig_dummy_fill, policy.thought_sig_dummy_value)

    # --- normalizzazione del nome richiesto:
    #     1) prefisso STORICO -> prefisso corrente (compatibilità client)
    #     2) alias (gateway.yaml) -> nome canonico
    model = policy.canonicalize(raw_model)

    # --- auth ---
    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)

    # --- autorizzazione modello (whitelist tre livelli, sul nome canonico) ---
    if not authn.authorize_model(auth, model):
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
    if router.policy.routing_active():
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
    router.note_session_activity(session_id)
    # RATE per-sessione (SOLO chat): alimenta warm_ready_min adattivo.
    router.note_session_request(session_id)

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
    _img_est = getattr(router.policy, "image_token_estimate", 0) or 0
    # Base di stima = payload con la sola histnorm (preview deterministica,
    # PRIMA di ctxcompact): la STESSA base su cui si apprende e si applica il
    # rapporto per-sessione, cosi' i char contati coincidono tra hook usage e
    # routing.
    _est_msgs = messages
    try:
        from .histnorm import hist_config_from_policy, normalize_messages

        _est_msgs, _ = normalize_messages(
            messages, hist_config_from_policy(router.policy), tail_floor=router.ctx_boundary_floor(session_id)
        )
    except Exception:  # noqa: BLE001
        _est_msgs = messages
    _est_chars_pre = _prompt_chars(_est_msgs, payload.get("tools"))
    _cpt = router.session_chars_per_token(session_id)
    if _cpt:
        # Dal 2o turno, DUE stime dalla stessa base pre-compressione:
        #  ctx_dim = token POST-compressione previsti -> scelta della dim;
        #  ctx_est = token PRE-compressione -> sicurezza (ctxcompact/overflow).
        ctx_dim, _ = router.estimate_for_session(
            session_id, _est_msgs, router.policy.estimate_divisor, _img_est, tools=payload.get("tools"), pre=True
        )
        ctx_est, _ = router.estimate_for_session(
            session_id, _est_msgs, router.policy.estimate_divisor, _img_est, tools=payload.get("tools")
        )
        metrics.inc("nx_sess_est_used_total")
        log.info(
            "[estimate] sess cpt_pre=%.2f cpt_post=%.2f -> ctx_dim≈%d ctx_pre≈%d (chars_pre=%d, +%.0f%%)",
            router.session_chars_per_token(session_id, pre=True),
            _cpt,
            ctx_dim,
            ctx_est,
            _est_chars_pre,
            (float(getattr(router.policy, "session_estimate_margin", 1.05)) - 1.0) * 100.0,
        )
    else:
        # 1o turno della sessione: stima euristica basata sui soli char.
        ctx_est = estimate_tokens(messages, router.policy.estimate_divisor, _img_est, tools=payload.get("tools"))
        ctx_dim = ctx_est
        metrics.inc("nx_sess_est_fallback_total")

    group_or_explicit = router.resolve_group_for_request(
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
            _missing = router._missing_media_caps(model, need)
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
                    "message": f"model '{model}' not managed by {policy.service_name}",
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
            _turn_go = router.note_session_turn(session_id)
        except Exception:  # noqa: BLE001
            _turn_go = False
    group_or_explicit, _refund_go = _apply_go_refund(router, group_or_explicit, auth.profile, _turn_go, session_id)

    explicit_req = router.is_explicit(model)
    # Se il client chiama esplicitamente un gruppo diverso (es. -200k -> -1000k
    # o -go), rilascia lo sticky per-deployment cosi' la richiesta esplicita
    # atterra sul nuovo gruppo/key scelta dal routing, non resta incollata al
    # vecchio deployment dello sticky precedente.
    if (explicit_req or _refund_go) and session_id:
        cur = router.dep_sticky_get(session_id)
        sd = router.config.deployment_by_unique(cur) if cur else None
        # Rilascia dep-sticky SOLO se il gruppo è cambiato o non c'è sticky:
        # se la richiesta esplicita punta allo stesso gruppo dello sticky,
        # lo preserviamo per la cache-preserving (misma key per sessione).
        if sd is None or sd.get("group") != group_or_explicit:
            router.dep_sticky_release(session_id)
        # Per richieste esplicite su un dim (-Nk): riàncora lo sticky di
        # gruppo cosi' le successive NON-esplicite continuano nel contesto
        # scelto dall'utente (crescita cache-preserving). Per -go/-fallback/
        # unique: libera lo sticky di gruppo, altrimenti il traffico
        # automatico verrebbe parcheggiato in un bucket a pagamento.
        if re.search(r"-\d+k$", group_or_explicit):
            router.sticky_set(session_id, group_or_explicit)
        else:
            router.sticky_release(session_id)

    dep = router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        # F31 fail-fast ingresso: se il ctx non entra nel gruppo (max_input di
        # tutti i dep < ctx) si compatta FORZANDO il gate min_saved e si
        # ricalcola; se resta sopra si risponde 400 sintetico senza toccare
        # l'upstream. Evita 2-3 tentativi di catena e 10-15s di prefill inutile.
        _max_grp = 0
        try:
            _max_grp = max(
                (int(d.get("max_input_tokens") or 0) for d in (router.config.groups.get(group_or_explicit) or [])),
                default=0,
            )
        except Exception:
            _max_grp = 0
        _zen_first = False
        try:
            _zen_first = router._zen_first_active()
        except Exception:  # noqa: BLE001
            _zen_first = False
        # Zen-first: si tiene conto anche della RISERVA DI OUTPUT (il picker
        # richiede ctx+out <= max_input) e si compatta per rientrare nel tier
        # zen. Per i non-nativi resta lo storico 5% di margine sull'input.
        try:
            _out_res = int(refill_out_budget(payload, router.policy) or 0)
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

                _ccf = ctxcompact_config_from_policy(router.policy)
                _ccf.min_saved_tokens = 0
                _img = getattr(router.policy, "image_token_estimate", 0) or 0
                _forced, _frep = compact_tool_outputs(
                    payload.get("messages") or [],
                    _ccf,
                    max_in=_budget,
                    estimator=lambda ms: router.estimate_for_session(
                        session_id, ms, router.policy.estimate_divisor, _img
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
                ctx_est = router.estimate_for_session(
                    session_id,
                    payload.get("messages") or [],
                    router.policy.estimate_divisor,
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
                    _up = router.climb_dim_group(group_or_explicit, ctx_est + (_out_res if _zen_first else 0))
                if _up:
                    log.info(
                        "[dim] ctx≈%d non entra in %s (max %d): salgo a %s", ctx_est, group_or_explicit, _max_grp, _up
                    )
                    group_or_explicit = _up
                    if explicit_req and session_id:
                        router.sticky_set(session_id, _up)
                else:
                    from .ctxcompact import compact_tool_outputs, ctxcompact_config_from_policy

                    # NB: CtxCompactConfig NON e' un dataclass -> niente
                    # `dataclasses.replace` (TypeError a runtime: era il bug di
                    # produzione). E' un'istanza fresca per chiamata: si muta il campo.
                    _ccf = ctxcompact_config_from_policy(router.policy)
                    _ccf.min_saved_tokens = 0
                    _img = getattr(router.policy, "image_token_estimate", 0) or 0
                    _forced, _frep = compact_tool_outputs(
                        payload.get("messages") or [],
                        _ccf,
                        max_in=_max_grp,
                        estimator=lambda ms: router.estimate_for_session(
                            session_id, ms, router.policy.estimate_divisor, _img
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
                    ctx_est = router.estimate_for_session(
                        session_id,
                        payload.get("messages") or [],
                        router.policy.estimate_divisor,
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
        _grp_is_dim = router.config.group_caps.get(group_or_explicit) is None and not router._is_renewal_bucket(
            group_or_explicit
        )
        _warm = (not explicit_req) or _grp_is_dim
        # CACHE PAGATA: SOLO su richiesta esplicita a -go/-fallback testo:
        # riusa la stessa chiave della sessione (KV-cache calda) anche se sta
        # su un tier di rinnovo peggiore; al 429 il holder si esclude da solo
        # e la rotazione prosegue nell'ordine normale (crediti "sommati" un
        # account alla volta). Auto-routing ed escalation interne non lo usano.
        _go_suf = router.config.go_suffix or "-go"
        _fb_suf = router.config.fallback_suffix or "-fallback"
        _paid_holder = explicit_req and (group_or_explicit.endswith(_go_suf) or group_or_explicit.endswith(_fb_suf))
        # PARITA' stream/non-stream sotto HOLD: se questa richiesta non-stream
        # sara' servita dal MOTORE STREAM (redirect hold, vedi _redirect sotto),
        # anche il pick iniziale deve ordinare il warm come lo stream
        # (prefer_fast=False). Qui `dep` non esiste ancora: l'intento si ricava
        # dalla policy (il flag per-deployment resta gestito dal ramo a valle).
        _pre_redirect = _nonstream_hold_redirect(stream, None, router.policy.qc_json, router.policy)
        dep = router.initial_pick(
            auth.profile,
            group_or_explicit,
            None if explicit_req else need,
            ctx_dim,
            session_id=session_id,
            warm=_warm,
            prefer_holder=_paid_holder,
            prefer_fast=(not stream) and not _pre_redirect,
            out_tokens=refill_out_budget(payload, router.policy),
        )
    if dep is None:
        # F31: se il motivo e' l'overflow (tutti i dep del gruppo hanno
        # max_input < ctx) NON e' un disservizio ma un errore del client:
        # 400 context_length_exceeded invece del 503 "nessun deployment".
        try:
            _mx = max(
                (int(d.get("max_input_tokens") or 0) for d in (router.config.groups.get(group_or_explicit) or [])),
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
    custom_key = router.resolve_alias_key(raw_model, model)
    if custom_key:
        dep = {**dep, "api_key": custom_key}

    profile = auth.profile or config.profile_of_base(model.split("__")[0]) or config.profile_of_base(model)

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
    autoprobe.maybe_spawn(router, forwarder, profile)

    # sticky session SOLO dal routing automatico (nome base): le richieste
    # esplicite (-Nk/-go/-fallback/__univoco) non leggono né scrivono sticky.
    # I bucket renewal (-go/-fallback) NON vengono mai salvati: -go si
    # raggiunge solo esplicitamente o a fine scala (free -> zen -> -go).
    if session_id and not router.is_explicit(model) and not router._is_renewal_bucket(group_or_explicit):
        router.sticky_set(session_id, group_or_explicit)

    # iniezione identità + modello univoco nel payload upstream
    inject_identity(payload, dep, router=router)

    # ---- L1/L2 preprocessing (cache-safe: solo la coda) ----
    from .histnorm import hist_config_from_policy, normalize_messages
    from .sampling import sampling_config_from_policy, apply_sampling_defaults
    from .schemaout import schemaout_config_from_policy, maybe_inject_response_format

    _hn = hist_config_from_policy(router.policy)
    _sm = sampling_config_from_policy(router.policy)
    _so = schemaout_config_from_policy(router.policy)
    _orig_msgs = payload.get("messages")  # pre-normalizzazione
    _orig_for_retry = None
    if _hn.enabled:
        _nm, _nr = normalize_messages(payload.get("messages"), _hn, tail_floor=router.ctx_boundary_floor(session_id))
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

    _cc = ctxcompact_config_from_policy(router.policy)
    _holder = router.session_holder(session_id)
    _max_in = int(dep.get("max_input_tokens") or 0)
    _same_family = False
    if _holder:
        _hd = router.config.deployment_by_unique(_holder)
        if _hd and _hd.get("family") and _hd.get("family") == dep.get("family"):
            _same_family = True
    # F14: correzione per-deployment appresa dal VERO prompt_tokens upstream
    # (tokenizer diverso da chars/4): la decisione di compattazione non deve
    # lavorare su stime sballate.
    try:
        _corr = router.estimate_correction(dep.get("unique", ""))
        if _corr != 1.0:
            _ctx_corr = max(1, int(ctx_est * _corr))
        else:
            _ctx_corr = ctx_est
    except Exception:
        _ctx_corr = ctx_est
    # H2: divisore chars/token CALIBRATO (F14) da usare per il budget della
    # frontiera: il 4 fisso sottostima i token sui tokenizer non-OpenAI.
    try:
        _div_eff = router.effective_divisor(dep.get("unique", ""))
    except Exception:
        _div_eff = float(getattr(router.policy, "estimate_divisor", 4) or 4)
    _dec = should_compact(
        _cc,
        _ctx_corr,
        _max_in,
        _holder,
        dep.get("unique"),
        bool(session_id and router.is_session_compact(session_id)),
        same_family=_same_family,
        reasoning=bool(dep.get("effort_capable")),
    )
    _do_compact = _dec["compact"]
    if _do_compact and session_id:
        router.mark_session_compact(session_id)
    _ctx_saved_hdr = 0
    if _do_compact:
        _cmsgs, _crep = compact_tool_outputs(
            payload.get("messages"),
            _cc,
            max_in=_max_in,
            estimator=lambda ms: router.estimate_for_session(
                session_id,
                ms,
                _div_eff,
                getattr(router.policy, "image_token_estimate", 0) or 0,
                unique=dep.get("unique"),
            )[0],
            boundary_floor=router.ctx_boundary_floor(session_id),
            divisor=_div_eff,
        )
        if _crep.get("changed"):
            payload["messages"] = _cmsgs
            router.note_compact_boundary(session_id, _crep.get("boundary"))
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
    if getattr(router.policy, "cache_prefix_audit", True) and session_id:
        _bnd = _crep.get("boundary") if (_do_compact and _crep.get("changed")) else None
        if _bnd is None:
            try:
                _bnd = frontier_boundary(
                    payload.get("messages") or [], _cc, _max_in, router.ctx_boundary_floor(session_id), _div_eff
                )
            except Exception:  # noqa: BLE001
                _bnd = None
        _aud = router.audit_prefix(session_id, payload.get("messages") or [], _bnd)
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
        if sniff.enabled(router.policy):
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

    qc_pol = router.policy.qc_json
    attempts_box: list[str] = []
    # HOLD-UNTIL-FINISH + richiesta non-stream: esegui il MOTORE STREAM (sotto
    # hold bufferizza l'intera risposta) e restituisci non-stream. Un solo
    # motore per entrambi -> comportamento identico. Kill-switch:
    # policy.nonstream_hold_redirect.
    _redirect = _nonstream_hold_redirect(stream, dep, qc_pol, router.policy)

    async def _redirect_once():
        from .protocols import sse_to_chat_obj

        _sp = dict(payload)
        _sp["stream"] = True
        try:
            if _sm.enabled:
                apply_sampling_defaults(_sp, dep, _sm)
            maybe_inject_response_format(_sp, dep, _so)
        except Exception:  # noqa: BLE001
            pass
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
            res = await _forward_coalesced(router.policy, payload, profile, _redirect_once)
        else:

            async def _fwd_once():
                return await forwarder.call_with_fallback(
                    router,
                    profile,
                    dep,
                    payload,
                    collect_qc_failures=bool(qc_pol.enabled or router.policy.qc_sanity.enabled),
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

            res = await _forward_coalesced(router.policy, payload, profile, _fwd_once)
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
        _last_d = (router.config.deployment_by_unique(_last_u) if _last_u else None) or dep
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
            len(attempts_box), err.detail, prefix_reason=_aud, trail=_trail, retry_at_ms=_retry_at_ms(router, _trail)
        )
    data, used = res[0], res[1]
    qc_failed = res[2] if len(res) > 2 else []

    # Divulgazione del modello nel campo "model" della risposta
    # (policy.response_model, vedi app/policy.py): nx_deployment è SEMPRE
    # presente con il deployment univoco realmente usato.
    if isinstance(data, dict):
        disc = router.policy.response_model
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
                router.note_estimate_error(used["unique"], ctx_est, _u_f14["prompt_tokens"])
                # Stima per-sessione: char REALI del payload inviato a monte
                # (post inject_identity/histnorm/ctxcompact) / prompt_tokens.
                router.note_session_estimate(
                    session_id,
                    _est_chars_pre,
                    _prompt_chars(payload.get("messages"), payload.get("tools")),
                    _u_f14["prompt_tokens"],
                )
                metrics.inc("nx_sess_est_samples_total")
        except Exception:
            pass
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
        _note_fb_refund(router, session_id, max(0, len(attempts_box) - 1))
    if sniff.enabled(router.policy):
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
            _wk = router.warm_api_keys(session, profile, requested_group or dep.get("group"))
            _xk = set((raced or {}).get("keys") or ()) | _wk
            _xk.add(str(dep.get("api_key") or ""))
            _ex_uniq = (raced or {}).get("uniq")
            _B = router.warm_fill_canary(
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
                    _age = float(getattr(router.policy, "warm_refill_wake_min_cooldown_age_sec", 3600.0) or 3600.0)
                except Exception:
                    _age = 3600.0
                _W = router.warm_wake_canary(
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
                    _Z = router.warm_fill_canary(
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
                            getattr(router.policy, "warm_refill_wake_min_cooldown_age_sec", 3600.0) or 3600.0
                        )
                    except Exception:  # noqa: BLE001
                        _age_z = 3600.0
                    try:
                        _Z = router.warm_wake_canary(
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
            _excl = set(router._sess_deps().get(session, ())) if fresh_only else None
            cands = router.hedge_canaries(
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
            _mx = int(getattr(router.policy, "warm_refill_max_inflight", 6) or 6)
        except Exception:
            _mx = 6
        try:
            _free = max(0, _mx - int(router.probes_in_flight(session)))
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
        inject_identity(p2, B, router=router)

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
            router.mark_failed(_u, seconds=_tct_cfg.cooldown_sec, reason="truncated_toolcall")

        router.note_start(_bu, ctx)
        if refill and raced is not None:
            raced.setdefault("uniq", set()).add(_bu)
            raced.setdefault("keys", set()).add(str(B.get("api_key") or ""))
        genB = None
        try:
            genB = await forwarder.stream_response(
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
                rate_hook=lambda u, rl: router.note_rate_limit(u, rl),
            )
            if router.is_cooled_down(_bu):
                router.clear_cooldown(_bu)
            futB = asyncio.ensure_future(_peek(genB, router.first_content_deadline_ms(_bu, ctx)))
            with contextlib.suppress(Exception):
                router.note_probe_started(session, _bu)
            return {"dep": B, "gen": genB, "t0": tB, "fut": futB, "wake": bool(wake)}
        except asyncio.CancelledError:
            if genB is not None:
                await _discard_stream(genB, None)
            with contextlib.suppress(Exception):
                router.note_end(_bu, ctx)
            raise
        except BaseException as exc:
            if genB is not None:
                await _discard_stream(genB, None)
            with contextlib.suppress(Exception):
                router.note_end(_bu, ctx)
            # REGE (utente): un errore durante il canary va in cooldown
            # COME AL SOLITO: 429/5xx transitori -> soft cooldown calibrato
            # sui fallimenti 24h (retry_after numerico ha priorita').
            # ECCEZIONE: il rifiuto "replay del reasoning" e' un problema del
            # PAYLOAD (tutte le chiavi del provider lo rifiutano): la chiave
            # e' sana -> nessuna penale (la richiesta principale ripara).
            _rk = reasoning_err_kind(str(exc))
            _qacct = 0
            with contextlib.suppress(Exception):
                _qacct = maybe_account_quota_cooldown(router, B, getattr(exc, "status", None), str(exc))
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
                        learn_thinking_replay(router, B.get("model"))
                    elif _rk == "rejects":
                        learn_strip_reasoning(router, B.get("model"))
                    elif _rk == "history":
                        learn_no_thinking(router, B.get("model"))
            else:
                try:
                    _sec = getattr(exc, "retry_after", None)
                    if not (isinstance(_sec, (int, float)) and _sec > 0):
                        try:
                            _f24 = router.stats_for(_bu).fail_count_24h
                        except Exception:
                            _f24 = 0
                        _sec = _soft_cd(_f24)
                    router.mark_failed(_bu, seconds=_sec, reason="canary_error")
                except Exception:
                    pass
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
                router.mark_session_slow(session, dep.get("unique"))
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
                    router.slow_race_allowed(session, profile, requested_group, need, ctx, out_tokens, tried_set)
                )
            except Exception:  # noqa: BLE001
                _allow = True
            if not _allow:
                metrics.inc("nx_slow_race_total", ("warm_full",))
                log.info(
                    "[slow-race] %s: warm gia' pieno (>=%s), niente canario",
                    dep.get("unique"),
                    getattr(router.policy, "slow_race_max_warm", 6),
                )
            else:
                metrics.inc("nx_slow_race_total", ("open",))
                _lc: list[dict] = []
                with contextlib.suppress(Exception):
                    _lc = router.hedge_canaries(
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
            _PROBE_TASKS.add(_pt)
            _pt.add_done_callback(_PROBE_TASKS.discard)

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
        router.note_probe_done(session, w["dep"]["unique"])
    if w.get("wake"):
        with contextlib.suppress(Exception):
            router.clear_cooldown(w["dep"]["unique"])  # sveglia riuscita
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
_PROBE_TASKS: set = set()


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
        if g.endswith(config.fallback_suffix or "-fallback"):
            return 2
        if g.endswith(router.policy.go_suffix or "-go"):
            return 1
        return 0

    # --- candidati: la CATENA CAPABILITY del profilo, che e' la lista
    #     completa dei deployment con cap `stt` (free + -go + -fallback).
    chains = getattr(router.config, "chains_cap", {}).get(profile or "") or {}
    uniques = list(chains.get("stt") or ())
    cands: list[dict] = []
    seen_u: set[str] = set()
    for u in uniques:
        if u in seen_u:
            continue
        seen_u.add(u)
        d = router.config.deployment_by_unique(u)
        if d is not None:
            cands.append(d)
    if not cands:
        # nessuna catena (routing spento o profilo senza stt): si prova il
        # gruppo -stt direttamente, se esiste.
        grp = None
        if profile:
            for cand in (f"{config.proxy_prefix}{profile}-stt", f"{config.proxy_prefix}{profile}-stt-fallback"):
                if cand in router.config.groups:
                    grp = cand
                    break
        if grp is None:
            return ""
        d = router.config.deployment_by_unique(grp) or router.pick_deployment(grp, need)
        if d is None:
            return ""
        cands = [d]

    # --- ordine: tier, poi i VIVI prima dei raffreddati, poi i liberi prima
    #     di quelli gia' presi da un altro chunk.
    def _key(d: dict):
        return (_tier(d), 1 if router.is_cooled_down(d["unique"]) else 0, 1 if d["unique"] in used else 0)

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
        _was_dormant = router.is_cooled_down(cur)
        router.note_start(cur)
        t0 = time.monotonic()
        try:
            res = await forwarder.transcribe(
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
            router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                router.clear_cooldown(cur)
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
            router.note_end(cur)
            st = abs(err.status) if err.status else 0
            if -err.status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:  # noqa: BLE001
                    pass
            if _was_dormant:
                router.mark_failed_double_residual(cur, reason=detail[:80], status=st or None)
            else:
                router.mark_failed(cur, seconds=err.retry_after, status=st or None)
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

        _cc = ctxcompact_config_from_policy(router.policy)
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
            payload, boundary=boundary, transcript_one=_one, policy=router.policy
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
    dep = first_dep
    # Tetto immagini: una volta sola, prima di qualsiasi tentativo, cosi' vale
    # per TUTTA la catena di fallback (niente righe per-deployment e nessuna
    # copia per tentativo). L'originale resta intatto: i rimedi che
    # ripristinano la history (reasoning replay) continuano a vedere tutto.
    try:
        _imax = int(getattr(router.policy, "chat_images_max", 0) or 0)
    except Exception:  # noqa: BLE001
        _imax = 0
    if _imax > 0 and count_image_parts(payload.get("messages") or []) > _imax:
        payload, _dropped = _trim_chat_images(payload, _imax)
        if _dropped:
            metrics.inc("nx_images_total", ((dep or {}).get("group", "-"), "chat_images_trimmed"))
            log.info(
                "[images] tetto chat_images_max=%d: %d immagini non "
                "inviate all'upstream (restano nella history del client)",
                _imax,
                _dropped,
            )
    # Gruppo ORIGINARIO della richiesta (es. -200k): serve al pin
    # escalation-winner per valere anche dopo la salita su altre dim.
    requested_group = requested_group or (first_dep or {}).get("group")
    tried = 0
    tried_set: set[str] = set()
    _rsn_steps: dict[str, set] = {}  # rimedi reasoning per dep
    _cstr_steps: dict[str, set] = {}  # rimedi content-string per dep
    _rsn_restored = False  # history originale gia' riprovata
    _max_tries = int(
        getattr(router.policy, "max_fallback_tries", os.environ.get("GATEWAY_MAX_FALLBACK_TRIES", "128")) or 128
    )
    # Tool repair config per streaming
    from .toolrepair import create_tool_repair_config
    from .fakecall import fake_config_from_policy, is_escalation_group, looks_like_fake_tool_call, TemplateTokenStripper

    _tr_cfg = create_tool_repair_config(
        {
            "tool_repair": {
                "enabled": router.policy.tool_repair_enabled,
                "default_level": router.policy.tool_repair_default_level,
                "disable_for_google": router.policy.tool_repair_disable_for_google,
                "max_args_size": router.policy.tool_repair_max_args_size,
            },
        }
    )
    _fc = fake_config_from_policy(router.policy)
    from .texttoolparse import (
        text_config_from_policy,
        parse_text_toolcalls,
        strip_toolid_markup,
        truncation_config_from_policy,
    )

    _tt = text_config_from_policy(router.policy)
    _tct_cfg = truncation_config_from_policy(router.policy)
    from .sampling import sampling_config_from_policy

    _sm = sampling_config_from_policy(router.policy)
    from .schemaout import enforce_response, schemaout_config_from_policy
    from .forwarder import _corrective_note, _corrective_kind

    _so = schemaout_config_from_policy(router.policy)
    # QC di contenuto (parita' col non-stream): attivi anche in hold.
    qc = router.policy.qc_json
    san = router.policy.qc_sanity
    _synth: list[bytes] = []
    # OUTPUT STRUTTURATO in HOLD: la risposta bufferizzata viene trattata come
    # non-streaming -> pulizia/riparazione JSON prima di inviare i byte.
    _so_corrected: set[str] = set()  # retry correttivo gia' provato per dep
    _so_rewrite = False  # il content va riscritto in emissione
    _so_text = ""  # content sanificato da inviare
    t_req = time.monotonic()
    try:
        _hedge_ms = int(getattr(router.policy.qc_json, "stream_hedge_delay_ms", 0) or 0)
    except Exception:
        _hedge_ms = 0
    attempts: list[str] = []
    # ATTEMPT TRAIL (P0): per ogni hop fallito, PERCHE' e' stato scartato
    # (classe d'errore onesta). Finisce nel body/header del 503 finale.
    trail: list = []
    skip_hosts: set[str] = set()  # P1-5: host saltati (errore provider)
    _lease = None  # P2-8: lease per chiave (opt-in)

    def _ret(resp):
        """Registra dep/attempts/trail finali per il chiamante (redirect
        non-stream) e ritorna la risposta invariata."""
        if result_box is not None:
            try:
                result_box["dep"] = dep
                result_box["attempts"] = list(attempts)
                # il trail DEVE passare da qui: il redirect non-stream non
                # vede la closure e senza questo finiva con _tr=None -> 503
                # con attempts vuoti e nessun X-Scrocco-Trail.
                result_box["trail"] = list(trail)
            except Exception:  # noqa: BLE001
                pass
        return resp

    def _next_filtered(*a, **k):
        """fallback_next + P1-5: salta gli host che hanno gia' fallito a
        livello provider in QUESTA richiesta; se non ne restano, torna al
        candidato saltato (mai lasciare la richiesta senza risposta)."""
        k.setdefault("out_tokens", refill_out_budget(payload, router.policy))
        _n = router.fallback_next(*a, **k)
        if _n is None or dep_host(_n) not in skip_hosts:
            return _n
        _saved = _n
        for _ in range(8):
            tried_set.add(_saved["unique"])
            _c = router.fallback_next(*a, **k)
            if _c is None:
                return _saved
            if dep_host(_c) not in skip_hosts:
                return _c
            _saved = _c
        return _saved

    _races_done = 0
    # WARM-REFILL a cascata: candidati gia' sonciati in QUESTA richiesta
    # (uniq + api_key) e round gia' consumati (budget per-richiesta =
    # warm_refill_max_inflight; il tetto GLOBALE e' il registro in volo per
    # sessione nel router).
    _raced: dict = {}
    _refill_rounds = 0
    _wake_spawned = False  # la SVEglia parte una volta per richiesta
    ttfb_ms: int | None = None  # letta da sse()/_summary via closure
    # Clamp max_tokens GATEWAY-side dell'attempt corrente (via maxtok_hook):
    # se il modello esaurisce il NOSTRO budget ridotto, la troncatura e'
    # auto-inflitta -> il watchdog non deve punire il deployment.
    _maxtok: dict = {}
    # Gemini 3 tool replay: una history con tool_call prive di firma rende Gemini
    # inutilizzabile. L'esclusione avviene A MONTE nel router (set_avoid_gemini in
    # chat_completions -> _gemini_blocked in pick_deployment/_walk_chain), quindi
    # qui non serve più alcun salto o tentativo finto.
    while True:
        tried += 1
        _maxtok.clear()
        _so_rewrite = False
        _so_text = ""
        attempts.append(dep["unique"])
        tried_set.add(dep["unique"])
        _was_dormant = router.is_cooled_down(dep["unique"])

        def _fail(u, *, seconds=None, reason=None, status=None, provenance=None, kind=None):
            if kind is not None:
                # errore che IMPONE una strategia (es. PERMANENT_DEAD ->
                # retirement): nessun cooldown, la decisione e' del lifecycle.
                return router.mark_failed(u, reason=reason, status=status, kind=kind)
            if _was_dormant:
                _r = router.mark_failed_double_residual(u, reason=reason, status=status)
            else:
                _r = router.mark_failed(u, seconds=seconds, reason=reason, status=status, provenance=provenance)
            # P1-4: 3 KO dello stesso MODELLO (anche su chiavi diverse) entro
            # la finestra -> bench del modello su tutte le sue chiavi.
            with contextlib.suppress(Exception):
                router.note_model_failure(dep)
            return _r

        router.note_start(dep["unique"], ctx)
        # qcp PRIMA del try: lo usano anche gli handler `except` (es.
        # stream_total_deadline_ms), quindi deve essere sempre definito anche se
        # `stream_response` solleva UpstreamError al primo invio (429/402 subito).
        qcp = router.policy.qc_json
        try:
            t_att = time.monotonic()
            # hook: a fine stream, se il guard ha trovato un tag tool-call
            # rotto, declassa il deployment (cooldown breve). `salvaged` dice
            # se la chiamata e' stata recuperata o scartata.
            _trunc_unique = dep["unique"]
            _trunc_was_dormant = _was_dormant

            def _trunc_hook(_salvaged, _u=_trunc_unique, _was=_trunc_was_dormant):
                metrics.inc("nx_truncated_toolcall_total", (_u, "salvaged" if _salvaged else "dropped"))
                repairlog.note(
                    "salvage_truncated",
                    source="stream",
                    outcome="ok" if _salvaged else "fail",
                    dep=_u,
                    model=dep.get("model", ""),
                    detail="tag tool-call rotto",
                )
                log.warning(
                    "[truncation] stream %s: tag tool-call rotto (%s) -> declasso %ds",
                    _u,
                    "salvato" if _salvaged else "scartato",
                    _tct_cfg.cooldown_sec,
                )
                if _was:
                    router.mark_failed_double_residual(_u, reason="truncated_toolcall")
                else:
                    router.mark_failed(_u, seconds=_tct_cfg.cooldown_sec, reason="truncated_toolcall")

            if dep.get("thinking_replay") and orig_messages:
                _tpr = restore_reasoning(payload, orig_messages)
                if _tpr:
                    metrics.inc("nx_thinking_replay_total", ("proactive",))
                    log.info(
                        "[thinking-replay] %s: %d campi reasoning rimessi PRIMA dell'invio (proattivo)",
                        dep["unique"],
                        _tpr,
                    )
            _lease = router.key_lease_acquire(dep)  # P2-8 (opt-in)
            # HOLD (parita' col non-stream): se la risposta sara' interamente
            # bufferizzata, la riparazione tool-call NON si fa nel filtro SSE
            # incrementale ma ALLA FINE sull'output GREZZO totale (stessa
            # riparazione del percorso non-streaming). Vedi blocco HOLD sotto.
            _defer_tr = bool(dep.get("hold_until_finish")) or bool(
                getattr(router.policy.qc_json, "stream_hold_until_finish", False)
            )
            gen = await forwarder.stream_response(
                dep,
                payload,
                profile=profile or "",
                ctx_est=ctx,
                client_ip=client_ip,
                session=session,
                attribution=attribution,
                tool_repair_config=_tr_cfg,
                truncation_config=_tct_cfg,
                truncation_hook=_trunc_hook,
                maxtok_hook=lambda old, new: _maxtok.update(cap=new, old=old),
                rate_hook=lambda u, rl: router.note_rate_limit(u, rl),
                defer_tool_repair=_defer_tr,
            )
            # la TTFB vera e' il tempo fino agli HEADER upstream
            # (send(stream=True) ritorna gia' col primo chunk bufferizzato:
            # misurarla sul primo yield darebbe sempre ~0ms e avvelenerebbe
            # l'EMA della rotazione adattiva con latenze nulle).
            ttfb_ms = int((time.monotonic() - t_att) * 1000)
            _quality = 1.0
            if _was_dormant:
                router.clear_cooldown(dep["unique"])
            # ANTI-STALLO (1): lo stream verso il client NON parte finche' non
            # arriva CONTENUTO DI RISPOSTA reale. Entro stream_first_content_ms
            # un upstream vuoto/errore/lento viene ruotato in modo TRASPARENTE
            # (nessun byte inviato). Esaurita la catena -> risposta "notice".
            qcp = router.policy.qc_json
            # ADATTIVO: deadline proporzionale alla latenza storica (EMA) del
            # dep scelto, con pavimento e tetto. Un dep normalmente veloce che
            # stalla non trattiene la richiesta per il cap; un dep lento ha un
            # margine proporzionato (mai oltre il cap). EMA ignota -> cap.
            fc_ms = router.first_content_deadline_ms(dep["unique"], ctx)
            incl_reason = bool(getattr(qcp, "stream_commit_include_reasoning", False))
            min_ch = int(getattr(qcp, "stream_commit_min_chars", 40) or 0)
            # HOLD-UNTIL-FINISH: attesa della chiusura PULITA dello stream
            # prima di inviare byte (per-deployment dal CSV, o globale da
            # policy). Cosi' una risposta troncata non arriva MAI al client:
            # si ruota pre-byte come per gli altri errori.
            hold = bool(dep.get("hold_until_finish")) or bool(getattr(qcp, "stream_hold_until_finish", False))
            hold_idle = int(getattr(qcp, "stream_hold_idle_ms", 120000) or 120000)
            hold_maxb = int(getattr(qcp, "stream_hold_max_buffer_bytes", 52428800) or 52428800)
            # --- peek + HEDGE (F3) + WARM-REFILL a cascata ------------------
            # La gara parte quando: (legacy) il warm non puo' aiutare —
            # catena fredda, holder lento o gia' provato; oppure (REFILL) la
            # sessione ha MENO di warm_ready_min caldi che possono
            # EFFETTIVAMENTE consegnare questa richiesta (need + ctx + output
            # assicurato). Il refill ignora lentezza e warm utile: 2 alla
            # volta (A + 1 canary nuovo, free-only), a cascata a ogni
            # rotazione. I perdenti restano in volo come probe reali.
            _h_ms = 0
            _fresh_only = False
            _legacy = False
            _refill = False
            _zen_hunt = False  # caccia canary zen-only (nativo)
            _pol = router.policy
            # Budget di output della richiesta: serve SEMPRE (non solo in
            # refill) — e' il criterio di "capace" per il gruppo warm (gate
            # della gara lenta e conteggi di prontezza). Senza questo il gate
            # conteggiava come caldi dep che non possono consegnare l'output.
            _need_out = refill_out_budget(payload, _pol)
            # DEGRADED (P1-6): in un blackout upstream l'esplorazione
            # (cascata refill, hedge canary, sveglia) si sospende: spreca
            # rate-limit e chiavi. Resta la rotazione della ladder.
            try:
                _degraded = router.degraded_active()
            except Exception:
                _degraded = False
            if _degraded and not _wake_spawned:
                _wake_spawned = True  # evita ripetizioni nel loop
                log.info("[degraded] esplorazione sospesa per questa richiesta (%s)", dep.get("unique"))
            # Bucket di escalation (-go/-fallback): niente esplorazione
            # Solo se il gruppo RICHIESTO esplicitamente e' un bucket di
            # escalation (-go/-fallback): niente refill/canary/gara lenta/hedge
            # (i bucket a pagamento non usano il caldo, sonde sprecate). Se ci
            # si arriva via FALLBACK dal dim, la speculativa resta attiva per
            # tornare al caldo appena possibile.
            _esc_grp = is_escalation_group(
                str(requested_group or ""), router.config.go_suffix, router.config.fallback_suffix
            )
            if (
                session
                and profile
                and not _degraded
                and not _esc_grp
                and not opencode_cautious_request()
                and bool(getattr(_pol, "warm_refill_enabled", True))
                and bool(getattr(_pol, "warm_pool_enabled", True))
            ):
                _ready = router.warm_ready_effective(session, _pol)
                _maxif = max(0, int(getattr(_pol, "warm_refill_max_inflight", 6) or 0))
                try:
                    _fly = router.probes_in_flight(session)
                except Exception:
                    _fly = 0
                if _ready and _refill_rounds < _maxif and _fly < _maxif:
                    try:
                        _pool = router.warm_valid_for(
                            session,
                            profile,
                            requested_group or dep.get("group"),
                            need,
                            ctx,
                            _need_out,
                            tried=tried_set,
                            include_borrowed=True,
                        )
                        _nv = len(_pool)
                    except Exception:
                        _pool, _nv = [], _ready
                    # Nativo opencode SENZA zen nel warm: caccia un canary
                    # zen-only anche se il conteggio MISTO basta (basta 1 zen).
                    _zen_hunt = (
                        router._zen_first_active()
                        and not any(is_opencode_zen_dep(d) for d in _pool)
                        and router.hunt_allowed(session, ctx)
                    )
                    _refill = (_nv < _ready) or _zen_hunt
                    if _zen_hunt:
                        router.note_hunt(session, ctx, gained=False)
                        log.info(
                            "[refill] %s: 0 zen nel warm per client nativo -> caccia canary zen-only", dep.get("unique")
                        )
                    if _refill:
                        _rpm = router.session_rpm(session)
                        log.info(
                            "[refill] %s: warm validi %d/%d, in volo "
                            "%d/%d (ctx=%s, out=%s, rpm=%.1f) -> "
                            "canario extra in gara",
                            dep.get("unique"),
                            _nv,
                            _ready,
                            _fly,
                            _maxif,
                            ctx,
                            _need_out,
                            _rpm,
                        )
                        if not _wake_spawned:
                            _wake_spawned = True
                            _spawn_wake_sweep(
                                payload, profile, dep, need, ctx, _need_out, requested_group, session, _raced
                            )
            # GARA LENTA: se A non ha ancora CONSEGNATO dopo N ms si apre 1
            # canario SENZA buttare via la risposta (regola utente): vince
            # chi consegna prima, ma per il giro successivo e' eletto chi ha
            # impiegato meno nel proprio tentativo. E' INDIPENDENTE
            # dall'hedge classico (che resta attivo) e vale anche in refill;
            # il canario lento NON concorre al tetto per-sessione.
            # NB: il campo vive su Policy (non su qc_json): leggerlo da qcp
            # lo lasciava sempre a 0 (bug: la gara lenta non partiva mai).
            _slow_ms = 0
            _slow_canary_ms = 0
            if not _degraded and not _esc_grp:
                try:
                    _slow_ms = int(getattr(router.policy, "stream_slow_race_after_ms", 0) or 0)
                except Exception:
                    _slow_ms = 0
                try:
                    _slow_canary_ms = int(getattr(router.policy, "slow_canary_after_ms", 0) or 0)
                except Exception:
                    _slow_canary_ms = 0
            _slow_only = bool((_slow_ms > 0 or _slow_canary_ms > 0) and not _refill)
            if not _degraded and not _esc_grp and (_hedge_ms > 0 or _refill or _slow_only):
                try:
                    _h_dep = router.cache_holder(need=need, ctx=ctx)
                    _h_u = _h_dep["unique"] if _h_dep else None
                except Exception:
                    _h_u = None
                _warm_useful = bool(_h_u and _h_u not in tried_set and _h_u != dep["unique"])
                _races_max = int(getattr(qcp, "stream_hedge_max_races", 0) or 0)
                _legacy = (
                    not _warm_useful
                    and (_races_max == 0 or _races_done < _races_max)
                    and router.hunt_allowed(session, ctx)
                )
                if _legacy or _refill or _slow_only:
                    if _refill:
                        # la cascata parte SUBITO e con il proprio picker:
                        # indipendente dalla lentezza di A (regola utente).
                        _h_ms = 1
                    else:
                        # HEDGE CLASSICO invariato (F13: ritardo calibrato sul
                        # bucket, TTFT fisiologico). La gara lenta NON lo
                        # sostituisce: e' un timer separato dentro _hedge_peek.
                        try:
                            _h_ms = router.hedge_delay_ms(dep["unique"], ctx)
                        except Exception:
                            _h_ms = _hedge_ms
                    if _h_ms <= 0 and _slow_only:
                        # hedge classico spento ma la gara lenta va armata:
                        # _hedge_peek deve essere chiamato comunque.
                        _h_ms = 1
                    _fresh_only = bool(_h_u and _h_u == dep["unique"])
            if not _esc_grp and _h_ms > 0:
                _races_done += 1
                if _refill:
                    _refill_rounds += 1
                _dep_before = dep["unique"]
                if _refill:
                    _hh_k = 2
                else:
                    _hh_k = (
                        max(1, int(getattr(qcp, "stream_hedge_tiers", 1) or 1))
                        if bool(getattr(qcp, "stream_hedge_cross_tier", True))
                        else 1
                    )
                _raced.setdefault("uniq", set()).add(dep["unique"])
                _raced.setdefault("keys", set()).add(str(dep.get("api_key") or ""))
                (dep, gen, t_att, verdict, prebuf, pending, meta) = await _hedge_peek(
                    dep,
                    gen,
                    t_att,
                    fc_ms,
                    incl_reason,
                    min_ch,
                    hold_idle,
                    hold_maxb,
                    payload=payload,
                    profile=profile,
                    need=need,
                    scope=scope,
                    ctx=ctx,
                    tried_set=tried_set,
                    attempts=attempts,
                    requested_group=requested_group,
                    session=session,
                    client_ip=client_ip,
                    attribution=attribution,
                    hedge_ms=_h_ms,
                    _tr_cfg=_tr_cfg,
                    _tct_cfg=_tct_cfg,
                    k=_hh_k,
                    fresh_only=_fresh_only,
                    hold=hold,
                    refill=_refill,
                    zen_only=_zen_hunt,
                    slow_race_ms=_slow_ms,
                    slow_canary_ms=_slow_canary_ms,
                    out_tokens=_need_out or None,
                    raced=_raced,
                )
                if _legacy:
                    # backoff "il buono non esiste": solo la gara legacy
                    # consuma il budget caccia; il refill ha il suo (round).
                    router.note_hunt(session, ctx, gained=(dep["unique"] != _dep_before))
            else:
                verdict, prebuf, pending, meta = await _peek_stream(
                    gen,
                    fc_ms,
                    incl_reason,
                    min_ch,
                    hold_until_finish=hold,
                    hold_idle_ms=hold_idle,
                    hold_max_bytes=hold_maxb,
                )
            # FIX paracadute: sulla catena -go/-fallback (ULTIMO scaglione del
            # ladder) il timeout sul primo contenuto NON deve produrre un 503:
            # li' non c'e' piu' nessuno dietro a cui ruotare, quindi si
            # consegna comunque quello che arriva (parametro opzionale
            # stream_parachute_no_timeout, default True). Sotto HOLD la
            # consegna e' SEMPRE bufferizzata (mai byte live): si scarta la
            # coda in volo cosi' il tool repair hold gira sul buffer parziale.
            _pv = _parachute_verdict(verdict, qcp, dep, router.policy, hold=hold, has_buffer=bool(prebuf))
            if hold and verdict == "timeout" and _pv == "content":
                await _discard_stream(gen, pending)
                pending = None
            verdict = _pv
            # HOLD: finish_reason=length -> risposta TRONCATA dal modello (non
            # dal cap del client): si ruota pre-byte, non si consegna il
            # parziale. Se invece il client ha chiesto max_tokens ed e' stato
            # raggiunto (stima answer_chars/4) la risposta e' voluta -> content.
            if verdict == "length_truncated":
                _req_max = payload.get("max_tokens") or payload.get("max_completion_tokens")
                _ans_chars = len(_buffered_answer_text(prebuf))
                _capped = False
                try:
                    if _req_max and _ans_chars > 0:
                        _capped = (_ans_chars / 4.0) >= float(_req_max) - 2
                except (TypeError, ValueError):
                    _capped = False
                if _capped:
                    verdict = "content"
            if verdict == "content" and _tt.enabled and payload.get("tools"):
                _parsed = parse_text_toolcalls(_buffered_answer_text(prebuf), payload.get("tools"), _tt)
                if _parsed:
                    _synth.extend(_tool_calls_sse(_parsed, dep.get("model")))
                    metrics.inc("nx_text_toolcall_total", (dep["unique"], "parsed"))
                    _quality = 0.6
                    repairlog.note(
                        "salvage_text",
                        source="stream",
                        outcome="ok",
                        dep=dep["unique"],
                        model=dep.get("model", ""),
                        detail="tool-call resi come testo",
                        count=len(_parsed),
                    )
                    # OPZIONE A: il tool-call va RICOSTRUITO ma il testo
                    # residuo (es. i marker <goal .../> del plugin) resta al
                    # client: si rimuove SOLO il markup del tool-call.
                    _tt_txt = _buffered_answer_text(prebuf)
                    _tt_res = strip_toolid_markup(_tt_txt)
                    if _tt_res != _tt_txt:
                        _so_text = _tt_res
                        _so_rewrite = True
            if verdict == "content" and _fc.enabled:
                _pat = looks_like_fake_tool_call(_buffered_answer_text(prebuf), _fc)
                if _pat:
                    metrics.inc("nx_fake_toolcall_total", (dep["unique"], "detected"))
                    _esc = is_escalation_group(dep.get("group"), router.config.go_suffix, router.config.fallback_suffix)
                    if _esc:
                        # sul bucket di escalation non c'e' dove ruotare senza
                        # loop: si logga e si lascia al sanitizzatore (strip dei
                        # marker), cosi' il client non li vede mai.
                        log.warning(
                            "[fake-tool-call] stream %s: tool-call reso "
                            "come testo (pattern=%s) su bucket di "
                            "escalation -> strip",
                            dep["unique"],
                            _pat,
                        )
                    else:
                        log.warning(
                            "[fake-tool-call] stream %s: tool-call reso come testo (pattern=%s), escalation",
                            dep["unique"],
                            _pat,
                        )
                        _quality = 0.3
                        verdict = "fake_tool_call"
            # TOOL REPAIR (HOLD): la risposta e' INTERAMENTE bufferizzata ->
            # STESSA riparazione del percorso non-streaming, applicata
            # ALL'OUTPUT GREZZO totale. Sotto hold il filtro SSE incrementale
            # NON gira (defer_tool_repair), quindi l'intenzione del modello e'
            # intatta; qui si assembla, si ripara e si riscrive lo stream
            # bufferizzato (content + tool_calls) prima di inviare i byte.
            if verdict == "content" and hold and not _synth:
                from .protocols import sse_to_chat_obj as _sse2obj
                from .toolrepair import repair_tool_calls as _rep_tc, sanitize_response as _san_resp

                try:
                    _tr_obj = _sse2obj(prebuf)
                except ValueError:
                    _tr_obj = None
                if _tr_obj is not None:
                    _tr_rep = _rep_tc(_tr_obj, payload, dep, _tr_cfg)
                    _tr_san = _san_resp(_tr_obj)
                    if _tr_rep.get("repaired") or _tr_san:
                        _tr_msg = _tr_obj["choices"][0]["message"]
                        if _tr_san:
                            _tr_c = _tr_msg.get("content")
                            if isinstance(_tr_c, str):
                                prebuf = _collapse_sse_field(prebuf, "content", _tr_c)
                            _tr_rc = _tr_msg.get("reasoning_content")
                            if isinstance(_tr_rc, str):
                                prebuf = _collapse_sse_field(prebuf, "reasoning_content", _tr_rc)
                            metrics.inc("nx_content_sanitized_total", (dep["unique"],))
                        if _tr_rep.get("repaired"):
                            _tr_tcs = _tr_msg.get("tool_calls")
                            if isinstance(_tr_tcs, list):
                                prebuf = _rewrite_sse_tool_calls(prebuf, _tr_tcs)
                            metrics.inc("nx_tool_repair_total", (dep["unique"], "ok"))
                            repairlog.note(
                                "repair_args",
                                source="stream",
                                outcome="ok",
                                dep=dep["unique"],
                                model=dep.get("model", ""),
                                detail="hold whole-output: moves=%s" % _tr_rep.get("moves"),
                            )
            # OUTPUT STRUTTURATO (HOLD): la risposta e' INTERAMENTE bufferizzata
            # -> la trattiamo come non-streaming. Pulizia (A) / riparazione
            # schema-driven (D) PRIMA di inviare qualunque byte: il client non
            # vede mai il JSON sporco, e rotazione/corrective restano
            # trasparenti. Solo con HOLD attivo (senza buffer completo non e'
            # possibile) e senza tool-call sintetizzate.
            if verdict == "content" and hold and _so.enabled and not _synth:
                _so_txt = _buffered_answer_text(prebuf)
                _so_tcs: list | None = None
                for _o in _sse_data_objs(b"".join(prebuf)):
                    for _ch in (_o.get("choices") or []) if isinstance(_o, dict) else []:
                        _d = _ch.get("delta") if isinstance(_ch, dict) else None
                        _tc = _d.get("tool_calls") if isinstance(_d, dict) else None
                        if _tc:
                            _so_tcs = (_so_tcs or []) + list(_tc)
                _so_data = {"choices": [{"message": {"content": _so_txt, "tool_calls": _so_tcs}}]}
                _so_rep = enforce_response(_so_data, payload, _so)
                _so_st = _so_rep.get("status")
                if _so_st in ("cleaned", "repaired"):
                    _so_new = _so_data["choices"][0]["message"].get("content")
                    if isinstance(_so_new, str) and _so_new != _so_txt:
                        _so_text = _so_new
                        _so_rewrite = True
                    metrics.inc("nx_struct_out_total", (dep["unique"], _so_st))
                    log.info("[struct-out] stream %s: %s", dep["unique"], _so_st)
                    repairlog.note(
                        "struct_cleaned" if _so_st == "cleaned" else "struct_repaired",
                        source="stream",
                        outcome="ok",
                        dep=dep["unique"],
                        model=dep.get("model", ""),
                        detail=",".join(_so_rep.get("moves") or []) or _so_st,
                    )
                elif _so_st == "invalid":
                    _r5 = _so_rep.get("reason") or "schema"
                    if getattr(router.policy, "corrective_retry_enabled", True) and dep["unique"] not in _so_corrected:
                        _so_corrected.add(dep["unique"])
                        payload.setdefault("messages", []).append(
                            {"role": "system", "content": _corrective_note("schema")}
                        )
                        metrics.inc("nx_corrective_retry_total", (dep["unique"], "schema"))
                        log.warning(
                            "[retry] stream %s contenuto non conforme (%s): retry correttivo", dep["unique"], _r5
                        )
                        repairlog.note(
                            "struct_corrective",
                            source="stream",
                            outcome="ok",
                            dep=dep["unique"],
                            model=dep.get("model", ""),
                            detail="schema",
                        )
                        verdict = "struct_corrective"
                    else:
                        metrics.inc("nx_struct_out_total", (dep["unique"], "invalid"))
                        log.warning(
                            "[struct-out] stream %s non conforme (%s): ruoto senza cooldown", dep["unique"], _r5
                        )
                        repairlog.note(
                            "struct_invalid",
                            source="stream",
                            outcome="fail",
                            dep=dep["unique"],
                            model=dep.get("model", ""),
                            detail=_r5,
                        )
                        verdict = "struct_invalid"
            # QC DI CONTENUTO (HOLD): parita' col percorso non-streaming. La
            # risposta e' INTERAMENTE bufferizzata -> si applicano check_response
            # (JSON quando richiesto) e check_sanity (anti-vuoto) come nel
            # non-stream, con corrective JSON sullo stesso dep e rotazione senza
            # penale se non conforme. (D3 'meno peggio' non si applica: in hold
            # non si consegna mai un body rotto, si ruota fino al 503.)
            if verdict == "content" and hold and not _synth and (qc.enabled or san.enabled):
                from .qc import check_response, check_sanity

                _qc_txt = _buffered_answer_text(prebuf)
                _qc_tcs = _merge_qc_tool_calls(b"".join(prebuf)) or None
                _qc_obj = {"choices": [{"message": {"content": _qc_txt, "tool_calls": _qc_tcs}}]}
                # QC solo su contenuto REALE: i casi vuoti/zero-answer (e il
                # paracadute -go che trasmette senza contenuto) restano gestiti
                # dalla macchina a verdict, non dalla sanity.
                _qc_reason = None
                if _qc_txt.strip() or _qc_tcs:
                    _qc_reason = check_response(_qc_obj, payload, qc) if qc.enabled else None
                    if not _qc_reason and san.enabled:
                        _qc_reason = check_sanity(_qc_obj, payload, san)
                if _qc_reason:
                    _ck = _corrective_kind(_qc_reason)
                    if getattr(router.policy, "corrective_retry_enabled", True) and dep["unique"] not in _so_corrected:
                        _so_corrected.add(dep["unique"])
                        payload.setdefault("messages", []).append({"role": "system", "content": _corrective_note(_ck)})
                        metrics.inc("nx_corrective_retry_total", (dep["unique"], _ck))
                        log.warning(
                            "[retry] stream %s contenuto non conforme (%s): retry correttivo %s",
                            dep["unique"],
                            _qc_reason,
                            _ck,
                        )
                        repairlog.note(
                            "struct_corrective",
                            source="stream",
                            outcome="ok",
                            dep=dep["unique"],
                            model=dep.get("model", ""),
                            detail=_ck,
                        )
                        verdict = "struct_corrective"
                    else:
                        metrics.inc("nx_qc_discarded_total", (dep["unique"], str(_qc_reason).split(" ")[0]))
                        log.warning(
                            "[qc] stream %s contenuto non conforme (%s): ruoto senza cooldown",
                            dep["unique"],
                            _qc_reason,
                        )
                        repairlog.note(
                            "struct_invalid",
                            source="stream",
                            outcome="fail",
                            dep=dep["unique"],
                            model=dep.get("model", ""),
                            detail=str(_qc_reason)[:60],
                        )
                        verdict = "struct_invalid"
            if verdict == "content":
                # risposta reale in arrivo: se questo deployment ha SERVITO in
                # salita (gruppo != richiesto), ricorda il winner come
                # scorciatoia per le prossime richieste di QUEL bucket.
                router.note_result(
                    dep["unique"], (time.monotonic() - t_att) * 1000, quality=_quality, ctx_est=ctx, kind="ttft"
                )
                router.record_escalation_win(requested_group, dep)
                router.note_session_success(
                    ses, dep["unique"], (time.monotonic() - t_att) * 1000, ctx_est=ctx, kind="ttft"
                )
                # P2-8: la gara e' decisa; la lease si libera qui (il cap
                # serve a non FAR PARTIRE nuovi tentativi su chiave satura).
                router.key_lease_release(_lease)
                _lease = None
                break  # risposta reale in arrivo: si parte
            # --- nessun contenuto: rotazione PRE-BYTE ---
            await _discard_stream(gen, pending)
            router.note_end(dep["unique"], ctx)
            router.key_lease_release(_lease)  # P2-8
            _lease = None
            # ATTEMPT TRAIL anche per i VERDETTI: senza questo hop un 503 con
            # catena esaurita per verdetti (empty_eof/length_truncated/
            # timeout/fake_tool_call/struct_invalid) riportava attempts=[] e
            # nessun X-Scrocco-Trail: il client non capiva QUANTI e QUALI
            # deployment erano stati scartati, e perche'.
            try:
                _v_cls = (
                    "timeout"
                    if verdict == "timeout"
                    else (
                        "struct_invalid"
                        if verdict in ("struct_corrective", "struct_invalid")
                        else (
                            verdict
                            if verdict in ("empty_eof", "length_truncated", "fake_tool_call")
                            else classify_error_class(502, verdict)
                        )
                    )
                )
                _v_st = (
                    504
                    if verdict == "timeout"
                    else (422 if verdict in ("struct_corrective", "struct_invalid") else 502)
                )
                trail.append(
                    {
                        "ord": len(trail) + 1,
                        "dep": dep.get("unique"),
                        "group": dep.get("group"),
                        "model": dep.get("model"),
                        "cls": _v_cls,
                        "status": _v_st,
                        "ms": int((time.monotonic() - t_att) * 1000),
                    }
                )
            except Exception:  # noqa: BLE001
                pass
            fr = meta.get("finish_reason")
            rot_len = getattr(router.policy.qc_sanity, "rotate_on_length_empty", False)
            # NON ruotare (e non punire) se il modello HA prodotto reasoning o
            # ha esaurito max_tokens: non e' rotto, ruotare non cambia nulla
            # (tutto il gruppo si comporterebbe uguale) -> 503 retryable diretto.
            no_rotate = verdict == "empty_eof" and meta.get("no_rotate") and not rot_len
            # Clamp GATEWAY-side: se il modello ha esaurito il max_tokens che
            # GLI ABBIAMO TAGLIATO NOI (fr=length + cap nostro), la troncatura
            # e' auto-inflitta: ruotare va bene (il dim dopo ha piu' spazio) ma
            # NON declassare il deployment (non e' colpa sua).
            _gw_clamp_trunc = bool(_maxtok.get("cap") and fr == "length")
            # 0 caratteri in hold (stop/[DONE] puliti senza risposta, o length
            # bruciato tutto in reasoning): il modello NON e' rotto, ha solo
            # finito il budget o risposto vuoto -> si ruota (ladder, poi
            # -go/-fallback) SENZA penale; la penale resta per gli stream
            # VAMENTE rotti (timeout, EOF sporco, length con mezzo answer).
            _zero_empty = (verdict == "empty_eof" and bool(meta.get("empty_clean"))) or (
                verdict == "length_truncated" and not _buffered_answer_text(prebuf)
            )
            if _zero_empty:
                log.info(
                    "[hold] %s: chiusura '%s' senza risposta (fr=%s): nessuna penale, ruoto su candidato piu' capace",
                    dep["unique"],
                    verdict,
                    fr,
                )
            elif _gw_clamp_trunc:
                log.info(
                    "[maxtok] %s: stream vuoto perche' ha esaurito il clamp gateway (%s->%s): nessuna penale, ruoto",
                    dep["unique"],
                    _maxtok.get("old"),
                    _maxtok.get("cap"),
                )
            elif not no_rotate:
                # TIMEOUT (upstream che appende): danno REALE (tempo perso) ->
                # cooldown lungo (timeout_cooldown_mult x classico). Vuoto/
                # troncato: fallimento SOFT -> cooldown corto con escalation
                # dolce sui fallimenti recenti (24h).
                if verdict == "timeout":
                    _fail(dep["unique"], reason="timeout")
                elif verdict == "fake_tool_call":
                    # ROTAZIONE SENZA PENALITA' (richiesta esplicita): il modello
                    # non e' rotto, ha solo reso la chiamata come testo ->
                    # nessun cooldown/streak, si ruota e basta.
                    log.info("[fake-tool-call] %s: rotazione senza cooldown", dep["unique"])
                elif verdict in ("struct_corrective", "struct_invalid"):
                    # OUTPUT STRUTTURATO (HOLD): risposta gia' completa e non
                    # conforme -> nessuna penale (no cooldown/streak): si
                    # ritenta lo stesso dep (corrective) o si ruota.
                    log.info("[struct-out] %s: %s senza cooldown", dep["unique"], verdict)
                else:
                    _fail(dep["unique"], seconds=_soft_cd(router.stats_for(dep["unique"]).fail_count_24h))
            over_deadline = (time.monotonic() - t_req) * 1000 > int(
                getattr(qcp, "stream_total_deadline_ms", 90000) or 90000
            )
            if verdict == "struct_corrective":
                # retry correttivo: STESSO deployment (la nota di sistema e'
                # gia' stata appesa al payload).
                nxt = dep
            elif verdict == "fake_tool_call":
                nxt = (
                    router.force_escalation(
                        dep, need, ctx, tried=tried_set, out_tokens=refill_out_budget(payload, router.policy)
                    )
                    if profile
                    else None
                )
                if nxt is None and profile and router._is_renewal_bucket(str(dep.get("group") or "")):
                    nxt = router._free_last_resort(
                        dep, need, ctx, tried_set, refill_out_budget(payload, router.policy), requested_group
                    )
            else:
                # Su troncatura/risposta-vuota preferiamo un candidato PIU'
                # CAPACE (finestra > corrente, poi intelligence), perche' il
                # problema e'fisicamente lo spazio di output: la scala normale
                # (dim ascendente) resta il fallback se il picker non trova di
                # meglio.
                _cap_pref = verdict == "length_truncated" or _zero_empty
                nxt = (
                    None
                    if (no_rotate or over_deadline)
                    else (
                        router.fallback_next(
                            profile,
                            dep,
                            need,
                            scope,
                            ctx=ctx,
                            tried=tried_set,
                            requested_group=requested_group,
                            out_tokens=refill_out_budget(payload, router.policy),
                            prefer_capable=_cap_pref,
                        )
                        if profile
                        else None
                    )
                )
            log.warning(
                "[fallback] stream %s pre-contenuto verdict=%s fr=%s no_rotate=%s -> %s",
                dep["unique"],
                verdict,
                fr,
                bool(no_rotate),
                nxt["unique"] if nxt else "503",
            )
            if nxt is None or tried > _max_tries:
                # ULTIMA RISORSA: bucket -go/-fallback esaurito -> scendi ai
                # free-dims (warm di chiunque cap-ok, poi canary, poi cooled)
                # pur di non consegnare un 503.
                _flr = None
                if (
                    nxt is None
                    and profile
                    and not over_deadline
                    and router._is_renewal_bucket(str(dep.get("group") or ""))
                ):
                    _flr = router._free_last_resort(
                        dep, need, ctx, tried_set, refill_out_budget(payload, router.policy), requested_group
                    )
                if _flr is not None:
                    nxt = _flr
                elif nxt is None or tried > _max_tries:
                    # nessun byte inviato al client -> errore RETRYABLE pulito
                    _emit_summary(
                        ses=ses or "-",
                        req=req or "-",
                        grp=dep.get("group"),
                        dep=dep.get("unique"),
                        tries=len(attempts),
                        fb=max(0, len(attempts) - 1),
                        dur_ms=int((time.monotonic() - t_req) * 1000),
                        stream=client_stream,
                        qc=True,
                        wd="chain-exhausted",
                        ttfb_ms=ttfb_ms,
                        usage=None,
                    )
                    return _ret(
                        _exhausted(
                            len(attempts),
                            "%s (%s)" % (verdict, fr) if fr else verdict,
                            prefix_reason=prefix_reason,
                            trail=trail,
                            retry_at_ms=_retry_at_ms(router, trail),
                        )
                    )
            dep = nxt
            inject_identity(payload, dep, router=router)
            continue  # ri-entra nel while col nuovo dep
        except UpstreamError as err:
            router.note_end(dep["unique"], ctx)  # tentativo chiuso senza stream
            router.key_lease_release(_lease)  # P2-8
            _lease = None
            detail = err.detail or ""
            # ATTEMPT TRAIL: registra l'hop fallito con la sua classe onesta
            # (anche quando il rimedio reasoning piu' sotto lo ritenta).
            try:
                trail.append(
                    {
                        "ord": len(trail) + 1,
                        "dep": dep.get("unique"),
                        "group": dep.get("group"),
                        "model": dep.get("model"),
                        "cls": classify_error_class(err.status, detail),
                        "status": abs(int(err.status)) if err.status else None,
                        "ms": int((time.monotonic() - t_att) * 1000),
                    }
                )
            except Exception:  # noqa: BLE001
                pass
            # P1-5 skipPlatforms: errore PROVIDER-level (5xx/timeout/transport)
            # -> salta TUTTO l'host per questa richiesta.
            try:
                if is_provider_level(classify_error_class(err.status, detail)):
                    _h = dep_host(dep)
                    if _h and _h not in skip_hosts:
                        skip_hosts.add(_h)
                        log.info(
                            "[skip-host] %s: errore provider-level -> host %s saltato per questa richiesta",
                            dep["unique"],
                            _h,
                        )
            except Exception:  # noqa: BLE001
                pass
            # "does not support vision input" (llm7/Cloudflare) su richieste
            # di PURO TESTO: il proxy maschera spesso lo stesso problema del
            # reasoning mancante (i payload reali hanno decine di assistant
            # con tool_calls e zero reasoning_content). Quindi lo trattiamo
            # come candidato replay: prima si ripara e si ritenta LO STESSO
            # dep; se fallisce di nuovo -> cooldown (reason=model_feature).
            _media_raw = bool(media_reject_signature(detail))
            media_sig = _media_raw and media_input_needed(need)
            _rsn_media = media_modality_signature(detail) and not media_sig and reasoning_err_kind(detail) is None
            # FAMIGLIA REASONING (needs/rejects/history): un rimedio per dep,
            # poi si ritenta LO STESSO deployment. Copre il replay del campo
            # `reasoning_content` (opencode zen / deepseek thinking), il
            # provider che lo RIFIUTA (Cloudflare "reasoning_content is
            # unsupported") e la history incoerente col thinking nativo
            # (Anthropic/Gemini). Ruotare non aiuta: tutte le chiavi dello
            # stesso provider rifiutano lo stesso payload.
            _steps = _rsn_steps.setdefault(dep["unique"], set())
            _replim = int(getattr(router.policy, "repair_exempt_streak_limit", 3) or 0)
            _rexb = router.repair_exempt_blocked(dep["unique"], _replim)
            _rr = (
                None
                if _rexb
                else repair_reasoning_error(
                    payload, detail, dep, _steps, orig_messages, force_kind=("needs" if _rsn_media else None)
                )
            )
            if _rexb:
                log.warning(
                    "[reasoning-exempt] %s: budget esenzione esaurito (%d) -> KO normale", dep["unique"], _replim
                )
                # Booking NORMALE: le classi payload/schema da sole non
                # prevedono cooldown, quindi lo applichiamo qui (altrimenti
                # il dep verrebbe ritentato all'infinito su ogni richiesta).
                with contextlib.suppress(Exception):
                    _f24 = router.stats_for(dep["unique"]).fail_count_24h
                    router.mark_failed(
                        dep["unique"],
                        seconds=_soft_cd(_f24),
                        reason="repair_exempt_exhausted",
                        status=abs(int(err.status)) if err.status else None,
                    )
            if _rr == "downgraded":
                dep = dict(dep)
                dep["_no_thinking"] = True  # copia locale, non il CSV
            if _rr:
                with contextlib.suppress(Exception):
                    router.note_repair_exempt(dep["unique"])
                metrics.inc("nx_reasoning_replay_total", (_rr,))
                log.warning("[reasoning-%s] %s: rimedio applicato -> ritento lo stesso deployment", _rr, dep["unique"])
                # IMPARA il flag corrispondente: d'ora in poi il CSV lo porta
                # per questo modello (tutti i gemelli) e parte corretto.
                with contextlib.suppress(Exception):
                    if _rr == "repaired":
                        learn_thinking_replay(router, dep.get("model"))
                    elif _rr == "stripped":
                        learn_strip_reasoning(router, dep.get("model"))
                    elif _rr == "downgraded":
                        learn_no_thinking(router, dep.get("model"))
                continue
            # CONTENT ARRAY -> STRING (provider schema stretto, es.
            # Cloudflare Workers AI): 400 "'array' not in 'string'" /
            # "required properties ... 'role,content'". Payload RIPARABILE:
            # impariamo `content_string` (gemelli del modello) e ritentiamo LO
            # STESSO deployment col payload appiattito (media-safe). Se la
            # bonifica non basta (array con media) o il flag c'e' gia', si
            # ricade sulla rotazione di _PAYLOAD_SCHEMA_RE piu' sotto.
            if _CONTENT_ARRAY_RE.search(detail):
                _csteps = _cstr_steps.setdefault(dep["unique"], set())
                # Solo se c'e' DAVVERO qualcosa da appiattire (altrimenti il
                # retry non aiuta: si ricade sulla rotazione piu' sotto).
                _flat, _fn = flatten_text_content((payload or {}).get("messages"))
                if _fn and "flatten" not in _csteps and not dep.get("content_string"):
                    _csteps.add("flatten")
                    metrics.inc("nx_content_string_total", ("learned",))
                    log.warning(
                        "[content-string] %s: 400 schema content-array "
                        "-> imparo content_string e ritento lo stesso "
                        "deployment (%d messaggi)",
                        dep["unique"],
                        _fn,
                    )
                    with contextlib.suppress(Exception):
                        learn_content_string(router, dep.get("model"))
                    dep = dict(dep)
                    dep["content_string"] = True  # copia locale (retry)
                    continue
            # ERRORE "OSCURO" su richiesta reasoning: il taglio del reasoning
            # (histnorm) e' un'ottimizzazione di token; se il provider non ci
            # da' una firma chiara, si ritenta UNA volta lo STESSO deployment
            # con la history ORIGINALE (reasoning intatto). Se l'errore e'
            # chiaro (quota/auth/ban/schema/...) il tentativo non serve.
            if orig_messages is not None and not _rsn_restored and is_unclear_error(err.status, detail):
                _nres = restore_reasoning(payload, orig_messages)
                if _nres:
                    _rsn_restored = True
                    metrics.inc("nx_reasoning_replay_total", ("restored",))
                    log.warning(
                        "[reasoning-restore] %s: errore non chiaro (%s) "
                        "-> reasoning ripristinato (%d campi), ritento "
                        "lo stesso deployment",
                        dep["unique"],
                        (detail or "")[:90],
                        _nres,
                    )
                    continue
            # BAN/ToS dell'endpoint (ip_banned / policy_review / Terms of
            # Service): quarantena dell'HOST 24h, cosi' la rotazione non
            # brucia una chiave dietro l'altra dello stesso provider.
            maybe_quarantine_ban(router, dep, err.status, detail)
            # 502/503 mid-stream di un aggregatore: e' l'HOST a essere
            # malato -> pausa BREVE dell'host invece di bruciare le chiavi
            # sorelle (elasticita' per un problema transitorio).
            maybe_host_transient_cooldown(router, dep, err.status, detail)
            # 413/400 "context length": il provider ha rivelato il VERO
            # limite di input -> ridimensiona il deployment (regola utente).
            note_context_limit(router, dep, err.status, detail, ctx)
            # QUOTA DI ACCOUNT (Cloudflare & co.): la quota e' dell'account,
            # non della chiave -> metti in pausa TUTTE le chiavi sorelle fino
            # al reset invece di ruotarle a vuoto una per una.
            with contextlib.suppress(Exception):
                maybe_account_quota_cooldown(router, dep, err.status, detail)
            # D5 anche in STREAMING: 4xx deployment-side (firma provider-side,
            # modello inesistente oppure 404) -> fallback pre-byte invece di
            # pass-through. Gli altri 4xx restano errori del client.
            thought_sig = bool(_THOUGHT_SIG_RE.search(detail))
            # CF Workers AI & co.: rifiuto di SCHEMA (content array vs string,
            # messaggio senza content) -> stesso trattamento del
            # thought_signature: ruota SENZA cooldown, mai pass-through finche'
            # c'e' un'alternativa (un provider OpenAI-compatibile lo accetta).
            # Include anche i rifiuti "campo sconosciuto" dei provider severi
            # (Google: "Unknown name \"store\" ... Invalid JSON payload"):
            # incompatibilita' col provider, NON colpa della richiesta ->
            # ruota senza cooldown, mai pass-through del 400 al client (parita'
            # col path non-stream, forwarder._UNKNOWN_FIELD_RE).
            schema_sig = bool(_PAYLOAD_SCHEMA_RE.search(detail) or _UNKNOWN_FIELD_RE.search(detail))
            # Google/Gemini 3 (anche via proxy OpenAI-compat): rifiuto della
            # COMBINAZIONE built-in tools + function calling (il flag
            # tool_config non e' passabile). Stesso trattamento dello schema:
            # ruota SENZA cooldown, mai pass-through; a catena esaurita NON e'
            # "actionable" -> 503 RETRYABLE (il client non puo' farci nulla).
            tool_combo_sig = tool_combo_signature(detail)
            if schema_sig or tool_combo_sig:
                thought_sig = True  # riusa tutta la logica no-cooldown
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
            prov_err = is_provider_error_body(detail)  # body {"error":...} & co.
            prov_fault = is_provider_fault_body(detail)
            # QUOTA: la firma basta da sola. Alcuni provider (Cloudflare
            # Workers AI) usano un envelope {"errors":[{...}]} che NON passa
            # `prov_err`, ma il messaggio di quota e' inequivocabile.
            quota_exhausted = (
                bool(_QUOTA_EXHAUSTED_RE.search(detail)) if (prov_err or abs(int(err.status or 0)) == 429) else False
            )
            transient = bool(_PROVIDER_TRANSIENT_RE.search(detail))
            # 403 di qualsiasi tipo: chiave/progetto rifiutato dal provider ->
            # deployment-side (mai colpa della richiesta), ruota (mai al client).
            upstream403 = err.status == -403
            # 401 upstream: la NOSTRA chiave e' rifiutata dal provider
            # (assente/invalidata/revocata). E' SEMPRE deployment-side: il
            # client si e' gia' autenticato da noi, quindi non e' colpa sua.
            # Ruota come il 403, mai pass-through.
            upstream401 = err.status == -401
            openai_sig = "bad_response_status_code" in detail or "openai_error" in detail
            # 4xx con body d'errore ASSENTE/illeggibile (stream appeso ->
            # _safe_aread scaduto): non c'e' alcun messaggio azionabile per il
            # client -> NON e' un errore del client, e' infrastruttura ->
            # ruota + cooldown corto, mai pass-through (503 se catena esaurita).
            empty_body = not detail.strip() or "body non leggibile" in detail.lower() or len(detail.strip()) < 12
            # motivo della classificazione deployment-side (per il log)
            if isinstance(err, StreamLoopDetected):
                reason = "loop_detected"
            elif quota_exhausted:
                # QUOTA prima dello schema: la quota va in cooldown (fino al
                # reset), non ruotata a vuoto senza cooldown.
                reason = "quota_exhausted"
            elif tool_combo_sig:
                reason = "tool_combo"
            elif schema_sig:
                reason = "payload_schema"
            elif media_sig:
                reason = "media_reject"
            elif _media_raw:
                # falso rifiuto di modalita': niente media nella richiesta ->
                # modello rotto per questa richiesta, cooldown normale.
                reason = "model_feature"
            elif thought_sig:
                reason = "thought_signature"
            elif prov_err:
                reason = "provider_error_body"
            elif transient:
                reason = "provider_transient"
            elif _MODEL_MISSING_RE.search(detail):
                reason = "model_missing"
            elif router.policy.qc_json.retry_provider_4xx and openai_sig:
                reason = "openai_error"
            elif err.status == -402:
                reason = "http_402"
            elif empty_body:
                reason = "empty_error_body"
            elif upstream403:
                reason = "upstream_403"
            elif upstream401:
                reason = "upstream_401"
            elif prov_fault:
                reason = "provider_fault"
            elif err.status == -429:
                # 429 esplicito: quota/chiave satura -> soft per-chiave (F7)
                # e NESSUNA penale reputazionale (record_failure class-aware).
                reason = "http_429"
            elif err.status is not None and err.status < 0:
                reason = "other_4xx"
            elif err.status is None and "upstream timeout" in detail.lower():
                # Upstream che APPENDE (read/connect timeout): danno reale ->
                # cooldown lungo via reason=timeout (timeout_cooldown_mult x).
                reason = "timeout"
            else:
                reason = "http_%s" % err.status if err.status else "network"
            if err.status is not None and err.status < 0:
                provider_side = (
                    (router.policy.qc_json.retry_provider_4xx and openai_sig)
                    or _MODEL_MISSING_RE.search(detail)
                    or thought_sig
                    or prov_err
                    or transient
                    or upstream403
                    or upstream401
                    or empty_body
                    or prov_fault
                    or _media_raw
                    or quota_exhausted
                    # 429 (anche a status negativo, es. body non-standard di
                    # un aggregatore): chiave/quota satura = deployment-side,
                    # MAI un errore della richiesta -> ruota, mai pass-through.
                    or err.status == -429
                    or err.status == -402
                )
                # né thought_signature né il body d'errore provider né
                # il 403 sono rifiuti di modalita': non alimentano l'auto-
                # learn (hook).
                if (
                    provider_side
                    and hook
                    and not thought_sig
                    and not prov_err
                    and not upstream403
                    and not upstream401
                    and not prov_fault
                    and media_sig
                ):
                    try:
                        hook(dep["model"], detail)
                    except Exception:
                        pass
                if _looks_context_limit(-err.status, detail):
                    # CONTEXT LENGTH: NON passiamo il 400 al client. Alziamo la
                    # soglia minima della sessione (le richieste successive
                    # partiranno da una dim che contiene il payload) e lasciamo
                    # cadere nel flusso di fallback: `_fail` + `_next_filtered`
                    # ruotano (e per i dim espliciti la ladder sale di dim).
                    _actual = extract_requested_tokens(detail)
                    try:
                        router.note_session_overflow(ses, _actual or 0)
                    except Exception:  # noqa: BLE001
                        pass
                    log.warning(
                        "[fallback] stream %s context_length_exceeded (%.90s): alzo la soglia sessione (%s) e ruoto",
                        dep["unique"],
                        detail,
                        (">=%d tok" % _actual) if _actual else "ctx-sconosciuto",
                    )
                # QUALSIASI altro non-200: ruota, mai pass-through al client.
                # (La rotazione termina solo a catena esaurita: a quel punto
                # _actionable_upstream_error consegna lo status vero oppure 503.)
            # Gemini 3 tool replay: ruota SENZA cooldown (vedi _THOUGHT_SIG_RE
            # nel forwarder) — la key Gemini resta sana per il traffico non-tool.
            # model_missing (inesistente/non servito/giu'): 24h fissi.
            if not thought_sig and not media_sig:
                _prov_q = None
                if reason == "quota_exhausted":
                    # Abbonamento flat esaurito: cooldown = tempo al reset
                    # (es. "Resets in 9 days" -> ~9gg), non escalation.
                    _cd = parse_quota_reset_seconds(detail)
                    # Provenienza: 'authoritative' SOLO se il provider ha
                    # dichiarato il reset ("Resets in ..."); la nostra stima
                    # (mezzanotte UTC) resta 'heuristic' -> la SVEglia puo'
                    # comunque tentare il risveglio (regola utente).
                    _prov_q = "authoritative" if _QUOTA_RESET_RE.search(detail or "") else "heuristic"
                    # Rilascia dep-sticky: questa key NON tornerà prima del
                    # reset; la sessione deve ripartire su un'altra chiave.
                    if ses:
                        cur = router.dep_sticky_get(ses)
                        if cur and cur == dep["unique"]:
                            router.dep_sticky_release(ses)
                elif reason in ("provider_transient", "empty_error_body"):
                    _cd = router.escalate_cooldown(
                        fwd.PROVIDER_TRANSIENT_COOLDOWN_S, router.stats_for(dep["unique"]).fail_count_24h
                    )
                elif reason == "model_missing":
                    _cd = fwd.MODEL_MISSING_COOLDOWN_S
                elif reason == "upstream_403":
                    # Key/progetto rifiutato dal provider: cooldown lungo +
                    # rilascia lo sticky, la sessione riparte su un'altra key.
                    _cd = fwd.PERMISSION_DENIED_COOLDOWN_S
                    if ses:
                        cur = router.dep_sticky_get(ses)
                        if cur and cur == dep["unique"]:
                            router.dep_sticky_release(ses)
                elif reason == "upstream_401":
                    # Chiave assente/invalidata/revocata: stessa gestione del
                    # 403 (cooldown lungo + rilascio sticky).
                    _cd = fwd.PERMISSION_DENIED_COOLDOWN_S
                    if ses:
                        cur = router.dep_sticky_get(ses)
                        if cur and cur == dep["unique"]:
                            router.dep_sticky_release(ses)
                elif reason == "loop_detected":
                    # Loop degenere in streaming: cooldown medio, si ruota
                    # subito (un'altra chiave/modello puo' rispondere).
                    _cd = fwd.STREAM_LOOP_COOLDOWN_S
                else:
                    _cd = err.retry_after
                # reason propagato solo per il TIMEOUT (mark_failed applica il
                # moltiplicatore dedicato); per gli altri resta il comportamento
                # storico (seconds esplicito / default).
                # reason/status propagati SEMPRE: senza, sul path streaming
                # restavano morti key-soft 429, budget-guard learning,
                # _punish_concurrency e le classi di cooldown (F18).
                if fwd.is_insufficient_balance(detail):
                    # BILANCIO ESAURITO: ritira il DEPLOYMENT (sblocco manuale).
                    if ses:
                        _cur = router.dep_sticky_get(ses)
                        if _cur and _cur == dep["unique"]:
                            router.dep_sticky_release(ses)
                    _fail(dep["unique"], reason="insufficient_balance", status=402, kind=fwd.ErrorKind.PERMANENT_DEAD)
                    log.warning(
                        "[fallback] stream %s 402 'insufficient balance': DEPLOYMENT RITIRATO (sblocco manuale)",
                        dep["unique"],
                    )
                else:
                    _fail(
                        dep["unique"],
                        seconds=_cd,
                        reason=reason,
                        status=abs(err.status) if err.status else None,
                        provenance=_prov_q,
                    )
            nxt = (
                _next_filtered(profile, dep, need, scope, ctx=ctx, tried=tried_set, requested_group=requested_group)
                if profile
                else None
            )
            if nxt is not None and thought_sig and nxt["unique"] in attempts:
                nxt = None  # gruppo/catena tutto Gemini 3
            log.warning(
                "[fallback] stream %s %s motivo=%s -> %s :: %.120s",
                dep["unique"],
                err.status or "conn",
                reason,
                nxt["unique"] if nxt else "nessun alternativo",
                detail,
            )
            if nxt is None or tried > _max_tries:
                # ULTIMA RISORSA free (solo se -go/-fallback esaurito).
                _over_dl = (time.monotonic() - t_req) * 1000 > int(
                    getattr(qcp, "stream_total_deadline_ms", 90000) or 90000
                )
                _flr = None
                if nxt is None and profile and not _over_dl and router._is_renewal_bucket(str(dep.get("group") or "")):
                    _flr = router._free_last_resort(
                        dep, need, ctx, tried_set, refill_out_budget(payload, router.policy), requested_group
                    )
                if _flr is not None:
                    nxt = _flr
                else:
                    # errori AZIONABILI (auth/credito/permessi/modello assente/
                    # thought_signature) -> status vero. Il resto -> 503.
                    if _actionable_upstream_error(err) and err.status:
                        return _ret(
                            JSONResponse(
                                status_code=abs(err.status),
                                content={"error": {"message": err.detail, "type": "upstream_error"}},
                            )
                        )
                    _emit_summary(
                        ses=ses or "-",
                        req=req or "-",
                        grp=dep.get("group"),
                        dep=dep.get("unique"),
                        tries=len(attempts),
                        fb=max(0, len(attempts) - 1),
                        dur_ms=int((time.monotonic() - t_req) * 1000),
                        stream=client_stream,
                        qc=True,
                        wd="chain-exhausted",
                        ttfb_ms=ttfb_ms,
                        usage=None,
                    )
                    return _ret(
                        _exhausted(
                            len(attempts),
                            err.detail,
                            prefix_reason=prefix_reason,
                            trail=trail,
                            retry_at_ms=_retry_at_ms(router, trail),
                        )
                    )
            if ses:
                router.sticky_handoff(ses, nxt)
            dep = nxt
            inject_identity(payload, dep, router=router)
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as exc:
            # qualsiasi errore IMPREVISTO nell'ottenere lo stream da questo
            # deployment (es. httpx che cade leggendo il body d'errore) -> NON
            # deve 500-are la richiesta: cooldown corto + rotazione, 503 solo
            # se non resta nulla.
            try:
                router.note_end(dep["unique"], ctx)
            except Exception:
                pass
            _fail(dep["unique"], seconds=_soft_cd(router.stats_for(dep["unique"]).fail_count_24h))
            nxt = (
                _next_filtered(profile, dep, need, scope, ctx=ctx, tried=tried_set, requested_group=requested_group)
                if profile
                else None
            )
            log.warning(
                "[fallback] stream %s errore imprevisto %r -> %s", dep["unique"], exc, nxt["unique"] if nxt else "503"
            )
            if nxt is None or tried > _max_tries:
                _over_dl = (time.monotonic() - t_req) * 1000 > int(
                    getattr(qcp, "stream_total_deadline_ms", 90000) or 90000
                )
                _flr = None
                if nxt is None and profile and not _over_dl and router._is_renewal_bucket(str(dep.get("group") or "")):
                    _flr = router._free_last_resort(
                        dep, need, ctx, tried_set, refill_out_budget(payload, router.policy), requested_group
                    )
                if _flr is not None:
                    nxt = _flr
                else:
                    _emit_summary(
                        ses=ses or "-",
                        req=req or "-",
                        grp=dep.get("group"),
                        dep=dep.get("unique"),
                        tries=len(attempts),
                        fb=max(0, len(attempts) - 1),
                        dur_ms=int((time.monotonic() - t_req) * 1000),
                        stream=client_stream,
                        qc=True,
                        wd="chain-exhausted",
                        ttfb_ms=ttfb_ms,
                        usage=None,
                    )
                    return _ret(
                        _exhausted(
                            len(attempts),
                            repr(exc)[:160],
                            prefix_reason=prefix_reason,
                            trail=trail,
                            retry_at_ms=_retry_at_ms(router, trail),
                        )
                    )
            if ses:
                router.sticky_handoff(ses, nxt)
            dep = nxt
            inject_identity(payload, dep, router=router)

    async def sse():
        # Watchdog PASSIVO (D4): conta chunk, rileva [DONE] ed eventi error.
        # NON modifica mai i byte verso il client. Due livelli:
        #   tier1 stream vuoto / evento "error" esplicito -> cooldown subito
        #   tier2 chiuso senza [DONE] -> solo log, cooldown se policy lo vuole
        # + (D2) ri-emissione del prebuffer e coda d'errore SSE finale (verdict C).
        if _synth:
            for _b in _synth:
                yield _b
            _emit_summary(
                ses=ses or "-",
                req=req or "-",
                grp=dep["group"],
                dep=dep["unique"],
                tries=len(attempts),
                fb=max(0, len(attempts) - 1),
                dur_ms=int((time.monotonic() - t_req) * 1000),
                stream=client_stream,
                qc=False,
                wd="text-toolcall",
                ttfb_ms=ttfb_ms,
                usage=None,
            )
            _note_fb_refund(router, ses, max(0, len(attempts) - 1))
            return
        sent_first = False
        chunks = 0
        seen_done = False
        seen_error = False
        finished = False
        wd: str | None = None
        usage_final: dict | None = None
        sum_sent = False
        answer_total = 0  # solo testo risposta (D2/C)
        req_has_input = not _payload_text_empty(payload)  # D2/C
        finish_len = False  # finish_reason == "length" (D2/C)
        saw_finish_reason = False  # QUALSIASI finish_reason non nullo
        last_finish_reason: str | None = None  # ultimo finish_reason visto
        had_tool_calls = False  # tool_calls visti (D2/C)
        req_max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")

        def _summary(dur_ms: int) -> None:
            nonlocal sum_sent
            if sum_sent:
                return
            sum_sent = True
            _emit_summary(
                ses=ses or "-",
                req=req or "-",
                grp=dep["group"],
                dep=dep["unique"],
                tries=len(attempts),
                fb=max(0, len(attempts) - 1),
                dur_ms=dur_ms,
                stream=client_stream,
                qc=False,
                wd=wd,
                ttfb_ms=ttfb_ms,
                fr=last_finish_reason,
                usage=usage_final,
            )
            _note_fb_refund(router, ses, max(0, len(attempts) - 1))

        # corpo del loop fattorizzato: aggiorna lo stato watchdog ed emette
        # il chunk invariato. Condiviso da prebuffer e dal flusso residuo.
        def _ingest(chunk: bytes) -> bytes:
            nonlocal chunks, seen_done, seen_error, usage_final
            nonlocal answer_total, finish_len, had_tool_calls, sent_first
            nonlocal saw_finish_reason, last_finish_reason
            if sniffer is not None:
                sniffer.feed(chunk)
            chunks += 1
            if b"[DONE]" in chunk:
                seen_done = True
            if b'data: {"error"' in chunk:
                seen_error = True
            if usage_final is None and b'"usage"' in chunk and chunks > 1:  # parse best-effort del chunk usage
                try:
                    line = next((ln for ln in chunk.split(b"\n") if ln.startswith(b"data:") and b'"usage"' in ln), None)
                    if line:
                        obj = json.loads(line[5:].strip())
                        u = obj.get("usage")
                        if isinstance(u, dict):
                            usage_final = {
                                k: u[k]
                                for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                                if u.get(k) is not None
                            }
                            _cached = _cached_tokens_of(u)
                            if _cached is not None:
                                usage_final["cached_tokens"] = _cached
                                if _cached > 0:
                                    metrics.inc("nx_cache_hit_requests_total", ())
                            c = u.get("cost")
                            if isinstance(c, dict):
                                usage_final["cost"] = c.get("total_cost")
                            elif c is not None:
                                usage_final["cost"] = c
                except Exception:
                    pass
            # F14: calibrazione closed-loop dell'estimator col prompt_tokens
            # reale del provider (stream: arriva nel chunk finale di usage).
            try:
                if isinstance(usage_final, dict) and usage_final.get("prompt_tokens"):
                    router.note_estimate_error(dep["unique"], ctx, usage_final["prompt_tokens"])
                    # Stima per-sessione (stream): char REALI inviati a monte.
                    router.note_session_estimate(
                        ses,
                        est_chars,
                        _prompt_chars(payload.get("messages"), payload.get("tools")),
                        usage_final["prompt_tokens"],
                    )
                    metrics.inc("nx_sess_est_samples_total")
            except Exception:
                pass
            for o in _sse_data_objs(chunk):
                answer_total += _answer_chars(o)
                for ch in o.get("choices") or []:
                    if not isinstance(ch, dict):
                        continue
                    fr = ch.get("finish_reason")
                    if fr:
                        saw_finish_reason = True
                        last_finish_reason = fr
                        if fr == "length":
                            finish_len = True
                    d = ch.get("delta") or ch.get("message") or {}
                    if isinstance(d, dict) and d.get("tool_calls"):
                        had_tool_calls = True
            if not sent_first:
                sent_first = True  # TTFB gia' presa agli header upstream
            return chunk

        gen_broken = False
        gen_stall = False  # stall mid-stream rilevato (anti-stall)
        gen_loop = False  # loop degenere rilevato in streaming
        aborted = False  # client disconnesso durante lo stream
        monitor: asyncio.Task | None = None
        # il task che sta eseguendo QUESTO generator (sse): e' lui che va
        # cancellato per interrompere SUBITO l'attesa upstream. asyncio
        # current_task() qui restituisce proprio il task della StreamingResponse.
        _sse_task = asyncio.current_task()

        async def _watch_disconnect() -> None:
            """Se il client chiude la connessione, interrompe SUBITO il task di
            sse() (CancelledError) invece di lasciare l'upstream generare fino a
            fine stream: niente token sprecati sul provider e niente raffiche di
            'socket.send() raised exception' verso una socket morta.

            NB: cancellare il task di sse() chiude anche `gen` (il generator
            upstream esegue il suo finally -> resp.aclose()); aclose() diretto
            da un altro task NON interrompe un generator in pausa, quindi e'
            il task a dover essere cancellato."""
            nonlocal aborted
            try:
                while True:
                    await asyncio.sleep(0.5)
                    disconnected = False
                    if request is not None:
                        try:
                            disconnected = await request.is_disconnected()
                        except Exception:
                            disconnected = False
                    if disconnected:
                        aborted = True
                        if _sse_task is not None:
                            _sse_task.cancel()
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        try:
            monitor = asyncio.create_task(_watch_disconnect())
            # STRIP dei marker di template (Nemotron/Ling): nessun marker di
            # tool-call testuale deve arrivare al client (anche sul bucket di
            # escalation, dove non ruotiamo).
            _stripper = TemplateTokenStripper()
            # (D2/B) ordine: prima il prebuffer gia' letto da _peek_stream, poi
            # l'eventuale lettura rimasta in volo (`pending`), poi il resto.
            # OUTPUT STRUTTURATO (HOLD): se il content e' stato pulito/riparato
            # prima di inviare i byte, si emette il testo sanificato al posto
            # dell'originale (finish_reason/usage/[DONE] preservati).
            _emit_chunks = _collapse_sse_content(prebuf, _so_text) if _so_rewrite else prebuf
            for chunk in _emit_chunks:
                yield _strip_sse_content(_ingest(chunk), _stripper)
            if pending is not None:
                try:
                    yield _strip_sse_content(_ingest(await pending), _stripper)
                except StopAsyncIteration:
                    finished = True
                except Exception:
                    gen_broken = True  # upstream rotto a meta' frame
            if not finished and not gen_broken:
                async for chunk in gen:
                    yield _strip_sse_content(_ingest(chunk), _stripper)
                finished = True  # StopAsyncIteration: stream chiuso
            if _stripper.tail:
                log.debug("[strip-tokens] coda residua scartata a fine stream (len=%d)", len(_stripper.tail))
        except (GeneratorExit, asyncio.CancelledError):
            # disconnessione client o aborted dal monitor: chiudi l'upstream e
            # non punire il deployment (e' il client che e' andato via).
            if not aborted:
                await _discard_stream(gen, pending)
            raise  # disconnessione client: non punire
        except Exception as exc:
            gen_broken = True
            # anti-stall: StreamStallError e' un asyncio.TimeoutError -> danno
            # reale (upstream appeso), cooldown lungo invece del soft.
            gen_stall = isinstance(exc, asyncio.TimeoutError)
            gen_loop = isinstance(exc, StreamLoopDetected)
        finally:
            if monitor is not None:
                monitor.cancel()
            dur_ms = int((time.monotonic() - t_req) * 1000)
            router.note_end(dep["unique"], ctx)
            if not aborted:
                # F1: durata TOTALE del tentativo vincente nel bucket di
                # contesto (il commit ha gia' registrato il TTFT).
                router.note_stream_end(
                    dep["unique"],
                    (time.monotonic() - t_att) * 1000,
                    ctx,
                    completion_tokens=(usage_final or {}).get("completion_tokens"),
                )
            # NB (fix): il watchdog NON inietta mai nulla nello stream verso il
            # client (un `data:` non-conforme viene renderizzato come testo da
            # opencode & simili). L'unica reazione automatica e' il cooldown del
            # deployment, cosi' i retry del client / le richieste successive
            # evitano la chiave che ha scazzato.
            if aborted or finished or gen_broken:
                if aborted:
                    # client disconnesso a meta' stream: NON e' colpa del
                    # deployment -> nessun cooldown, solo log diagnostico.
                    wd = "client-aborted"
                    log.info("[watchdog] client disconnesso durante lo stream da %s (chunks=%d)", dep["unique"], chunks)
                elif chunks == 0:
                    wd = "tier1-empty"
                    metrics.inc("nx_qc_watchdog_total", (dep["unique"], "empty"))
                    log.warning("[watchdog] tier1 stream VUOTO da %s (chunks=0): cooldown", dep["unique"])
                    _fail(dep["unique"], seconds=_soft_cd(router.stats_for(dep["unique"]).fail_count_24h))
                elif seen_error:
                    wd = "tier1-error"
                    metrics.inc("nx_qc_watchdog_total", (dep["unique"], "error"))
                    log.warning("[watchdog] tier1 evento error esplicito da %s (chunks=%d)", dep["unique"], chunks)
                    _fail(dep["unique"], seconds=_soft_cd(router.stats_for(dep["unique"]).fail_count_24h))
                elif gen_loop:
                    # loop degenere: il modello streammava output ripetitivo,
                    # il detector l'ha killato -> cooldown medio e riparti.
                    wd = "loop-detected"
                    metrics.inc("nx_qc_watchdog_total", (dep["unique"], "loop"))
                    log.warning(
                        "[watchdog] stream in LOOP da %s (chunks=%d): kill precoce, cooldown %ds",
                        dep["unique"],
                        chunks,
                        fwd.STREAM_LOOP_COOLDOWN_S,
                    )
                    _fail(dep["unique"], seconds=fwd.STREAM_LOOP_COOLDOWN_S, reason="loop_detected")
                elif gen_broken or (not seen_done and not saw_finish_reason):
                    # troncamento GENUINO: stream rotto a meta' oppure niente
                    # [DONE] E niente finish_reason -> il modello ha scazzato.
                    if gen_stall:
                        # upstream "congelato" a meta' stream (nessun byte per
                        # stream_stall_sec): danno REALE -> cooldown lungo.
                        wd = "tier2-stall"
                        metrics.inc("nx_qc_watchdog_total", (dep["unique"], "stall"))
                        log.warning(
                            "[watchdog] tier2 stream in STALLO da %s (chunk=%d, stall=%.0fs): cooldown",
                            dep["unique"],
                            chunks,
                            float(getattr(router.policy, "stream_stall_sec", 0) or 0),
                        )
                        _fail(dep["unique"], reason="timeout")
                    else:
                        wd = "tier2-truncated"
                        metrics.inc("nx_qc_watchdog_total", (dep["unique"], "truncated"))
                        log.warning(
                            "[watchdog] tier2 stream TRONCATO da %s (chunk=%d, finish_reason=%s): cooldown",
                            dep["unique"],
                            chunks,
                            saw_finish_reason,
                        )
                        _fail(dep["unique"], seconds=_soft_cd(router.stats_for(dep["unique"]).fail_count_24h))
                elif not seen_done:
                    # c'e' un finish_reason ma manca [DONE]: risposta di fatto
                    # completa, il provider omette solo il sentinel. Solo log.
                    wd = "tier2-no-done"
                    log.info(
                        "[watchdog] tier2 %s: finish_reason presente, nessun [DONE] (provider senza sentinel)",
                        dep["unique"],
                    )
                elif (
                    finish_len
                    and _maxtok.get("cap")
                    and (usage_final or {}).get("completion_tokens") is not None
                    and int((usage_final or {}).get("completion_tokens")) >= int(_maxtok["cap"]) - 2
                ):
                    # Troncatura AUTO-INFLITTA: il gateway ha clampato
                    # max_tokens e il modello ha esaurito ESATTAMENTE quel
                    # budget (finish_reason=length). Non e' colpa del
                    # deployment: nessuna penale, solo log (i byte sono gia'
                    # partiti). Serve a non far scattare il cooldown
                    # zero-answer/length-truncated su risposte monche nostre.
                    wd = "clamp-truncated"
                    log.info(
                        "[watchdog] %s: risposta troncata dal clamp "
                        "gateway (max_tokens %s->%s, completion=%s): "
                        "nessuna penale",
                        dep["unique"],
                        _maxtok.get("old"),
                        _maxtok["cap"],
                        (usage_final or {}).get("completion_tokens"),
                    )
                elif _length_truncated_should_fail(
                    finish_len,
                    answer_total,
                    req_max_tokens,
                    (usage_final or {}).get("completion_tokens"),
                    router.policy.qc_sanity.rotate_on_length_truncated,
                ):
                    # risposta TRONCATA dal modello (finish_reason=length) ma
                    # con contenuto: come un errore -> cooldown del dep, cosi'
                    # le prossime richieste ruotano su un altro modello.
                    # (La risposta corrente e' gia' partita: non e' ritraibile.)
                    wd = "length-truncated"
                    metrics.inc("nx_qc_watchdog_total", (dep["unique"], "length_truncated"))
                    log.warning(
                        "[watchdog] risposta TRONCATA (finish_reason="
                        "length) da %s (chunk=%d, answer=%d, "
                        "completion=%s, req_max=%s): cooldown + "
                        "rotazione",
                        dep["unique"],
                        chunks,
                        answer_total,
                        (usage_final or {}).get("completion_tokens"),
                        req_max_tokens,
                    )
                    _fail(dep["unique"], seconds=_soft_cd(router.stats_for(dep["unique"]).fail_count_24h))
                elif (
                    answer_total == 0
                    and req_has_input
                    and not had_tool_calls
                    and not (finish_len and not router.policy.qc_sanity.rotate_on_length_empty)
                ):
                    # stream "completo" ma 0 testo di risposta con input reale:
                    # fallimento silenzioso -> cooldown (nessun artefatto verso
                    # il client: i byte, per quanto vuoti, sono gia' partiti).
                    wd = "zero-answer"
                    metrics.inc("nx_qc_watchdog_total", (dep["unique"], "zero_answer"))
                    log.warning(
                        "[watchdog] stream 0-answer da %s (input non vuoto, finish_len=%s): cooldown",
                        dep["unique"],
                        finish_len,
                    )
                    _fail(dep["unique"], seconds=_soft_cd(router.stats_for(dep["unique"]).fail_count_24h))
            _summary(dur_ms)
            if sniffer is not None:
                sniffer.finish_stream(
                    {
                        "status": "success" if (finished and not gen_broken) else ("aborted" if aborted else "broken"),
                        "wd": wd,
                        "chunks": chunks,
                        "answer_chars": answer_total,
                        "had_tool_calls": had_tool_calls,
                        "finish_reason_len": finish_len,
                        "saw_finish_reason": saw_finish_reason,
                        "seen_done": seen_done,
                        "usage": usage_final,
                        "dep_final": dep.get("unique"),
                        "tries": len(attempts),
                    }
                )

    return _ret(StreamingResponse(sse(), media_type="text/event-stream"))


# ---------------------------------------------------------- image generations
@app.post("/v1/images/generations")
async def images_generations(request: Request):
    """Endpoint OpenAI-compatibile per la generazione immagini.

    Instrada SOLO verso deployment con capacità image_gen (model_capabilities).
    Su 404/provider-4xx dall'endpoint /images/generations e con
    capability_routing.images_chat_fallback=true, ritenta via chat/completions
    (modelli immagine esposti come chat, es. gemini-image).
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

    model = policy.canonicalize(raw_model)

    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"image_gen"}) if router.policy.routing_active() else frozenset()
    refs = image_refs_from_payload(payload)
    if refs:
        need = need | {"image_edit"}
    _set_opencode_gate(request)
    session_id = _session_id(request, payload)
    scope = "group" if router.is_explicit(model) else "chain"

    # Con immagini di riferimento la generazione va SEMPRE via chat multimodale:
    # i modelli image-edit (Gemini/nano-banana) ricevono la reference solo così.
    if refs:
        dep, profile, scope, err = _images_pick_dep(auth.profile, model, raw_model, session_id, need, payload)
        if err:
            return err
        custom_key = router.resolve_alias_key(raw_model, model)
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

    group_or_explicit = router.resolve_group_for_request(model, [], session_id, need, profile=auth.profile)
    if group_or_explicit is None:
        return JSONResponse(
            status_code=400 if need else 404,
            content={
                "error": {
                    "message": (
                        "nessun deployment dichiara image_gen: configura "
                        "capability_routing.model_capabilities in gateway.yaml"
                        if need
                        else f"model '{model}' not managed by {policy.service_name}"
                    ),
                    "type": "invalid_request_error",
                }
            },
        )

    dep = router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        dep = router.pick_deployment(group_or_explicit, need)
    if dep is None and auth.profile:
        dep = router.fallback_after(auth.profile, None, need, out_tokens=refill_out_budget(payload, router.policy))
    if dep is None:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "nessun deployment disponibile per image_gen", "type": "server_error"}},
        )

    custom_key = router.resolve_alias_key(raw_model, model)
    if custom_key:
        dep = {**dep, "api_key": custom_key}

    _sess = _opencode_session(request) or session_id
    _cip = _client_ip(request)
    _attr = _client_attribution(request)

    profile = auth.profile or config.profile_of_base(model.split("__")[0]) or config.profile_of_base(model)

    metrics.inc("nx_images_total", (dep["group"], "attempt"))
    log.info("[images] %s -> %s (prompt=%d chars)", model, dep["unique"], len(str(payload.get("prompt") or "")))

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
        data = await forwarder.call(dep, chat_payload, session=_sess, client_ip=_cip, attribution=_attr)
        router.note_result(cur, (time.monotonic() - t0) * 1000)
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
                log.warning("[images] chat su %s: nessuna immagine riconosciuta nella risposta", cur)
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
        _was_dormant = router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        router.note_start(cur)
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
                router.note_end(cur)
                router.mark_failed(
                    cur, seconds=chat_err.retry_after, status=abs(chat_err.status) if chat_err.status else None
                )
                metrics.inc("nx_images_total", (dep["group"], "retry"))
                nxt = (
                    router.fallback_next(
                        profile, dep, need, scope, tried=tried, out_tokens=refill_out_budget(payload, router.policy)
                    )
                    if profile
                    else None
                )
                if nxt is None:
                    break
                dep = nxt
                continue
        try:
            data = await forwarder.call_images(
                dep, payload, profile=profile or "", client_ip=_cip, session=_sess, attribution=_attr
            )
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                data["data"] = await _localize_images(request, images_dual(data["data"]))
            router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                router.clear_cooldown(cur)
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
            router.note_end(cur)
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
                or router.policy.images_chat_fallback
                and (image_chat_fallback_signature(err.status, detail) or chat_only_image_error(detail))
                or (
                    router.policy.images_chat_fallback
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
            if router.policy.images_chat_fallback and _chat_useful and cur not in chat_tried:
                chat_tried.add(cur)
                log.info(
                    "[images] %s: /images/generations non disponibile (status=%s): ritenta via chat",
                    cur,
                    -status or "?",
                )
                try:
                    return await _attempt_via_chat(cur, t0)
                except UpstreamError as chat_err:
                    log.warning("[images] fallback chat su %s fallito: %s", cur, chat_err.detail[:120])
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    pass
            if _was_dormant:
                router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                router.mark_failed(cur, seconds=err.retry_after, status=abs(err.status) if err.status else None)
            metrics.inc("nx_images_total", (dep["group"], "retry"))
            nxt = (
                router.fallback_next(
                    profile, dep, need, scope, tried=tried, out_tokens=refill_out_budget(payload, router.policy)
                )
                if profile
                else None
            )
            if nxt is None:
                break
            dep = nxt
        finally:
            if cur in tried:
                router.note_end(cur)

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
@app.post("/v1/images/edits")
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
                    log.warning("[images] campo 'mask' presente ma vuoto")
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

    model = policy.canonicalize(raw_model)
    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"image_gen", "image_edit"}) if router.policy.routing_active() else frozenset()
    _set_opencode_gate(request)
    session_id = _session_id(request, payload)
    dep, profile, scope, err = _images_pick_dep(auth.profile, model, raw_model, session_id, need, payload)
    if err:
        return err
    custom_key = router.resolve_alias_key(raw_model, model)
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


@app.get("/v1/images/files/{file_id}")
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


# ----------------------------------------------------------------- audio TTS
def _audio_route(profile: str | None, model: str, raw_model: str, session_id: str | None, need: frozenset[str]):
    """Routing condiviso degli endpoint audio: risolve il primo deployment
    capace (o explicit pass-through) oppure ritorna una JSONResponse d'errore.
    Ritorna (dep, profile, error_response)."""
    group_or_explicit = router.resolve_group_for_request(model, [], session_id, need, profile=profile)
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
                            else f"model '{model}' not managed by {policy.service_name}"
                        ),
                        "type": "invalid_request_error",
                    }
                },
            ),
        )

    dep = router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        dep = router.pick_deployment(group_or_explicit, need)
    if dep is None and profile:
        dep = router.fallback_after(profile, None, need, out_tokens=refill_out_budget({}, router.policy))
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

    custom_key = router.resolve_alias_key(raw_model, model)
    if custom_key:
        dep = {**dep, "api_key": custom_key}
    return dep, profile, None


@app.post("/v1/audio/speech")
async def audio_speech(request: Request):
    """TTS OpenAI-compatibile: instrada SOLO su deployment con capacità tts."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400, content={"error": {"message": "invalid JSON body", "type": "invalid_request_error"}}
        )
    raw_model = payload.get("model") or ""
    if not str(payload.get("input") or "").strip():
        return JSONResponse(
            status_code=400, content={"error": {"message": "'input' è obbligatorio", "type": "invalid_request_error"}}
        )
    model = policy.canonicalize(raw_model)
    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"tts"}) if router.policy.routing_active() else frozenset()
    scope = "group" if router.is_explicit(model) else "chain"
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
        _was_dormant = router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        router.note_start(cur)
        t0 = time.monotonic()
        try:
            content, ctype = await forwarder.call_speech(
                dep, payload, profile=profile or "", client_ip=_cip, session=_sess, attribution=_attr
            )
            router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                router.clear_cooldown(cur)
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
            router.note_end(cur)
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
                    pass
            if _was_dormant:
                router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                router.mark_failed(cur, seconds=err.retry_after, status=abs(err.status) if err.status else None)
            metrics.inc("nx_tts_total", (dep["group"], "retry"))
            nxt = (
                router.fallback_next(
                    profile, dep, need, scope, tried=tried, out_tokens=refill_out_budget(payload, router.policy)
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
@app.post("/v1/systemone")
async def systemone(request: Request):
    """Jev / TypeSafe System One: decisione strutturata.

    Body nativo `{model, state, questions}` -> risposta nativa
    `{model, answers, usage}` con `nx_deployment`/`nx_provider` aggiunti.
    Instrada SOLO su deployment con capacità `decision`. Non-streaming, nessuna
    traduzione di protocollo, niente tool/reasoning/hold.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400, content={"error": {"message": "invalid JSON body", "type": "invalid_request_error"}}
        )
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

    model = policy.canonicalize(raw_model)
    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"decision"}) if router.policy.routing_active() else frozenset()
    scope = "group" if router.is_explicit(model) else "chain"
    _set_opencode_gate(request)
    session_id = _session_id(request, payload)
    # Ruota anche con master key: il profilo si ricava dal nome richiesto,
    # altrimenti la rotazione resterebbe spenta (bug dei path audio).
    _prof = auth.profile or config.profile_of_base(model.split("__")[0]) or config.profile_of_base(model)

    group_or_explicit = router.resolve_group_for_request(model, [], session_id, need, profile=_prof)
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
                        else f"model '{model}' non gestito da {policy.service_name}"
                    ),
                    "type": "invalid_request_error",
                }
            },
        )

    explicit_req = router.is_explicit(model)
    dep = router.config.deployment_by_unique(group_or_explicit)
    _cap = router.config.group_caps.get(group_or_explicit)
    # Gruppo capacità con primario VUOTO (es. righe Jev a pagamento tutte
    # `fallback`): la catena capability attraversa free -> -go -> -fallback e
    # trova i deployment che il solo gruppo primario non ha.
    if dep is None and _cap is not None and not explicit_req:
        dep = _cap_chain_pick(_prof, need)
    if dep is None:
        dep = router.initial_pick(
            _prof, group_or_explicit, None if explicit_req else need, out_tokens=refill_out_budget(payload, policy)
        )
    if dep is None and _prof and not explicit_req:
        dep = _cap_chain_pick(_prof, need) or router.fallback_after(
            _prof, None, need, out_tokens=refill_out_budget(payload, router.policy)
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
        _was_dormant = router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        router.note_start(cur)
        t0 = time.monotonic()
        try:
            out = await forwarder.call_systemone(
                dep, payload, profile=_prof or "", client_ip=_cip, session=_sess, attribution=_attr
            )
            # QC: ogni chiave richiesta in `questions` deve comparire in
            # `answers`; una risposta incompleta e' un problema del deployment
            # -> rotazione (502 positivo = ritriabile).
            _answers = out.get("answers") if isinstance(out, dict) else None
            _missing = [k for k in questions if not (isinstance(_answers, dict) and k in _answers)]
            if _missing:
                raise UpstreamError(502, f"risposta systemone incompleta: chiavi mancanti {_missing}")
            router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                router.clear_cooldown(cur)
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
            deployment_side = (
                status >= 0
                or -status in (401, 402, 403, 404, 405, 415, 422)
                or _MODEL_MISSING_RE.search(detail)
                or _PROVIDER_TRANSIENT_RE.search(detail)
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
                    pass
            if _was_dormant:
                router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                router.mark_failed(cur, seconds=err.retry_after, status=abs(err.status) if err.status else None)
            metrics.inc("nx_systemone_total", (dep["group"], "retry"))
            nxt = (
                router.fallback_next(
                    _prof, dep, need, scope, tried=tried, out_tokens=refill_out_budget(payload, router.policy)
                )
                if _prof
                else None
            )
            if nxt is None:
                break
            dep = nxt
        finally:
            router.note_end(cur)

    return _exhausted(
        len(attempts), last_err.detail if last_err else None, trail=trail, retry_at_ms=_retry_at_ms(router, trail)
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

    model = policy.canonicalize(raw_model)
    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"stt"}) if router.policy.routing_active() else frozenset()
    scope = "group" if router.is_explicit(model) else "chain"
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
        _was_dormant = router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        router.note_start(cur)
        t0 = time.monotonic()
        try:
            result = await forwarder.transcribe(
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
            router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                router.clear_cooldown(cur)
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
            router.note_end(cur)
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
                    pass
            if _was_dormant:
                router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                router.mark_failed(cur, seconds=err.retry_after, status=abs(err.status) if err.status else None)
            metrics.inc("nx_stt_total", (dep["group"], "retry"))
            nxt = (
                router.fallback_next(
                    profile, dep, need, scope, tried=tried, out_tokens=refill_out_budget({}, router.policy)
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


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request):
    """STT OpenAI-compatibile: audio -> testo nella lingua originale."""
    return await _audio_transcribe(request, "transcriptions")


@app.post("/v1/audio/translations")
async def audio_translations(request: Request):
    """STT OpenAI-compatibile: audio -> testo tradotto in inglese."""
    return await _audio_transcribe(request, "translations")


# ------------------------------------------------------------- video async
@app.post("/v1/videos/generations")
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

    model = policy.canonicalize(raw_model)
    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"video_gen"}) if router.policy.routing_active() else frozenset()
    # i2v / reference-to-video: le immagini d'input esigono il token vision
    # nell'inner-filter (solo modelli che accettano frame/reference)
    if payload.get("frame_images") or payload.get("input_references"):
        need = need | {"vision"}
    scope = "group" if router.is_explicit(model) else "chain"
    _set_opencode_gate(request)
    session_id = _session_id(request, payload)

    group_or_explicit = router.resolve_group_for_request(
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

    explicit_req = router.is_explicit(model)
    dep = router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        dep = router.initial_pick(
            auth.profile,
            group_or_explicit,
            None if explicit_req else need,
            out_tokens=refill_out_budget(payload, policy),
        )
    if dep is None and auth.profile and not explicit_req:
        dep = router.fallback_after(auth.profile, None, need, out_tokens=refill_out_budget(payload, router.policy))
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
    _prof = auth.profile or config.profile_of_base(model.split("__")[0]) or config.profile_of_base(model)
    t_req = time.monotonic()
    last_err: UpstreamError | None = None
    while dep is not None and len(tried) < 32:
        cur = dep["unique"]
        _was_dormant = router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        router.note_start(cur)
        t0 = time.monotonic()
        try:
            envelope = await forwarder.submit_video(
                dep, payload, profile=_prof or "", client_ip=_cip, session=_sess, attribution=_attr
            )
            router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                router.clear_cooldown(cur)
            metrics.inc("nx_videos_total", (dep["group"], "ok"))
            job_id = str(envelope.get("id") or "")
            _videos_jobs[job_id] = {
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
                        st = await forwarder.poll_video(
                            dep,
                            job_id,
                            profile=_prof or _videos_jobs.get(job_id, {}).get("_prof", ""),
                            client_ip=_cip or _videos_jobs.get(job_id, {}).get("_cip", ""),
                            session=_sess or _videos_jobs.get(job_id, {}).get("_sess"),
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
            router.note_end(cur)
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
                    pass
            if _was_dormant:
                router.mark_failed_double_residual(
                    cur, reason=str(err.detail or "")[:80], status=abs(err.status) if err.status else None
                )
            else:
                router.mark_failed(cur, seconds=err.retry_after, status=abs(err.status) if err.status else None)
            metrics.inc("nx_videos_total", (dep["group"], "retry"))
            nxt = (
                router.fallback_next(
                    profile, dep, need, scope, tried=tried, out_tokens=refill_out_budget(payload, router.policy)
                )
                if (
                    profile := auth.profile
                    or config.profile_of_base(model.split("__")[0])
                    or config.profile_of_base(model)
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
        canonical = policy.canonicalize(model)
        deps = router.video_gen_candidates(canonical)
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
    snap = _videos_jobs.get(job_id)
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


@app.get("/v1/videos/generations/{job_id}")
async def videos_status(job_id: str, request: Request, model: str | None = None):
    auth = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    deps, err = _job_deps(job_id, model or request.query_params.get("model"))
    if err:
        return err
    _prof = model or request.query_params.get("model") or ""
    _prof = config.profile_of_base(_prof.split("__")[0]) or ""
    try:
        status = await forwarder.poll_video_any(
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


@app.get("/v1/videos/generations/{job_id}/content")
async def videos_content(job_id: str, request: Request, model: str | None = None):
    auth = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    deps, err = _job_deps(job_id, model or request.query_params.get("model"))
    if err:
        return err
    _prof = model or request.query_params.get("model") or ""
    _prof = config.profile_of_base(_prof.split("__")[0]) or ""
    try:
        t_dl = time.monotonic()
        content, ctype = await forwarder.download_video_any(
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


from .compat.ollama import router as _ollama_router  # noqa: E402 (local import to break cycle)
from .models_and_health import router as _models_and_health_router  # noqa: E402 (local import to break cycle)

# Re-export: `app/compat/ollama.py` chiama questi helper via
# `import app.main as M` (M._caps_and_deps, M._model_entry,
# M._visible_model_names). Senza questi nomi qui, quelle rotte
# (/v1/models/{id}, /api/tags, /api/show) prenderebbero un AttributeError.
from .models_and_health import (  # noqa: E402 (re-export per app/compat/ollama.py)
    VIEWS,
    _caps_and_deps,
    _dedup_deps,
    _deps_for_name,
    _model_entry,
    _names_for_auth,
    _stable_names,
    _view_for,
    _visible_model_names,
)

app.include_router(_ollama_router)
app.include_router(_models_and_health_router)


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
