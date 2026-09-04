"""/admin/playground: simulatore di chat READ-ONLY.

Rigira UNA richiesta reale (canonicalize -> resolve_group_for_request ->
initial_pick -> fallback_next su errore) e riporta il TRACE di routing/
fallback, ma NON tocca lo stato del router di produzione:
niente mark_failed/note_start/note_end, niente scritture su _cooldown/
_stats/keyhealth e stato interno ripristinato a fine giro.
"""
import os

import pytest
from fastapi.testclient import TestClient

from app.forwarder import UpstreamError

# Questa suite e' la prima (ordine alfabetico) a importare app.main: replico
# l'env che test_bootstrap si aspetta al primo import, altrimenti
# authn.master_key resterebbe il default e quelle probe darebbero 401.
os.environ["GATEWAY_MASTER_KEY"] = "test-master-not-default"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,model-a,groq,https://a.test/v1,free,1000,8000,5,K-A,
t@x,model-b,groq,https://b.test/v1,free,2000,8000,5,K-B,
"""
BASE = "scrocco-llm-test"
MK = {"Authorization": "Bearer test-master-playground"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv = tmp_path / "k.csv"
    csv.write_text(CSV)
    import app.main as m
    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-playground"
    orig_csv = m.config.csv_path
    m.config.csv_path = csv
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    m.config.reload()
    yield TestClient(m.app), m
    m.config.csv_path = orig_csv
    m.config.reload()
    m.authn.master_key = orig_mk
    m.router._cooldown.clear()


def _uid(m, group, idx=0):
    return m.config.groups[group][idx]["unique"]


def _install_forwarder(monkeypatch, m, responses):
    """Sostituisce forwarder.call con una fake sequenziale: l'ultima risposta
    si ripete. (crumbs, record) -> list of unique chiamati."""
    calls = []

    async def fake(dep, payload, **kwargs):
        calls.append(dep["unique"])
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(m.forwarder, "call", fake)
    return calls


def test_playground_success_single_try(client, monkeypatch):
    c, m = client
    ok_resp = {"id": "chatcmpl-1",
               "choices": [{"message": {"role": "assistant",
                                        "content": "Ciao!"}}]}
    calls = _install_forwarder(monkeypatch, m, [ok_resp])
    r = c.post("/admin/playground", json={
        "model": "scrocco-llm-test",
        "messages": [{"role": "user", "content": "ciao"}]}, headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["attempts"] == 1 and body["fallbacks"] == 0
    assert body["resolved_model"] == "scrocco-llm-test"
    assert body["profile"] == "test"
    assert body["group"] == f"{BASE}-1000k"
    assert body["content"] == "Ciao!"
    assert body["used"] == {"unique": _uid(m, f"{BASE}-1000k"),
                            "group": f"{BASE}-1000k"}
    assert len(body["trace"]) == 1
    st = body["trace"][0]
    assert st["step"] == 1
    assert st["unique"] == _uid(m, f"{BASE}-1000k")
    assert st["group"] == f"{BASE}-1000k"
    assert st["profile"] == "test"
    assert st["reason"] is None
    assert st["verdict"] == "ok"
    assert calls == [_uid(m, f"{BASE}-1000k")]


def test_playground_fallback_one_failure(client, monkeypatch):
    c, m = client
    ok_resp = {"id": "chatcmpl-2",
               "choices": [{"message": {"role": "assistant",
                                        "content": "ok dal fallback"}}]}
    calls = _install_forwarder(monkeypatch, m,
                               [UpstreamError(503, "boom"), ok_resp])
    r = c.post("/admin/playground", json={
        "model": "scrocco-llm-test",
        "messages": [{"role": "user", "content": "ciao"}]}, headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["attempts"] == 2 and body["fallbacks"] == 1
    assert body["content"] == "ok dal fallback"
    assert body["used"]["unique"] == _uid(m, f"{BASE}-2000k")
    assert calls == [_uid(m, f"{BASE}-1000k"), _uid(m, f"{BASE}-2000k")]
    tr = body["trace"]
    assert len(tr) == 2
    assert tr[0]["verdict"] == "fail" and tr[0]["reason"] == "http_503"
    assert tr[0]["unique"] == _uid(m, f"{BASE}-1000k")
    assert tr[1]["verdict"] == "ok" and tr[1]["reason"] is None
    assert tr[1]["unique"] == _uid(m, f"{BASE}-2000k")


def test_playground_chain_exhausted(client, monkeypatch):
    c, m = client
    calls = _install_forwarder(monkeypatch, m, [UpstreamError(503, "boom")])
    r = c.post("/admin/playground", json={
        "model": "scrocco-llm-test",
        "messages": [{"role": "user", "content": "ciao"}]}, headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["attempts"] == 2 and body["fallbacks"] == 1
    assert "error" in body and body["error"]["message"]
    assert calls == [_uid(m, f"{BASE}-1000k"), _uid(m, f"{BASE}-2000k")]
    assert all(t["verdict"] == "fail" for t in body["trace"])
    assert [t["reason"] for t in body["trace"]] == ["http_503", "http_503"]


def test_playground_no_side_effects(client, monkeypatch):
    """Rete fallita: nessun cooldown/stats/keyhealth/sticky toccati."""
    c, m = client
    _install_forwarder(monkeypatch, m, [UpstreamError(503, "boom")])
    before_cd = dict(m.router._cooldown)
    before_stats = dict(m.router._stats)
    before_kh = dict(m.KEYHEALTH.data)
    before_sticky = dict(m.router._sticky)
    before_defer = dict(m.router.media_deferred)
    r = c.post("/admin/playground", json={
        "model": "scrocco-llm-test",
        "messages": [{"role": "user", "content": "ciao"}]}, headers=MK)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    # il deployment fallito NON è in cooldown, stats senza nuovi ingressi
    assert _uid(m, f"{BASE}-1000k") not in m.router._cooldown
    assert dict(m.router._cooldown) == before_cd
    assert dict(m.router._stats) == before_stats
    assert dict(m.KEYHEALTH.data) == before_kh
    assert dict(m.router._sticky) == before_sticky
    assert dict(m.router.media_deferred) == before_defer


def test_playground_requires_master(client):
    c, _ = client
    r = c.post("/admin/playground", json={
        "model": "scrocco-llm-test",
        "messages": [{"role": "user", "content": "ciao"}]})
    assert r.status_code == 401


def test_playground_unroutable_model(client, monkeypatch):
    c, m = client
    calls = _install_forwarder(monkeypatch, m, [{"id": "x", "choices": []}])
    r = c.post("/admin/playground", json={
        "model": "modello-sconosciuto",
        "messages": [{"role": "user", "content": "ciao"}]}, headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["attempts"] == 0 and body["fallbacks"] == 0
    assert body["trace"] == []
    assert calls == []                       # nessuna chiamata upstream


def test_playground_profile_max_tokens(client, monkeypatch):
    c, m = client
    seen = {}

    async def fake(dep, payload, **kwargs):
        seen["payload"] = payload
        return {"id": "chatcmpl-3",
                "choices": [{"message": {"role": "assistant",
                                         "content": "ok"}}]}

    monkeypatch.setattr(m.forwarder, "call", fake)
    r = c.post("/admin/playground", json={
        "model": "scrocco-llm-test",
        "messages": [{"role": "user", "content": "ciao"}],
        "profile": "test", "max_tokens": 7}, headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["profile"] == "test"
    assert seen["payload"]["max_tokens"] == 7
    assert seen["payload"]["model"] == "scrocco-llm-test"
    assert seen["payload"]["messages"] == [{"role": "user", "content": "ciao"}]