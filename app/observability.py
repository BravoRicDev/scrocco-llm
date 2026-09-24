"""Observability: trace ID middleware, JSON logging, Prometheus metrics.

[IT] Modulo centralizzato per l'osservabilità:
- Trace ID middleware: aggiunge X-Request-ID a ogni richiesta (o genera uno)
- JSON logging: formatter strutturato per log aggregabili (Loki, ELK, etc.)
- Prometheus /metrics: endpoint /metrics per scraping (latency, counters, gauges)

[EN] Centralized observability module:
- Trace ID middleware: adds X-Request-ID to every request (or generates one)
- JSON logging: structured formatter for aggregatable logs (Loki, ELK, etc.)
- Prometheus /metrics: /metrics endpoint for scraping (latency, counters, gauges)
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


log = logging.getLogger("nx.observability")

import contextvars

# ContextVar per trace ID per-request
_trace_id_ctx = contextvars.ContextVar("trace_id", default="")


def get_trace_id() -> str:
    """Ritorna il trace ID corrente (vuoto se non in contesto richiesta)."""
    return _trace_id_ctx.get("")


def set_trace_id(trace_id: str):
    """Imposta il trace ID nel contesto corrente. Ritorna token per reset."""
    return _trace_id_ctx.set(trace_id)


class JSONFormatter(logging.Formatter):
    """Formatter JSON per log strutturati.

    Output: {"timestamp": "...", "level": "...", "logger": "...", "message": "...", "trace_id": "...", ...}
    """

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": get_trace_id(),
        }
        # Aggiungi campi extra dal record
        for key, value in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs", "message",
                "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "thread", "threadName", "exc_info",
                "exc_text", "stack_info"
            ):
                data[key] = value
        return json.dumps(data, ensure_ascii=False)


class TraceIDMiddleware(BaseHTTPMiddleware):
    """Middleware che aggiunge/genera trace ID e logga inizio/fine richiesta."""

    def __init__(
        self,
        app,
        header_name: str = "X-Request-ID",
        generate_if_missing: bool = True,
        log_requests: bool = True,
    ):
        super().__init__(app)
        self.header_name = header_name
        self.generate_if_missing = generate_if_missing
        self.log_requests = log_requests

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        trace_id = request.headers.get(self.header_name)
        if not trace_id and self.generate_if_missing:
            trace_id = uuid.uuid4().hex[:16]
        elif not trace_id:
            trace_id = ""

        token = set_trace_id(trace_id)

        start = time.time()
        method = request.method
        path = request.url.path

        if self.log_requests and path not in ("/healthz", "/metrics"):
            log.debug(
                "[http] -> %s %s da %s",
                method, path,
                request.client.host if request.client else "-",
                extra={
                    "trace_id": trace_id,
                    "method": method,
                    "path": path,
                    "client": request.client.host if request.client else None,
                },
            )

        try:
            response = await call_next(request)
            duration_ms = (time.time() - start) * 1000.0

            response.headers[self.header_name] = trace_id

            if self.log_requests and path not in ("/healthz", "/metrics"):
                _mark = "OK" if response.status_code < 400 else "KO"
                log.info(
                    "[http] %s %s %s -> %d in %.0fms",
                    _mark, method, path, response.status_code, duration_ms,
                    extra={
                        "trace_id": trace_id,
                        "method": method,
                        "path": path,
                        "status": response.status_code,
                        "duration_ms": round(duration_ms, 2),
                    },
                )

            # Record Prometheus metrics
            record_request_metrics(method, path, response.status_code, duration_ms, trace_id)

            return response
        except Exception as e:
            duration_ms = (time.time() - start) * 1000.0
            if self.log_requests:
                log.exception(
                    "[http] ERRORE %s %s dopo %.0fms: %s",
                    method, path, duration_ms, e,
                    extra={
                        "trace_id": trace_id,
                        "method": method,
                        "path": path,
                        "duration_ms": round(duration_ms, 2),
                        "error": str(e),
                    },
                )
            # Record error metrics
            record_request_metrics(method, path, 500, duration_ms, trace_id)
            raise
        finally:
            # Reset trace ID context
            _trace_id_ctx.reset(token)


# Prometheus metrics
class MetricsCollector:
    """Collettore metriche in-memory per Prometheus."""

    def __init__(self):
        self._counters: dict[str, float] = {}
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = {}
        self._labels: dict[str, dict[str, str]] = {}

    def inc_counter(self, name: str, value: float = 1.0, labels: dict[str, str] | None = None) -> None:
        key = self._make_key(name, labels)
        self._counters[key] = self._counters.get(key, 0.0) + value
        if labels:
            self._labels[key] = labels

    def set_gauge(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._make_key(name, labels)
        self._gauges[key] = value
        if labels:
            self._labels[key] = labels

    def observe_histogram(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        key = self._make_key(name, labels)
        if key not in self._histograms:
            self._histograms[key] = []
        self._histograms[key].append(value)
        if labels:
            self._labels[key] = labels

    def _make_key(self, name: str, labels: dict[str, str] | None) -> str:
        if not labels:
            return name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def generate_prometheus(self) -> str:
        """Genera output in formato Prometheus text exposition."""
        lines = []
        typed: set[str] = set()

        # Counters: UNA sola riga # TYPE per nome metrica (emetterla dentro il
        # loop delle label produceva TYPE duplicati -> scrape rifiutato).
        for key, value in self._counters.items():
            base = key.split("{")[0]
            if base not in typed:
                lines.append(f"# TYPE {base} counter")
                typed.add(base)
            lines.append(f"{key} {value}")

        # Gauges
        for key, value in self._gauges.items():
            base = key.split("{")[0]
            if base not in typed:
                lines.append(f"# TYPE {base} gauge")
                typed.add(base)
            lines.append(f"{key} {value}")

        # Histograms. Le osservazioni sono in MILLISECONDI (nome "*_ms"): i
        # boundary DEVONO essere in ms (prima 0.05..5.0 -> un 300ms finiva
        # comunque in +Inf).
        for key, values in self._histograms.items():
            if not values:
                continue
            base_name = key.split("{")[0]
            labels = self._labels.get(key, {})
            label_str = ",".join(
                f'{k}="{v}"' for k, v in sorted(labels.items())) if labels else ""
            prefix = f"{base_name}{{{label_str}}}" if label_str else base_name
            count = len(values)
            total = sum(values)
            if base_name not in typed:
                lines.append(f"# TYPE {base_name} histogram")
                typed.add(base_name)
            lines.append(f"{prefix}_count {count}")
            lines.append(f"{prefix}_sum {total}")
            for bound in _HIST_BUCKETS_MS:
                lines.append(
                    f'{prefix}_bucket{{le="{bound}"}} '
                    f"{sum(1 for v in values if v <= bound)}")
            lines.append(f'{prefix}_bucket{{le="+Inf"}} {count}')

        return "\n".join(lines) + "\n"


# Istanza globale
metrics_collector = MetricsCollector()

# Bucket degli istogrammi in MILLISECONDI (le metriche *_ms ricevono ms).
_HIST_BUCKETS_MS: tuple[int, ...] = (
    5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000,
)

# La route /metrics e' registrata UNA sola volta in app/main.py. Questo flag
# abilita l'inclusione del testo delle metriche HTTP nel corpo unico.
_PROM_ENABLED = False


def render_prometheus() -> str:
    """Testo Prometheus delle metriche HTTP (vuoto se disabilitate)."""
    if not _PROM_ENABLED:
        return ""
    return metrics_collector.generate_prometheus()


def record_request_metrics(
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    trace_id: str | None = None,
) -> None:
    """Registra metriche per una richiesta HTTP."""
    labels = {"method": method, "path": path, "status": str(status)}
    metrics_collector.inc_counter("http_requests_total", 1.0, labels)
    metrics_collector.observe_histogram("http_request_duration_ms", duration_ms, labels)


def setup_observability(
    app: FastAPI,
    enable_json_logging: bool = True,
    enable_trace_id: bool = True,
    enable_prometheus: bool = True,
    trace_header: str = "X-Request-ID",
) -> None:
    """Configura l'osservabilità per l'app FastAPI.

    Args:
        app: Istanza FastAPI
        enable_json_logging: Abilita formatter JSON su stdout
        enable_trace_id: Abilita middleware trace ID
        enable_prometheus: Abilita endpoint /metrics
        trace_header: Header name per trace ID
    """
    # JSON logging
    if enable_json_logging:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        root = logging.getLogger()
        # Rimuovi handler esistenti per evitare duplicati
        for h in list(root.handlers):
            if isinstance(h, logging.StreamHandler):
                root.removeHandler(h)
        root.addHandler(handler)

    # Trace ID middleware
    if enable_trace_id:
        app.add_middleware(TraceIDMiddleware, header_name=trace_header)

    # La route /metrics e' registrata UNA sola volta in app/main.py. Qui ci
    # limitiamo ad abilitare il testo delle metriche HTTP: registrarla anche
    # qui la rendeva l'handler VINCENTE (Starlette usa la PRIMA route) e i
    # ~50 nx_* di app.metrics non erano mai esposti.
    global _PROM_ENABLED
    _PROM_ENABLED = enable_prometheus

    log.info("observability configured", extra={
        "json_logging": enable_json_logging,
        "trace_id": enable_trace_id,
        "prometheus": enable_prometheus,
    })


@dataclass
class ReplayEntry:
    """Singola entry per replay."""
    trace_id: str
    timestamp: float
    request: dict[str, Any]
    response: dict[str, Any] | None
    error: str | None
    duration_ms: float


class ReplayBuffer:
    """Buffer in-memory per replay delle richieste (debugging)."""

    def __init__(self, max_entries: int = 1000):
        self.max_entries = max_entries
        self._entries: list[ReplayEntry] = []

    def add(self, entry: ReplayEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) > self.max_entries:
            self._entries.pop(0)

    def get_recent(self, n: int = 100) -> list[ReplayEntry]:
        return self._entries[-n:]

    def get_by_trace_id(self, trace_id: str) -> list[ReplayEntry]:
        return [e for e in self._entries if e.trace_id == trace_id]

    def clear(self) -> None:
        self._entries.clear()


# Buffer replay globale
replay_buffer = ReplayBuffer()


def add_replay_entry(
    trace_id: str,
    request: dict[str, Any],
    response: dict[str, Any] | None = None,
    error: str | None = None,
    duration_ms: float = 0.0,
) -> None:
    """Aggiunge entry al replay buffer."""
    entry = ReplayEntry(
        trace_id=trace_id,
        timestamp=time.time(),
        request=request,
        response=response,
        error=error,
        duration_ms=duration_ms,
    )
    replay_buffer.add(entry)


def _require_master(request: Request) -> JSONResponse | None:
    """Guardia master condivisa (lazy import: evita cicli admin->main)."""
    from .admin import _require_master as _check
    return _check(request)


def setup_replay_endpoint(app: FastAPI) -> None:
    """Aggiunge endpoint /admin/replay per debugging (master-only).

    COMPAT: /admin/replay e' include_in_schema=False, non documentato, senza
    consumer e senza scrittori (add_replay_entry non ha chiamanti): renderlo
    master-only non rompe client. Il DELETE anonimo azzerava il buffer.
    """

    @app.get("/admin/replay", include_in_schema=False)
    async def replay_endpoint(request: Request, limit: int = 100,
                              trace_id: str | None = None):
        denied = _require_master(request)
        if denied:
            return denied
        if trace_id:
            entries = replay_buffer.get_by_trace_id(trace_id)
        else:
            entries = replay_buffer.get_recent(limit)
        return {
            "count": len(entries),
            "entries": [
                {
                    "trace_id": e.trace_id,
                    "timestamp": e.timestamp,
                    "request": e.request,
                    "response": e.response,
                    "error": e.error,
                    "duration_ms": e.duration_ms,
                }
                for e in entries
            ],
        }

    @app.delete("/admin/replay", include_in_schema=False)
    async def replay_clear(request: Request):
        denied = _require_master(request)
        if denied:
            return denied
        replay_buffer.clear()
        return {"ok": True, "message": "Replay buffer cleared"}