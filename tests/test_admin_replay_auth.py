"""FASE 5 — /admin/replay e' master-only (GET e DELETE).

Regressione:
- GET senza master -> 401; con master -> 200;
- DELETE senza master -> 401 E buffer NON azzerato; con master -> 200 e buffer
  svuotato (prima il DELETE anonimo era un no-op latente ma distruttivo);
- /admin/replay resta `include_in_schema=False` (assente da OpenAPI).
"""
import os

import pytest
from fastapi.testclient import TestClient

from app.observability import add_replay_entry, replay_buffer

MKH = {"Authorization": "Bearer test-master-f5"}
MK = "test-master-f5"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")
    import app.main as m
    orig_mk = m.authn.master_key
    m.authn.master_key = MK
    replay_buffer.clear()
    try:
        yield TestClient(m.app), m
    finally:
        replay_buffer.clear()
        m.authn.master_key = orig_mk


def _seed():
    add_replay_entry("t-1", {"path": "/v1/chat/completions"}, {"ok": True})


def test_get_replay_master_only(client):
    c, _m = client
    _seed()
    assert c.get("/admin/replay").status_code == 401
    assert c.get("/admin/replay",
                 headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.get("/admin/replay", headers=MKH)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["entries"][0]["trace_id"] == "t-1"


def test_delete_replay_master_only_e_buffer_intatto(client):
    c, _m = client
    _seed()
    r = c.delete("/admin/replay")
    assert r.status_code == 401
    assert len(replay_buffer.get_recent()) == 1        # non azzerato
    r2 = c.delete("/admin/replay", headers=MKH)
    assert r2.status_code == 200
    assert len(replay_buffer.get_recent()) == 0        # azzerato solo col master


def test_replay_assente_da_openapi(client):
    c, m = client
    paths = m.app.openapi().get("paths", {})
    assert "/admin/replay" not in paths
