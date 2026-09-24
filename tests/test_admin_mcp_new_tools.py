"""FASE 5 — gli 11 tool MCP delle Fasi 2-4.

Verifica:
- `_mcp_tool_specs()` espone i 11 tool con `inputSchema` valido (59 totali);
- `tools/list` ritorna 59 tool;
- `tools/call` per ciascuno dei nuovi tool non da' errore JSON-RPC (handler
  raggiungibile, envelope MCP anche in caso di errore applicativo);
- `hosts_drain` senza `unique` -> errore dentro l'envelope;
- execute/call senza master -> 401.
"""
import os

import pytest
from fastapi.testclient import TestClient

from app import metrics
from app.admin import _mcp_known_names, _mcp_tool_specs

BASE = "scrocco-llm-test"
GROUP = f"{BASE}-500k"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
 t@x,model-a,groq,https://a.test/v1,free,500,8000,5,K-A,
 t@x,model-b,groq,https://b.test/v1,free,500,8000,5,K-B,
"""

MK = "test-master-f5m"
MKH = {"Authorization": f"Bearer {MK}"}

NEW_TOOLS = [
    "policy_schema_get", "warm_get", "keys_soft_get", "circuits_get",
    "warm_wake", "hosts_drain", "hosts_undrain", "metrics_reset",
    "scores_reset", "sessions_purge", "keys_leases_clear",
]


@pytest.fixture()
def client(monkeypatch, tmp_path):
    os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")
    csv = tmp_path / "k5.csv"
    csv.write_text(CSV)
    import app.main as m
    import app.admin as admin_mod

    async def fake_probe(http, dep, force, *, client_ip="", session=None,
                         attribution=None):
        return {"unique": dep["unique"], "ok": True, "latency_ms": 1,
                "status": 200}

    monkeypatch.setattr(admin_mod, "_probe_one", fake_probe)

    orig_mk = m.authn.master_key
    orig_csv = m.config.csv_path
    orig_var = m.VAR_DIR
    m.authn.master_key = MK
    m.config.csv_path = csv
    m.VAR_DIR = tmp_path
    m.config.reload()
    try:
        yield TestClient(m.app), m
    finally:
        m.router._drain().clear()
        m.router._cooldown.clear()
        metrics.reset()
        m.config.csv_path = orig_csv
        m.VAR_DIR = orig_var
        m.config.reload()
        m.authn.master_key = orig_mk


def _uid(m, key):
    for lst in m.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d["unique"]
    raise AssertionError(key)


def _rpc(c, method, params=None):
    return c.post("/admin/mcp/config/call", headers=MKH, json={
        "jsonrpc": "2.0", "id": 1, "method": method,
        "params": params or {}})


def test_specs_validi_e_conteggio(client):
    specs = _mcp_tool_specs()
    assert len(specs) == 59
    names = {t["name"] for t in specs}
    assert set(NEW_TOOLS) <= names
    for t in specs:
        assert t.get("name")
        assert t.get("description")
        schema = t.get("inputSchema")
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        assert isinstance(schema.get("properties"), dict)
    assert set(NEW_TOOLS) <= _mcp_known_names()


def test_tools_list_ritorna_59(client):
    c, _m = client
    body = _rpc(c, "tools/list").json()
    tools = body["result"]["tools"]
    assert len(tools) == 59
    assert set(NEW_TOOLS) <= {t["name"] for t in tools}


def test_tools_call_nuovi_tool_non_404(client):
    c, m = client
    uid = _uid(m, "K-A")
    args = {
        "policy_schema_get": {}, "warm_get": {}, "keys_soft_get": {},
        "circuits_get": {},
        "warm_wake": {"unique": uid},
        "hosts_drain": {"unique": uid},
        "hosts_undrain": {"unique": uid},
        "metrics_reset": {}, "scores_reset": {}, "sessions_purge": {},
        "keys_leases_clear": {"unique": uid},
    }
    for name in NEW_TOOLS:
        body = _rpc(c, "tools/call",
                    {"name": name, "arguments": args[name]}).json()
        assert "error" not in body, (name, body)
        assert "result" in body, (name, body)
        assert "isError" in body["result"], name


def test_hosts_drain_senza_unique_errore_in_envelope(client):
    c, _m = client
    body = _rpc(c, "tools/call",
                {"name": "hosts_drain", "arguments": {}}).json()
    assert "result" in body
    env = body["result"]
    assert env["isError"] is True
    assert "unique" in env["content"][0]["text"]


def test_execute_e_call_senza_master_401(client):
    c, _m = client
    r1 = c.post("/admin/mcp/config/execute",
                json={"tool": "policy_get", "arguments": {}})
    assert r1.status_code == 401
    r2 = c.post("/admin/mcp/config/call",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r2.status_code == 401
    r3 = c.post("/admin/mcp/config/execute",
                headers={"Authorization": "Bearer nope"},
                json={"tool": "policy_get", "arguments": {}})
    assert r3.status_code in (401, 403)
