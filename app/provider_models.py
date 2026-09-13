"""Dedup delle chiamate GET {endpoint}/models.

[IT] COSA: un provider espone la STESSA lista `/models` per tutte le chiavi
dello stesso endpoint. Interrogarlo una volta per chiave e' inutile e
rischioso (i provider hanno controlli anti-DDoS). Questo modulo raggruppa per
endpoint e usa la PRIMA chiave che risponde 200, con fallback sulle chiavi
successive solo se la prima fallisce. I risultati sono cachati in-memory con
TTL configurabile (policy `provider_models_ttl_sec`, default 300s).

[EN] WHAT: dedup of GET {endpoint}/models across keys of the same endpoint:
first working key wins, siblings are skipped; in-memory TTL cache.

Nessuna dipendenza da altri moduli app.* (evita cicli di import).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

log = logging.getLogger("nx.models")

DEFAULT_TTL_SEC = 300
DEFAULT_TIMEOUT_S = 20.0

_CACHE: dict[str, "ProviderModels"] = {}


def _mask(key: str) -> str:
    return f"{key[:6]}…{key[-3:]}" if len(key) > 10 else "***"


@dataclass
class ProviderModels:
    """Esito di UNA chiamata `/models` per endpoint (condivisa tra le chiavi)."""
    endpoint: str
    ok: bool = False
    ids: set[str] = field(default_factory=set)
    items: list[dict] = field(default_factory=list)
    key_masked: str = ""
    status: int | None = None
    error: str | None = None
    tried: int = 0
    skipped: int = 0
    cached: bool = False
    fetched_at: float = 0.0


def clear_cache() -> None:
    """Svuota la cache (usato nei test e per forzare un refresh globale)."""
    _CACHE.clear()


def cache_info() -> dict[str, dict[str, Any]]:
    now = time.monotonic()
    return {ep: {"ok": v.ok, "tried": v.tried, "cached_for": round(now - v.fetched_at, 1)}
            for ep, v in _CACHE.items()}


async def fetch_provider_models(http, endpoint: str, keys: Iterable[str], *,
                                ttl_sec: int = DEFAULT_TTL_SEC,
                                force: bool = False) -> ProviderModels:
    """Ritorna la lista modelli di `endpoint` usando la prima chiave valida.

    - `keys`: chiavi candidate in ordine di preferenza (ordine CSV).
    - cache per endpoint con TTL `ttl_sec`; `force=True` la ignora.
    - su 405/501 (endpoint senza GET /models) non prova le chiavi restanti.
    - le chiavi gemelle vengono saltate appena una risponde 200.
    """
    base = (endpoint or "").rstrip("/")
    now = time.monotonic()
    if not force:
        cached = _CACHE.get(base)
        if cached is not None and (now - cached.fetched_at) < max(0, int(ttl_sec)):
            return replace(cached, cached=True)

    ordered: list[str] = []
    seen: set[str] = set()
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            ordered.append(k)
    total_keys = len(ordered)

    tried = 0
    last: ProviderModels | None = None
    for key in ordered:
        tried += 1
        masked = _mask(key)
        try:
            r = await http.get(f"{base}/models",
                               headers={"Authorization": f"Bearer {key}"})
        except Exception as exc:                      # noqa: BLE001
            last = ProviderModels(base, key_masked=masked, tried=tried,
                                  error=f"{type(exc).__name__}: {exc}")
            continue
        if r.status_code == 200:
            try:
                items = r.json().get("data") or []
            except Exception:                         # noqa: BLE001
                items = []
            ids = {m.get("id", "") for m in items if isinstance(m, dict)}
            res = ProviderModels(base, ok=True, ids=ids, items=items,
                                 key_masked=masked, status=200, tried=tried,
                                 skipped=max(0, total_keys - tried),
                                 fetched_at=time.monotonic())
            _CACHE[base] = res
            if tried > 1 or res.skipped:
                log.info("[models] %s: chiave %s ok dopo %d tentativo/i "
                         "(%d chiavi gemelle saltate)", base, masked, tried,
                         res.skipped)
            return replace(res, cached=False)
        last = ProviderModels(base, key_masked=masked, status=r.status_code,
                              tried=tried, skipped=max(0, total_keys - tried),
                              error=f"HTTP {r.status_code}")
        # 405/501: il provider non supporta GET /models -> le altre chiavi
        # falliranno identicamente. Evita N chiamate inutili.
        if r.status_code in (405, 501):
            log.info("[models] %s: GET /models non supportato (HTTP %d), "
                     "%d chiavi restanti saltate", base, r.status_code,
                     max(0, total_keys - tried))
            break

    res = last or ProviderModels(base, error="nessuna chiave disponibile")
    res.fetched_at = time.monotonic()
    res.skipped = max(0, total_keys - tried)
    _CACHE[base] = res
    log.debug("[models] %s: non disponibile (%s) dopo %d chiave/i",
              base, res.error, tried)
    return res
