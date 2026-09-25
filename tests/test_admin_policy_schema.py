"""Fase 2 — /admin/policy: schema, effective completo, deep-merge, validazione.

Fixture sul modello di tests/test_admin_policy_raw.py: POLICY_PATH e VAR_DIR
su tmp (mai il gateway.yaml di produzione) + master key di test.
"""
import os

os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")

import pytest
import yaml
from fastapi.testclient import TestClient

from app import main as m

MK = {"Authorization": "Bearer test-master-policy-schema"}

BASE_YAML = (
    "step_up_pct: 25\n"
    "warm_pool:\n"
    "  enabled: true\n"
    "  refill_enabled: true\n"
    "  slow_race_after_ms: 900\n"
    "aliases:\n"
    "  fast: groq/llama-3.1-8b\n"
)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    pol = tmp_path / "gateway.yaml"
    pol.write_text(BASE_YAML)
    (tmp_path / "backups").mkdir()

    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-policy-schema"
    monkeypatch.setattr(m, "POLICY_PATH", pol)
    monkeypatch.setattr(m, "VAR_DIR", tmp_path)
    m.router.policy = m.Policy.load(pol)
    globals_pol = m.policy
    m.policy = m.router.policy
    assert str(m.POLICY_PATH).startswith(str(tmp_path))
    yield TestClient(m.app)
    m.authn.master_key = orig_mk
    m.policy = globals_pol
    m.router.policy = globals_pol


def _raw() -> dict:
    return yaml.safe_load(m.POLICY_PATH.read_text()) or {}


def test_schema_endpoint(client):
    r = client.get("/admin/policy/schema", headers=MK)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["count"] == 394
    assert "warm_pool" in j["yaml_keys"]
    assert any(f["yaml_path"] == "warm_pool.refill_enabled" for f in j["fields"])


def test_schema_requires_master(client):
    assert client.get("/admin/policy/schema").status_code == 401


def test_policy_effective_complete_and_no_secrets(client):
    r = client.put("/admin/policy/raw",
                   json={"raw": "aliases:\n  a: groq/x\nalias_keys:\n"
                                "  a: SUPERSECRET\nstep_up_pct: 25\n"},
                   headers=MK)
    assert r.status_code == 200, r.text
    r = client.get("/admin/policy", headers=MK)
    assert r.status_code == 200
    eff = r.json()["effective"]
    assert eff["step_up_pct"] == 25
    assert "warm_refill_enabled" in eff          # campo asdict, non storico
    assert "hunt_backoff_sec" in eff
    assert "alias_keys" not in eff
    assert "client_keys" not in eff
    assert "SUPERSECRET" not in r.text
    assert eff["alias_keys_masked"]["a"] != "SUPERSECRET"


def test_tuning_policy_effective_is_complete(client):
    r = client.get("/admin/tuning", headers=MK)
    assert r.status_code == 200, r.text
    pe = r.json()["policy_effective"]
    assert "warm_refill_enabled" in pe
    assert "hunt_backoff_sec" in pe
    assert pe["step_up_pct"] == 25


def test_patch_deep_merge_preserves_siblings(client):
    r = client.patch("/admin/policy",
                     json={"warm_pool": {"slow_race_after_ms": 1234}},
                     headers=MK)
    assert r.status_code == 200, r.text
    raw = _raw()
    assert raw["warm_pool"]["enabled"] is True
    assert raw["warm_pool"]["refill_enabled"] is True
    assert raw["warm_pool"]["slow_race_after_ms"] == 1234
    assert m.router.policy.stream_slow_race_after_ms == 1234


def test_patch_flat_dual_key_ok(client):
    r = client.patch("/admin/policy", json={"warm_borrow_enabled": False},
                     headers=MK)
    assert r.status_code == 200, r.text
    assert m.router.policy.warm_borrow_enabled is False


def test_patch_unknown_key_400(client):
    r = client.patch("/admin/policy", json={"bogus_key": 1}, headers=MK)
    assert r.status_code == 400
    assert "bogus_key" in r.text
    assert _raw().get("bogus_key") is None


def test_patch_unknown_allowed_query(client):
    r = client.patch("/admin/policy?allow_unknown=1",
                     json={"bogus_key": 1, "allow_unknown": True}, headers=MK)
    assert r.status_code == 200, r.text
    raw = _raw()
    assert "allow_unknown" not in raw            # rimosso prima del merge


def test_put_unknown_key_400(client):
    before = m.POLICY_PATH.read_text()
    r = client.put("/admin/policy/raw",
                   json={"raw": "step_up_pct: 25\nbogus_key: 1\n"}, headers=MK)
    assert r.status_code == 400
    assert m.POLICY_PATH.read_text() == before


def test_put_unknown_allowed(client):
    r = client.put("/admin/policy/raw",
                   json={"raw": "step_up_pct: 25\nbogus_key: 1\n",
                         "allow_unknown": True}, headers=MK)
    assert r.status_code == 200, r.text


def test_put_non_map_yaml_400(client):
    r = client.put("/admin/policy/raw", json={"raw": "- a\n- b\n"}, headers=MK)
    assert r.status_code == 400


def test_aliases_replaced_not_merged(client):
    r = client.patch("/admin/policy", json={"aliases": {"only": "groq/z"}},
                     headers=MK)
    assert r.status_code == 200, r.text
    assert _raw()["aliases"] == {"only": "groq/z"}


def test_alias_keys_empty_value_deletes(client):
    client.put("/admin/policy/raw",
               json={"raw": "aliases:\n  a: groq/x\nalias_keys:\n  a: sec\n"},
               headers=MK)
    r = client.patch("/admin/policy", json={"alias_keys": {"a": ""}}, headers=MK)
    assert r.status_code == 200, r.text
    assert "a" not in (_raw().get("alias_keys") or {})
