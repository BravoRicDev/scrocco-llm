"""GP-04: GET /admin/backups + POST /admin/backups/restore.

SICUREZZA: la fixture patcha m.CSV_PATH / m.POLICY_PATH / m.VAR_DIR su tmp.
"""
import os

os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")

import pytest
from fastapi.testclient import TestClient

from app import main as m

MK = {"Authorization": "Bearer test-master-backups"}

CSV_HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
              "priority,scrocco-llm-test,caps")
CSV_A = CSV_HEADER + "\nt,model-a,groq,https://a/v1,free,1000,8000,5,K-A,\n"
CSV_B = (CSV_HEADER + "\nt,model-a,groq,https://a/v1,free,1000,8000,5,K-A,\n"
         "t,model-b,groq,https://b/v1,free,2000,8000,5,K-B,\n")
YAML_A = "step_up_pct: 25\naliases:\n  fast: groq/x\n"
YAML_B = "step_up_pct: 55\naliases:\n  fast: groq/x\n  slow: groq/y\n"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv_f = tmp_path / "keys_rotation.csv"
    csv_f.write_text(CSV_B)                     # live = B
    pol_f = tmp_path / "gateway.yaml"
    pol_f.write_text(YAML_B)                    # live = B
    bdir = tmp_path / "backups"
    bdir.mkdir()
    (bdir / "keys_rotation-20260101-000000.csv").write_text(CSV_A)
    (bdir / "gateway.yaml-20260101-000000.yaml").write_text(YAML_A)

    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-backups"
    monkeypatch.setattr(m, "CSV_PATH", csv_f)
    monkeypatch.setattr(m, "POLICY_PATH", pol_f)
    monkeypatch.setattr(m, "VAR_DIR", tmp_path)
    monkeypatch.setattr(m.config, "csv_path", csv_f)
    m.config.reload()
    orig_pol = m.policy
    m.router.policy = m.Policy.load(pol_f)
    m.policy = m.router.policy
    assert str(m.CSV_PATH).startswith(str(tmp_path))
    assert str(m.POLICY_PATH).startswith(str(tmp_path))
    yield TestClient(m.app)
    m.authn.master_key = orig_mk
    monkeypatch.setattr(m.config, "csv_path", m.CSV_PATH)
    m.config.reload()
    m.policy = orig_pol
    m.router.policy = orig_pol
    m.router._cooldown.clear()


def test_list_backups(client):
    j = client.get("/admin/backups", headers=MK).json()
    assert j["dir"].endswith("backups")
    assert [b["filename"] for b in j["csv"]] == ["keys_rotation-20260101-000000.csv"]
    assert [b["filename"] for b in j["yaml"]] == ["gateway.yaml-20260101-000000.yaml"]
    assert j["csv"][0]["size"] > 0 and isinstance(j["csv"][0]["mtime"], int)


def test_list_requires_master(client):
    assert client.get("/admin/backups").status_code == 401


def test_restore_csv(client):
    r = client.post("/admin/backups/restore",
                    json={"filename": "keys_rotation-20260101-000000.csv"},
                    headers=MK)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["restored"] == "keys_rotation-20260101-000000.csv"
    assert j["rows"] == 1                       # CSV_A ha 1 riga
    assert m.CSV_PATH.read_text() == CSV_A or "model-b" not in m.CSV_PATH.read_text()
    # _commit_csv ha messo l'attuale (B, 2 righe) in backup prima
    bks = list((m.VAR_DIR / "backups").glob("keys_rotation-*.csv"))
    assert any("model-b" in b.read_text() for b in bks)


def test_restore_yaml(client):
    r = client.post("/admin/backups/restore",
                    json={"filename": "gateway.yaml-20260101-000000.yaml"},
                    headers=MK)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["effective"]["step_up_pct"] == 25
    assert m.router.policy.step_up_pct == 25
    assert "step_up_pct: 25" in m.POLICY_PATH.read_text()
    bks = list((m.VAR_DIR / "backups").glob("gateway.yaml-*.yaml"))
    assert any("step_up_pct: 55" in b.read_text() for b in bks)   # l'attuale B


@pytest.mark.parametrize("bad", [
    "../keys_rotation.csv",
    "../../etc/passwd",
    "keys_rotation-20260101-000000.csv/../../gateway.yaml",
    "random.csv",
    "keys_rotation-does-not-exist.csv",
    "",
    "gateway.yaml",
])
def test_restore_path_traversal_and_missing(client, bad):
    r = client.post("/admin/backups/restore", json={"filename": bad}, headers=MK)
    assert r.status_code in (400, 404)
    assert "error" in r.json()
    # nulla e' stato toccato
    assert "model-b" in m.CSV_PATH.read_text()
    assert "step_up_pct: 55" in m.POLICY_PATH.read_text()


def test_restore_requires_master(client):
    assert client.post("/admin/backups/restore",
                       json={"filename": "keys_rotation-20260101-000000.csv"}
                       ).status_code == 401
