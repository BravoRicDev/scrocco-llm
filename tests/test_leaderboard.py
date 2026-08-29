"""Feature: /admin/insights/leaderboard classifica deployment (A2).

Isola il LEDGER in tmp_path come tests/test_insights.py e verifica la
struttura, l'auth, le metriche (calls/p95/error_rate), l'ordinamento e la
finestra temporale.
"""
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv = tmp_path / "k.csv"
    csv.write_text("commento,modello,provider,endpoint,data,context,"
                   "max_input,priority,scrocco-llm-test,caps\n")
    import app.main as m
    # patch DETERMINISTICA della master key (non via env)
    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-leaderboard"
    orig_csv = m.config.csv_path
    m.LEDGER.flush()                        # scarica buffer pregressi
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m.config, "csv_path", csv)
    m.config.reload()
    # LEDGER isolato nella tmp
    from app.ledger import Ledger
    led = Ledger(tmp_path)
    monkeypatch.setattr(m, "LEDGER", led)
    yield TestClient(m.app), m, led
    m.router._cooldown.clear()
    m.authn.master_key = orig_mk
    m.config.csv_path = orig_csv
    m.config.reload()


MK = {"Authorization": "Bearer test-master-leaderboard"}


def _seed(led, dep, dur_ms, fb, n=1, ts=None):
    for _ in range(n):
        led.record({
            "ses": "s", "profile": "test", "req": "r", "kind": "chat",
            "grp": "g", "dep": dep, "model": "openai/gpt-x",
            "tries": 1, "fb": fb, "dur_ms": dur_ms,
            "stream": False, "qc": False,
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "total_tokens": 150},
        }, pricing={"openai/gpt-*": {"prompt_per_1m": 1.0,
                                     "completion_per_1m": 2.0}},
        upstream_model="openai/gpt-x")
    if ts is not None:
        for r in led._buf[-n:]:
            r["ts"] = ts
    led.flush()


def test_no_auth_401(client):
    c, _, led = client
    _seed(led, "da", 100, 0)
    _seed(led, "db", 300, 0)
    assert c.get("/admin/insights/leaderboard").status_code == 401
    # chiave sbagliata -> sempre 401
    assert c.get("/admin/insights/leaderboard",
                 headers={"Authorization": "Bearer sk-test"}).status_code == 401


def test_leaderboard_metrics(client):
    c, m, led = client
    # da: 2 call, dur [100,200], fb=1 su una -> error_rate 0.5
    _seed(led, "da", 100, 1)
    _seed(led, "da", 200, 0)
    # db: 2 call, dur [300,400], nessun fb -> error_rate 0
    _seed(led, "db", 300, 0)
    _seed(led, "db", 400, 0)
    j = c.get("/admin/insights/leaderboard", headers=MK).json()
    assert j["count"] == 2
    by_dep = {r["dep"]: r for r in j["rows"]}
    assert set(by_dep) == {"da", "db"}
    assert by_dep["da"]["calls"] == 2 and by_dep["db"]["calls"] == 2
    # p95 di [100,200] = 195 ; p95 di [300,400] = 395
    assert by_dep["da"]["p95_dur_ms"] == 195
    assert by_dep["db"]["p95_dur_ms"] == 395
    # error_rate = fallback_rate + qc_rate
    assert by_dep["da"]["error_rate"] > 0
    assert by_dep["db"]["error_rate"] == 0
    # api_key MAI esposta
    assert "api_key" not in by_dep["da"]
    # struttura campi attesi
    for f in ("dep", "profile", "group", "provider", "model", "calls",
              "avg_dur_ms", "p95_dur_ms", "error_rate", "last_used",
              "health", "probe_ms"):
        assert f in by_dep["da"], f


def test_sort_avg_asc(client):
    c, m, led = client
    _seed(led, "da", 100, 0)
    _seed(led, "da", 200, 0)
    _seed(led, "db", 300, 0)
    _seed(led, "db", 400, 0)
    j = c.get("/admin/insights/leaderboard?sort=avg_dur_ms&order=asc",
              headers=MK).json()
    avgs = [r["avg_dur_ms"] for r in j["rows"]]
    # da avg 150 < db avg 350 -> ordinamento crescente
    assert avgs == sorted(avgs)
    assert avgs[0] == 150 and avgs[-1] == 350


def test_window_1h_excludes_old(client):
    c, m, led = client
    _seed(led, "da", 100, 0)               # recente
    _seed(led, "db", 300, 0)               # recente
    old_ts = int(time.time()) - 30 * 86400  # 30 giorni fa -> escluso
    _seed(led, "old", 500, 0, ts=old_ts)
    j = c.get("/admin/insights/leaderboard?window=1h", headers=MK).json()
    deps = {r["dep"] for r in j["rows"]}
    assert "old" not in deps
    assert deps == {"da", "db"}
