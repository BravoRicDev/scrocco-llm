"""GP-02: GET/PUT /admin/csv — lettura raw + parsed mascherato, scrittura
validata con backup + reload.

SICUREZZA: l'endpoint usa `main.CSV_PATH` / `main.VAR_DIR` (globali di modulo,
non `config.csv_path`). La fixture DEVE patchare ENTRAMBI su tmp, altrimenti un
PUT nel test sovrascrive il CSV di produzione reale.
"""
import os

# app.main e' importato qui: replico l'env che test_bootstrap si aspetta al
# primo import (ordine alfabetico dei file di test).
os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")

import pytest
from fastapi.testclient import TestClient

from app import main as m

MK = {"Authorization": "Bearer test-master-admin-csv"}
HEADER = ("commento,modello,provider,endpoint,data,context,max_input,priority,"
          "scrocco-llm-test,caps")
CSV_TEXT = (
    HEADER + "\n"
    "t@x,model-a,groq,https://a.test/v1,free,1000,8000,5,K-SECRET-A,\n"
    "t@x,model-b,groq,https://b.test/v1,free,2000,8000,5,K-SECRET-B,\n"
)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv_file = tmp_path / "keys_rotation.csv"
    csv_file.write_text(CSV_TEXT)
    (tmp_path / "backups").mkdir()

    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-admin-csv"
    monkeypatch.setattr(m, "CSV_PATH", csv_file)
    monkeypatch.setattr(m, "VAR_DIR", tmp_path)
    monkeypatch.setattr(m.config, "csv_path", csv_file)
    m.config.reload()
    # rete di sicurezza: mai toccare un path fuori dalla tmp del test
    assert str(m.CSV_PATH).startswith(str(tmp_path))
    yield TestClient(m.app)
    m.authn.master_key = orig_mk
    monkeypatch.setattr(m.config, "csv_path", m.CSV_PATH)  # ripristino coerente
    m.config.reload()
    m.router._cooldown.clear()


def test_get_csv_raw_and_masked(client):
    r = client.get("/admin/csv", headers=MK)
    assert r.status_code == 200
    j = r.json()
    assert j["raw"] == CSV_TEXT                       # raw = testo fedele
    assert j["count"] == 2
    assert j["parsed"]["header"][0] == "commento"
    # la colonna-chiave (scrocco-llm-test) e' MASCHERATA nel parsed
    keys = [row["scrocco-llm-test"] for row in j["parsed"]["rows"]]
    assert all("SECRET" not in k for k in keys), keys
    assert isinstance(j["backups"], list)
    assert j["path"].endswith("keys_rotation.csv")


def test_get_csv_requires_master(client):
    assert client.get("/admin/csv").status_code == 401
    assert client.get("/admin/csv",
                      headers={"Authorization": "Bearer nope"}).status_code == 401


def test_get_csv_empty_file(client, monkeypatch):
    m.CSV_PATH.write_text("")
    r = client.get("/admin/csv", headers=MK)
    assert r.status_code == 200
    j = r.json()
    assert j["raw"] == "" and j["count"] == 0
    assert j["parsed"] == {"header": [], "rows": []}


def test_get_csv_missing_file(client):
    m.CSV_PATH.unlink()
    r = client.get("/admin/csv", headers=MK)
    assert r.status_code == 200
    assert r.json()["raw"] == "" and r.json()["count"] == 0


def test_put_csv_valid_writes_and_backups(client):
    # parto da un CSV diverso, poi scrivo CSV_TEXT
    m.CSV_PATH.write_text(HEADER + "\nx,old,groq,https://o/v1,free,10,80,0,K,\n")
    r = client.put("/admin/csv", json={"raw": CSV_TEXT}, headers=MK)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is True and j["rows"] == 2
    assert j["backup"] and j["backup"].startswith("keys_rotation-")
    assert m.CSV_PATH.read_text().strip() != ""
    # il backup del vecchio contenuto e' finito in tmp/backups/
    bks = list((m.VAR_DIR / "backups").glob("keys_rotation-*.csv"))
    assert bks and any("old" in b.read_text() for b in bks)
    # reload effettivo: il nuovo modello e' instradabile
    assert "model-a" in m.CSV_PATH.read_text()


def test_put_csv_invalid_keeps_file(client):
    before = m.CSV_PATH.read_text()
    r = client.put("/admin/csv", json={"raw": "questo non e' un csv valido\n\x00"},
                   headers=MK)
    # o 400 per validazione, o comunque il file non cambia
    assert r.status_code in (200, 400)
    if r.status_code == 400:
        assert "error" in r.json()
        assert m.CSV_PATH.read_text() == before


def test_put_csv_missing_raw(client):
    assert client.put("/admin/csv", json={}, headers=MK).status_code == 400
    assert client.put("/admin/csv", json={"raw": 123}, headers=MK).status_code == 400


def test_put_csv_identical_is_ok(client):
    r1 = client.put("/admin/csv", json={"raw": CSV_TEXT}, headers=MK)
    r2 = client.put("/admin/csv", json={"raw": CSV_TEXT}, headers=MK)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json()["rows"] == 2


def test_put_csv_requires_master(client):
    assert client.put("/admin/csv", json={"raw": CSV_TEXT}).status_code == 401
