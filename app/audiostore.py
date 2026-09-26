"""Cache in-memory delle trascrizioni STT, per non ritrascrivere a ogni turno.

Perche' esiste. Il client rimanda l'audio nella history a ogni turno (il
gateway non modifica la history del client), quindi senza cache ogni turno
rifarebbe le chiamate STT: costoso e lento. La chiave e' l'hash dei byte
dell'audio, quindi lo STT-bridge riusa la trascrizione per gli stessi dati.

Non persistita su disco, di proposito: lo stato di routing (`routing_state.json`)
non e' il posto per minuti di audio, e dopo un restart si ri-trascrive una
volta (comportamento corretto e self-healing). Solo HIT si memorizzano: un
fallimento non viene messo in cache, cosi' il retry recupera da solo appena i
deployment tornano disponibili.

Stesso pattern in-memory di `app/imagestore.py` (dict con TTL, cap e sweep).
"""
from __future__ import annotations

import hashlib
import threading
import time

_LOCK = threading.Lock()
# hash -> {"text": str, "ts": float}. L'ordine di inserimento del dict (py>=3.7)
# e' anche l'ordine di eviction (oldest-first).
_ITEMS: dict[str, dict] = {}
_TOTAL_BYTES = 0

_TTL_SEC = 3600
_MAX_ITEMS = 256
# Budget sui caratteri di testo, non sui byte audio: un'ora di parlato sono
# ~40k caratteri, e la cache deve stare in memoria senza crescere.
_MAX_CHARS = 4_000_000


def configure(*, ttl_sec=None, max_items=None, max_chars=None) -> None:
    """Aggiorna i limiti della cache (policy `stt_chat.*`). None = invariato."""
    global _TTL_SEC, _MAX_ITEMS, _MAX_CHARS
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
    if max_chars is not None:
        try:
            _MAX_CHARS = max(0, int(max_chars))
        except (TypeError, ValueError):
            pass


def ttl_sec() -> int:
    return _TTL_SEC


def key_for(data: bytes) -> str:
    """Chiave di cache: hash dei byte grezzi dell'audio.

    Usa sha256 sui byte ORIGINALI (non sull'OGG normalizzato): cosi' la cache
    funziona anche se cambiano i parametri di normalizzazione, e due file
    identici ma codificati diversamente condividono la trascrizione."""
    return hashlib.sha256(data).hexdigest()


def get(key: str) -> str | None:
    """Trascrizione in cache, o None. Non registra un HIT (nessun logging)."""
    if not key:
        return None
    with _LOCK:
        e = _ITEMS.get(key)
        if e is None:
            return None
        if _TTL_SEC > 0 and time.time() - float(e.get("ts") or 0) > _TTL_SEC:
            _drop_locked(key)
            return None
        return str(e.get("text") or "") or None


def put(key: str, text: str) -> None:
    """Memorizza una trascrizione (HIT). Ignora testo vuoto."""
    if not key or not text or not str(text).strip():
        return
    n = len(str(text))
    with _LOCK:
        if _MAX_CHARS > 0 and n > _MAX_CHARS:
            return                      # non spendiamo il budget su un caso limite
        _drop_locked(key)
        _ITEMS[key] = {"text": str(text), "ts": time.time()}
        globals()["_TOTAL_BYTES"] += n
        _evict_locked()


def _drop_locked(key: str) -> None:
    global _TOTAL_BYTES
    e = _ITEMS.pop(key, None)
    if e:
        _TOTAL_BYTES = max(0, _TOTAL_BYTES - len(str(e.get("text") or "")))


def _evict_locked() -> None:
    """Evict LRU-by-insertion finche' si sta nei limiti."""
    while _ITEMS and (
            len(_ITEMS) > _MAX_ITEMS
            or (_MAX_CHARS > 0 and _TOTAL_BYTES > _MAX_CHARS)):
        _drop_locked(next(iter(_ITEMS)))


def sweep() -> int:
    """Rimuove le voci scadute. Ritorna quante ne ha rimosse."""
    if _TTL_SEC <= 0:
        return 0
    now = time.time()
    gone = 0
    with _LOCK:
        for k in [k for k, e in _ITEMS.items()
                  if now - float(e.get("ts") or 0) > _TTL_SEC]:
            _drop_locked(k)
            gone += 1
    return gone


def stats() -> dict:
    """Numeri per /admin (dimensioni, TTL, occupazione)."""
    with _LOCK:
        return {"items": len(_ITEMS), "chars": _TOTAL_BYTES,
                "ttl_sec": _TTL_SEC, "max_items": _MAX_ITEMS,
                "max_chars": _MAX_CHARS}


def clear() -> None:
    with _LOCK:
        _ITEMS.clear()
        globals()["_TOTAL_BYTES"] = 0
