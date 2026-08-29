"""Compat endpoints: fix dei 404 osservati sui client OSS (Ollama / llama.cpp /
OpenAI SDK) che oggi chiamano path non gestiti dal gateway.

Ogni client vede solo i modello visibili (master => tutti, altrimenti
whitelist del profilo), coerente con /v1/models. Le route statiche
(/version, /props, /v1/props, /api/version) NON richiedono auth perche'
molti client le chiamano prima di avere configurato la chiave.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv = tmp_path / "k.csv"
    csv.write_text(
        "commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "seed,openai/gpt-4o-mini,openai,https://api.openai.com/v1,"
        "free,8,8000,1,sk-test-key,\n"
    )
    import app.main as m
    # Patch DETERMINISTICA dell'attributo (come test_insights): app.main puo'
    # essere gia' importato da altri test con altra GATEWAY_MASTER_KEY.
    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-compat"
    orig_csv = m.config.csv_path
    m.LEDGER.flush()
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m.config, "csv_path", csv)
    m.config.reload()
    from app.ledger import Ledger
    led = Ledger(tmp_path)
    monkeypatch.setattr(m, "LEDGER", led)
    yield TestClient(m.app), m, led
    m.router._cooldown.clear()
    m.authn.master_key = orig_mk
    m.config.csv_path = orig_csv
    m.config.reload()


MK = {"Authorization": "Bearer test-master-compat"}


def _real_unique(m) -> str:
    return next(d["unique"] for deps in m.config.groups.values() for d in deps)


def test_api_tags_with_auth(client):
    c, m, _ = client
    r = c.get("/api/tags", headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert "models" in body and isinstance(body["models"], list)
    assert len(body["models"]) > 0
    for item in body["models"]:
        assert "name" in item and "model" in item


def test_retrieve_real_model(client):
    c, m, _ = client
    uid = _real_unique(m)
    r = c.get(f"/v1/models/{uid}", headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "model"
    assert body["id"] == uid


def test_retrieve_base_and_group_names(client):
    """Regression: il NOME BASE (scrocco-llm-<profilo>) e i gruppi -Nk/-go...
    che il client usa davvero nelle chat devono risolvere 200, non solo gli
    univoci grp__model__idx."""
    c, m, _ = client
    prefix = m.config.proxy_prefix
    prof = m.config.profiles[0]
    r = c.get(f"/v1/models/{prefix}{prof}", headers=MK)
    assert r.status_code == 200 and r.json()["id"] == f"{prefix}{prof}"
    # un gruppo reale del profilo (se esiste un -Nk/-go/...)
    grp = next((g for g in m.config.groups if g.startswith(f"{prefix}{prof}-")),
               None)
    if grp:
        r2 = c.get(f"/v1/models/{grp}", headers=MK)
        assert r2.status_code == 200 and r2.json()["id"] == grp


def test_retrieve_fake_model_404(client):
    c, m, _ = client
    r = c.get("/v1/models/modello-inventato-xyz", headers=MK)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


def test_api_v1_models(client):
    c, m, _ = client
    r = c.get("/api/v1/models", headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list) and len(body["data"]) > 0


def test_api_show(client):
    c, m, _ = client
    uid = _real_unique(m)
    r = c.post("/api/show", headers=MK, json={"name": uid})
    assert r.status_code == 200
    body = r.json()
    assert "capabilities" in body


def test_version_no_auth(client):
    c, m, _ = client
    r = c.get("/version")
    assert r.status_code == 200
    assert r.json() == {"version": "0.2.0"}


def test_v1_props_no_auth(client):
    c, m, _ = client
    r = c.get("/v1/props")
    assert r.status_code == 200
    assert "total_slots" in r.json()


def test_api_tags_no_auth_401(client):
    c, m, _ = client
    r = c.get("/api/tags")
    assert r.status_code == 401
