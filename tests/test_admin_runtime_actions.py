"""FASE 4 — Azioni di runtime via API (additive, master-auth, journal).

Copre i 7 endpoint `/admin/*` che espongono azioni oggi solo CLI/watcher:
warm/wake, hosts/drain, hosts/undrain, metrics/reset, scores/reset,
sessions/purge, keys/leases/clear.

Punti chiave:
- master-only (401 senza master);
- nessun segreto (api_key) nelle risposte;
- drain esclude dal pick e undrain lo rende eleggibile MANTENENDOLO in config;
- purge copre anche `_sticky_dep` / `_session_deps` / `_dep_last_session`
  (gap di `release`, che azzera solo `_sticky`);
- journal registra le op.
"""
import json
import os
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

from app import journal, metrics
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"
GROUP = f"{BASE}-500k"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
 t@x,model-a,groq,https://a.test/v1,free,500,8000,5,K-A,
 t@x,model-b,groq,https://b.test/v1,free,500,8000,5,K-B,
"""

MKH = {"Authorization": "Bearer test-master-f4"}
PATHS = (
    "/admin/warm/wake", "/admin/hosts/drain", "/admin/hosts/undrain",
    "/admin/metrics/reset", "/admin/scores/reset", "/admin/sessions/purge",
    "/admin/keys/leases/clear",
)


@pytest.fixture()
def client(monkeypatch, tmp_path):
    os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")
    csv = tmp_path / "k4.csv"
    csv.write_text(CSV)
    import app.main as m
    orig_mk = m.authn.master_key
    orig_csv = m.config.csv_path
    orig_var = m.VAR_DIR
    m.authn.master_key = "test-master-f4"
    m.config.csv_path = csv
    m.VAR_DIR = tmp_path                       # journal isolato per test
    m.config.reload()
    m.router._cooldown.clear()
    # _key_soft/_key_hints possono restare popolati da altri test (es. p2
    # pressure/clear): vanno azzerati o il pick esclude il dep sbagliato.
    getattr(m.router, "_key_soft", {}).clear()
    getattr(m.router, "_key_hints", {}).clear()
    try:
        yield TestClient(m.app), m
    finally:
        for attr in ("_sticky", "_sticky_dep", "_session_group",
                     "_session_last_ok", "_session_deps", "_session_slow",
                     "_session_slow_timer", "_ctx_frontier", "_prefix_fp",
                     "_session_compact", "_sess_ratio", "_sess_floor",
                     "_session_rate", "_session_turns", "_probes_flight",
                     "_dep_last_session", "_base_scores", "_provider_scores",
                     "_key_scores", "_key_soft", "_key_hints"):
            d = getattr(m.router, attr, None)
            if isinstance(d, dict):
                d.clear()
        m.router._drain().clear()
        m.router._key_leases().clear()
        m.router._cooldown.clear()
        metrics.reset()
        m.VAR_DIR = orig_var
        m.config.csv_path = orig_csv
        m.config.reload()
        m.authn.master_key = orig_mk


def _dep(m, key):
    for lst in m.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d
    raise AssertionError(key)


def _uid(m, key):
    return _dep(m, key)["unique"]


# --------------------------------------------------------------- master-auth
def test_tutti_gli_endpoint_master_only(client):
    c, _m = client
    for path in PATHS:
        assert c.post(path).status_code == 401, path
        assert c.post(path, headers={"Authorization": "Bearer nope"},
                      json={}).status_code in (401, 403), path
        assert c.post(path, headers=MKH, json={}).status_code != 401, path


# ------------------------------------------------------------- metrics/reset
def test_metrics_reset_svuota_snapshot(client):
    c, _m = client
    metrics.inc("nx_test_f4_counter", ("x",))
    assert metrics.snapshot()
    before = metrics.render()
    assert "nx_uptime_seconds" in before
    out = c.post("/admin/metrics/reset", headers=MKH).json()
    assert out == {"ok": True, "reset": True}
    assert metrics.snapshot() == {}
    assert "nx_uptime_seconds" in metrics.render()   # uptime non toccato


# -------------------------------------------------------------- scores/reset
def test_scores_reset_filtrato_per_model(client):
    c, m = client
    r = m.router
    da, db = _dep(m, "K-A"), _dep(m, "K-B")
    r._base_scores = {da["unique"]: 1.0, db["unique"]: 2.0}
    r._provider_scores = {r._provider_key(da): 1.0, r._provider_key(db): 1.0}
    r._key_scores = {"K-A": 1.0, "K-B": 1.0}
    decay_before = r._scores_decay_ts
    out = c.post("/admin/scores/reset", headers=MKH,
                 json={"model": "model-a"}).json()
    assert out["ok"] is True and out["base"] == 1
    assert out["provider"] == 1 and out["key"] == 1
    assert da["unique"] not in r._base_scores
    assert db["unique"] in r._base_scores          # il non filtrato resta
    assert "K-B" in r._key_scores
    assert r._scores_decay_ts == decay_before      # decay invariato

    out2 = c.post("/admin/scores/reset", headers=MKH, json={}).json()
    assert out2["base"] >= 1
    assert r._base_scores == {} and r._provider_scores == {}
    assert r._key_scores == {}


# --------------------------------------------------------- sessions/purge
def test_sessions_purge_completa(client):
    c, m = client
    r = m.router
    uid = _uid(m, "K-A")
    now = time.time()
    r._sticky["S1"] = (GROUP, now)
    r._sticky["S2"] = (GROUP, now)
    r._sticky_dep["S1"] = (uid, now)
    r._session_group["S1"] = (GROUP, now)
    r._session_last_ok["S1"] = (uid, now)
    r._session_deps["S1"] = {uid}
    r._sess_ratio["S1"] = {"chars": 10, "pt": 5, "n": 1, "ts": now}
    r._sess_floor["S1"] = (7, now)
    r._sess_turns_map()["S1"] = {"n": 2, "ts": now}
    r._dep_last_session[uid] = ("S1", now)

    out = c.post("/admin/sessions/purge", headers=MKH,
                 json={"session_id": "S1"}).json()
    assert out["ok"] is True and out["session_id"] == "S1"
    assert r._sticky.get("S1") is None
    assert r._sticky_dep.get("S1") is None
    assert r._session_deps.get("S1") is None
    assert r._sess_ratio.get("S1") is None
    assert r._sess_floor.get("S1") is None
    assert r._session_turns.get("S1") is None
    assert uid not in r._dep_last_session           # gap di release coperto
    assert out["purged"]["_dep_last_session"] == 1
    assert r._sticky.get("S2") is not None          # altre sessioni intatte

    out_all = c.post("/admin/sessions/purge", headers=MKH, json={}).json()
    assert out_all["session_id"] is None
    assert r._sticky == {}
    assert r._dep_last_session == {}


# ------------------------------------------------------ keys/leases/clear
def test_leases_clear_senza_leak_e_404(client):
    c, m = client
    r = m.router
    da, db = _dep(m, "K-A"), _dep(m, "K-B")
    r._key_leases()["K-A"] = [("tok-a", time.time(), da["unique"])]

    out = c.post("/admin/keys/leases/clear", headers=MKH,
                 json={"unique": da["unique"]}).json()
    assert out == {"ok": True, "keys": 1, "leases": 1}
    text = json.dumps(out)
    assert "K-A" not in text and "K-B" not in text
    assert "K-A" not in r._key_leases()

    # unique valido senza lease -> 200 keys=0
    out2 = c.post("/admin/keys/leases/clear", headers=MKH,
                  json={"unique": db["unique"]}).json()
    assert out2 == {"ok": True, "keys": 0, "leases": 0}

    # unique inesistente -> 404
    assert c.post("/admin/keys/leases/clear", headers=MKH,
                  json={"unique": "non-esiste__x"}).status_code == 404

    # clear-all
    r._key_leases()["K-B"] = [("tok-b", time.time(), db["unique"])]
    out3 = c.post("/admin/keys/leases/clear", headers=MKH, json={}).json()
    assert out3["keys"] == 1 and r._key_leases() == {}


# -------------------------------------------------------- hosts/drain|undrain
def test_drain_undrain_idempotenti_e_config(client):
    c, m = client
    r = m.router
    uid = _uid(m, "K-A")

    r1 = c.post("/admin/hosts/drain", headers=MKH,
                json={"unique": uid, "inflight": 1}).json()
    assert r1["draining"] is True and r1.get("already") is None
    assert r.is_draining(uid)
    # idempotente
    r2 = c.post("/admin/hosts/drain", headers=MKH,
                json={"unique": uid, "inflight": 1}).json()
    assert r2["already"] is True
    # escluso dal pick
    picks = {r.pick_deployment(GROUP, need=frozenset({"text"}))["unique"]
             for _ in range(40)}
    assert uid not in picks
    # undrain: torna eleggibile e RESTA in config
    r3 = c.post("/admin/hosts/undrain", headers=MKH,
                json={"unique": uid}).json()
    assert r3["undrained"] is True and r3["already"] is False
    assert not r.is_draining(uid)
    assert m.config.deployment_by_unique(uid) is not None
    assert "model-a" in {r.pick_deployment(GROUP, need=frozenset({"text"}))
                         ["model"] for _ in range(40)}
    # undrain idempotente
    r4 = c.post("/admin/hosts/undrain", headers=MKH,
                json={"unique": uid}).json()
    assert r4["already"] is True

    assert c.post("/admin/hosts/drain", headers=MKH,
                  json={"unique": "non-esiste__x"}).status_code == 404
    assert c.post("/admin/hosts/drain", headers=MKH,
                  json={}).status_code == 400


def test_operator_drain_sopravvive_a_note_end(client, monkeypatch):
    """Regressione: un drain OPERATORE non deve sparire dalla config quando
    l'inflight va a zero (altrimenti undrain non troverebbe piu' l'entry)."""
    c, m = client
    r = m.router
    uid = _uid(m, "K-A")
    c.post("/admin/hosts/drain", headers=MKH,
           json={"unique": uid, "inflight": 1})
    assert m.config.deployment_by_unique(uid) is not None
    r.note_end(uid)                                # ultima richiesta in volo
    assert not r.is_draining(uid)
    assert m.config.deployment_by_unique(uid) is not None   # ancora in config
    r5 = c.post("/admin/hosts/undrain", headers=MKH,
                json={"unique": uid}).json()
    assert r5["already"] is True                    # nessun drain da annullare
    assert m.config.deployment_by_unique(uid) is not None


# ------------------------------------------------------------- warm/wake
def test_warm_wake_probe_e_clear_cooldown(client, monkeypatch):
    c, m = client
    import app.admin as admin_mod
    r = m.router
    uid = _uid(m, "K-A")
    r.mark_failed(uid, seconds=600, reason="http_429")
    assert r.is_cooled_down(uid)

    calls = []

    async def fake_probe(http, dep, force, *, client_ip="", session=None,
                         attribution=None):
        calls.append((dep["unique"], force))
        return {"unique": dep["unique"], "ok": True, "latency_ms": 5,
                "status": 200}

    monkeypatch.setattr(admin_mod, "_probe_one", fake_probe)
    out = c.post("/admin/warm/wake", headers=MKH,
                 json={"unique": uid}).json()
    assert out["ok"] is True and out["count"] == 1
    assert out["woken"][0]["ok"] is True
    assert calls == [(uid, True)]                   # force=True
    assert not r.is_cooled_down(uid)                # cooldown azzerato

    # validazione esplicita di min_age_sec (non piu' inghiottita)
    assert c.post("/admin/warm/wake", headers=MKH,
                  json={"group": GROUP, "min_age_sec": "abc"}
                  ).status_code == 400
    assert c.post("/admin/warm/wake", headers=MKH,
                  json={"unique": "non-esiste__x"}).status_code == 404
    assert c.post("/admin/warm/wake", headers=MKH,
                  json={"limit": 1}).status_code == 400


# ---------------------------------------------------------------- journal
def test_journal_registra_le_op(client, monkeypatch):
    c, m = client
    import app.admin as admin_mod
    r = m.router
    uid = _uid(m, "K-A")

    async def fake_probe(http, dep, force, *, client_ip="", session=None,
                         attribution=None):
        return {"unique": dep["unique"], "ok": False, "error_class": "x"}

    monkeypatch.setattr(admin_mod, "_probe_one", fake_probe)
    c.post("/admin/warm/wake", headers=MKH, json={"unique": uid})
    c.post("/admin/hosts/drain", headers=MKH,
           json={"unique": uid, "inflight": 0})
    c.post("/admin/hosts/undrain", headers=MKH, json={"unique": uid})
    c.post("/admin/metrics/reset", headers=MKH)
    c.post("/admin/scores/reset", headers=MKH, json={})
    c.post("/admin/sessions/purge", headers=MKH, json={})
    c.post("/admin/keys/leases/clear", headers=MKH, json={})

    ops = {e["op"] for e in journal.history(m.VAR_DIR, 50)["entries"]}
    for op in ("warm_wake", "hosts_drain", "hosts_undrain", "metrics_reset",
               "scores_reset", "sessions_purge", "keys_leases_clear"):
        assert op in ops, op
