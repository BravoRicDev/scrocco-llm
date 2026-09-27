"""Persistenza runtime: stats adattive, cooldown, routing state, thought_sig,
coalescing richieste, bootstrap da log, watcher hot-reload CSV/policy,
scheduler notturno autoprobe.
Estratti verbatim da `app/main.py` (cluster C1, Round 4 Clean Code, ULTIMO
del giro R4). Gli oggetti creati a RUNTIME dentro main.py (router, config,
policy, log, forwarder, PERSIST_STATS, PERSIST_ROUTING, VAR_DIR, POLICY_PATH,
_stats_file, _thought_sigs_file, _cooldown_file, _routing_file, LEDGER,
KEYHEALTH, _videos_jobs, VIDEO_JOB_TTL_SEC, _COALESCE_CACHE_MAX,
_coalesce_cache, _inflight_coalesce, _inflight_lock, _so_cfg_from_policy) e
le 7 dichiarazioni `global` originarie (VIDEO_JOB_TTL_SEC,
_COALESCE_CACHE_MAX, _last_stats_save, _last_routing_save,
_last_cooldown_save) sono raggiunti/riassegnati con `import app.main as M`
DENTRO il corpo delle funzioni: a livello di modulo si creerebbe un ciclo di
import, e in Python un `global X` in un modulo diverso punterebbe al
namespace SBAGLIATO. Ogni `global X; X = ...` e' diventato `M.X = ...`.

DEVIAZIONE DOCUMENTATA (1 riga): in `_watcher`, `globals()["policy"] = fresh`
diventa `M.policy = fresh` — `globals()` e' lessicalmente legato al modulo in
cui il codice VIVE fisicamente: rimasto `globals()` qui avrebbe mutato il
namespace di questo modulo invece di quello di main.py, lasciando `policy`
in main.py per sempre stantio dopo il primo hot-reload.

`_apply_misc_policy`, `set_video_job_ttl_sec`, `set_coalesce_cache_max` sono
chiamate da main.py A LIVELLO DI MODULO (bootstrap, prima che `app` esista):
main.py le importa con un import STATICO in testa (non nella sezione dei
late-import "break cycle" usata per gli altri cluster), perche' servono
gia' pronte a quel punto dell'esecuzione.
"""

import asyncio
import copy
import hashlib
import json
import os
import random
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from . import autoprobe, imagestore, audiostore, metrics, repairlog, sniff
from .schemaout import schemaout_config_from_policy as _so_cfg_from_policy
from .suppressed import report_suppressed
from . import forwarder as fwd
from .atomic_store import JsonSnapshot
from .atomic_store import freeze_json as _freeze_json
from .atomic_store import load_json as _load_json
from .atomic_store import save_json_text as _save_json_text
from .caution import background_cautious_enabled
from .config import csv_mtime_ns, maybe_reload
from .forwarder import (
    apply_cooldown_policy,
    set_adaptive_timeout,
    set_estimate_defaults,
    set_reasoning_reserve,
    set_retry_after_floors,
    set_schemaout_config,
    set_stall_bucket,
    set_stream_stall_sec,
    set_strip_client_fields,
    set_ttft_lookup,
)
from .policy import Policy
from .router import configure_estimate


def set_video_job_ttl_sec(value=None) -> None:
    """TTL dei job video in memoria (policy `video_job_ttl_sec`, default 24h)."""
    import app.main as M

    if value is not None:
        try:
            M.VIDEO_JOB_TTL_SEC = max(0, int(value))
        except (TypeError, ValueError):
            pass


def set_coalesce_cache_max(value=None) -> None:
    """Cap entry della cache di coalescing (policy `coalesce_cache_max`)."""
    import app.main as M

    if value is not None:
        try:
            M._COALESCE_CACHE_MAX = max(0, int(value))
        except (TypeError, ValueError):
            pass


def _apply_misc_policy(pol) -> None:
    """Propaga i parametri di policy alle costanti runtime dei moduli minori.

    Chiamata all'avvio e ad ogni hot-reload. Non cambia la logica: i default
    restano identici alle costanti storiche dei moduli."""
    import app.main as M

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
            storage_dir=Path(M.VAR_DIR) / "images",
        )
    except Exception:  # noqa: BLE001
        report_suppressed("runtime_persistence._apply_misc_policy.images")
    try:
        # Cache delle trascrizioni STT: TTL dalla policy, gli altri limiti
        # restano quelli del modulo (il budget e' in caratteri di testo, che
        # non ha un equivalente nella policy delle immagini).
        audiostore.configure(ttl_sec=getattr(pol, "stt_chat_cache_ttl_sec", None))
    except Exception:  # noqa: BLE001
        report_suppressed("runtime_persistence._apply_misc_policy.stt_cache")
    try:
        sniff.set_sniff_caps(
            max_b64_chars=getattr(pol, "sniff_max_b64_chars", None),
            max_str_chars=getattr(pol, "sniff_max_str_chars", None),
            max_sse_bytes=getattr(pol, "sniff_max_sse_bytes", None),
        )
    except Exception:  # noqa: BLE001
        report_suppressed("runtime_persistence._apply_misc_policy.sniff")
    try:
        from .keyhealth import set_health_thresholds

        set_health_thresholds(
            streak_dead=getattr(pol, "keyhealth_streak_dead_threshold", None),
            success_ema_floor=getattr(pol, "keyhealth_success_ema_floor", None),
        )
    except Exception:  # noqa: BLE001
        report_suppressed("runtime_persistence._apply_misc_policy.keyhealth")
    try:
        from .ctxcompact import set_min_protected_msgs

        set_min_protected_msgs(getattr(pol, "ctxcompact_min_protected_msgs", None))
    except Exception:  # noqa: BLE001
        report_suppressed("runtime_persistence._apply_misc_policy.ctxcompact")
    try:
        from .toolrepair import set_max_unwrap_depth

        set_max_unwrap_depth(getattr(pol, "toolrepair_max_unwrap_depth", None))
    except Exception:  # noqa: BLE001
        report_suppressed("runtime_persistence._apply_misc_policy.toolrepair")
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
        report_suppressed("runtime_persistence._apply_misc_policy.upstream_http")


def _coalesce_cache_take(key: str, now: float):
    import app.main as M

    ent = M._coalesce_cache.get(key)
    if ent is None:
        return None
    if now >= ent["exp"]:
        M._coalesce_cache.pop(key, None)
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
    import app.main as M

    if not _coalesce_cacheable(res):
        return
    M._coalesce_cache[key] = {"res": res, "exp": exp}
    while len(M._coalesce_cache) > M._COALESCE_CACHE_MAX:
        oldest = min(M._coalesce_cache, key=lambda k: M._coalesce_cache[k]["exp"])
        M._coalesce_cache.pop(oldest, None)


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
    import app.main as M

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
    async with M._inflight_lock:
        entry = M._inflight_coalesce.get(key)
        if entry is None or entry["future"].done():
            fut = asyncio.get_running_loop().create_future()
            entry = {"future": fut, "waiters": 0}
            M._inflight_coalesce[key] = entry
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
            M._inflight_coalesce.pop(key, None)
            raise
        except BaseException as exc:  # noqa: BLE001
            if entry["waiters"] > 0 and not entry["future"].done():
                entry["future"].set_exception(exc)
            M._inflight_coalesce.pop(key, None)
            raise
        if not entry["future"].done():
            entry["future"].set_result(res)
        M._inflight_coalesce.pop(key, None)
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
    import app.main as M

    if not M.PERSIST_STATS:
        return
    try:
        data = _load_json(M._stats_file, dict)
        if data:
            M.router.load_stats(data)
            M.log.info("[stats] ripristinate da %s (%d deployment tracciati)", M._stats_file.name, len(M.router._stats))
    except Exception as exc:  # mai bloccare lo startup
        M.log.warning("[stats] load fallito (%s): riparto pulito", exc)


def _load_thought_sigs() -> None:
    """All'avvio: ripristina le firme Gemini catturate (sopravvivono al restart)."""
    import app.main as M

    if not M.PERSIST_STATS:
        return
    try:
        data = _load_json(M._thought_sigs_file, dict)
        if data:
            from .thought_sig import THOUGHT_SIGS

            THOUGHT_SIGS.load(data)
            M.log.info("[thought_sig] ripristinate %d firme da %s", len(THOUGHT_SIGS), M._thought_sigs_file.name)
    except Exception as exc:  # mai bloccare lo startup
        M.log.warning("[thought_sig] load fallito (%s): riparto pulito", exc)


class _PendingWrite(NamedTuple):
    """Uno snapshot di stato GIA' codificato (sull'event loop) in attesa di
    essere scritto su disco; `on_fail(exc)` registra il fallimento."""

    path: Path
    text: JsonSnapshot
    on_fail: Callable[[BaseException | None], None]


def _run_writes(writes: list[_PendingWrite]) -> None:
    """Scrive gli snapshot in ordine. Sincrona: allo shutdown gira sul
    chiamante, nel watcher su un thread (l'I/O non ferma l'event loop)."""
    for w in writes:
        try:
            ok, exc = _save_json_text(w.path, w.text), None
        except Exception as e:  # noqa: BLE001 - una scrittura non blocca le altre
            ok, exc = False, e
        if not ok:
            w.on_fail(exc)


def _dispatch(writes: list[_PendingWrite], defer: bool) -> list[_PendingWrite]:
    """defer=False: scrive subito (comportamento storico) e ritorna [].
    defer=True: ritorna gli snapshot, che il chiamante scrivera' con
    `_run_writes` (il watcher lo fa su un thread)."""
    if defer:
        return writes
    _run_writes(writes)
    return []


def _maybe_save_thought_sigs(force: bool = False, *, defer: bool = False) -> list[_PendingWrite]:
    """Salvataggio atomico throttled (max ogni 60s) delle firme Gemini."""
    import app.main as M

    if not M.PERSIST_STATS:
        return []
    now = time.time()
    if not force and now - M._last_thought_sigs_save < 60:
        return []
    M._last_thought_sigs_save = now
    from .thought_sig import THOUGHT_SIGS

    return _dispatch([_PendingWrite(M._thought_sigs_file, _freeze_json(THOUGHT_SIGS.dump()),
                                    lambda _exc: M.log.warning("[thought_sig] save fallito"))], defer)


def _maybe_save_adaptive_stats(force: bool = False, *, defer: bool = False) -> list[_PendingWrite]:
    """Salvataggio atomico throttled (max ogni 60s) delle stats adattive."""
    import app.main as M

    if not M.PERSIST_STATS:
        return []
    now = time.time()
    if not force and now - M._last_stats_save < 60:
        return []
    M._last_stats_save = now
    return _dispatch([_PendingWrite(M._stats_file, _freeze_json(M.router.dump_stats()),
                                    lambda _exc: M.log.error("[stats] save fallito"))], defer)


def _maybe_save_all(force: bool = False, *, defer: bool = False) -> list[_PendingWrite]:
    """F26: stats adattive (EMA per bucket, prefill-rate, calibration) e stato
    di routing (holder/sticky/warm/frontier) sono due viste DELLO STESSO
    istante. Salvandole con timer indipendenti, un crash nel mezzo lasciava
    holder freschi con EMA vecchie (o viceversa). Qui il throttle e' unico:
    o si flushano insieme, o nessuna delle due."""
    import app.main as M

    now = time.time()
    if not force and now - min(M._last_stats_save, M._last_routing_save) < 60:
        return []
    M._last_stats_save = now
    M._last_routing_save = now
    writes = _maybe_save_adaptive_stats(force=True, defer=True)
    writes += _maybe_save_routing_state(force=True, defer=True)
    return _dispatch(writes, defer)


def _load_cooldowns() -> None:
    """All'avvio: ripristina i cooldown NON scaduti dal file dedicato."""
    import app.main as M

    if not M.PERSIST_STATS:
        return
    try:
        data = _load_json(M._cooldown_file, dict)
        if data:
            n = M.router.load_cooldowns(data)
            M.log.info("[cooldown] ripristinati %d cooldown da %s", n, M._cooldown_file.name)
    except Exception as exc:  # mai bloccare lo startup
        M.log.warning("[cooldown] load fallito (%s): riparto pulito", exc)


def _routing_save_failed(exc: BaseException | None) -> None:
    """save_json() e' best-effort: non solleva, logga gia' l'errore REALE con
    traceback in atomic_store. Qui registriamo il fatto (stato di routing
    perso -> warm pool perso al restart). Un solo call site per snapshot e
    scrittura: `exc_info` riceve l'eccezione VERA quando c'e', altrimenti
    False (non None!) -> niente "NoneType: None", che e' un'evidenza falsa in
    on-call."""
    import app.main as M

    M.log.error("[warmstart] save fallito", exc_info=exc or False)


def _maybe_save_routing_state(force: bool = False, *, defer: bool = False) -> list[_PendingWrite]:
    """Snapshot throttled (max ogni 60s + force allo shutdown) dello stato di
    routing legato alle sessioni: il restart non deve azzerare il pool caldo."""
    import app.main as M

    if not M.PERSIST_ROUTING:
        return []
    now = time.time()
    if not force and now - M._last_routing_save < 60:
        return []
    M._last_routing_save = now
    try:
        text = _freeze_json(M.router.dump_routing_state())
    except Exception as e:  # noqa: BLE001 - mai bloccare lo shutdown
        _routing_save_failed(e)
        return []
    return _dispatch([_PendingWrite(M._routing_file, text, _routing_save_failed)], defer)


def _load_routing_state() -> None:
    """All'avvio: ripristina lo stato di routing NON scaduto (validazione e
    TTL dentro load_routing_state)."""
    import app.main as M

    if not M.PERSIST_ROUTING:
        return
    try:
        data = _load_json(M._routing_file, dict)
        if data:
            rep = M.router.load_routing_state(data)
            if any(rep.values()):
                M.log.info("[warmstart] ripristinato %s", rep)
    except Exception as exc:  # mai bloccare lo startup
        M.log.warning("[warmstart] load fallito (%s): riparto freddo", exc)


def _bootstrap_runtime_from_logs() -> None:
    """All'avvio: ricostruisce le finestre rolling-24h (uso + probe) dal log,
    cosi' il cold-spread e il moltiplicatore dell'autoprobe non ripartono
    'a freddo'. Scansiona solo `gateway.log` (nessun ruotato). La scansione e'
    in `app.logboot.scan_log`. Mai bloccare lo startup."""
    import app.main as M

    from . import autoprobe as _ap
    from .logboot import scan_log

    path = os.environ.get("GATEWAY_LOG_FILE", str(M.VAR_DIR / "gateway.log"))
    try:
        usage, probes = scan_log(path, time.time() - 86400.0)
    except Exception as exc:  # mai bloccare lo startup
        M.log.warning("[bootstrap] scan log fallito (%s)", exc)
        return
    for u, ts in usage:
        M.router.note_usage(u, ts)
    for u, ts in probes:
        _ap.note_probe_time(u, ts)
    if usage or probes:
        M.log.info("[bootstrap] finestre 24h da log: %d tentativi, %d probe", len(usage), len(probes))


def _maybe_save_cooldowns(force: bool = False, *, defer: bool = False) -> list[_PendingWrite]:
    """Salvataggio atomico throttled (max ogni 60s) dei cooldown attivi."""
    import app.main as M

    if not M.PERSIST_STATS:
        return []
    now = time.time()
    if not force and now - M._last_cooldown_save < 60:
        return []
    M._last_cooldown_save = now
    return _dispatch([_PendingWrite(M._cooldown_file, _freeze_json(M.router.save_cooldowns()),
                                    lambda _exc: M.log.error("[cooldown] save fallito"))], defer)


def _all_uniques() -> set:
    """Set degli unique attualmente configurati (per il diff hot-reload)."""
    import app.main as M

    out: set = set()
    for _lst in M.config.groups.values():
        for _d in _lst:
            out.add(_d.get("unique"))
    return out


def _all_deps() -> dict:
    """unique -> dep dict di tutti i deployment configurati (per il drain
    hot-reload: serve il dep VECCHIO di quelli rimossi)."""
    import app.main as M

    out: dict = {}
    for _lst in M.config.groups.values():
        for _d in _lst:
            out[_d.get("unique")] = _d
    return out


async def _watcher(interval: float) -> None:
    """Ogni `interval` secondi controlla mtime di CSV (credenziali) e
    gateway.yaml (policy) e ricarica ciò che è cambiato.

    Un file corrotto/mancante NON abbatta il servizio: lo stato vecchio resta vivo.
    """
    import app.main as M

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
            M.router.purge_expired()  # igiene: sticky/cooldown scaduti
            M.router.purge_draining()  # draining scaduti oltre il TTL
            # Snapshot sull'event loop (stato coerente), scritture su disco
            # su un thread: l'I/O non ferma le richieste in volo.
            writes = _maybe_save_all(defer=True)  # F26: stats+routing, stesso istante
            # giro giornaliero sui RITIRATI: parte al primo tick dopo
            # mezzanotte e li sonda con calma (un probe riuscito riabilita)
            if not background_cautious_enabled():  # cautela: nessun probe automatico
                autoprobe.maybe_spawn_retired(M.router, M.forwarder)
            writes += _maybe_save_cooldowns(defer=True)  # cooldown attivi su disco
            writes += _maybe_save_thought_sigs(defer=True)  # firme Gemini: persistite su disco
            if writes:
                await asyncio.to_thread(_run_writes, writes)
            await M.LEDGER.flush_async()  # ledger usage: offload su thread
            await repairlog.flush_async()  # ledger riparazioni: idem
            # keyhealth: osserva TUTTI i deployment con stats e aggiorna
            # l'evidenza su disco (throttled dal tick stesso)
            try:
                now = time.time()
                for u, s in list(M.router._stats.items()):
                    cooled = M.router._cooldown.get(u, 0) > now
                    M.KEYHEALTH.observe(
                        u,
                        fail_streak=s.fail_streak,
                        success_ema=s.success_ema,
                        is_cooled=cooled,
                        reason=getattr(s, "last_reason", None),
                        now=now,
                    )
                new_retired = M.KEYHEALTH.apply_retirement(M.policy.retire_after_days)
                if new_retired:
                    M.log.warning(
                        "[keyhealth] %d chiavi passate RETIRED: %s", len(new_retired), ", ".join(new_retired[:5])
                    )
                await M.KEYHEALTH.save_async()
            except Exception:  # analytics non deve mai mordere
                M.log.warning("[keyhealth] tick error", exc_info=True)
            # purge job video scaduti (mapping in memoria, TTL 24h)
            now = time.time()
            expired = [j for j, m in M._videos_jobs.items() if now - m.get("created", 0) > M.VIDEO_JOB_TTL_SEC]
            for j in expired:
                M._videos_jobs.pop(j, None)
            # purge immagini scadute (store in memoria, TTL policy images.*)
            try:
                imagestore.sweep()
            except Exception:  # noqa: BLE001
                M.log.warning("[images] sweep error", exc_info=True)

            async with _reload_lock:
                new = maybe_reload(M.config, last_csv)
                if last_csv is not None and new != last_csv:
                    M.log.info(
                        "[config] CSV ricaricato: profili=%s deployment=%d",
                        ",".join(M.config.profiles),
                        sum(len(v) for v in M.config.groups.values()),
                    )
                    try:
                        M.router.apply_quirks()  # flag in-memory (P2-9)
                    except Exception:
                        report_suppressed("runtime_persistence._watcher")
                    # Probe immediato dei deployment appena aggiunti: scoprono lo
                    # stato di salute PRIMA del traffico reale (vedi autoprobe).
                    _added = sorted(_all_uniques() - _prev_uniques)
                    _max_p = max(0, int(getattr(M.policy, "hotreload_probe_max", 20) or 0))
                    if _added and _max_p and not background_cautious_enabled():
                        autoprobe.spawn_hotreload_probe(M.router, M.forwarder, _added[:_max_p])
                        M.log.info("[hotreload] %d deployment nuovi: probe fire-and-forget", min(len(_added), _max_p))
                    # CONNECTION DRAINING: i deployment rimossi dal CSV con
                    # richieste in volo restano in config marcati draining
                    # (ignorati dal pick per le nuove richieste); l'inflight
                    # viene drenato in note_end, il TTL li forza comunque via.
                    _cur = _all_uniques()
                    for _u in sorted(set(_prev_deps) - _cur):
                        _inf = M.router.stats_for(_u).inflight
                        if _inf > 0:
                            M.router.start_draining(_u, _prev_deps[_u], _inf)
                            M.log.info(
                                "[drain] %s: rimosso dal CSV con %d richieste in volo -> draining (TTL %ds)",
                                _u,
                                _inf,
                                int(getattr(M.policy, "hotreload_drain_ttl_sec", 120) or 120),
                            )
                        else:
                            M.log.debug("[drain] %s: rimosso dal CSV, nessuna richiesta in volo -> drop immediato", _u)
                last_csv = new
                _prev_uniques = _all_uniques()
                _prev_deps = _all_deps()

            ym = csv_mtime_ns(M.POLICY_PATH)
            if ym is not None and last_yaml is not None and ym != last_yaml:
                try:
                    fresh = Policy.load(M.POLICY_PATH)
                except Exception as exc:
                    M.log.warning("[policy] reload FALLITO (%s): resta la precedente", exc)
                else:
                    M.router.policy = fresh  # swap atomico dei riferimenti
                    M.policy = fresh  # deviazione documentata: globals() qui puntava a main.py, non piu valido dopo lo spostamento
                    set_retry_after_floors(fresh.retry_after_min_sec, fresh.retry_after_floor_by_provider)
                    set_stream_stall_sec(fresh.stream_stall_sec)
                    set_strip_client_fields(fresh.strip_client_fields)
                    set_ttft_lookup(lambda u, ctx=None: M.router.bucket_latency_ms(u, ctx, "ttft"))
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
                    M.forwarder._keepalive_pool = fresh.http_keepalive_pool
                    configure_estimate(
                        adaptive=fresh.estimate_adaptive_enabled,
                        shadow=fresh.estimate_adaptive_shadow,
                        auto_enable=fresh.estimate_adaptive_auto_enable,
                        auto_min_n=fresh.estimate_adaptive_auto_min_n,
                        auto_max_delta_pct=(fresh.estimate_adaptive_auto_max_delta_pct),
                    )
                    M.log.info(
                        "[policy] ricaricata: step_up=%s%% aliases=%d per-profilo=%s",
                        fresh.step_up_pct,
                        len(fresh.aliases),
                        fresh.profile_step_up_pct or "-",
                    )
            if ym is not None and last_yaml is None:
                M.log.info("[policy] watcher: baseline %s", M.POLICY_PATH.name)
            last_yaml = ym
        except Exception as exc:  # mai far morire il watcher
            M.log.warning("[config] watcher error: %s", exc)
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
    import app.main as M

    while True:
        try:
            await asyncio.sleep(seconds_to_midnight() + random.uniform(0.0, 120.0))
            if (
                str(getattr(M.router.policy, "cooldown_autoprobe_schedule", "nightly")).lower() == "nightly"
                and autoprobe._cfg(M.router.policy)[0]
                and not background_cautious_enabled()
            ):
                await autoprobe.nightly_pass(M.router, M.forwarder)
        except asyncio.CancelledError:
            raise
        except Exception:  # il scheduler non muore MAI
            M.log.warning("[autoprobe] nightly scheduler: errore", exc_info=True)
            await asyncio.sleep(60.0)
