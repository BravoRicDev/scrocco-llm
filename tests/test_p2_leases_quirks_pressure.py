"""P2-8 lease di concorrenza per chiave, P2-9 registro quirk locale,
P2-10 penalty inspector + clear.

Tutto OPT-IN / dichiarativo: con policy di default il comportamento non
cambia (lease disabilitate -> nessun filtro; quirks vuoti -> nessuna
applicazione).
"""
import os
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,order
t@x.com,m/nx2-a,groq,https://a.test/v1,free,32,32000,5,K-A,,5
t@x.com,m/nx2-b,groq,https://b.test/v1,free,32,32000,5,K-B,,5
t@x.com,m/nx2-quirk-1,groq,https://q.test/v1,free,128,128000,5,K-Q,,5
t@x.com,m/nx2-quirk-1,groq,https://q.test/v1,free,128,128000,5,K-Q2,,5
"""


@pytest.fixture()
def mk(request):
    def _mk(**pol):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as f:
            f.write(CSV)
        request.addfinalizer(lambda: os.path.exists(path) and os.unlink(path))
        r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                   Policy.from_dict(pol))
        return r
    return _mk


def _dep(r, key):
    for lst in r.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d
    raise AssertionError(key)


# ---------------------------------------------------------- P2-8 lease
def test_lease_off_di_default(mk):
    r = mk()
    a = _dep(r, "K-A")
    assert r.key_lease_acquire(a) is None          # opt-in: nessuna lease
    assert r.key_inflight(a) == 0
    assert r._lease_filter([a]) == [a]


def test_lease_cap_e_filtro_soft(mk):
    r = mk(key_concurrency_enabled=True, key_concurrency_max=1)
    a, b = _dep(r, "K-A"), _dep(r, "K-B")
    tok = r.key_lease_acquire(a)
    assert tok is not None
    assert r.key_inflight(a) == 1
    assert r.key_lease_acquire(a) is None          # cap raggiunto
    # soft: la chiave satura viene deprioritizzata, non eliminata
    assert r._lease_filter([a, b]) == [b]
    assert r._lease_filter([a]) == [a]             # mai lista vuota
    r.key_lease_release(tok)
    assert r.key_inflight(a) == 0
    assert r._lease_filter([a, b]) == [a, b]


def test_lease_scaduta_non_satura(mk):
    r = mk(key_concurrency_enabled=True, key_concurrency_max=1,
           key_concurrency_lease_max_age_sec=10)
    a = _dep(r, "K-A")
    r.key_lease_acquire(a)
    r._key_leases()[a["api_key"]][0] = (
        r._key_leases()[a["api_key"]][0][0],
        time.time() - 999, a["unique"])
    assert r.key_lease_acquire(a) is not None      # prune -> riammessa


def test_walk_chain_salta_la_chiave_satura(mk):
    r = mk(key_concurrency_enabled=True, key_concurrency_max=1)
    a, b = _dep(r, "K-A"), _dep(r, "K-B")
    chain = [a["unique"], b["unique"]]
    r.key_lease_acquire(a)
    nxt = r._walk_chain(chain, None)
    assert nxt is not None and nxt["unique"] == b["unique"]


# ---------------------------------------------------------- P2-9 quirk
def test_quirks_default_vuoti(mk):
    r = mk()
    assert r.quirks_view() == {"declared": 0, "applied": 0, "by_model": {}}
    assert not _dep(r, "K-Q").get("strip_reasoning")


def test_quirks_applicati_in_memoria(mk):
    r = mk(quirks=[{"model": "*nx2-quirk*", "flag": "strip_reasoning",
                    "severity": "blocker", "note": "provider rifiuta rc"}])
    q1, q2 = _dep(r, "K-Q"), _dep(r, "K-Q2")
    assert q1.get("strip_reasoning") is True
    assert q2.get("strip_reasoning") is True
    assert not _dep(r, "K-A").get("strip_reasoning")
    v = r.quirks_view()
    assert v["declared"] == 1 and v["applied"] == 2
    assert v["by_model"] == {"*nx2-quirk*": 2}


def test_quirks_flag_sconosciuto_ignorato(mk):
    r = mk(quirks=[{"model": "*nx2-a*", "flag": "non_esiste"}])
    assert not _dep(r, "K-A").get("non_esiste")
    assert r.quirks_view()["applied"] == 0


def test_quirks_riapplicati_dopo_reload(mk):
    r = mk(quirks=[{"model": "*nx2-nuovo*", "flag": "no_thinking"}])
    with open(r.config.csv_path, "a") as f:
        f.write("t@x.com,m/nx2-nuovo,groq,https://n.test/v1,free,32,"
                "32000,5,K-N,,5\n")
    r.config.reload()
    assert not _dep(r, "K-N").get("no_thinking")
    r.apply_quirks()
    assert _dep(r, "K-N").get("no_thinking") is True


def test_quirks_parsing_policy_errori():
    with pytest.raises(ValueError):
        Policy.from_dict({"quirks": [{"model": "*x*"}]})
    with pytest.raises(ValueError):
        Policy.from_dict({"quirks": "nope"})
    p = Policy.from_dict({"quirks": [{"model": "*X*", "flag": "No_Thinking",
                                      "severity": "BLOCKER"}]})
    assert p.quirks[0]["model"] == "*x*"
    assert p.quirks[0]["flag"] == "no_thinking"
    assert p.quirks[0]["severity"] == "blocker"


# ------------------------------------------------------- P2-10 pressure
def test_pressure_view_e_clear_per_modello(mk):
    r = mk()
    a, b, q = _dep(r, "K-A"), _dep(r, "K-B"), _dep(r, "K-Q")
    r.mark_failed(a["unique"], seconds=600, reason="http_429")
    r.mark_failed(b["unique"], seconds=1200, reason="model_unhealthy")
    r.mark_failed(q["unique"], seconds=1200, reason="model_unhealthy")
    r.quarantine_endpoint("a.test", 600)
    r.note_repair_exempt(a["unique"])
    v = r.pressure_view()
    assert v["cooldowns_total"] == 3
    # ordinati per residuo decrescente
    rem = [c["remaining_sec"] for c in v["cooldowns"]]
    assert rem == sorted(rem, reverse=True)
    assert v["model_bench"] == {"m/nx2-quirk-1": 1, "m/nx2-b": 1}
    assert "a.test" in v["endpoint_quarantine"]
    assert v["repair_exempt"].get(a["unique"]) == 1
    import json
    assert json.dumps(v, default=str)         # serializzabile per l'API
    out = r.clear_pressure(model="m/nx2-b")
    assert out["ok"] and out["count"] == 1
    assert not r.is_cooled_down(b["unique"])
    assert r.is_cooled_down(a["unique"])
    r.clear_pressure()
    assert not r.is_cooled_down(a["unique"])
    assert r.pressure_view()["cooldowns_total"] == 0
    assert r.pressure_view()["key_leases"] == {}


# ---------------------------------------------------- endpoint HTTP admin
CSV_HTTP = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,model-a,groq,https://a.test/v1,free,500,8000,5,K-A,
"""
MKH = {"Authorization": "Bearer test-master-p2"}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")
    csv = tmp_path / "k2.csv"
    csv.write_text(CSV_HTTP)
    import app.main as m
    orig_mk = m.authn.master_key
    orig_csv = m.config.csv_path
    m.authn.master_key = "test-master-p2"
    m.config.csv_path = csv
    m.config.reload()
    m.router._cooldown.clear()
    yield TestClient(m.app), m
    m.config.csv_path = orig_csv
    m.config.reload()
    m.authn.master_key = orig_mk
    m.router._cooldown.clear()
    m.router._key_leases().clear()


def test_admin_pressure_clear_e_state(client):
    c, m = client
    uid = m.config.groups[f"{BASE}-500k"][0]["unique"]
    m.router.mark_failed(uid, seconds=600, reason="http_429")
    st = c.get("/admin/state", headers=MKH).json()
    ad = st["adaptive"]
    assert ad["pressure"]["cooldowns_total"] >= 1
    assert "key_leases" in ad and "quirks" in ad and "degraded" in ad
    insp = c.post("/admin/pressure/inspect", headers=MKH,
                  json={"limit": 5}).json()
    assert insp["cooldowns_total"] >= 1
    r = c.post("/admin/pressure/clear", headers=MKH, json={}).json()
    assert r["ok"] and r["count"] >= 1
    assert c.post("/admin/pressure/clear",
                  headers={"Authorization": "Bearer sbagliata"},
                  json={}).status_code in (401, 403)
