"""Potatura delle mappe in-memory per-sessione/per-deployment.

[IT] Il router tiene decine di dict "session_id -> (valore, ts)" limitati da
un TTL e da un tetto di voci (anti-crescita della RAM). La stessa logica era
ripetuta a mano in piu' punti (con varianti O(n^2) `while len > cap: min()`):
qui vive una sola implementazione.

[EN] Shared TTL/cap eviction helpers for the router's in-memory maps.
`evict_oldest` removes exactly the entries `sorted(d, key)[:len(d) - cap]`
would (heapq.nsmallest is stable), in O(n log k) instead of O(n^2).
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from typing import Any

# Tetto storico delle mappe per-sessione del router.
SESSION_MAP_CAP = 4096


def drop_expired(d: dict, ts_of: Callable[[Any], float], now: float, ttl: float) -> None:
    """Rimuove le voci con `now - ts_of(valore) > ttl`."""
    for k in [k for k, v in d.items() if now - ts_of(v) > ttl]:
        d.pop(k, None)


def evict_oldest(d: dict, ts_of: Callable[[Any], float], cap: int = SESSION_MAP_CAP) -> None:
    """Se `d` supera `cap` voci, rimuove le piu' vecchie (ts minore) fino a
    tornare a `cap`. A parita' di ts vince l'ordine di inserimento."""
    excess = len(d) - cap
    if excess <= 0:
        return
    for k in heapq.nsmallest(excess, d, key=lambda k: ts_of(d[k])):
        d.pop(k, None)


# Accessor del timestamp per le forme di voce usate dal router.
def entry_ts(v: tuple) -> float:
    """(valore, ts)."""
    return v[1]


def record_ts(r: dict | None) -> float:
    """{..., "ts": epoch} (None/assente = 0)."""
    return float((r or {}).get("ts") or 0.0)


def last_sample_ts(dq) -> float:
    """deque di (ts, n): ts dell'ultimo campione (vuota = 0)."""
    return dq[-1][0] if dq else 0.0


def fingerprint_ts(v: tuple) -> float:
    """(h_body, h_sys, ts)."""
    return v[2]
