"""GP-03: GET/PUT /admin/policy/raw — editor raw del gateway.yaml (full-replace).

SICUREZZA: l'endpoint usa `main.POLICY_PATH` / `main.VAR_DIR` (globali di
modulo). La fixture DEVE patchare ENTRAMBI su tmp, altrimenti un PUT nel test
sovrascrive il gateway.yaml di produzione.
"""
import os

os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")

import pytest
from fastapi.testclient import TestClient

from app import main as m

MK = {"Authorization": "Bearer test-master-policy-raw"}

VALID_YAML = (
    "step_up_pct: 25\n"
    "aliases:\n"
    "  fast: groq/llama-3.1-8b\n"
    "  smart: openai/gpt-4o\n"
)
VALID_YAML_2 = "step_up_pct: 40\naliases:\n  solo: groq/x\n"
INVALID_YAML = "step_up_pct: [questo, non, e', un, intero]\n:::::\n  - broken\n"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    pol = tmp_path / "gateway.yaml"
    pol.write_text(VALID_YAML)
    (tmp_path / "backups").mkdir()

    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-policy-raw"
    monkeypatch.setattr(m, "POLICY_PATH", pol)
    monkeypatch.setattr(m, "VAR_DIR", tmp_path)
    # riallinea la policy runtime al file tmp
    m.router.policy = m.Policy.load(pol)
    globals_pol = m.policy
    m.policy = m.router.policy
    assert str(m.POLICY_PATH).startswith(str(tmp_path))
    yield TestClient(m.app)
    m.authn.master_key = orig_mk
    m.policy = globals_pol
    m.router.policy = globals_pol


def test_get_raw(client):
    r = client.get("/admin/policy/raw", headers=MK)
    assert r.status_code == 200
    j = r.json()
    assert j["raw"] == VALID_YAML
    assert j["path"].endswith("gateway.yaml")


def test_get_raw_missing_file(client):
    m.POLICY_PATH.unlink()
    r = client.get("/admin/policy/raw", headers=MK)
    assert r.status_code == 200 and r.json()["raw"] == ""


def test_get_raw_requires_master(client):
    assert client.get("/admin/policy/raw").status_code == 401


def test_put_raw_valid_replaces_and_reloads(client):
    r = client.put("/admin/policy/raw", json={"raw": VALID_YAML_2}, headers=MK)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["validated"] and j["reloaded"]
    assert j["effective"]["step_up_pct"] == 40
    # file sostituito + runtime aggiornato
    assert m.POLICY_PATH.read_text() == VALID_YAML_2
    assert m.router.policy.step_up_pct == 40
    # backup del vecchio contenuto
    bks = list((m.VAR_DIR / "backups").glob("gateway.yaml-*.yaml"))
    assert bks and any("step_up_pct: 25" in b.read_text() for b in bks)
    # GET riflette il nuovo valore
    assert "step_up_pct: 40" in client.get("/admin/policy/raw", headers=MK).json()["raw"]


def test_put_raw_full_replace_not_merge(client):
    # VALID_YAML ha 2 alias; VALID_YAML_2 ne ha 1 -> il replace NON fonde
    client.put("/admin/policy/raw", json={"raw": VALID_YAML_2}, headers=MK)
    assert m.router.policy.aliases == {"solo": "groq/x"}


def test_put_raw_invalid_keeps_file(client):
    before = m.POLICY_PATH.read_text()
    r = client.put("/admin/policy/raw", json={"raw": INVALID_YAML}, headers=MK)
    assert r.status_code == 400
    assert "error" in r.json()
    assert m.POLICY_PATH.read_text() == before          # file INTATTO
    assert m.router.policy.step_up_pct == 25            # runtime INTATTO


def test_put_raw_missing_raw(client):
    assert client.put("/admin/policy/raw", json={}, headers=MK).status_code == 400
    assert client.put("/admin/policy/raw", json={"raw": 5},
                      headers=MK).status_code == 400


def test_put_raw_requires_master(client):
    assert client.put("/admin/policy/raw",
                      json={"raw": VALID_YAML_2}).status_code == 401
