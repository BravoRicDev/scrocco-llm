"""Segnalazione degli errori ignorati apposta (`except Exception: pass`).

[IT] Molti punti del gateway proseguono di proposito dopo un errore
(best-effort: metriche, hook di apprendimento, statistiche admin,
applicazione di parametri di policy). Un `pass` nudo li rendeva invisibili:
un bug poteva restare muto per sempre. `report_suppressed` va chiamato DENTRO
l'`except`: conta l'occorrenza e ne logga il traceback a WARNING al massimo
una volta ogni `_REPORT_INTERVAL_SEC` per punto (niente rumore sui percorsi
ad alta frequenza). Il flusso del chiamante non cambia.

[EN] Rate-limited reporting for intentionally swallowed exceptions.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("nx.suppressed")

_REPORT_INTERVAL_SEC = 600.0
_lock = threading.Lock()
_counts: dict[str, int] = {}
_last_report: dict[str, float] = {}


def report_suppressed(site: str) -> None:
    """Registra un'eccezione ignorata in `site` ("modulo.funzione"). Da
    chiamare dentro il blocco `except`; non solleva mai."""
    now = time.monotonic()
    with _lock:
        count = _counts.get(site, 0) + 1
        _counts[site] = count
        last = _last_report.get(site)
        if last is not None and now - last < _REPORT_INTERVAL_SEC:
            return
        _last_report[site] = now
    log.warning("[suppressed] %s: errore ignorato (%d occorrenze finora)",
                site, count, exc_info=True)


def suppressed_counts() -> dict[str, int]:
    """Occorrenze per punto dall'avvio (diagnostica/test)."""
    with _lock:
        return dict(_counts)
