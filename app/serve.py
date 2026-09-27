"""Avvio del gateway: processo singolo (default) o N worker (`GATEWAY_WORKERS`).

    python -m app.serve [--host H] [--port P]

[IT]
- `GATEWAY_WORKERS` assente o 1: `exec python -m uvicorn app.main:app --host
  H --port P`, cioe' ESATTAMENTE il comando di prima (stesso processo, stesse
  opzioni uvicorn). Nessuna differenza.
- `GATEWAY_WORKERS=N` (N>1) o `auto` (= core disponibili, max 16): questo
  processo diventa il SUPERVISORE:
  * apre UNA volta la porta TCP e la passa ai worker (il kernel distribuisce
    le connessioni tra loro);
  * avvia il bus di replica (app/cluster.py) su un socket unix privato;
  * avvia N worker `python -m app.serve --worker` (ognuno un uvicorn
    completo: stesse opzioni del processo singolo, piu' un socket unix
    privato per le richieste instradate per sessione, vedi app/affinity.py);
  * riavvia un worker che esce o il cui event loop e' fermo (battito
    `GATEWAY_HEARTBEAT_FILE.wI` piu' vecchio di GATEWAY_HEARTBEAT_MAX_AGE);
  * aggiorna il battito del container (`GATEWAY_HEARTBEAT_FILE`) finche'
    almeno un worker e' vivo: l'HEALTHCHECK Docker resta quello di prima;
  * su SIGTERM/SIGINT inoltra lo stop ai worker (ognuno fa il suo drain) e
    attende fino a `GATEWAY_SHUTDOWN_TIMEOUT` secondi.

[EN] Entry point: single process (exec of the historical uvicorn command) or
a supervisor that shares the listening socket among N workers and restarts
dead or stuck ones.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from . import cluster, liveness

log = logging.getLogger("nx.serve")

MAX_AUTO_WORKERS = 16
START_GRACE_SEC = 60.0          # come lo start-period dell'HEALTHCHECK
CHECK_INTERVAL_SEC = 1.0


def resolve_workers(raw: str | None, cpu_count: int | None = None) -> int:
    """`GATEWAY_WORKERS`: vuoto/1 -> 1, `auto` -> core (max 16), N -> N."""
    value = (raw or "").strip().lower()
    if value in ("", "0", "1"):
        return 1
    if value == "auto":
        return max(1, min(cpu_count or os.cpu_count() or 1, MAX_AUTO_WORKERS))
    try:
        n = int(value)
    except ValueError:
        raise SystemExit(f"GATEWAY_WORKERS non valido: {raw!r} (intero >= 1 oppure 'auto')") from None
    return max(1, n)


def single_process_argv(host: str, port: int) -> list[str]:
    """Il comando storico del container, invariato."""
    return [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m app.serve")
    p.add_argument("--host", default=os.environ.get("GATEWAY_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("GATEWAY_PORT", "4001")))
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:  # pragma: no cover - entrypoint
    args = _parse_args(argv)
    if args.worker:
        run_worker(args.host, args.port)
        return
    workers = resolve_workers(os.environ.get(cluster.ENV_SIZE))
    if workers == 1:
        argv_ = single_process_argv(args.host, args.port)
        os.execv(argv_[0], argv_)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sys.exit(Supervisor(workers, args.host, args.port).run())


# ---------------------------------------------------------------- worker --
def run_worker(host: str, port: int) -> None:
    """Un worker: il server pubblico (socket TCP ereditato, con instradamento
    per sessione) e quello privato (socket unix) sulla STESSA app."""
    import uvicorn

    sock = socket.socket(fileno=int(os.environ["GATEWAY_LISTEN_FD"]))
    # Config prima dell'import dell'app, come fa la CLI uvicorn (logging).
    public = uvicorn.Config("app.main:app", host=host, port=port)
    from .affinity import AffinityProxy, InternalEntry
    from .main import app

    public.app = AffinityProxy(app)
    internal = uvicorn.Config(
        InternalEntry(app), uds=cluster.worker_socket(cluster.index()), lifespan="off",
        proxy_headers=False, access_log=False, log_config=None,
        timeout_keep_alive=75, timeout_graceful_shutdown=30,
    )
    asyncio.run(_serve_worker(uvicorn.Server(public), _internal_server_class()(internal), sock))


def _internal_server_class():
    import uvicorn

    class InternalServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):     # i segnali li gestisce il server pubblico
            yield

    return InternalServer


async def _serve_worker(public, internal, sock: socket.socket) -> None:
    from .affinity import PEERS

    internal_task = asyncio.create_task(internal.serve())
    try:
        await public.serve(sockets=[sock])
    finally:
        internal.should_exit = True
        await internal_task
        await PEERS.aclose()


# ------------------------------------------------------------ supervisore --
class _Worker:
    def __init__(self, index: int):
        self.index = index
        self.proc: subprocess.Popen | None = None
        self.started = 0.0
        self.next_start = 0.0
        self.backoff = 1.0


class Supervisor:
    def __init__(self, workers: int, host: str, port: int, *, python: str | None = None,
                 heartbeat_file: str | None = None, max_age: float | None = None):
        self.workers = [_Worker(i) for i in range(workers)]
        self.host = host
        self.port = port
        self.python = python or sys.executable
        self.heartbeat_file = heartbeat_file or liveness.heartbeat_path()
        self.max_age = max_age if max_age is not None else float(
            os.environ.get("GATEWAY_HEARTBEAT_MAX_AGE", liveness.DEFAULT_MAX_AGE_SEC))
        self.shutdown_timeout = float(os.environ.get("GATEWAY_SHUTDOWN_TIMEOUT", "60"))
        self.run_dir = ""
        # un seme per vita del cluster: stessi unique su tutti i worker, anche
        # su quelli riavviati (vedi app/config.py::_shuffle_rng)
        self.seed = os.environ.get(cluster.ENV_SEED) or secrets.token_hex(8)
        self._sock: socket.socket | None = None
        self._stopping: asyncio.Event | None = None
        self._last_beat = 0.0

    def run(self) -> int:
        return asyncio.run(self._main())

    def _bind(self) -> socket.socket:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.set_inheritable(True)
        return sock

    async def _main(self) -> int:
        self._stopping = asyncio.Event()
        self._sock = self._bind()
        self.run_dir = tempfile.mkdtemp(prefix="scrocco-llm-", dir=os.environ.get(cluster.ENV_RUN_DIR) or None)
        os.chmod(self.run_dir, 0o700)
        hub = cluster.BusHub(os.path.join(self.run_dir, "bus.sock"))
        await hub.start()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._stopping.set)
        log.info("[serve] supervisore: %d worker su %s:%d (pid %d)", len(self.workers),
                 self.host, self.port, os.getpid())
        try:
            while not self._stopping.is_set():
                self._tick(time.time())
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), CHECK_INTERVAL_SEC)
            await self._shutdown()
        finally:
            await hub.stop()
            self._sock.close()
            shutil.rmtree(self.run_dir, ignore_errors=True)
        log.info("[serve] supervisore terminato")
        return 0

    def _worker_heartbeat(self, w: _Worker) -> str:
        return f"{self.heartbeat_file}.w{w.index}"

    def _spawn(self, w: _Worker, now: float) -> None:
        hb = self._worker_heartbeat(w)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(hb)
        env = dict(os.environ)
        env.update({
            cluster.ENV_SIZE: str(len(self.workers)),
            cluster.ENV_INDEX: str(w.index),
            cluster.ENV_RUN_DIR: self.run_dir,
            cluster.ENV_SEED: self.seed,
            "GATEWAY_LISTEN_FD": str(self._sock.fileno()),
            "GATEWAY_HEARTBEAT_FILE": hb,
        })
        w.proc = subprocess.Popen(
            [self.python, "-m", "app.serve", "--worker", "--host", self.host, "--port", str(self.port)],
            env=env, pass_fds=(self._sock.fileno(),))
        w.started = now
        log.info("[serve] worker %d avviato (pid %d)", w.index, w.proc.pid)

    def _tick(self, now: float) -> None:
        alive_fresh = False
        for w in self.workers:
            if w.proc is None:
                if now >= w.next_start:
                    self._spawn(w, now)
                continue
            rc = w.proc.poll()
            if rc is not None:
                # crash-loop: backoff crescente (max 30s); dopo un minuto di vita riparte subito
                w.backoff = 1.0 if now - w.started > START_GRACE_SEC else min(30.0, w.backoff * 2)
                log.error("[serve] worker %d uscito (codice %s): riavvio tra %.0fs", w.index, rc, w.backoff)
                w.proc = None
                w.next_start = now + w.backoff
                continue
            fresh = liveness.check(self._worker_heartbeat(w), self.max_age, now)
            if fresh:
                alive_fresh = True
            elif now - w.started > max(START_GRACE_SEC, self.max_age):
                log.error("[serve] worker %d bloccato (battito piu' vecchio di %.0fs): kill",
                          w.index, self.max_age)
                w.proc.kill()
        if alive_fresh and now - self._last_beat >= liveness.INTERVAL_SEC:
            with contextlib.suppress(OSError):
                liveness._touch(self.heartbeat_file)
            self._last_beat = now

    async def _shutdown(self) -> None:
        running = [w for w in self.workers if w.proc is not None and w.proc.poll() is None]
        log.info("[serve] stop: SIGTERM a %d worker (attesa max %.0fs)", len(running), self.shutdown_timeout)
        for w in running:
            with contextlib.suppress(ProcessLookupError):
                w.proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + self.shutdown_timeout
        while any(w.proc.poll() is None for w in running) and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        for w in running:
            if w.proc.poll() is None:
                log.warning("[serve] worker %d non uscito in tempo: kill", w.index)
                w.proc.kill()
                w.proc.wait()


if __name__ == "__main__":  # pragma: no cover
    main()
