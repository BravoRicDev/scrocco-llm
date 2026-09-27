"""Avvio (app/serve.py): 1 worker = comando storico; N worker = supervisore."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from app import serve
from app.serve import Supervisor, resolve_workers, single_process_argv


@pytest.mark.parametrize("raw,expected", [(None, 1), ("", 1), ("0", 1), ("1", 1), ("4", 4), (" 12 ", 12)])
def test_resolve_workers(raw, expected):
    assert resolve_workers(raw) == expected


def test_resolve_workers_auto_is_capped():
    assert resolve_workers("auto", cpu_count=3) == 3
    assert resolve_workers("AUTO", cpu_count=128) == serve.MAX_AUTO_WORKERS


def test_resolve_workers_rejects_garbage():
    with pytest.raises(SystemExit):
        resolve_workers("molti")


def test_single_worker_runs_the_historical_command():
    assert single_process_argv("0.0.0.0", 4001) == [
        sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4001"]


def test_dockerfile_starts_through_app_serve():
    text = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
    cmd = [ln for ln in text.splitlines() if ln.startswith("CMD")]
    assert cmd and "app.serve" in cmd[-1]


class _Proc:
    _pid = 1000

    def __init__(self):
        _Proc._pid += 1
        self.pid = _Proc._pid
        self.rc = None
        self.killed = False

    def poll(self):
        return self.rc

    def kill(self):
        self.killed = True
        self.rc = -9


@pytest.fixture()
def sup(tmp_path, monkeypatch):
    s = Supervisor(2, "127.0.0.1", 0, heartbeat_file=str(tmp_path / "hb"), max_age=30.0)
    spawned: list[tuple[int, float]] = []

    def fake_spawn(w, now):
        w.proc = _Proc()
        w.started = now
        spawned.append((w.index, now))

    monkeypatch.setattr(s, "_spawn", fake_spawn)
    return s, spawned, tmp_path


def _beat(path: str, at: float) -> None:
    Path(path).write_text("x")
    os.utime(path, (at, at))


def test_supervisor_starts_all_workers(sup):
    s, spawned, _ = sup
    s._tick(1000.0)
    assert [i for i, _ in spawned] == [0, 1]


def test_dead_worker_is_restarted_with_backoff(sup):
    s, spawned, _ = sup
    s._tick(1000.0)
    s.workers[1].proc.rc = 1                    # crash subito dopo l'avvio
    s._tick(1005.0)
    assert s.workers[1].proc is None and s.workers[1].next_start == 1005.0 + 2.0
    s._tick(1006.0)
    assert len(spawned) == 2                    # ancora in backoff
    s._tick(1007.5)
    assert [i for i, _ in spawned] == [0, 1, 1]


def test_stuck_worker_is_killed_after_grace(sup):
    s, _spawned, tmp = sup
    t0 = time.time()
    s._tick(t0)
    _beat(str(tmp / "hb.w0"), t0 + 100)
    _beat(str(tmp / "hb.w1"), t0)                # battito fermo
    s._tick(t0 + 100)
    assert s.workers[1].proc.killed and not s.workers[0].proc.killed


def test_container_heartbeat_follows_live_workers(sup):
    s, _spawned, tmp = sup
    t0 = time.time()
    s._tick(t0)
    assert not (tmp / "hb").exists()             # nessun worker ha ancora battuto
    _beat(str(tmp / "hb.w0"), t0)
    s._tick(t0 + 1)
    assert (tmp / "hb").exists()
