"""Store in-memory delle immagini generate/editate (URL di download).

Gli endpoint /v1/images/* restituiscono, accanto a `b64_json`, un `url`
NOSTRO che punta a `GET /v1/images/files/{id}`: cosi' il client puo' scaricare
l'immagine al volo anche quando l'upstream risponde solo in base64. Gli URL
temporanei dei provider vengono scaricati e ri-ospitati (``mirror``).

E' lo stesso pattern in-memory dei job video (``app/main.py``): dict con TTL e
cap. Al restart del processo gli id gia' consegnati decadono (404) — accettabile
perche' l'URL serve al download immediato. Nessuna persistenza su disco.
"""
from __future__ import annotations

import secrets
import threading
import time

_LOCK = threading.Lock()
# id -> {"data": bytes, "mime": str, "ts": float}. L'ordine di inserimento del
# dict (py>=3.7) e' anche l'ordine di eviction (oldest-first).
_ITEMS: dict[str, dict] = {}
_TOTAL_BYTES = 0

_TTL_SEC = 86400
_MAX_ITEMS = 500
_MAX_BYTES = 536870912

_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
}


def configure(*, ttl_sec=None, max_items=None, max_bytes=None) -> None:
    """Aggiorna i limiti dello store (policy `images.*`). None = invariato."""
    global _TTL_SEC, _MAX_ITEMS, _MAX_BYTES
    if ttl_sec is not None:
        try:
            _TTL_SEC = max(0, int(ttl_sec))
        except (TypeError, ValueError):
            pass
    if max_items is not None:
        try:
            _MAX_ITEMS = max(1, int(max_items))
        except (TypeError, ValueError):
            pass
    if max_bytes is not None:
        try:
            _MAX_BYTES = max(0, int(max_bytes))
        except (TypeError, ValueError):
            pass


def ttl_sec() -> int:
    return _TTL_SEC


def normalize_mime(mime: str | None) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    return m or "application/octet-stream"


def ext_for_mime(mime: str | None) -> str:
    return _EXT_BY_MIME.get(normalize_mime(mime), "bin")


def _looks_image(mime: str | None, data: bytes) -> bool:
    m = normalize_mime(mime)
    if m.startswith("image/"):
        return True
    if m in ("application/octet-stream", ""):
        head = bytes(data[:4])
        return (head[:3] == b"\xff\xd8\xff"           # JPEG
                or head == b"\x89PNG"                  # PNG
                or head[:4] == b"RIFF"                 # WEBP (RIFF....)
                or head[:3] == b"GIF")
    return False


def _evict_locked() -> None:
    global _TOTAL_BYTES
    while _ITEMS and (len(_ITEMS) > _MAX_ITEMS
                      or (_MAX_BYTES and _TOTAL_BYTES > _MAX_BYTES)):
        oldest = next(iter(_ITEMS))
        _TOTAL_BYTES -= len(_ITEMS[oldest]["data"])
        _ITEMS.pop(oldest, None)


def put(data: bytes, mime: str | None) -> str | None:
    """Salva i byte e ritorna l'id (None se non e' un'immagine o e' vuota)."""
    global _TOTAL_BYTES
    if not data:
        return None
    mime = normalize_mime(mime)
    if not _looks_image(mime, data):
        return None
    file_id = secrets.token_urlsafe(24)
    with _LOCK:
        _ITEMS[file_id] = {"data": bytes(data), "mime": mime, "ts": time.time()}
        _TOTAL_BYTES += len(data)
        _evict_locked()
    return file_id


def get(file_id: str) -> tuple[bytes, str] | None:
    """(bytes, mime) per id, o None se ignoto/scaduto."""
    global _TOTAL_BYTES
    with _LOCK:
        entry = _ITEMS.get(file_id)
        if entry is None:
            return None
        if _TTL_SEC and (time.time() - entry["ts"]) > _TTL_SEC:
            _TOTAL_BYTES -= len(entry["data"])
            _ITEMS.pop(file_id, None)
            return None
        return entry["data"], entry["mime"]


def sweep() -> int:
    """Rimuove le entry scadute; ritorna quante ne ha eliminate."""
    global _TOTAL_BYTES
    if not _TTL_SEC:
        return 0
    now = time.time()
    removed = 0
    with _LOCK:
        for file_id in [k for k, v in _ITEMS.items()
                        if (now - v["ts"]) > _TTL_SEC]:
            _TOTAL_BYTES -= len(_ITEMS[file_id]["data"])
            _ITEMS.pop(file_id, None)
            removed += 1
    return removed


def clear() -> None:
    """Svuota lo store (usato dai test)."""
    global _TOTAL_BYTES
    with _LOCK:
        _ITEMS.clear()
        _TOTAL_BYTES = 0


def stats() -> dict:
    with _LOCK:
        return {"items": len(_ITEMS), "bytes": _TOTAL_BYTES,
                "ttl_sec": _TTL_SEC, "max_items": _MAX_ITEMS,
                "max_bytes": _MAX_BYTES}
