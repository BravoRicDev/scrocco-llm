"""Liveness del processo tramite heartbeat su file (per l'HEALTHCHECK Docker).

[IT] PERCHE': l'healthcheck interrogava /healthz via HTTP con timeout di 3s.
Sotto carico l'event loop e' occupato, /healthz risponde in piu' di 3s, tre
fallimenti -> container "unhealthy" -> restart di un processo che stava
lavorando (e che ripartiva freddo sotto lo stesso carico: crash-loop).
Misurava la cosa sbagliata: un gateway sotto carico deve essere LENTO, non
morto.

COME: un task sull'event loop aggiorna un file ogni `INTERVAL_SEC`; il
check (`python -m app.liveness`) guarda solo l'eta' del file, senza HTTP:
passa finche' il loop continua a girare, anche se lento, e fallisce solo se
il loop e' fermo da `GATEWAY_HEARTBEAT_MAX_AGE` secondi (processo davvero
bloccato). Il task misura anche il ritardo del loop (gauge
`nx_event_loop_lag_ms`). /healthz resta com'era per il monitoraggio esterno.

Questo modulo usa solo la libreria standard: il check non importa il gateway.

[EN] File-heartbeat liveness for the Docker HEALTHCHECK (no HTTP, load-proof).
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

INTERVAL_SEC = 5.0
DEFAULT_MAX_AGE_SEC = 120.0


def heartbeat_path() -> str:
    return os.environ.get("GATEWAY_HEARTBEAT_FILE", "/tmp/scrocco-llm.heartbeat")


def _touch(path: str) -> None:
    try:
        os.utime(path)
    except FileNotFoundError:
        with open(path, "w", encoding="ascii") as f:
            f.write("scrocco-llm heartbeat\n")


async def heartbeat_loop(path: str | None = None, interval: float = INTERVAL_SEC) -> None:
    """Task di vita: batte ogni `interval` e pubblica il ritardo del loop."""
    from . import metrics

    path = path or heartbeat_path()
    expected = time.monotonic()
    while True:
        now = time.monotonic()
        lag_ms = max(0.0, (now - expected) * 1000.0)
        metrics.set_gauge("nx_event_loop_lag_ms", round(lag_ms, 1))
        try:
            _touch(path)
        except OSError:
            pass                  # file non scrivibile: il check fallira', e' voluto
        expected = time.monotonic() + interval
        await asyncio.sleep(interval)


def check(path: str | None = None, max_age: float | None = None, now: float | None = None) -> bool:
    """True se l'ultimo battito e' piu' recente di `max_age` secondi."""
    path = path or heartbeat_path()
    if max_age is None:
        try:
            max_age = float(os.environ.get("GATEWAY_HEARTBEAT_MAX_AGE", DEFAULT_MAX_AGE_SEC))
        except ValueError:
            max_age = DEFAULT_MAX_AGE_SEC
    try:
        age = (time.time() if now is None else now) - os.stat(path).st_mtime
    except OSError:
        return False
    return age <= max_age


if __name__ == "__main__":        # HEALTHCHECK del container
    sys.exit(0 if check() else 1)
