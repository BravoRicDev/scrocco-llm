"""Regressione: lo shutdown del lifespan NON deve esplodere in modalita' CAUTA.

Con `BACKGROUND_CAUTIOUS=1` (come su cubotto) i task health/nightly NON vengono
avviati: `_health_task`/`_nightly_task` restano None. Prima del fix erano
assegnati come LOCALI (senza `global`) e il blocco `finally` che li referenzia
sollevava `UnboundLocalError` allo shutdown (traceback su `main.py:896`).
"""
import asyncio

import app.main as M


def test_lifespan_shutdown_cautious_no_error(monkeypatch):
    monkeypatch.setattr(M, "background_cautious_enabled", lambda: True)

    async def _noop(*_a, **_k):
        return 0

    for name in ("_load_adaptive_stats", "_load_cooldowns",
                 "_load_routing_state", "_bootstrap_runtime_from_logs",
                 "_load_thought_sigs", "_maybe_save_adaptive_stats",
                 "_maybe_save_all", "_maybe_save_cooldowns",
                 "_maybe_save_thought_sigs"):
        monkeypatch.setattr(M, name, lambda *a, **k: None)
    monkeypatch.setattr(M, "_drain_probe_tasks", _noop)
    monkeypatch.setattr(M, "_watcher", _noop)
    monkeypatch.setattr(M, "health_loop", _noop)
    monkeypatch.setattr(M, "_nightly_scheduler", _noop)
    monkeypatch.setattr(M.forwarder, "aclose", _noop)
    monkeypatch.setattr(M.router, "inflight_total", lambda *a, **k: 0)
    monkeypatch.setattr(M.LEDGER, "flush_sync", lambda *a, **k: 0)
    monkeypatch.setattr(M.repairlog, "flush_sync", lambda *a, **k: 0)

    async def _run():
        async with M.lifespan(object()):
            assert M._watch_task is not None
            assert M._health_task is None      # cauta: health non avviato
            assert M._nightly_task is None     # cauta: nightly non avviato

    asyncio.run(_run())
    # usciti dallo shutdown senza UnboundLocalError
    assert M._health_task is None
    assert M._nightly_task is None
