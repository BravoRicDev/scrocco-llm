"""Audit /models: report dei modelli disponibili NON configurati (extra_models)
e della euristica free/zen, oltre ai missing_models gia' esistenti.

GET /models e' zero-token: qui il client httpx e' FAKE, nessuna rete reale.
"""
import os

os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")

import pytest
from fastapi.testclient import TestClient

from app import admin as admin_mod
from app import main as m
from app.admin import _free_guess

MK = {"Authorization": "Bearer test-master-audit"}
HEADER = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
          "scrocco-llm-test,caps")
CSV_TEXT = (
    HEADER + "\n"
    "t@x,model-a,FastProv,https://a.test/v1,free,128,8000,5,K-SECRET-A,\n"
)

MODELS = {"data": [
    {"id": "model-a"},
    {"id": "brand-new-free",
     "pricing": {"prompt": "0", "completion": "0"}, "context_length": 128000},
    {"id": "paid-model",
     "pricing": {"prompt": "0.1", "completion": "0.2"}},
]}


class _Resp:
    def __init__(self, data, status=200):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):     # noqa: ARG002
        return _Resp(MODELS)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv_file = tmp_path / "keys_rotation.csv"
    csv_file.write_text(CSV_TEXT)
    (tmp_path / "backups").mkdir()

    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-audit"
    monkeypatch.setattr(m, "CSV_PATH", csv_file)
    monkeypatch.setattr(m, "VAR_DIR", tmp_path)
    monkeypatch.setattr(m.config, "csv_path", csv_file)
    m.config.reload()
    monkeypatch.setattr(admin_mod.httpx, "AsyncClient", _FakeClient)
    assert str(m.CSV_PATH).startswith(str(tmp_path))
    yield TestClient(m.app)
    m.authn.master_key = orig_mk
    m.config.reload()


def test_free_guess_heuristics():
    assert _free_guess("opencode/zen-x", {}, "https://opencode.ai/zen/v1")
    assert _free_guess("foo:free", {}, "https://x.example/v1")
    assert _free_guess("m", {"pricing": {"prompt": "0", "completion": "0"}},
                       "https://openrouter.ai/api/v1")
    assert not _free_guess("m", {"pricing": {"prompt": "0.1",
                                             "completion": "0"}},
                           "https://openrouter.ai/api/v1")
    assert not _free_guess("m", {}, "https://x.example/v1")


def test_audit_reports_extra_models(client):
    r = client.post("/admin/capabilities/audit", headers=MK)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["missing_models"] == []
    assert j["extra_models_count"] == 2
    assert j["extra_free_models_count"] == 1
    free = j["extra_free_models"][0]
    assert free["model"] == "brand-new-free"
    assert free["free_guess"] is True
    assert free["context_length"] == 128000
    extra_ids = {e["model"] for e in j["extra_models"]}
    assert extra_ids == {"brand-new-free", "paid-model"}


def test_audit_requires_master(client):
    assert client.post("/admin/capabilities/audit").status_code == 401
