"""Debug SNIFF: registra INPUT (payload richiesta) e OUTPUT (SSE completa o
JSON) di ogni chiamata chat su file con ROTAZIONE ORARIA e retention
configurabile (default 24h).

Perche': il routing/tool-parsing va debugghato "a naso" senza vedere cio' che
il modello ha realmente restituito. Questo file e' la scatola nera.

Accanto al file COMPLETO (`debug-sniff.log`) c'e' un INDICE LEGGERO
(`debug-sniff-index.log`): una riga sintetica per richiesta (stato, latenza,
chunk, tool-call, mosse di riparazione) piu' un ROLLUP ORARIO aggregato. Serve
a scorrere rapidamente cosa e' andato storto senza leggere payload interi.

Attivazione (in ordine di precedenza):
  - env  GATEWAY_DEBUG_SNIFF=1        (override esplicito)
  - policy gateway.yaml  debug.sniff.enabled: true

I file contengono la conversazione COMPLETA (nessuna redazione): sono file
LOCALI gitignored (var/debug-sniff*.log*). Default OFF. Il modulo OSSERVA e
non modifica MAI i byte verso il client.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from logging.handlers import TimedRotatingFileHandler

_logger: logging.Logger | None = None
_index_logger: logging.Logger | None = None
_lock = threading.Lock()

# --- Memory Bloat Guard -----------------------------------------------------
# I payload multimodali possono contenere immagini/PDF in base64 da molti MB:
# scriverli interi riempie il disco e fa esplodere la RAM durante la
# serializzazione JSON. Prima di loggare, ogni stringa "binaria" viene
# sostituita da un placeholder sintetico, preservando il testo del prompt.
_MAX_B64_CHARS = 2048          # oltre questa soglia una stringa base64 e' sospetta
_MAX_STR_CHARS = 20000         # cap di sicurezza per stringhe di testo enormi
_MAX_SSE_BYTES = 1_500_000     # cap sul totale dei byte SSE accumulati
_B64_RE = re.compile(r"^[A-Za-z0-9+/\r\n=]+$")
_DATA_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?;base64,", re.I)

# Stato del rollup orario (protetto da _lock).
_rollup: dict = {}
_rollup_hour: int | None = None
_rollup_start: float | None = None

_ROLLUP_ZERO = ("calls", "ok", "fail", "streams", "json", "tool_calls",
                "repairs", "escalations", "sum_ms", "sum_answer_chars")


def _default_index_path(path: str) -> str:
    d = os.path.dirname(path)
    return os.path.join(d, "debug-sniff-index.log") if d \
        else "debug-sniff-index.log"


def _install(lg: logging.Logger, path: str, retention_hours: int) -> bool:
    lg.setLevel(logging.INFO)
    lg.propagate = False                 # NON inquina gateway.log/stdout
    if lg.handlers:
        return True
    try:
        h = TimedRotatingFileHandler(
            path, when="H", interval=1,
            backupCount=max(1, int(retention_hours)), encoding="utf-8",
            delay=True)
        h.setFormatter(logging.Formatter("%(message)s"))
        lg.addHandler(h)
        return True
    except OSError as exc:                       # noqa: BLE001
        logging.getLogger(__name__).warning(
            "[sniff] file %s non scrivibile (%s): debug disattivo", path, exc)
        return False


def configure(path: str, retention_hours: int = 24,
              index_path: str | None = None) -> None:
    """Installa gli handler (completo + indice) con rotazione oraria. Idempotente."""
    global _logger, _index_logger, _rollup_hour, _rollup_start
    if _install(logging.getLogger("nx.sniff"), path, retention_hours):
        _logger = logging.getLogger("nx.sniff")
    if _install(logging.getLogger("nx.sniff.index"),
                index_path or _default_index_path(path), retention_hours):
        _index_logger = logging.getLogger("nx.sniff.index")
    with _lock:
        if not _rollup:
            _reset_rollup_locked()
        _rollup_hour = int(time.time() // 3600)
        _rollup_start = _rollup_hour * 3600


def enabled(policy=None) -> bool:
    env = os.environ.get("GATEWAY_DEBUG_SNIFF")
    if env is not None:
        return env.strip().lower() not in ("0", "", "false", "no", "off")
    return bool(getattr(policy, "debug_sniff_enabled", False))


def _emit_locked(lg: logging.Logger | None, rec: dict) -> None:
    if lg is None:
        return
    try:
        lg.info(json.dumps(rec, ensure_ascii=False, default=str))
    except Exception:                              # noqa: BLE001
        pass


def _write(rec: dict) -> None:
    if _logger is None:
        return
    with _lock:
        _emit_locked(_logger, rec)


def _reset_rollup_locked() -> None:
    global _rollup
    _rollup = {k: 0 for k in _ROLLUP_ZERO}
    _rollup["statuses"] = {}


def _emit_rollup_locked(now: float) -> None:
    """Scrive il rollup dell'ora appena chiusa e azzera i contatori."""
    if _index_logger is None or _rollup.get("calls", 0) <= 0:
        _reset_rollup_locked()
        return
    calls = _rollup["calls"]
    rec = {k: _rollup[k] for k in _ROLLUP_ZERO}
    rec.update({
        "dir": "rollup",
        "window_start": _rollup_start,
        "window_end": now,
        "statuses": dict(_rollup.get("statuses") or {}),
        "avg_ms": round(_rollup["sum_ms"] / calls, 1) if calls else 0,
        "avg_answer_chars": round(_rollup["sum_answer_chars"] / calls, 1)
        if calls else 0,
    })
    _emit_locked(_index_logger, rec)
    _reset_rollup_locked()


def _record_index(rid: str, meta: dict, extra: dict | None, t0: float,
                  is_stream: bool) -> None:
    """Riga d'indice sintetica + aggiornamento rollup orario."""
    global _rollup_hour, _rollup_start
    if _index_logger is None:
        return
    now = time.time()
    extra = extra or {}
    status = extra.get("status")
    rec = {
        "dir": "idx", "rid": rid, "ts": now,
        "ms": int(max(0.0, now - t0) * 1000),
        "stream": bool(is_stream),
        "status": status,
        "chunks": extra.get("chunks"),
        "answer_chars": extra.get("answer_chars"),
        "tool_calls": bool(extra.get("had_tool_calls")),
        "tries": extra.get("tries"),
        "repairs": extra.get("repairs"),
        "escalations": extra.get("escalations"),
        "dep": extra.get("dep_final"),
    }
    with _lock:
        hour = int(now // 3600)
        if _rollup_hour is None:
            _rollup_hour, _rollup_start = hour, hour * 3600
        elif hour != _rollup_hour:
            _emit_rollup_locked(now)
            _rollup_hour, _rollup_start = hour, hour * 3600
        _rollup["calls"] += 1
        ok = (isinstance(status, int) and 200 <= status < 300) \
            or status == "success"
        if ok:
            _rollup["ok"] += 1
        else:
            _rollup["fail"] += 1
        _rollup["streams" if is_stream else "json"] += 1
        if rec["tool_calls"]:
            _rollup["tool_calls"] += 1
        _rollup["repairs"] += int(rec.get("repairs") or 0)
        _rollup["escalations"] += int(rec.get("escalations") or 0)
        _rollup["sum_ms"] += int(rec.get("ms") or 0)
        _rollup["sum_answer_chars"] += int(rec.get("answer_chars") or 0)
        if status is not None:
            key = str(status)
            _rollup["statuses"][key] = \
                _rollup["statuses"].get(key, 0) + 1
        _emit_locked(_index_logger, rec)


def _human(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{int(v)}B" if unit == "B" else f"{v:.1f}{unit}"
        v /= 1024.0
    return f"{v:.1f}GB"


def _shrink_str(s: str) -> str:
    """Sostituisce base64/data-URI enormi con un placeholder leggibile."""
    if not isinstance(s, str) or len(s) <= _MAX_B64_CHARS:
        return s
    m = _DATA_RE.match(s.lstrip()[:64])
    body = s.strip()
    if m or (len(body) > _MAX_B64_CHARS and _B64_RE.match(body)):
        media = (m.group(1) or "") if m else ""
        label = "IMAGE_BASE64" if media.lower().startswith("image/") \
            else "BASE64"
        return f"[{label}_TRUNCATED_BY_SNIFFER: {_human(len(s))}]"
    if len(s) > _MAX_STR_CHARS:
        return s[:_MAX_STR_CHARS] + \
            f"...[STRING_TRUNCATED_BY_SNIFFER: {_human(len(s))} total]"
    return s


def _shrink(obj, _depth: int = 0):
    """Copia il payload sostituendo i dati binari/base64 con placeholder."""
    if _depth > 12:
        return obj
    if isinstance(obj, dict):
        return {k: _shrink(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        if len(obj) > _MAX_B64_CHARS and all(
                isinstance(x, int) and 0 <= x <= 255 for x in obj[:64]):
            return [f"[BYTE_ARRAY_TRUNCATED_BY_SNIFFER: {len(obj)} elements]"]
        return [_shrink(v, _depth + 1) for v in obj]
    if isinstance(obj, str):
        return _shrink_str(obj)
    return obj


class Sniffer:
    """Cattura di UNA richiesta/risposta (streaming o JSON)."""

    def __init__(self, rid: str, meta: dict):
        self.rid = rid
        self.meta = meta
        self._chunks: list[bytes] = []
        self._t0 = time.time()
        self._stored = 0
        self._dropped = 0

    def feed(self, chunk: bytes) -> None:
        """Accoda un chunk SSE inviato al client (osservazione pura).

        Oltre il cap di memoria i byte non vengono piu' accumulati (solo
        contati): uno stream con immagini base64 non puo' far esplodere la RAM.
        """
        try:
            b = chunk if isinstance(chunk, bytes) else bytes(chunk)
        except Exception:                          # noqa: BLE001
            return
        if self._stored >= _MAX_SSE_BYTES:
            self._dropped += len(b)
            return
        self._chunks.append(b)
        self._stored += len(b)

    def finish_stream(self, extra: dict | None = None) -> None:
        raw = b"".join(self._chunks)
        sse = raw.decode("utf-8", "replace")
        if self._dropped:
            sse += ("\n[SSE_TRUNCATED_BY_SNIFFER: "
                    f"{_human(self._dropped)} scartati]")
        rec = {
            "dir": "out", "rid": self.rid, "ts": time.time(),
            "stream": True, "meta": self.meta,
            "sse_bytes": len(raw) + self._dropped,
            "sse": sse,
        }
        rec.update(extra or {})
        _write(rec)
        _record_index(self.rid, self.meta, extra, self._t0, True)

    def finish_json(self, data, extra: dict | None = None) -> None:
        rec = {
            "dir": "out", "rid": self.rid, "ts": time.time(),
            "stream": False, "meta": self.meta,
            "response": _shrink(data),
        }
        rec.update(extra or {})
        _write(rec)
        _record_index(self.rid, self.meta, extra, self._t0, False)


def begin(rid: str, meta: dict, payload) -> Sniffer:
    """Registra l'INPUT e ritorna lo Sniffer per la risposta."""
    _write({"dir": "in", "rid": rid, "ts": time.time(),
            "meta": meta, "payload": _shrink(payload)})
    return Sniffer(rid, meta)
