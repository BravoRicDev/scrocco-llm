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
