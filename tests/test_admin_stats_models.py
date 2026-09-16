"""GET /admin/stats/models: ok/fail per MODELLO dal ledger + costi.

Bug storico: la classifica sovrascriveva ok/fail del modello con i contatori
runtime di UN SOLO deployment (l'ultimo visitato) e, se ok==0, ripiegava su
`calls` -> success_rate 100% anche per un modello che aveva fallito tutto.
Inoltre non emetteva i costi (colonna sempre "-" nella TUI).
"""
import os
import time

os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")

import pytest
from fastapi.testclient import TestClient

from app import main as m

MK = {"Authorization": "Bearer test-master-stats"}

CSV = ("commento,modello,provider,endpoint,data,context,max_input,"
       "priority,scrocco-llm-test,caps\n"
       "t,model-a,groq,https://a/v1,free,1000,8000,5,K-A,\n")

ROWS = [
    {"ts": time.time(), "model": "model-a", "dur_ms": 100,
     "usage": {"prompt_tokens": 10, "completion_tokens": 5,
               "total_tokens": 15, "cost": 0.5, "cost_est": 0.25}},
    {"ts": time.time(), "model": "model-a", "dur_ms": 300, "qc": True,
     "usage": {"total_tokens": 15}},
    {"ts": time.time(), "model": "model-b", "dur_ms": 200, "wd": "zero-answer",
     "usage": {"total_tokens": 7}},
]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv_f = tmp_path / "keys_rotation.csv"
    csv_f.write_text(CSV)
    pol_f = tmp_path / "gateway.yaml"
    pol_f.write_text("")
    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-stats"
    monkeypatch.setattr(m, "CSV_PATH", csv_f)
    monkeypatch.setattr(m, "POLICY_PATH", pol_f)
    monkeypatch.setattr(m, "VAR_DIR", tmp_path)
    monkeypatch.setattr(m.config, "csv_path", csv_f)
    m.config.reload()
    yield TestClient(m.app)
    m.authn.master_key = orig_mk
    monkeypatch.setattr(m.config, "csv_path", m.CSV_PATH)
    m.config.reload()


def test_ok_fail_dal_ledger_e_costi(client, monkeypatch):
    async def _rows():
        return list(ROWS)

    monkeypatch.setattr(m.LEDGER, "iter_rows_async", _rows)
    r = client.get("/admin/stats/models", headers=MK)
    assert r.status_code == 200, r.text
    by = {x["model"]: x for x in r.json()["ranking"]}
    a = by["model-a"]
    assert (a["calls"], a["ok"], a["fail"]) == (2, 1, 1)
    assert a["success_rate_percent"] == 50.0
    assert a["cost_reported_usd"] == 0.5
    assert a["cost_estimated_usd"] == 0.25
    # tutti i tentativi falliti NON devono diventare 100% (era il fallback
    # `ok = stats.get("ok", 0) or calls`)
    b = by["model-b"]
    assert (b["calls"], b["ok"], b["fail"]) == (1, 0, 1)
    assert b["success_rate_percent"] == 0.0
