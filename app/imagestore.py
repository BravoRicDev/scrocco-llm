"""Store delle immagini generate/editate con cache in memoria e persistenza disco.

Gli endpoint /v1/images/* restituiscono, accanto a `b64_json`, un `url`
NOSTRO che punta a `GET /v1/images/files/{id}`: cosi' il client puo' scaricare
l'immagine al volo anche quando l'upstream risponde solo in base64. Gli URL
temporanei dei provider vengono scaricati e ri-ospitati (``mirror``).

Pattern con TTL e cap: cache veloce in memoria (_ITEMS) e persistenza su disco
se `storage_dir` e' configurato (Path(VAR_DIR) / "images"), cosi' gli ID gia'
consegnati ai client sopravvivono al riavvio del processo o a worker multipli.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from pathlib import Path

from . import cluster

_LOCK = threading.Lock()
# id -> {"data": bytes, "mime": str, "ts": float}. L'ordine di inserimento del
# dict (py>=3.7) e' anche l'ordine di eviction (oldest-first).
_ITEMS: dict[str, dict] = {}
_TOTAL_BYTES = 0
# Radice della persistenza su disco (None = solo memoria, comportamento
# storico). Se impostata ogni put() scrive <id>.bin + <id>.json, cosi' gli URL
# gia' consegnati ai client sopravvivono a riavvio/redeploy/worker multipli.
_STORAGE_DIR: Path | None = None

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


def configure(*, ttl_sec=None, max_items=None, max_bytes=None,
              storage_dir=None) -> None:
    """Aggiorna i limiti dello store (policy `images.*`). None = invariato.

    `storage_dir` abilita la persistenza su disco (None la disabilita: si
    torna al solo in-memory, che e' il default storico)."""
    global _TTL_SEC, _MAX_ITEMS, _MAX_BYTES, _STORAGE_DIR
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
    if storage_dir is not None:
        _STORAGE_DIR = Path(storage_dir) if storage_dir else None
        if _STORAGE_DIR is not None:
            # mai bloccare lo startup: se la cartella non e' scrivibile lo
            # store resta comunque funzionante in memoria.
            try:
                _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            except OSError:
                _STORAGE_DIR = None


def _disk_paths(file_id: str) -> tuple[Path, Path] | None:
    """(path .bin, path .json) dell'item, o None se la persistenza e' off."""
    if _STORAGE_DIR is None:
        return None
    return _STORAGE_DIR / f"{file_id}.bin", _STORAGE_DIR / f"{file_id}.json"


def _unlink_quiet(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _drop_disk(file_id: str) -> None:
    """Rimuove i file su disco dell'item (eviction/TTL/clear)."""
    paths = _disk_paths(file_id)
    if not paths:
        return
    for p in paths:
        _unlink_quiet(p)


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
        # il disco non deve accumulare oltre i limiti: senza questo gli item
        # evictati tornerebbero a vivere al primo get() successivo al restart.
        _drop_disk(oldest)


def put(data: bytes, mime: str | None) -> str | None:
    """Salva i byte e ritorna l'id (None se non e' un'immagine o e' vuota)."""
    global _TOTAL_BYTES
    if not data:
        return None
    mime = normalize_mime(mime)
    if not _looks_image(mime, data):
        return None
    if _MAX_BYTES and len(data) > _MAX_BYTES:
        # Item monolitico oltre il budget: _evict_locked() lo eliminerebbe
        # SUBITO (oldest-first), quindi restituire un id qui significa dare al
        # client un url /v1/images/files/{id} morto al millisecondo (404
        # silenzioso dopo un 200). Invariante: put() ritorna un id solo se
        # l'item e' DAVVERO in store. Stesso guard di audiostore.py.
        return None
    file_id = secrets.token_urlsafe(24)
    ts = time.time()
    with _LOCK:
        _ITEMS[file_id] = {"data": bytes(data), "mime": mime, "ts": ts}
        _TOTAL_BYTES += len(data)
        # disco PRIMA dell'eviction: se il file appena creato e' oltre budget
        # verrebbe subito evictato, ma il client ha gia' ricevuto l'id.
        _write_disk(file_id, data, mime, ts)
        _evict_locked()
    return file_id


def _write_disk(file_id: str, data: bytes, mime: str, ts: float) -> None:
    """Scrive <id>.bin + <id>.json. Mai sollevare: il fallback e' la memoria."""
    paths = _disk_paths(file_id)
    if not paths:
        return
    bin_p, json_p = paths
    try:
        # .bin e .json su file distinti: un lettore che vede il .json sa
        # subito che il .bin e' atteso, senza dover leggere metadati incompleti.
        tmp = bin_p.with_suffix(".bin.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, bin_p)
        json_p.write_text(json.dumps({"mime": mime, "ts": ts}),
                          encoding="utf-8")
    except OSError:
        _unlink_quiet(bin_p)


def _load_from_disk(file_id: str) -> tuple[bytes, str, float] | None:
    """Ricarica l'item dal disco (cache vuota: restart, worker diverso).

    Ritorna (bytes, mime, ts ORIGINALE). Il TTL e' rivalutato su quel `ts`,
    non sul mtime: ricaricare non deve prorogare la vita dell'immagine."""
    paths = _disk_paths(file_id)
    if not paths:
        return None
    bin_p, json_p = paths
    if not (json_p.is_file() and bin_p.is_file()):
        return None
    try:
        meta = json.loads(json_p.read_text(encoding="utf-8"))
        ts = float(meta["ts"])
        mime = str(meta["mime"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if _TTL_SEC and (time.time() - ts) > _TTL_SEC:
        _drop_disk(file_id)             # scaduta: libera subito lo spazio
        return None
    try:
        data = bin_p.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    return data, mime, ts


def get(file_id: str) -> tuple[bytes, str] | None:
    """(bytes, mime) per id, o None se ignoto/scaduto."""
    global _TOTAL_BYTES
    with _LOCK:
        entry = _ITEMS.get(file_id)
        if entry is None:
            # restart/worker diverso: la memoria e' vuota, il disco no.
            loaded = _load_from_disk(file_id)
            if loaded is None:
                return None
            data, mime, ts = loaded
            _ITEMS[file_id] = {"data": data, "mime": mime, "ts": ts}
            _TOTAL_BYTES += len(data)
            _evict_locked()              # niente crescita oltre i limiti
            return data, mime
        if _TTL_SEC and (time.time() - entry["ts"]) > _TTL_SEC:
            _TOTAL_BYTES -= len(entry["data"])
            _ITEMS.pop(file_id, None)
            _drop_disk(file_id)
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
            _drop_disk(file_id)
            removed += 1
    return removed + _sweep_disk(now)


def _sweep_disk(now: float) -> int:
    """Pulisce ANCHE i file scaduti mai caricati in memoria (restart incluso).

    Senza questo un .json/.bin abbandonato occuperebbe spazio per sempre:
    l'eviction e' per chiave in memoria, il disco no."""
    if _STORAGE_DIR is None or not _TTL_SEC:
        return 0
    if not cluster.is_leader():
        return 0      # multi-worker: la directory e' condivisa, la scansiona il leader
    removed = 0
    try:
        metas = list(_STORAGE_DIR.glob("*.json"))
    except OSError:
        return 0
    for json_p in metas:
        file_id = json_p.name[:-len(".json")]
        try:
            ts = float(json.loads(
                json_p.read_text(encoding="utf-8"))["ts"])
        except (OSError, ValueError, TypeError, KeyError):
            ts = 0.0                     # metadato illeggibile: non lo teniamo
        # se l'item e' ancora in memoria, il suo `ts` vale piu' del file:
        # un restart prolungato non deve far morire un'immagine valida.
        live = _ITEMS.get(file_id)
        if live is not None:
            continue
        if (now - ts) > _TTL_SEC:
            _drop_disk(file_id)
            removed += 1
    return removed


def clear() -> None:
    """Svuota lo store (usato dai test)."""
    global _TOTAL_BYTES
    with _LOCK:
        _ITEMS.clear()
        _TOTAL_BYTES = 0
        if _STORAGE_DIR is not None and _STORAGE_DIR.is_dir():
            for pat in ("*.bin", "*.json", "*.bin.tmp"):
                try:
                    for p in _STORAGE_DIR.glob(pat):
                        _unlink_quiet(p)
                except OSError:
                    pass


def stats() -> dict:
    with _LOCK:
        return {"items": len(_ITEMS), "bytes": _TOTAL_BYTES,
                "ttl_sec": _TTL_SEC, "max_items": _MAX_ITEMS,
                "max_bytes": _MAX_BYTES,
                "storage_dir": str(_STORAGE_DIR) if _STORAGE_DIR else None}
