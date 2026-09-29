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
import faulthandler
import logging
import os
import sys
import threading
import time

log = logging.getLogger("nx.liveness")

INTERVAL_SEC = 5.0
DEFAULT_MAX_AGE_SEC = 120.0
# Soglia del watchdog: quanto il loop puo' restare fermo prima che si
# scrivano le stack di tutti i thread (diagnosi, NON kill).
DEFAULT_STALL_SEC = 30.0

# Ultimo giro del loop, in monotonic: aggiornato da heartbeat_loop() e letto
# dal watchdog per distinguere "loop occupato" da "loop fermo".
_LAST_TICK = 0.0
_TICK_LOCK = threading.Lock()


def last_tick() -> float:
    with _TICK_LOCK:
        return _LAST_TICK


def _mark_tick(value: float | None = None) -> None:
    global _LAST_TICK
    with _TICK_LOCK:
        _LAST_TICK = time.monotonic() if value is None else value


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
    _mark_tick()
    while True:
        now = time.monotonic()
        lag_ms = max(0.0, (now - expected) * 1000.0)
        metrics.set_gauge("nx_event_loop_lag_ms", round(lag_ms, 1))
        _mark_tick(now)
        try:
            _touch(path)
        except OSError:
            pass  # file non scrivibile: il check fallira', e' voluto
        expected = time.monotonic() + interval
        await asyncio.sleep(interval)


def start_thread_beater(
    path: str | None = None,
    interval: float = INTERVAL_SEC,
    stop: threading.Event | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Batte il file da un THREAD, indipendente dall'event loop.

    PERCHE': il battito viveva solo come task del loop. Sotto carico il loop
    non gira per piu' di `max_age` e il supervisore interpretava "occupato"
    come "morto": SIGKILL del worker e richieste in volo buttate (fino a 65),
    poi ripartenza a freddo sotto lo stesso carico. Un thread non dipende dal
    loop: il file resta fresco finche' il PROCESSO e' vivo. Se il loop e'
    davvero fermo lo dice il watchdog (`start_stall_watchdog`), con le stack.
    """
    path = path or heartbeat_path()
    stop = stop or threading.Event()

    def _run() -> None:
        while not stop.is_set():
            try:
                _touch(path)
            except OSError:
                pass
            stop.wait(interval)

    thread = threading.Thread(target=_run, name="nx-heartbeat", daemon=True)
    thread.start()
    return thread, stop


def start_stall_watchdog(
    dump_dir: str | None = None,
    stall_sec: float = DEFAULT_STALL_SEC,
    interval: float = 1.0,
    cooldown_sec: float = 300.0,
    stop: threading.Event | None = None,
) -> tuple[threading.Thread, threading.Event]:
    """Thread: se l'event loop non avanza per `stall_sec`, scrive le stack.

    Serve a diagnosticare i blocchi lunghi in produzione: Docker non concede
    CAP_SYS_PTRACE, quindi py-spy non puo' agganciarsi. `faulthandler` invece
    scrive i traceback di TUTTI i thread su un file, e da li' si vede il punto
    esatto che tiene fermo il loop. Non uccide nulla: osserva e documenta.
    """
    dump_dir = dump_dir or os.environ.get("GATEWAY_STALL_DUMP_DIR") or "var"
    stop = stop or threading.Event()
    _mark_tick()

    def _dump(lag: float) -> str | None:
        try:
            os.makedirs(dump_dir, exist_ok=True)
            path = os.path.join(dump_dir, time.strftime("stall-%Y%m%d-%H%M%S.txt"))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"event loop fermo da {lag:.0f}s (pid {os.getpid()})\n")
                faulthandler.dump_traceback(file=fh, all_threads=True)
            return path
        except OSError:
            return None

    def _run() -> None:
        while not stop.wait(interval):
            lag = time.monotonic() - last_tick()
            if lag < stall_sec:
                continue
            where = _dump(lag)
            log.critical("[stall] event loop fermo da %.0fs: stack in %s", lag, where or "(dump fallito)")
            stop.wait(cooldown_sec)

    thread = threading.Thread(target=_run, name="nx-stall-watchdog", daemon=True)
    thread.start()
    return thread, stop


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


if __name__ == "__main__":  # HEALTHCHECK del container
    sys.exit(0 if check() else 1)
