"""Modalita' multi-worker: topologia, replica dello stato di routing, bus.

[IT] Con `GATEWAY_WORKERS=N` (N>1) `python -m app.serve` avvia N processi
worker (vedi app/serve.py). Ogni worker ha il SUO router in memoria; per
non cambiare la rotazione rispetto al processo singolo lo stato va tenuto
allineato. Lo stato del router e' di due nature:

- PER-SESSIONE (sticky, cache holder, warm owner, frontiere ctx, slow per
  sessione, ...): vive SOLO sul worker proprietario della sessione. Ogni
  richiesta con un id di sessione viene servita dal suo proprietario
  (`owner_of`, instradamento in app/affinity.py), quindi quello stato resta
  locale e completo, esattamente come col processo singolo.
- GLOBALE (cooldown, streak, EMA di latenza/successo, finestre d'uso e
  budget, richieste in volo, rate-limit appresi, lease per chiave,
  quarantene, circuit breaker, ...): e' osservato da tutti i worker. Qui lo
  si REPLICA: i metodi di osservazione/comando del router (`ROUTER_METHODS`)
  sono avvolti; la chiamata PIU' ESTERNA eseguita su un worker viene
  pubblicata sul bus e RIESEGUITA identica sugli altri (stessi argomenti,
  stessa logica, stessa policy). Le chiamate annidate non si pubblicano
  (le rifa' la replica della chiamata esterna). Durante la replica log e
  metriche sono spenti: li ha gia' prodotti il worker che ha servito.
  I cooldown calcolati (che dipendono dall'istante) viaggiano anche come
  stato finale e vengono copiati alla lettera.

Consistenza EVENTUALE: tra due worker passa il tempo di un messaggio su un
socket locale (sotto il millisecondo). Se un worker muore, gli altri
tolgono le sue richieste in volo e le sue lease (messaggio `down` dal hub).

Con un solo worker (default) nulla di questo e' attivo: `enabled()` e'
False, nessun metodo viene avvolto, nessun socket viene aperto.

[EN] Multi-worker mode: session-scoped state stays on the session owner
(affinity routing); global routing observations are replicated by re-running
the same router method on every other worker over a local fan-out bus.
"""
from __future__ import annotations

import asyncio
import base64
import contextvars
import functools
import inspect
import json
import logging
import os
import pickle
import threading
import zlib
from pathlib import Path
from typing import Any, Callable

from . import metrics

log = logging.getLogger("nx.cluster")

ENV_SIZE = "GATEWAY_WORKERS"
ENV_INDEX = "GATEWAY_WORKER_INDEX"
ENV_RUN_DIR = "GATEWAY_RUN_DIR"

_LINE_LIMIT = 64 * 1024 * 1024


# ------------------------------------------------------------ topologia --
def size() -> int:
    """Numero di worker del cluster (1 = processo singolo, il default).

    Vale solo dentro un worker avviato da app/serve.py (che imposta
    GATEWAY_WORKER_INDEX): un `uvicorn app.main:app` lanciato a mano con
    GATEWAY_WORKERS nell'ambiente resta un processo singolo coerente."""
    if ENV_INDEX not in os.environ:
        return 1
    try:
        return max(1, int(os.environ.get(ENV_SIZE, "1") or 1))
    except ValueError:
        return 1


def enabled() -> bool:
    return size() > 1


def index() -> int:
    if not enabled():
        return 0
    try:
        return int(os.environ.get(ENV_INDEX, "0") or 0)
    except ValueError:
        return 0


def is_leader() -> bool:
    """Il worker 0 fa i lavori UNICI del gateway (health loop, giro notturno,
    probe dei ritirati/hot-reload, salvataggi dello stato globale). Con un
    processo singolo e' sempre True."""
    return index() == 0


def owner_of(session_id: str, workers: int | None = None) -> int:
    """Worker proprietario di una sessione (hash stabile tra processi)."""
    n = workers if workers is not None else size()
    return zlib.crc32(str(session_id).encode("utf-8", "replace")) % max(1, n)


def run_dir() -> Path:
    return Path(os.environ.get(ENV_RUN_DIR) or "/tmp")


def worker_socket(i: int) -> str:
    return str(run_dir() / f"w{i}.sock")


def bus_socket() -> str:
    return str(run_dir() / "bus.sock")


def per_worker_path(path: Path) -> Path:
    """File di stato PER-SESSIONE: uno per worker (`nome.wI.ext`)."""
    if not enabled():
        return path
    p = Path(path)
    return p.with_name(f"{p.stem}.w{index()}{p.suffix}")


# ----------------------------------------------------------------- codec --
class Unencodable(TypeError):
    """Argomento non serializzabile: la chiamata non viene replicata."""


class _MissingDep(LookupError):
    pass


_TAGS = ("__dep__", "__t__", "__s__")


def encode(v: Any) -> Any:
    """Argomenti -> JSON. I deployment (dict con unique+api_key) viaggiano
    come riferimento `{"__dep__": unique}`: nessuna credenziale sul bus, e
    il ricevente usa il SUO oggetto deployment (stessa identita' di config)."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        if "unique" in v and "api_key" in v:
            return {"__dep__": str(v["unique"])}
        out = {}
        for k, x in v.items():
            if not isinstance(k, str):
                raise Unencodable("chiave non stringa")
            out[k] = encode(x)
        if len(out) == 1 and next(iter(out)) in _TAGS:
            raise Unencodable("dict ambiguo")
        return out
    if isinstance(v, tuple):
        return {"__t__": [encode(x) for x in v]}
    if isinstance(v, list):
        return [encode(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return {"__s__": [encode(x) for x in v]}
    raise Unencodable(type(v).__name__)


def decode(v: Any, resolve_dep: Callable[[str], dict | None]) -> Any:
    if isinstance(v, list):
        return [decode(x, resolve_dep) for x in v]
    if isinstance(v, dict):
        if len(v) == 1:
            if "__dep__" in v:
                dep = resolve_dep(v["__dep__"])
                if dep is None:
                    raise _MissingDep(v["__dep__"])
                return dep
            if "__t__" in v:
                return tuple(decode(x, resolve_dep) for x in v["__t__"])
            if "__s__" in v:
                return {decode(x, resolve_dep) for x in v["__s__"]}
        return {k: decode(x, resolve_dep) for k, x in v.items()}
    return v


# ---------------------------------------------------------- replicazione --
# Punti d'ingresso che mutano stato GLOBALE del router. Tutto il resto
# (pick, sticky, warm, sessioni, cache) e' per-sessione o una cache locale.
ROUTER_METHODS: tuple[str, ...] = (
    # fallimenti / cooldown
    "mark_failed", "mark_failed_double_residual", "clear_cooldown",
    "drop_cooldown", "drop_all_cooldowns", "set_cooldown_until",
    "reset_after_probe", "reset_for_unretire",
    "reset_probe_fail_streak", "bump_probe_fail_streak",
    "note_rate_limit", "quarantine_endpoint",
    # traffico, latenze, budget
    "note_start", "note_end", "note_result", "note_stream_end",
    "note_usage", "note_output_tokens", "note_estimate_error",
    "note_discovered_max_input", "note_json_fallback", "note_wake",
    # capability, modelli, riparazioni, escalation
    "note_cap_strike", "note_model_failure", "note_repair_exempt",
    "record_escalation_win",
    # SESSION-DEP GUARD: "quale sessione ha usato per ultima il dep" e'
    # indicizzata per deployment, quindi e' stato di TUTTE le sessioni
    "_note_dep_session", "note_session_activity",
    # lease di concorrenza per chiave
    "_lease_put", "_lease_drop",
    # comandi operatore (admin)
    "reset_scores", "clear_pressure", "clear_key_leases",
    "sticky_release", "sticky_release_all", "purge_sessions",
    "drain_by_operator", "undrain_by_operator",
)
KEYHEALTH_METHODS: tuple[str, ...] = ("set_state", "clear")
# metodi il cui cooldown risultante dipende dall'istante: si copia lo stato
_COOLDOWN_SYNC = frozenset({"mark_failed", "mark_failed_double_residual", "set_cooldown_until"})

_depth: contextvars.ContextVar[int] = contextvars.ContextVar("nx_cluster_depth", default=0)
_replaying: contextvars.ContextVar[bool] = contextvars.ContextVar("nx_cluster_replaying", default=False)


def replaying() -> bool:
    return _replaying.get()


class _ReplayLogFilter(logging.Filter):
    """Niente log durante la replica: la riga l'ha gia' scritta chi ha servito."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _replaying.get()


def _ctx_tokens(ctx_est: Any) -> int:
    try:
        return max(0, int(ctx_est)) if ctx_est else 0
    except (TypeError, ValueError):
        return 0


# Stato GLOBALE del router copiato a un worker che (ri)entra nel cluster
# (snapshot del worker piu' anziano): cio' che i metodi replicati mutano.
# Esclusi lo stato per-sessione (resta al proprietario) e le cache locali.
SYNC_ATTRS: tuple[str, ...] = (
    "_stats", "_cooldown", "_cooldown_since", "_cooldown_full", "_cooldown_prov_map",
    "_key_hints", "_key_soft", "_usage_times", "_out_tokens", "_wake_times", "_gen_rate",
    "_gen_last_model", "_cap_strikes", "_esc_win", "_base_scores", "_provider_scores",
    "_key_scores", "_avg_latencies", "_lat_buckets", "_ttft_buckets", "_prefill_rate",
    "_est_div", "_scores_decay_ts", "_scores_decay_log_ts", "_conc_limit", "_conc_ok",
    "_circuit_breakers", "_dep_circuit_breakers", "_model_cb", "_model_fail_win_map",
    "_repair_exempt_map", "_key_leases_map", "_discovered_max_input", "_endpoint_quarantine",
    "_last_attempt", "_prov_last", "_dep_last_session", "_session_deps",
)
SYNC_TIMEOUT_SEC = 10.0


class Replicator:
    """Avvolge i metodi replicati e riesegue quelli ricevuti dagli altri."""

    def __init__(self, router, keyhealth, send: Callable[[dict], None], origin: str = ""):
        self.router = router
        self.keyhealth = keyhealth
        self.origin = origin
        self._send = send
        self._targets = {"r": router, "kh": keyhealth}
        self._allowed = {"r": frozenset(ROUTER_METHODS), "kh": frozenset(KEYHEALTH_METHODS)}
        self._sigs: dict[tuple[str, str], inspect.Signature] = {}
        self._wrapped: list[tuple[object, str]] = []
        # richieste in volo [n, token] e lease PER PROCESSO d'origine (questo
        # compreso): se un processo muore le sue vanno tolte (non arrivera'
        # mai il note_end); un worker che entra le riceve nello snapshot.
        self._origin_inflight: dict[str, dict[str, list[int]]] = {}
        self._origin_leases: dict[str, set[str]] = {}
        # ultimo numero di sequenza applicato per processo d'origine: rende
        # esatto il passaggio snapshot -> messaggi arrivati nel frattempo
        self._seq = 0
        self._seen: dict[str, int] = {}
        self._syncing = False
        self._buffer: list[dict] = []
        self._synced: asyncio.Event | None = None
        self.last_sync_applied = False
        self.published = 0
        self.replayed = 0
        self.skipped = 0
        self.errors = 0

    # -- installazione --------------------------------------------------
    def install(self) -> None:
        for name in ROUTER_METHODS:
            self._wrap("r", self.router, name)
        if self.keyhealth is not None:
            for name in KEYHEALTH_METHODS:
                self._wrap("kh", self.keyhealth, name)

    def uninstall(self) -> None:
        for obj, name in self._wrapped:
            try:
                delattr(obj, name)
            except AttributeError:
                pass
        self._wrapped.clear()

    def _wrap(self, kind: str, obj, name: str) -> None:
        fn = getattr(obj, name, None)
        if fn is None:
            return
        fn = getattr(fn, "__cluster_original__", fn)
        self._sigs[(kind, name)] = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if _replaying.get() or _depth.get():
                return fn(*args, **kwargs)
            token = _depth.set(1)
            try:
                result = fn(*args, **kwargs)
            finally:
                _depth.reset(token)
            self._publish(kind, name, args, kwargs)
            return result

        wrapper.__cluster_original__ = fn  # type: ignore[attr-defined]
        setattr(obj, name, wrapper)
        self._wrapped.append((obj, name))

    def _original(self, kind: str, name: str):
        fn = getattr(self._targets[kind], name)
        return getattr(fn, "__cluster_original__", fn)

    def _bound(self, kind: str, name: str, args, kwargs) -> dict:
        sig = self._sigs.get((kind, name))
        try:
            if sig is None:
                sig = self._sigs[(kind, name)] = inspect.signature(self._original(kind, name))
            return sig.bind(*args, **kwargs).arguments
        except (AttributeError, TypeError, ValueError):
            return {}

    # -- pubblicazione ----------------------------------------------------
    def _publish(self, kind: str, name: str, args, kwargs) -> None:
        try:
            msg: dict = {"k": kind, "m": name, "a": encode(list(args)), "kw": encode(kwargs)}
        except Unencodable as exc:
            self.skipped += 1
            log.debug("[cluster] %s non replicato: argomento %s", name, exc)
            return
        if kind == "r" and name in _COOLDOWN_SYNC:
            unique = self._bound(kind, name, args, kwargs).get("unique")
            if isinstance(unique, str):
                msg["cd"] = [unique, self._cooldown_snapshot(unique)]
        self._track_origin(self.origin, kind, name, args, kwargs)
        self._seq += 1
        msg["s"] = self._seq
        self.published += 1
        self._send(msg)

    def local_inflight(self) -> int:
        """Richieste in volo servite da QUESTO worker (drain allo shutdown)."""
        return sum(n for n, _tokens in self._origin_inflight.get(self.origin, {}).values())

    # -- cooldown: copia dello stato finale --------------------------------
    def _cooldown_snapshot(self, unique: str) -> list:
        r = self.router
        return [r._cooldown.get(unique), r._cooldown_since.get(unique), r._cooldown_full_map().get(unique)]

    def _cooldown_restore(self, unique: str, snap: list) -> None:
        r = self.router
        for store, value in zip((r._cooldown, r._cooldown_since, r._cooldown_full_map()), snap):
            if value is None:
                store.pop(unique, None)
            else:
                store[unique] = float(value)

    # -- ricezione --------------------------------------------------------
    def handle(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "config_changed":
            for callback in list(_CONFIG_LISTENERS):
                callback()
            return
        if t == "sync_req":
            self._answer_sync(str(msg.get("o") or ""))
            return
        if t == "sync":
            if self._syncing:
                self.apply_sync(msg)
            return
        if self._syncing:              # in attesa dello snapshot: in coda
            self._buffer.append(msg)
            return
        self._apply(msg)

    def _apply(self, msg: dict) -> None:
        if msg.get("t") == "down":
            self.peer_down(str(msg.get("o") or ""))
            return
        origin = str(msg.get("o") or "")
        seq = msg.get("s")
        if isinstance(seq, int) and origin:
            if seq <= self._seen.get(origin, 0):
                return                 # gia' compreso nello snapshot
            self._seen[origin] = seq
        self._replay(msg, origin)

    def _replay(self, msg: dict, origin: str) -> None:
        kind, name = msg.get("k"), msg.get("m")
        if kind not in self._allowed or name not in self._allowed[kind]:
            return
        if self._targets.get(kind) is None:
            return
        try:
            args = decode(msg.get("a") or [], self._resolve_dep)
            kwargs = decode(msg.get("kw") or {}, self._resolve_dep)
        except _MissingDep:
            self.skipped += 1        # deployment non (ancora) nella nostra config
            return
        failure: Exception | None = None
        token = _replaying.set(True)
        try:
            with metrics.muted():
                self._original(kind, name)(*args, **kwargs)
                if "cd" in msg:
                    unique, snap = msg["cd"]
                    self._cooldown_restore(unique, snap)
                self._track_origin(origin, kind, name, args, kwargs)
                if kind == "kh" and is_leader() and name == "set_state":
                    self.keyhealth.save()     # il leader e' chi scrive il file
            self.replayed += 1
        except Exception as exc:  # noqa: BLE001 - una replica fallita non ferma il bus
            failure = exc
        finally:
            _replaying.reset(token)
        if failure is not None:       # fuori dal contesto di replica: il log passa
            self.errors += 1
            log.warning("[cluster] replica di %s fallita: %r", name, failure)

    def _resolve_dep(self, unique: str) -> dict | None:
        try:
            return self.router.config.deployment_by_unique(unique)
        except Exception:  # noqa: BLE001
            return None

    def _track_origin(self, origin: str, kind: str, name: str, args, kwargs) -> None:
        if kind != "r" or not origin:
            return
        if name in ("note_start", "note_end"):
            bound = self._bound(kind, name, args, kwargs)
            unique = bound.get("unique")
            if not isinstance(unique, str):
                return
            per = self._origin_inflight.setdefault(origin, {})
            cnt = per.setdefault(unique, [0, 0])
            sign = 1 if name == "note_start" else -1
            cnt[0] = max(0, cnt[0] + sign)
            cnt[1] = max(0, cnt[1] + sign * _ctx_tokens(bound.get("ctx_est")))
            if cnt == [0, 0]:
                per.pop(unique, None)
        elif name == "_lease_put":
            bound = self._bound(kind, name, args, kwargs)
            self._origin_leases.setdefault(origin, set()).add(str(bound.get("tok")))
        elif name == "_lease_drop":
            bound = self._bound(kind, name, args, kwargs)
            self._origin_leases.get(origin, set()).discard(str(bound.get("tok")))

    def peer_down(self, origin: str) -> None:
        """Un processo e' uscito: le sue richieste in volo non chiuderanno mai."""
        self._seen.pop(origin, None)
        inflight = self._origin_inflight.pop(origin, {})
        leases = self._origin_leases.pop(origin, set())
        if not inflight and not leases:
            return
        token = _replaying.set(True)
        try:
            for unique, (n, tokens) in inflight.items():
                s = self.router.stats_for(unique)
                s.inflight = max(0, s.inflight - n)
                s.inflight_tokens = max(0, s.inflight_tokens - tokens)
            drop = self._original("r", "_lease_drop")
            for tok in leases:
                drop(tok)
        finally:
            _replaying.reset(token)
        log.info("[cluster] processo %s uscito: tolte %d richieste in volo e %d lease",
                 origin, sum(v[0] for v in inflight.values()), len(leases))

    # -- ingresso nel cluster: snapshot dal worker piu' anziano ----------
    def snapshot(self) -> dict:
        """Stato globale attuale + in volo/lease per processo + sequenze viste.
        Preso in un colpo solo sull'event loop: e' un punto coerente."""
        state = {a: getattr(self.router, a) for a in SYNC_ATTRS if hasattr(self.router, a)}
        seen = dict(self._seen)
        seen[self.origin] = self._seq
        return {
            "state": base64.b64encode(pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)).decode("ascii"),
            "inflight": {o: {u: list(c) for u, c in m.items()} for o, m in self._origin_inflight.items() if m},
            "leases": {o: sorted(t) for o, t in self._origin_leases.items() if t},
            "seen": seen,
        }

    def _answer_sync(self, requester: str) -> None:
        if not requester:
            return
        if self._syncing:              # anch'io sto entrando: niente da offrire
            self._send({"t": "sync", "to": requester, "empty": True})
            return
        try:
            self._send({"t": "sync", "to": requester, **self.snapshot()})
        except Exception:  # noqa: BLE001 - chi entra ripiega sullo stato da disco
            log.warning("[cluster] snapshot per %s fallito", requester, exc_info=True)
            self._send({"t": "sync", "to": requester, "empty": True})

    def _restore_own(self, inflight: dict, toks: set, leases: list) -> None:
        if inflight:
            self._origin_inflight[self.origin] = {u: list(c) for u, c in inflight.items()}
            for unique, (n, tokens) in inflight.items():
                s = self.router.stats_for(unique)
                s.inflight += n
                s.inflight_tokens += tokens
        if toks:
            self._origin_leases[self.origin] = set(toks)
            leases_map = self.router._key_leases()
            for key, entry in leases:
                if not any(e[0] == entry[0] for e in leases_map.get(key, ())):
                    leases_map.setdefault(key, []).append(entry)

    def begin_sync(self) -> asyncio.Event:
        self._syncing = True
        self._buffer = []
        self._synced = asyncio.Event()
        return self._synced

    def apply_sync(self, msg: dict | None) -> bool:
        """Applica lo snapshot (None/empty = nessuno: resta lo stato da disco),
        poi i messaggi arrivati nel frattempo che lo snapshot non comprende."""
        applied = False
        if msg and not msg.get("empty"):
            try:
                state = pickle.loads(base64.b64decode(msg["state"]))
                # le PROPRIE richieste in volo e lease (dopo una riconnessione
                # gli altri le hanno tolte col `down`): si rimettono sullo snapshot
                own_inflight = self._origin_inflight.get(self.origin, {})
                own_toks = self._origin_leases.get(self.origin, set())
                own_leases = [(key, e) for key, ent in (getattr(self.router, "_key_leases_map", None) or {}).items()
                              for e in ent if e[0] in own_toks]
                for attr, value in state.items():
                    if attr in SYNC_ATTRS:
                        setattr(self.router, attr, value)
                self._origin_inflight = {o: {u: [int(c[0]), int(c[1])] for u, c in m.items()}
                                         for o, m in (msg.get("inflight") or {}).items()}
                self._origin_leases = {o: set(t) for o, t in (msg.get("leases") or {}).items()}
                self._seen = {o: int(v) for o, v in (msg.get("seen") or {}).items()}
                self._restore_own(own_inflight, own_toks, own_leases)
                applied = True
            except Exception:  # noqa: BLE001
                log.warning("[cluster] snapshot illeggibile: resto sullo stato da disco", exc_info=True)
        self._syncing = False
        pending, self._buffer = self._buffer, []
        for m in pending:
            self._apply(m)
        self.last_sync_applied = applied
        if self._synced is not None:
            self._synced.set()
        return applied


# ------------------------------------------------------------------- bus --
def _dumps(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


class BusClient:
    """Lato worker: pubblica le proprie osservazioni, riceve quelle altrui."""

    def __init__(self, path: str, origin: str, index_: int, on_message: Callable[[dict], None]):
        self.path = path
        self.origin = origin
        self.index = index_
        self._on_message = on_message
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._stopping = False
        self.dropped = 0
        self.reconnects = 0
        # chiamata dopo una riconnessione: i messaggi persi nel frattempo
        # vanno recuperati (cluster.start la usa per chiedere uno snapshot)
        self.on_reconnect: Callable[[], None] | None = None

    async def start(self, timeout: float = 15.0) -> None:
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        reader = await self._connect(timeout)
        self._task = asyncio.create_task(self._read_loop(reader))

    async def _connect(self, timeout: float) -> asyncio.StreamReader:
        deadline = self._loop.time() + timeout
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self.path, limit=_LINE_LIMIT)
                break
            except OSError:
                if self._loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.2)
        writer.write(_dumps({"t": "hello", "w": self.index, "o": self.origin}))
        self._writer = writer
        return reader

    def send(self, msg: dict) -> None:
        msg["o"] = self.origin
        writer = self._writer
        if writer is None or writer.is_closing():
            self.dropped += 1
            return
        data = _dumps(msg)
        if threading.get_ident() == self._loop_thread:
            writer.write(data)
        else:                                     # chiamata da un thread
            self._loop.call_soon_threadsafe(writer.write, data)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while not self._stopping:
            try:
                line = await reader.readline()
            except (ConnectionError, asyncio.LimitOverrunError, ValueError):
                line = b""
            if not line:
                if self._stopping:
                    return
                log.warning("[cluster] bus perso: riconnessione")
                self._writer = None
                try:
                    reader = await self._connect(timeout=60.0)
                except OSError:
                    log.error("[cluster] bus irraggiungibile: replica sospesa")
                    return
                self.reconnects += 1
                if self.on_reconnect is not None:
                    self.on_reconnect()
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self._on_message(msg)

    async def stop(self) -> None:
        self._stopping = True
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


class BusHub:
    """Lato supervisore: ogni riga ricevuta va a tutti gli ALTRI worker; la
    chiusura di una connessione diventa un messaggio `down` per gli altri."""

    def __init__(self, path: str):
        self.path = path
        self._peers: dict[str, asyncio.StreamWriter] = {}
        self._index: dict[str, int] = {}
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._handle, path=self.path, limit=_LINE_LIMIT)
        os.chmod(self.path, 0o600)

    def _route(self, line: bytes, origin: str) -> None:
        """Replica e `down` a tutti gli altri; `to` a un solo destinatario;
        `sync_req` al worker piu' anziano (indice piu' basso) tra gli altri."""
        if b'"to":' in line or b'"sync_req"' in line:
            try:
                msg = json.loads(line)
            except ValueError:
                return
            if msg.get("t") == "sync_req":
                others = sorted((i, o) for o, i in self._index.items() if o != origin and o in self._peers)
                if not others:
                    self._send_to(origin, _dumps({"t": "sync", "to": origin, "empty": True}))
                else:
                    self._send_to(others[0][1], line)
                return
            if msg.get("to"):
                self._send_to(str(msg["to"]), line)
                return
        self._broadcast(line, origin)

    def _send_to(self, origin: str, data: bytes) -> None:
        w = self._peers.get(origin)
        if w is not None and not w.is_closing():
            w.write(data)

    def _broadcast(self, data: bytes, skip: str | None) -> None:
        for origin, w in list(self._peers.items()):
            if origin == skip or w.is_closing():
                continue
            w.write(data)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        origin = None
        try:
            hello = json.loads(await reader.readline() or b"{}")
            origin = str(hello.get("o") or "")
            if hello.get("t") != "hello" or not origin:
                return
            self._peers[origin] = writer
            self._index[origin] = int(hello.get("w") or 0)
            while True:
                line = await reader.readline()
                if not line:
                    break
                self._route(line, origin)
        except (ConnectionError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            if origin and self._peers.get(origin) is writer:
                self._peers.pop(origin, None)
                self._index.pop(origin, None)
                self._broadcast(_dumps({"t": "down", "o": origin}), origin)
            writer.close()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for w in list(self._peers.values()):
            w.close()
        self._peers.clear()


# ------------------------------------------------------ ciclo di vita --
_REPLICATOR: Replicator | None = None
_BUS: BusClient | None = None
_LOG_FILTER = _ReplayLogFilter()
_CONFIG_LISTENERS: list[Callable[[], None]] = []


def on_config_changed(callback: Callable[[], None]) -> None:
    """Registra chi va avvisato quando un altro worker riscrive CSV/policy."""
    if callback not in _CONFIG_LISTENERS:
        _CONFIG_LISTENERS.append(callback)


def notify_config_changed() -> None:
    """Questo worker ha riscritto CSV/policy: gli altri ricaricano subito."""
    if _BUS is not None:
        _BUS.send({"t": "config_changed"})


async def start(router, keyhealth) -> None:
    """Chiamata dal lifespan DOPO il caricamento dello stato da disco."""
    global _REPLICATOR, _BUS
    if not enabled() or _REPLICATOR is not None:
        return
    origin = f"w{index()}:{os.getpid()}"
    bus = BusClient(bus_socket(), origin, index(), on_message=lambda m: rep.handle(m))
    rep = Replicator(router, keyhealth, send=bus.send, origin=origin)
    for h in logging.getLogger().handlers:
        h.addFilter(_LOG_FILTER)
    synced = rep.begin_sync()          # cio' che arriva prima dello snapshot va in coda
    bus.on_reconnect = lambda: _resync(rep, bus)
    await bus.start()
    rep.install()
    _REPLICATOR, _BUS = rep, bus
    bus.send({"t": "sync_req"})
    try:
        await asyncio.wait_for(synced.wait(), SYNC_TIMEOUT_SEC)
        mode = "allineato" if rep.last_sync_applied else "primo del cluster (stato da disco)"
    except asyncio.TimeoutError:
        rep.apply_sync(None)
        mode = "snapshot non arrivato (stato da disco)"
    log.info("[cluster] worker %d/%d collegato al bus (%s): %s", index(), size(), origin, mode)


def _resync(rep: Replicator, bus: BusClient) -> None:
    """Dopo una riconnessione al bus: i messaggi persi si recuperano con uno
    snapshot, come all'ingresso (senza snapshot entro il timeout si prosegue)."""
    waiting = rep.begin_sync()
    bus.send({"t": "sync_req"})

    def give_up() -> None:
        if rep._syncing and rep._synced is waiting:     # proprio QUESTA attesa
            rep.apply_sync(None)

    asyncio.get_running_loop().call_later(SYNC_TIMEOUT_SEC, give_up)
    log.warning("[cluster] bus ripristinato: richiesto riallineamento")


async def stop() -> None:
    global _REPLICATOR, _BUS
    if _REPLICATOR is not None:
        _REPLICATOR.uninstall()
    if _BUS is not None:
        await _BUS.stop()
    for h in logging.getLogger().handlers:
        h.removeFilter(_LOG_FILTER)
    _REPLICATOR = _BUS = None


def local_inflight(router) -> int:
    """Richieste in volo servite da questo processo (drain dello shutdown)."""
    if _REPLICATOR is None:
        return router.inflight_total()
    return _REPLICATOR.local_inflight()


def stats() -> dict:
    """Stato del cluster visto da questo worker (GET /admin/cluster, /metrics)."""
    base = {"enabled": enabled(), "workers": size(), "index": index(), "leader": is_leader(),
            "pid": os.getpid()}
    if _REPLICATOR is None:
        return base
    r = _REPLICATOR
    return {**base, "origin": r.origin, "synced_from_peer": r.last_sync_applied,
            "published": r.published, "replayed": r.replayed, "skipped": r.skipped,
            "errors": r.errors, "dropped": _BUS.dropped if _BUS else 0,
            "reconnects": _BUS.reconnects if _BUS else 0,
            "local_inflight": r.local_inflight(),
            "peers_tracked": sorted(o for o in r._origin_inflight if o != r.origin)}


# contatori esposti su /metrics come nx_cluster_<nome> (gauge per worker)
METRIC_FIELDS = ("published", "replayed", "skipped", "errors", "dropped", "reconnects", "local_inflight")
