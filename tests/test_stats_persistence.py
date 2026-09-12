"""Feature: persistenza dei punteggi deployment in adaptive_stats.

I contatori cumulativi (ok/fail), i timestamp (ultimo successo/fallimento),
il motivo dell'ultimo errore e la latenza EMA devono sopravvivere al restart
tramite router.dump_stats()/load_stats(), ed essere esposti da
GET /admin/deployments/stats.
"""
import time

from fastapi.testclient import TestClient

from app.policy import Policy
from app.router import Router


def _router():
    r = Router.__new__(Router)
    r.policy = Policy()
    r._stats = {}
    r._cooldown = {}
    r._cooldown_since = {}
    r._cap_strikes = {}
    return r


def test_dump_includes_new_score_fields():
    r = _router()
    s = r.stats_for("g__m__0")
    s.ok_count = 3
    s.fail_count = 2
    s.last_success_ts = 111.0
    s.last_fail_ts = 222.0
    s.last_reason = "http_429"
    s.ema_latency_ms = 1234.5
    row = r.dump_stats()["stats"]["g__m__0"]
    assert row["ok_count"] == 3
    assert row["fail_count"] == 2
    assert row["last_success_ts"] == 111.0
    assert row["last_fail_ts"] == 222.0
    assert row["last_reason"] == "http_429"
    assert row["ema_latency_ms"] == 1234.5


def test_roundtrip_restores_counters_and_timestamps():
    r = _router()
    s = r.stats_for("g__m__0")
    s.ok_count = 7
    s.fail_count = 5
    s.last_success_ts = 1000.5
    s.last_fail_ts = 900.25
    s.last_reason = "timeout"
    s.ema_latency_ms = 500.0
    dump = r.dump_stats()

    r2 = _router()
    r2.load_stats(dump)
    s2 = r2.stats_for("g__m__0")
    assert s2.ok_count == 7
    assert s2.fail_count == 5
    assert s2.last_success_ts == 1000.5
    assert s2.last_fail_ts == 900.25
    assert s2.last_reason == "timeout"
    assert s2.ema_latency_ms == 500.0


def test_load_is_backward_compatible_without_new_fields():
    """Vecchi file adaptive_stats (senza i campi nuovi) -> default puliti."""
    r = _router()
    r.load_stats({"stats": {"g__m__0": {
        "ema_latency_ms": 12.0, "last_used": 5.0, "fail_streak": 1,
        "success_ema": 0.8, "fail_count_24h": 2, "fail_day_key": "2026-01-01",
    }}})
    s = r.stats_for("g__m__0")
    assert s.ok_count == 0 and s.fail_count == 0
    assert s.last_success_ts == 0.0 and s.last_fail_ts == 0.0
    assert s.ema_latency_ms == 12.0


def test_note_result_increments_ok_count():
    # senza config: record_success fa early-return, note_result resta sicuro
    r = _router()
    before = time.time()
    r.note_result("g__m__0", 250.0)
    s = r.stats_for("g__m__0")
    assert s.ok_count == 1
    assert s.last_success_ts >= before
    r.note_result("g__m__0", 300.0)
    assert s.ok_count == 2


def test_endpoint_exposes_persisted_scores(monkeypatch):
    """L'endpoint legge i punteggi da router._stats (persistiti/live)."""
    import app.main as m
    monkeypatch.setattr(m.authn, "master_key", "test-master-stats")
    unique = "scrocco-llm-test-32k__m__0"
    try:
        s = m.router.stats_for(unique)
        s.ok_count = 4
        s.fail_count = 1
        s.ema_latency_ms = 321.0
        s.last_reason = "http_500"
        c = TestClient(m.app)
        # senza master key -> 401
        assert c.get("/admin/deployments/stats").status_code == 401
        r = c.get("/admin/deployments/stats",
                  headers={"Authorization": "Bearer test-master-stats"})
        assert r.status_code == 200
        j = r.json()
        row = next(x for x in j["rows"] if x["dep"] == unique)
        assert row["ok"] == 4 and row["fail"] == 1
        assert row["ema_latency_ms"] == 321.0
        assert row["last_reason"] == "http_500"
    finally:
        m.router._stats.pop(unique, None)
