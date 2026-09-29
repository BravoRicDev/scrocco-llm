"""Heartbeat di liveness per l'HEALTHCHECK Docker (app/liveness.py)."""
import asyncio
import os
import subprocess
import sys
import time

from app import liveness


def test_check_fresh_stale_missing(tmp_path):
    p = tmp_path / "hb"
    assert liveness.check(str(p), max_age=60) is False        # mai battuto
    p.write_text("x")
    assert liveness.check(str(p), max_age=60) is True
    old = time.time() - 300
    os.utime(p, (old, old))
    assert liveness.check(str(p), max_age=120) is False        # loop fermo da 5 min


def test_heartbeat_loop_touches_file_and_publishes_lag(tmp_path):
    from app import metrics
    p = tmp_path / "hb"

    async def main():
        t = asyncio.create_task(liveness.heartbeat_loop(str(p), interval=0.05))
        await asyncio.sleep(0.2)
        t.cancel()
    asyncio.run(main())
    assert liveness.check(str(p), max_age=5)
    assert "nx_event_loop_lag_ms" in metrics.render()


def test_healthcheck_command(tmp_path):
    """Il comando dell'HEALTHCHECK: exit 0 col battito fresco, 1 senza."""
    p = tmp_path / "hb"
    env = {**os.environ, "GATEWAY_HEARTBEAT_FILE": str(p)}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cmd = [sys.executable, "-m", "app.liveness"]
    assert subprocess.run(cmd, env=env, cwd=root).returncode == 1
    p.write_text("x")
    assert subprocess.run(cmd, env=env, cwd=root).returncode == 0


def test_thread_beater_tiene_fresco_il_file_senza_loop(tmp_path):
    """Il battito da thread non dipende dall'event loop: un worker occupato
    (loop fermo) risulta VIVO e non viene piu' SIGKILLato dal supervisore."""
    p = tmp_path / "hb"
    t, stop = liveness.start_thread_beater(str(p), interval=0.05)
    try:
        deadline = time.time() + 3
        while not p.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert p.exists()
        old = time.time() - 300
        os.utime(p, (old, old))
        time.sleep(0.2)
        assert liveness.check(str(p), max_age=5) is True     # il thread ha ribattuto
    finally:
        stop.set()
        t.join(timeout=2)


def test_stall_watchdog_scrive_le_stack_quando_il_loop_e_fermo(tmp_path):
    """Loop fermo -> stack di TUTTI i thread su file (diagnosi, non kill)."""
    liveness._mark_tick(time.monotonic() - 999)
    t, stop = liveness.start_stall_watchdog(
        dump_dir=str(tmp_path), stall_sec=0.05, interval=0.02, cooldown_sec=0.05)
    try:
        deadline = time.time() + 3
        files: list = []
        while time.time() < deadline:
            files = list(tmp_path.glob("stall-*.txt"))
            if files:
                break
            time.sleep(0.02)
        assert files, "nessun dump scritto dal watchdog"
        text = files[0].read_text(encoding="utf-8")
        assert "event loop fermo" in text
        assert "Thread" in text or 'File "' in text
    finally:
        stop.set()
        t.join(timeout=2)


def test_stall_watchdog_tace_se_il_loop_gira(tmp_path):
    liveness._mark_tick()
    t, stop = liveness.start_stall_watchdog(
        dump_dir=str(tmp_path), stall_sec=5.0, interval=0.02)
    try:
        time.sleep(0.2)
        assert list(tmp_path.glob("stall-*.txt")) == []
    finally:
        stop.set()
        t.join(timeout=2)
