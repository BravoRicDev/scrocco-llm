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
import contextvars
import functools
import inspect
import json
import logging
import os
import threading
import zlib
from pathlib import Path
from typing import Any, Callable

from . import metrics

log = logging.getLogger("nx.cluster")

ENV_SIZE = "GATEWAY_WORKERS"
ENV_INDEX = "GATEWAY_WORKER_INDEX"
ENV_RUN_DIR = "GATEWAY_RUN_DIR"
ENV_SEED = "GATEWAY_CLUSTER_SEED"      # mescolamento del CSV identico tra worker (app/config.py)

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


class Replicator:
    """Avvolge i metodi replicati e riesegue quelli ricevuti dagli altri."""

    def __init__(self, router, keyhealth, send: Callable[[dict], None]):
        self.router = router
        self.keyhealth = keyhealth
        self._send = send
        self._targets = {"r": router, "kh": keyhealth}
        self._allowed = {"r": frozenset(ROUTER_METHODS), "kh": frozenset(KEYHEALTH_METHODS)}
        self._sigs: dict[tuple[str, str], inspect.Signature] = {}
        self._wrapped: list[tuple[object, str]] = []
        # richieste in volo / lease ricevute da ogni altro processo: se quel
        # processo muore vanno tolte (non arrivera' mai il note_end)
        self._origin_inflight: dict[str, dict[str, list[int]]] = {}
        self._origin_leases: dict[str, set[str]] = {}
        self._local_inflight: dict[str, int] = {}
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
        if kind == "r":
            if name in _COOLDOWN_SYNC or name in ("note_start", "note_end"):
                bound = self._bound(kind, name, args, kwargs)
                unique = bound.get("unique")
                if name in _COOLDOWN_SYNC and isinstance(unique, str):
                    msg["cd"] = [unique, self._cooldown_snapshot(unique)]
                elif isinstance(unique, str):
                    delta = 1 if name == "note_start" else -1
                    n = self._local_inflight.get(unique, 0) + delta
                    if n > 0:
                        self._local_inflight[unique] = n
                    else:
                        self._local_inflight.pop(unique, None)
        self.published += 1
        self._send(msg)

    def local_inflight(self) -> int:
        """Richieste in volo servite da QUESTO worker (drain allo shutdown)."""
        return sum(self._local_inflight.values())

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
        if msg.get("t") == "down":
            self.peer_down(str(msg.get("o") or ""))
            return
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
                self._track_origin(str(msg.get("o") or ""), kind, name, args, kwargs)
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
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._handle, path=self.path, limit=_LINE_LIMIT)
        os.chmod(self.path, 0o600)

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
            while True:
                line = await reader.readline()
                if not line:
                    break
                self._broadcast(line, origin)
        except (ConnectionError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            if origin and self._peers.get(origin) is writer:
                self._peers.pop(origin, None)
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


async def start(router, keyhealth) -> None:
    """Chiamata dal lifespan DOPO il caricamento dello stato da disco."""
    global _REPLICATOR, _BUS
    if not enabled() or _REPLICATOR is not None:
        return
    origin = f"w{index()}:{os.getpid()}"
    bus = BusClient(bus_socket(), origin, index(), on_message=lambda m: rep.handle(m))
    rep = Replicator(router, keyhealth, send=bus.send)
    for h in logging.getLogger().handlers:
        h.addFilter(_LOG_FILTER)
    await bus.start()
    rep.install()
    _REPLICATOR, _BUS = rep, bus
    log.info("[cluster] worker %d/%d collegato al bus (%s)", index(), size(), origin)


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
    if _REPLICATOR is None:
        return {"enabled": enabled(), "workers": size(), "index": index()}
    r = _REPLICATOR
    return {"enabled": True, "workers": size(), "index": index(),
            "published": r.published, "replayed": r.replayed, "skipped": r.skipped,
            "errors": r.errors, "dropped": _BUS.dropped if _BUS else 0}
