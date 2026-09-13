"""Debug SNIFF: registra INPUT (payload richiesta) e OUTPUT (SSE completa o
JSON) di ogni chiamata chat su file con ROTAZIONE ORARIA e retention
configurabile (default 24h).

Perche': il routing/tool-parsing va debugghato "a naso" senza vedere cio' che
il modello ha realmente restituito. Questo file e' la scatola nera.

Attivazione (in ordine di precedenza):
  - env  GATEWAY_DEBUG_SNIFF=1        (override esplicito)
  - policy gateway.yaml  debug.sniff.enabled: true

I file contengono la conversazione COMPLETA (nessuna redazione): sono file
LOCALI gitignored (var/debug-sniff.log*). Default OFF. Il modulo OSSERVA e
non modifica MAI i byte verso il client.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from logging.handlers import TimedRotatingFileHandler

_logger: logging.Logger | None = None
_lock = threading.Lock()


def configure(path: str, retention_hours: int = 24) -> None:
    """Installa l'handler con rotazione oraria. Idempotente."""
    global _logger
    lg = logging.getLogger("nx.sniff")
    lg.setLevel(logging.INFO)
    lg.propagate = False                 # NON inquina gateway.log/stdout
    if lg.handlers:
        _logger = lg
        return
    try:
        h = TimedRotatingFileHandler(
            path, when="H", interval=1,
            backupCount=max(1, int(retention_hours)), encoding="utf-8",
            delay=True)
        h.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(h)
        _logger = lg
    except OSError as exc:                       # noqa: BLE001
        logging.getLogger(__name__).warning(
            "[sniff] file %s non scrivibile (%s): debug disattivo", path, exc)


def enabled(policy=None) -> bool:
    env = os.environ.get("GATEWAY_DEBUG_SNIFF")
    if env is not None:
        return env.strip().lower() not in ("0", "", "false", "no", "off")
    return bool(getattr(policy, "debug_sniff_enabled", False))


def _write(rec: dict) -> None:
    if _logger is None:
        return
    try:
        line = json.dumps(rec, ensure_ascii=False, default=str)
    except Exception:                              # noqa: BLE001
        return
    try:
        with _lock:
            _logger.info(line)
    except Exception:                              # noqa: BLE001
        pass


class Sniffer:
    """Cattura di UNA richiesta/risposta (streaming o JSON)."""

    def __init__(self, rid: str, meta: dict):
        self.rid = rid
        self.meta = meta
        self._chunks: list[bytes] = []

    def feed(self, chunk: bytes) -> None:
        """Accoda un chunk SSE inviato al client (osservazione pura)."""
        try:
            self._chunks.append(
                chunk if isinstance(chunk, bytes) else bytes(chunk))
        except Exception:                          # noqa: BLE001
            pass

    def finish_stream(self, extra: dict | None = None) -> None:
        raw = b"".join(self._chunks)
        rec = {
            "dir": "out", "rid": self.rid, "ts": time.time(),
            "stream": True, "meta": self.meta,
            "sse_bytes": len(raw),
            "sse": raw.decode("utf-8", "replace"),
        }
        rec.update(extra or {})
        _write(rec)

    def finish_json(self, data, extra: dict | None = None) -> None:
        rec = {
            "dir": "out", "rid": self.rid, "ts": time.time(),
            "stream": False, "meta": self.meta,
            "response": data,
        }
        rec.update(extra or {})
        _write(rec)


def begin(rid: str, meta: dict, payload) -> Sniffer:
    """Registra l'INPUT e ritorna lo Sniffer per la risposta."""
    _write({"dir": "in", "rid": rid, "ts": time.time(),
            "meta": meta, "payload": payload})
    return Sniffer(rid, meta)
