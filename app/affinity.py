"""Instradamento per sessione tra worker (solo multi-worker, GATEWAY_WORKERS>1).

[IT] Il kernel distribuisce le connessioni TCP tra i worker a caso; lo stato
PER-SESSIONE del router (sticky, cache holder, warm owner, frontiere, ...)
deve pero' restare su UN worker, come col processo singolo. Quindi:

- `AffinityProxy` (davanti all'app, solo sul socket TCP pubblico) calcola
  l'id di sessione della richiesta con la STESSA funzione del gateway
  (header di sessione, poi `user`/`metadata.session_id`, poi fingerprint
  anonima) e il worker proprietario (`cluster.owner_of`). Se e' un altro
  worker, gli passa la richiesta cosi' com'e' sul suo socket unix privato e
  ritrasmette la risposta byte per byte (stream compresi, disconnessione del
  client propagata). Senza sessione, o se il proprietario non risponde (es.
  sta ripartendo), la richiesta si serve localmente: nessun errore in piu'.
- `InternalEntry` (davanti all'app sul socket unix privato) ripristina
  l'indirizzo/schema del client originale, che arriva in header privati
  `x-scrocco-*`. Quegli header valgono SOLO sul socket unix (directory 0700
  del supervisore): dal TCP pubblico vengono sempre scartati.

Anche /metrics usa i socket privati: il worker che riceve lo scrape unisce
le metriche di tutti con la label `worker`.

[EN] Session-affinity routing between workers over private unix sockets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import parse_qs

import httpx

from . import cluster, metrics, offload

log = logging.getLogger("nx.affinity")

metrics.declare("nx_affinity_fallback_total", "owner")


class _HideHopLog(logging.Filter):
    """httpx logga ogni richiesta a INFO: l'hop interno tra worker non e' una
    chiamata upstream e non deve comparire tra quelle."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "http://worker/" not in record.getMessage()


logging.getLogger("httpx").addFilter(_HideHopLog())

_PRIVATE = b"x-scrocco-"
_H_CLIENT = b"x-scrocco-client"
_H_SCHEME = b"x-scrocco-scheme"
_H_SERVER = b"x-scrocco-server"
_SESSION_HEADERS = (b"x-opencode-session", b"x-session-affinity", b"x-session-id")
_HOP_BY_HOP = frozenset({b"connection", b"keep-alive", b"proxy-connection", b"transfer-encoding",
                         b"te", b"trailer", b"upgrade", b"content-length"})
# aggiunti dal server che risponde al client (niente doppioni)
_RESPONSE_DROP = _HOP_BY_HOP - {b"content-length"} | {b"date", b"server"}


def _header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or ():
        if k == name:
            return v.decode("latin-1")
    return None


def _strip_private(scope) -> dict:
    headers = scope.get("headers") or []
    if not any(k.startswith(_PRIVATE) for k, _ in headers):
        return scope
    scope = dict(scope)
    scope["headers"] = [(k, v) for k, v in headers if not k.startswith(_PRIVATE)]
    return scope


def _affinity_kind(scope) -> str | None:
    """Quali richieste hanno una sessione: LLM (/v1, /api ollama) e le viste
    admin che citano una sessione. Tutto il resto e' senza stato di sessione."""
    method, path = scope.get("method"), scope.get("path", "")
    if method == "POST" and (path.startswith("/v1/") or path.startswith("/api/")):
        return "llm"
    if path.startswith("/admin/"):
        return "admin"
    return None


def _header_session(scope) -> str | None:
    for name in _SESSION_HEADERS:
        v = (_header(scope, name) or "").strip()
        if v:
            return v
    return None


def _is_json(scope) -> bool:
    return "json" in (_header(scope, b"content-type") or "").lower()


async def _read_body(receive) -> tuple[bytes, dict | None]:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return b"".join(chunks), message
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks), None


async def _body_session(scope, body: bytes, kind: str) -> tuple[str | None, object]:
    """(sessione, body decodificato). Il body decodificato torna utile se la
    richiesta resta qui: l'endpoint non lo decodifica una seconda volta."""
    try:
        payload = await offload.run(json.loads, body, size=len(body))
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, payload
    if kind == "admin":
        sid = payload.get("session_id")
        return (str(sid) if sid else None), payload
    from starlette.requests import Request

    from .chat_helpers import _session_id

    try:
        return _session_id(Request(scope), payload), payload
    except Exception:  # noqa: BLE001 - nel dubbio si serve in locale
        return None, payload


def _replaying_receive(body: bytes, disconnected: dict | None, receive):
    replayed = False

    async def replay():
        nonlocal replayed
        if not replayed:
            replayed = True
            if disconnected is not None:
                return disconnected
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


class _Peers:
    """Un client httpx per worker, sul suo socket unix privato."""

    def __init__(self) -> None:
        self._clients: dict[int, httpx.AsyncClient] = {}

    def get(self, i: int) -> httpx.AsyncClient:
        client = self._clients.get(i)
        if client is None:
            client = httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=cluster.worker_socket(i)),
                timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=None),
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=64,
                                    keepalive_expiry=30.0),
            )
            self._clients[i] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()


PEERS = _Peers()


def _forward_headers(scope) -> list[tuple[bytes, bytes]]:
    headers = [(k, v) for k, v in scope.get("headers") or () if k not in _HOP_BY_HOP]
    client = scope.get("client")
    if client:
        headers.append((_H_CLIENT, json.dumps([client[0], client[1]]).encode()))
    server = scope.get("server")
    if server:
        headers.append((_H_SERVER, json.dumps([server[0], server[1]]).encode()))
    headers.append((_H_SCHEME, str(scope.get("scheme") or "http").encode()))
    return headers


def _target_url(scope) -> httpx.URL:
    raw = scope.get("raw_path") or scope.get("path", "/").encode()
    qs = scope.get("query_string") or b""
    return httpx.URL(scheme="http", host="worker", raw_path=raw + (b"?" + qs if qs else b""))


async def _wait_disconnect(receive) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


class AffinityProxy:
    """Middleware ASGI sul socket TCP pubblico di ogni worker (cluster)."""

    def __init__(self, app, *, index: int | None = None, workers: int | None = None, peers: _Peers | None = None):
        self.app = app
        self.index = cluster.index() if index is None else index
        self.workers = cluster.size() if workers is None else workers
        self.peers = peers or PEERS
        self._unreachable: dict[int, tuple[float, int]] = {}

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope = _strip_private(scope)
        kind = _affinity_kind(scope)
        if kind is None:
            await self.app(scope, receive, send)
            return
        sid = _header_session(scope)
        body = disconnected = payload = None
        if sid is None and kind == "admin" and scope.get("method") == "GET":
            vals = parse_qs((scope.get("query_string") or b"").decode("latin-1")).get("session_id")
            sid = vals[0] if vals else None
        elif sid is None and _is_json(scope):
            body, disconnected = await _read_body(receive)
            if disconnected is None:
                sid, payload = await _body_session(scope, body, kind)
        owner = cluster.owner_of(sid, self.workers) if sid else self.index
        if owner != self.index:
            if body is None:
                body, disconnected = await _read_body(receive)
            if disconnected is None and await self._proxy(owner, scope, body, receive, send):
                return
        if body is not None:
            receive = _replaying_receive(body, disconnected, receive)
            if payload is not None and kind == "llm":
                # decodificato una volta sola: l'endpoint lo riprende da qui
                scope = dict(scope)
                scope[offload.PARSED_BODY_KEY] = (body, payload)
        await self.app(scope, receive, send)

    UNREACHABLE_LOG_EVERY_SEC = 10.0

    def _note_unreachable(self, owner: int, exc: BaseException) -> None:
        """Durante il riavvio di un worker OGNI sua richiesta ripiega qui:
        una riga ogni 10s per worker (col conteggio), non una per richiesta."""
        metrics.inc("nx_affinity_fallback_total", (str(owner),))
        now = time.monotonic()
        last, skipped = self._unreachable.get(owner, (0.0, 0))
        if now - last < self.UNREACHABLE_LOG_EVERY_SEC:
            self._unreachable[owner] = (last, skipped + 1)
            return
        self._unreachable[owner] = (now, 0)
        log.warning("[affinity] worker %d irraggiungibile (%s): servo in locale%s", owner, exc,
                    f" (+{skipped} richieste nei {self.UNREACHABLE_LOG_EVERY_SEC:.0f}s precedenti)"
                    if skipped else "")

    async def _proxy(self, owner: int, scope, body: bytes, receive, send) -> bool:
        """True = risposta del proprietario consegnata (anche se interrotta);
        False = proprietario irraggiungibile, nulla inviato: si serve qui."""
        client = self.peers.get(owner)
        request = client.build_request(scope["method"], _target_url(scope),
                                       headers=_forward_headers(scope), content=body)
        try:
            response = await client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as exc:
            self._note_unreachable(owner, exc)
            return False
        try:
            await send({"type": "http.response.start", "status": response.status_code,
                        "headers": [(k.lower(), v) for k, v in response.headers.raw
                                    if k.lower() not in _RESPONSE_DROP]})
            pump = asyncio.create_task(self._pump(response, send))
            gone = asyncio.create_task(_wait_disconnect(receive))
            try:
                await asyncio.wait({pump, gone}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (pump, gone):
                    task.cancel()
                await asyncio.gather(pump, gone, return_exceptions=True)
            if pump.done() and not pump.cancelled() and pump.exception() is not None:
                log.warning("[affinity] risposta dal worker %d interrotta: %s", owner, pump.exception())
        finally:
            await response.aclose()        # client andato: chiude anche verso il proprietario
        return True

    @staticmethod
    async def _pump(response: httpx.Response, send) -> None:
        async for chunk in response.aiter_raw():
            if chunk:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class InternalEntry:
    """Ingresso sul socket unix privato di un worker: richieste inoltrate da
    un altro worker (o scrape /metrics interni)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            scope = dict(scope)
            headers = []
            for k, v in scope.get("headers") or ():
                if not k.startswith(_PRIVATE):
                    headers.append((k, v))
                    continue
                try:
                    if k == _H_CLIENT:
                        host, port = json.loads(v)
                        scope["client"] = (str(host), int(port))
                    elif k == _H_SERVER:
                        host, port = json.loads(v)
                        scope["server"] = (str(host), int(port))
                    elif k == _H_SCHEME and v in (b"http", b"https"):
                        scope["scheme"] = v.decode()
                except (ValueError, TypeError):
                    pass
            scope["headers"] = headers
            scope["scrocco.internal"] = True
        await self.app(scope, receive, send)


# ------------------------------------------------------------- /metrics --
async def cluster_metrics(local_text: str) -> str:
    """Metriche di tutti i worker, ciascuna serie con la label `worker`."""
    texts = {cluster.index(): local_text}

    async def fetch(i: int) -> None:
        try:
            r = await PEERS.get(i).get("http://worker/metrics", timeout=3.0,
                                       headers={"x-scrocco-client": b'["127.0.0.1", 0]'})
            if r.status_code == 200:
                texts[i] = r.text
        except httpx.HTTPError:
            pass                      # worker in riavvio: le sue serie mancano

    await asyncio.gather(*(fetch(i) for i in range(cluster.size()) if i != cluster.index()))
    return merge_prometheus(texts)


def _family_of(name: str, families: dict) -> str:
    if name in families:
        return name
    for suffix in ("_bucket", "_sum", "_count"):
        if name.endswith(suffix) and name[: -len(suffix)] in families:
            return name[: -len(suffix)]
    return name


def _with_worker(line: str, worker: int) -> tuple[str, str]:
    cut = min((i for i in (line.find("{"), line.find(" ")) if i >= 0), default=-1)
    if cut < 0:
        return line, line
    name = line[:cut]
    label = f'worker="{worker}"'
    if line[cut] == "{":
        rest = line[cut + 1:]
        sep = "" if rest.startswith("}") else ","
        return name, f"{name}{{{label}{sep}{rest}"
    return name, f"{name}{{{label}}}{line[cut:]}"


def merge_prometheus(texts: dict[int, str]) -> str:
    """Unisce esposizioni Prometheus di piu' worker: stesse famiglie
    raggruppate (HELP/TYPE una volta), ogni campione con `worker="i"`."""
    families: dict[str, dict] = {}
    for worker, text in sorted(texts.items()):
        for line in text.splitlines():
            if not line.strip():
                continue
            if line.startswith("#"):
                parts = line.split(None, 3)
                if len(parts) >= 3 and parts[1] in ("HELP", "TYPE"):
                    fam = families.setdefault(parts[2], {"meta": {}, "samples": []})
                    fam["meta"].setdefault(parts[1], line)
                continue
            name, sample = _with_worker(line, worker)
            families.setdefault(_family_of(name, families), {"meta": {}, "samples": []})["samples"].append(sample)
    out: list[str] = []
    for fam in families.values():
        for key in ("HELP", "TYPE"):
            if key in fam["meta"]:
                out.append(fam["meta"][key])
        out.extend(fam["samples"])
    return "\n".join(out) + "\n"
