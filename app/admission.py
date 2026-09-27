"""Controllo di ammissione del PROCESSO (tetto globale di richieste LLM in volo).

[IT] PERCHE': il gateway e' UN processo con UN event loop. I limiti esistenti
(concorrenza per deployment, lease per chiave, warm_refill_max_inflight)
proteggono le chiavi dei provider, non il processo: N client concorrenti
diventavano N task sullo stesso loop, senza coda. Qui c'e' la porta:

- conta le richieste LLM (POST /v1/...) dall'ingresso alla FINE della
  risposta, stream compreso: uno stream lungo occupa il suo posto per tutta
  la durata;
- gli stream hanno un tetto DEDICATO (`admission_max_streams`), perche' 8
  stream lunghi pesano sul loop molto piu' di 8 richieste brevi;
- oltre il tetto NON si rifiuta: si ASPETTA in coda (FIFO di fatto) fino a
  `admission_queue_timeout_sec`; solo allora un 503 retryable con
  Retry-After (stesso formato degli altri 503 del gateway).

Con i default (ampi) il normale traffico non tocca mai la coda: il
comportamento verso il client resta quello di prima; sotto sovraccarico il
processo resta reattivo invece di saturarsi. 0 = nessun tetto.

[EN] Process-level admission gate for LLM requests, with a dedicated cap for
streams; excess requests wait in a queue, 503 + Retry-After only on timeout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import dataclass

from . import metrics

log = logging.getLogger("nx.admission")

_STREAM_TRUE = re.compile(rb'"stream"\s*:\s*true')

metrics.declare("nx_admission_total", "outcome", "kind")


@dataclass(frozen=True)
class AdmissionLimits:
    max_inflight: int = 128
    max_streams: int = 48
    queue_timeout_sec: float = 60.0


def limits_from_policy(policy) -> AdmissionLimits:
    d = AdmissionLimits()
    try:
        return AdmissionLimits(
            max_inflight=max(0, int(getattr(policy, "admission_max_inflight", d.max_inflight))),
            max_streams=max(0, int(getattr(policy, "admission_max_streams", d.max_streams))),
            queue_timeout_sec=max(0.0, float(getattr(policy, "admission_queue_timeout_sec", d.queue_timeout_sec))),
        )
    except (TypeError, ValueError):
        return d


class AdmissionGate:
    """Contatori globali + attesa. Thread/loop-safe: i contatori sono sotto un
    lock e i waiter sono future risvegliate sul loop che le ha create."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.inflight = 0
        self.streams = 0
        self.waiting = 0
        self._waiters: list[asyncio.Future] = []

    def _fits(self, is_stream: bool, lim: AdmissionLimits) -> bool:
        if lim.max_inflight and self.inflight >= lim.max_inflight:
            return False
        if is_stream and lim.max_streams and self.streams >= lim.max_streams:
            return False
        return True

    def _take(self, is_stream: bool) -> None:
        self.inflight += 1
        if is_stream:
            self.streams += 1

    async def acquire(self, is_stream: bool, lim: AdmissionLimits) -> bool:
        """True = ammesso (va SEMPRE seguito da release); False = attesa scaduta."""
        with self._lock:
            if self._fits(is_stream, lim) and not self._waiters:
                self._take(is_stream)
                return True
            self.waiting += 1
        deadline = time.monotonic() + lim.queue_timeout_sec
        loop = asyncio.get_running_loop()
        try:
            while True:
                fut = loop.create_future()
                with self._lock:
                    if self._fits(is_stream, lim):
                        self._take(is_stream)
                        return True
                    self._waiters.append(fut)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(fut, remaining)
                except asyncio.TimeoutError:
                    return False
                finally:
                    with self._lock:
                        if fut in self._waiters:
                            self._waiters.remove(fut)
        finally:
            with self._lock:
                self.waiting -= 1

    def release(self, is_stream: bool) -> None:
        with self._lock:
            self.inflight -= 1
            if is_stream:
                self.streams -= 1
            waiters, self._waiters = self._waiters, []
        # si svegliano tutti: ognuno ricontrolla il proprio tetto (gli stream
        # possono restare in coda mentre passa una richiesta breve)
        for fut in waiters:
            fut_loop = fut.get_loop()
            if not fut_loop.is_closed():
                fut_loop.call_soon_threadsafe(_resolve, fut)

    def publish(self) -> None:
        """Gauge Prometheus dello stato della porta."""
        snap = self.snapshot()
        metrics.set_gauge("nx_admission_inflight", snap["inflight"])
        metrics.set_gauge("nx_admission_streams", snap["streams"])
        metrics.set_gauge("nx_admission_waiting", snap["waiting"])

    def snapshot(self) -> dict:
        with self._lock:
            return {"inflight": self.inflight, "streams": self.streams, "waiting": self.waiting}


def _resolve(fut: asyncio.Future) -> None:
    if not fut.done():
        fut.set_result(None)


GATE = AdmissionGate()


def _is_llm_request(scope) -> bool:
    return scope.get("method") == "POST" and scope.get("path", "").startswith("/v1/")


def _looks_streaming(scope, body: bytes) -> bool:
    ctype = ""
    for k, v in scope.get("headers") or ():
        if k == b"content-type":
            ctype = v.decode("latin-1").lower()
            break
    return "json" in ctype and b'"stream"' in body and bool(_STREAM_TRUE.search(body))


_BUSY_BODY = json.dumps({"error": {
    "message": "gateway saturo: troppe richieste in corso, riprova tra poco",
    "type": "upstream_unavailable",
    "code": "gateway_busy",
}}).encode()


class AdmissionMiddleware:
    """Middleware ASGI: applica `GATE` alle richieste LLM. Health, metriche e
    admin non passano mai dalla porta (restano rispondenti sotto carico)."""

    def __init__(self, app, policy_getter):
        self.app = app
        self._policy = policy_getter

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not _is_llm_request(scope):
            await self.app(scope, receive, send)
            return
        lim = limits_from_policy(self._policy())
        if not (lim.max_inflight or lim.max_streams):
            await self.app(scope, receive, send)
            return
        # Il body serve per riconoscere gli stream: lo si legge qui e lo si
        # riconsegna identico all'app (che l'avrebbe letto comunque).
        chunks: list[bytes] = []
        more = True
        disconnected = None
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = message
                break
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)
        is_stream = _looks_streaming(scope, body)
        kind = "stream" if is_stream else "request"

        if not await GATE.acquire(is_stream, lim):
            metrics.inc("nx_admission_total", ("rejected", kind))
            log.warning("[admission] %s rifiutata dopo %.0fs in coda (%s)",
                        kind, lim.queue_timeout_sec, GATE.snapshot())
            await send({"type": "http.response.start", "status": 503, "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", b"2"),
                (b"content-length", str(len(_BUSY_BODY)).encode()),
            ]})
            await send({"type": "http.response.body", "body": _BUSY_BODY})
            return
        metrics.inc("nx_admission_total", ("admitted", kind))
        GATE.publish()

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                if disconnected is not None:
                    return disconnected
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        try:
            await self.app(scope, replay_receive, send)
        finally:
            GATE.release(is_stream)
            GATE.publish()
