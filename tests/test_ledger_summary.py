"""Aggregazione giornaliera del ledger: i segmenti ruotati oltre soglia
vengono compressi in usage_summary.json e iter_rows legge summary + segmenti
recenti. /admin/insights continua a funzionare sulle righe aggregate."""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.ledger import Ledger


def _row(dep="d0", fb=0, qc=0, wd=None, n=1, day=None):
    """Genera n righe; `fb`/`qc` = quante righe (delle n) hanno il flag attivo."""
    rows = []
    ts = day if day else int(time.time())
    for i in range(n):
        r = {
            "ses": "s", "profile": "test", "req": "r", "kind": "chat",
            "grp": "g", "dep": dep, "model": "openai/gpt-x",
            "tries": 1, "fb": 1 if i < fb else 0, "dur_ms": 100,
            "stream": False, "qc": i < qc, "wd": wd, "ts": ts,
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "total_tokens": 150, "cost": 0.01, "cost_est": 0.02},
        }
        rows.append(r)
    return rows


def _write_segment(tmp_path, rows, name="usage_ledger.jsonl.1"):
    p = tmp_path / name
    with open(p, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return str(p)


def test_aggregate_segment_below_threshold_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ledger.LEDGER_SUMMARY_MIN_ROWS", 1000)
    led = Ledger(str(tmp_path))
    seg = _write_segment(tmp_path, _row(n=5))
    assert led._aggregate_segment(seg) is False
    assert not os.path.exists(led.summary_path)


def test_aggregate_segment_compresses(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ledger.LEDGER_SUMMARY_MIN_ROWS", 3)
    led = Ledger(str(tmp_path))
    old_day = int(time.time()) - 3 * 86400
    rows = (_row(n=5, fb=1) + _row(dep="d1", qc=2, n=2)
            + _row(wd="zero-answer", n=1, day=old_day))
    seg = _write_segment(tmp_path, rows)
    assert led._aggregate_segment(seg) is True
    os.remove(seg)                           # come fa _aggregate_oldest
    assert os.path.exists(led.summary_path)
    sm = led.iter_rows()
    assert len(sm) == 3                      # d0 oggi, d1 oggi, d0 3gg fa
    today = next(r for r in sm if r["dep"] == "d0" and r["count"] == 5)
    assert today["usage"]["prompt_tokens"] == 500
    assert today["usage"]["total_tokens"] == 750
    assert today["fb"] == 1
    assert today["qc"] == 0
    d1 = next(r for r in sm if r["dep"] == "d1")
    assert d1["count"] == 2 and d1["qc"] == 2
    old = next(r for r in sm if r["count"] == 1 and r["ts"] < today["ts"])
    assert old["wd_fail"] == 1
    assert old["model"] == "openai/gpt-x"


def test_aggregate_merges_into_existing_summary(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ledger.LEDGER_SUMMARY_MIN_ROWS", 3)
    led = Ledger(str(tmp_path))
    s1 = _write_segment(tmp_path, _row(n=4), "usage_ledger.jsonl.2")
    assert led._aggregate_segment(s1) is True
    os.remove(s1)
    s2 = _write_segment(tmp_path, _row(n=6), "usage_ledger.jsonl.1")
    assert led._aggregate_segment(s2) is True
    os.remove(s2)
    sm = [r for r in led.iter_rows() if r["dep"] == "d0"]
    assert len(sm) == 1
    assert sm[0]["count"] == 10              # 4 + 6 sullo stesso giorno
    assert sm[0]["usage"]["total_tokens"] == 1500


def test_iter_rows_reads_summary_then_recent(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ledger.LEDGER_SUMMARY_MIN_ROWS", 3)
    led = Ledger(str(tmp_path))
    led._aggregate_segment(_write_segment(tmp_path, _row(n=4),
                                          "usage_ledger.jsonl.2"))
    _write_segment(tmp_path, _row(dep="fresh", n=2), "usage_ledger.jsonl.1")
    led.record({"ses": "s", "profile": "test", "req": "r", "kind": "chat",
                "grp": "g", "dep": "live", "model": "m", "tries": 1, "fb": 0,
                "dur_ms": 5, "stream": False, "qc": False, "wd": None,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2}})
    led.flush()
    deps = [r["dep"] for r in led.iter_rows()]
    assert "d0" in deps and "fresh" in deps and "live" in deps
    assert deps[0] == "d0"                   # summary per primo


def test_insights_aggregate_handles_summary_rows():
    from app import admin as A
    rows = [{
        "count": 5, "ts": int(time.time()),
        "profile": "test", "model": "openai/gpt-x", "dep": "d0", "grp": "g",
        "kind": "chat", "dur_ms": 500, "fb": 2, "qc": 0, "wd_fail": 1,
        "wd_ok": 0, "usage": {"prompt_tokens": 500, "completion_tokens": 250,
                              "total_tokens": 750, "cost": 0.05, "cost_est": 0.1},
    }]
    agg = A._insights_aggregate(rows, "model")
    a = agg["openai/gpt-x"]
    assert a["calls"] == 5
    assert a["prompt_tokens"] == 500
    assert a["fallback_rate"] == 0.4         # 2/5
    assert a["wd_fail_rate"] == 0.2          # 1/5
    assert a["bad_rate"] == 0.6              # (2+1)/5


def test_endpoint_insights_with_summary(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ledger.LEDGER_SUMMARY_MIN_ROWS", 3)
    import app.main as m
    orig_key = m.authn.master_key
    m.authn.master_key = "test-master-ledger-summary"
    orig_ledger = m.LEDGER
    led = Ledger(str(tmp_path))
    monkeypatch.setattr(m, "LEDGER", led)
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    try:
        seg = _write_segment(tmp_path, _row(n=5, fb=1), "usage_ledger.jsonl.1")
        assert led._aggregate_segment(seg) is True
        os.remove(seg)                       # come fa _aggregate_oldest
        c = TestClient(m.app)
        j = c.get("/admin/insights?days=7&group_by=model",
                  headers={"Authorization": "Bearer test-master-ledger-summary"})
        assert j.status_code == 200
        a = j.json()["by_model"]["openai/gpt-x"]
        assert a["calls"] == 5
        assert a["cost_reported_usd"] == pytest.approx(0.05)
        assert a["cost_estimated_usd"] == pytest.approx(0.1)
        assert a["fallback_rate"] == 0.2     # 1 fb su 5
        assert a["bad_rate"] == 0.2
    finally:
        m.authn.master_key = orig_key
        monkeypatch.setattr(m, "LEDGER", orig_ledger)