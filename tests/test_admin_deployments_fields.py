"""Admin API: POST /admin/deployments copre TUTTI i campi del CSV.

Verifica che i campi opzionali (api_style, model_preference, intelligence_score,
effort_capable, order, hold_until_finish) siano accettati e scritti, e che un
campo ignoto o un `api_style` non valido siano rifiutati con 400.

SICUREZZA: come test_admin_csv, la fixture patcha `main.CSV_PATH`/`main.VAR_DIR`
su tmp_path per non toccare mai il CSV di produzione.
"""
import csv
import os

os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")

import pytest
from fastapi.testclient import TestClient

from app import main as m

MK = {"Authorization": "Bearer test-master-dep-fields"}
HEADER = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
          "scrocco-llm-test,caps,effort_capable,intelligence_score,"
          "model_preference,media_defer,order,enabled,hold_until_finish,"
          "api_style,thinking_replay")
CSV_TEXT = HEADER + "\n"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv_file = tmp_path / "keys_rotation.csv"
    csv_file.write_text(CSV_TEXT)
    (tmp_path / "backups").mkdir()
    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-dep-fields"
    monkeypatch.setattr(m, "CSV_PATH", csv_file)
    monkeypatch.setattr(m, "VAR_DIR", tmp_path)
    monkeypatch.setattr(m.config, "csv_path", csv_file)
    m.config.reload()
    assert str(m.CSV_PATH).startswith(str(tmp_path))
    yield TestClient(m.app), csv_file
    m.authn.master_key = orig_mk
    monkeypatch.setattr(m.config, "csv_path", m.CSV_PATH)
    m.config.reload()
    m.router._cooldown.clear()


def _rows(csv_file):
    with open(csv_file, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_create_with_all_optional_fields(client):
    c, csv_file = client
    body = {
        "profile": "test", "modello": "muse-spark-1.2-contributor",
        "provider": "opencode-go", "endpoint": "https://opencode.ai/zen/go/v1",
        "data": "20", "context": 1000, "max_input": 0, "priority": 0,
        "caps": "", "effort_capable": False, "intelligence_score": 5,
        "model_preference": 50, "media_defer": False, "order": 20,
        "hold_until_finish": True, "api_style": "responses",
        "thinking_replay": "", "key": "sk-test-key-1234567890",
    }
    r = c.post("/admin/deployments", headers=MK, json=body)
    assert r.status_code == 200, r.text
    row = _rows(csv_file)[0]
    assert row["modello"] == "muse-spark-1.2-contributor"
    assert row["provider"] == "opencode-go"
    assert row["api_style"] == "responses"
    assert row["model_preference"] == "50"
    assert row["intelligence_score"] == "5"
    assert row["effort_capable"] == "false"
    assert row["order"] == "20"
    assert row["hold_until_finish"] == "true"
    assert row["scrocco-llm-test"] == "sk-test-key-1234567890"


def test_create_chat_style_and_preferred_model(client):
    c, csv_file = client
    body = {
        "profile": "test", "modello": "deepseek-v4.1-flash",
        "provider": "opencode-go", "endpoint": "https://opencode.ai/zen/go/v1",
        "data": "20", "context": 1000, "max_input": 0, "priority": 0,
        "intelligence_score": 10, "model_preference": 100,
        "hold_until_finish": True, "thinking_replay": True,
        "key": "sk-test-key-abcdefghij",
    }
    r = c.post("/admin/deployments", headers=MK, json=body)
    assert r.status_code == 200, r.text
    row = _rows(csv_file)[0]
    assert row["api_style"] in ("", "chat")
    assert row["model_preference"] == "100"
    assert row["thinking_replay"] == "true"


def test_unknown_field_rejected(client):
    c, _ = client
    body = {"profile": "test", "modello": "m", "endpoint": "https://x/v1",
            "data": "free", "context": 8, "key": "sk-test-key-12345678",
            "bogus": 1}
    r = c.post("/admin/deployments", headers=MK, json=body)
    assert r.status_code == 400
    assert "bogus" in r.text


def test_invalid_api_style_rejected(client):
    c, _ = client
    body = {"profile": "test", "modello": "m", "endpoint": "https://x/v1",
            "data": "free", "context": 8, "key": "sk-test-key-12345678",
            "api_style": "bogus"}
    r = c.post("/admin/deployments", headers=MK, json=body)
    assert r.status_code == 400
    assert "api_style" in r.text
