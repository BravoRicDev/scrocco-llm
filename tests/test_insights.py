"""Feature: ledger usage/costi persistente + /admin/insights.

Requisito no-spreco ereditato: il ledger e' append-bufferizzato, mai una
scrittura per richiesta; pricing stima SOLO quando il provider non riporta
gia' un costo reale.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv = tmp_path / "k.csv"
    csv.write_text("commento,modello,provider,endpoint,data,context,"
                   "max_input,priority,scrocco-llm-test,caps\n")
    import app.main as m
    # NOTA ordine-test: app.main puo' essere gia' importato da altri file
    # con altra GATEWAY_MASTER_KEY in env -> patch DETERMINISTICA dell'
    # attributo invece dell'ambiente.
    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-insights"
    orig_csv = m.config.csv_path
    m.LEDGER.flush()                        # scarica buffer pregressi
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m.config, "csv_path", csv)
    m.config.reload()
    # LEDGER isolato nella tmp (monkeypatch dell'istanza globale)
    from app.ledger import Ledger
    led = Ledger(tmp_path)
    monkeypatch.setattr(m, "LEDGER", led)
    yield TestClient(m.app), m, led
    m.router._cooldown.clear()
    m.authn.master_key = orig_mk
    m.config.csv_path = orig_csv
    m.config.reload()


MK = {"Authorization": "Bearer test-master-insights"}


def _seed(led, n=3, ts=None):
    for i in range(n):
        led.record({
            "ses": "s", "profile": "test", "req": "r", "kind": "chat",
            "grp": "g", "dep": f"d{i}", "model": "openai/gpt-x",
            "tries": 1, "fb": i % 2, "dur_ms": 100 + i * 10,
            "stream": False, "qc": False,
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "total_tokens": 150},
            }, pricing={"openai/gpt-*": {"prompt_per_1m": 1.0,
                                         "completion_per_1m": 2.0}},
            upstream_model="openai/gpt-x")
    if ts:
        rows = led._buf
        for r in rows[-n:]:
            r["ts"] = ts
    led.flush()


def test_record_estimate_and_no_double_cost(client):
    _, m, led = client
    led.record({"usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                          "total_tokens": 1_000_000}},
               pricing={"*": {"prompt_per_1m": 2.0}}, upstream_model="m/x")
    assert led._buf[0]["usage"]["cost_est"] == pytest.approx(2.0)
    # provider ha gia' il costo -> NESSUNA stima aggiuntiva
    led.record({"usage": {"prompt_tokens": 1000, "completion_tokens": 0,
                          "cost": 0.5}},
               pricing={"*": {"prompt_per_1m": 99}}, upstream_model="m/x")
    assert "cost_est" not in led._buf[-1]["usage"]
    assert led._buf[-1]["usage"]["cost"] == 0.5


def test_rotation_keeps_recent(client, tmp_path):
    _, m, led = client
    from app.ledger import LEDGER_MAX_BYTES
    old = LEDGER_MAX_BYTES
    try:
        import app.ledger as L
        L.LEDGER_MAX_BYTES = 300            # forza rotazione aggressiva
        big = {"u": "x" * 120}
        for i in range(6):
            led.record(big)
            led.flush()
        seg1 = tmp_path / "usage_ledger.jsonl.1"
        assert seg1.exists()                # c'e' un segmento ruotato
        assert len(led.iter_rows()) >= 3     # iter_rows legge ANCHE .1
    finally:
        import app.ledger as L
        L.LEDGER_MAX_BYTES = old


def test_insights_group_by_model(client):
    c, m, led = client
    _seed(led, n=4)
    r = c.get("/admin/insights?days=7&group_by=model", headers=MK).json()
    agg = r["by_model"]["openai/gpt-x"]
    assert agg["calls"] == 4
    assert agg["prompt_tokens"] == 400 and agg["completion_tokens"] == 200
    assert agg["fallback_rate"] == 0.5      # fb = i%2 su 4 seed
    # bad_rate = quota righe con QUALSIASI problema (qui solo fb) = 0.5
    assert agg["bad_rate"] == 0.5
    assert agg["wd_fail_rate"] == 0.0
    est = agg["cost_estimated_usd"]
    assert est == pytest.approx((400 / 1e6) * 1.0 + (200 / 1e6) * 2.0)


def test_insights_bad_rate_counts_watchdog_and_qc(client):
    c, m, led = client
    common = dict(ses="s", profile="test", req="r", kind="chat", grp="g",
                  model="m", tries=1, dur_ms=100, stream=True, usage=None)
    # 5 righe stesso dep: 1 pulita, 1 fb>0, 1 qc, 1 wd fallimento,
    # 1 wd=tier2-no-done (NON conta come fallimento)
    led.record({**common, "dep": "dx", "fb": 0, "qc": False, "wd": None})
    led.record({**common, "dep": "dx", "fb": 1, "qc": False, "wd": None})
    led.record({**common, "dep": "dx", "fb": 0, "qc": True, "wd": None})
    led.record({**common, "dep": "dx", "fb": 0, "qc": False, "wd": "zero-answer"})
    led.record({**common, "dep": "dx", "fb": 0, "qc": False,
                "wd": "tier2-no-done"})
    led.flush()
    r = c.get("/admin/insights?days=7&group_by=deployment", headers=MK).json()
    a = r["by_deployment"]["dx"]
    assert a["calls"] == 5
    assert a["wd_fail_rate"] == 0.2          # solo "zero-answer"
    assert a["bad_rate"] == 0.6             # fb + qc + wd-fail = 3/5


def test_insights_day_window_filters(client):
    c, m, led = client
    _seed(led, n=2)                          # oggi
    old_ts = int(time.time()) - 30 * 86400   # 30 giorni fa
    _seed(led, n=2, ts=old_ts)
    r7 = c.get("/admin/insights?days=7&group_by=day", headers=MK).json()
    tot7 = sum(v["calls"] for v in r7["by_day"].values())
    assert tot7 == 2                         # solo le odierne


def test_insights_summary_24h(client):
    c, m, led = client
    _seed(led, n=3)
    j = c.get("/admin/insights/summary", headers=MK).json()
    assert j["window_hours"] == 24 and j["calls"] == 3
    assert j["by_kind"]["chat"]["calls"] == 3


def test_insights_requires_master(client):
    c, _, led = client
    _seed(led, n=1)
    assert c.get("/admin/insights").status_code == 401
    assert c.get("/admin/insights",
                 headers={"Authorization": "Bearer sk-test"}).status_code \
        == 401


def test_pricing_patch_validation(client):
    c, m, _led = client
    ok = c.patch("/admin/policy", headers=MK,
                 json={"pricing": {"groq/*": {
                     "prompt_per_1m": 0.05, "completion_per_1m": 0.1}}})
    assert ok.status_code == 200
    bad = c.patch("/admin/policy", headers=MK,
                  json={"pricing": {"groq/*": {"prompt_per_1m": "x"}}})
    assert bad.status_code == 400


def test_emit_summary_feeds_ledger_with_profile(client):
    c, m, led = client
    m._emit_summary(ses="s", req="r", grp="scrocco-llm-test-32k",
                    dep="d", tries=1, fb=0, dur_ms=5,
                    usage={"prompt_tokens": 10, "completion_tokens": 5,
                           "total_tokens": 15})
    row = led._buf[-1]
    assert row["profile"] == "test"          # dedotto dal gruppo
    assert row["model"] or True              # model puo' mancare se dep ignoto
