"""FASE 3 — Viste stato runtime (additive, master-auth).

Copre i metodi `*_view()` (unit sul Router, anche "nudo" via __new__) e la
loro esposizione HTTP read-only (`/admin/warm`, `/admin/keys/soft`,
`/admin/circuits`) oltre all'arricchimento additivo di `/admin/state`,
`/admin/sessions` e `/admin/sessions/{id}`.

Punti chiave:
- nessun segreto in chiaro (api_key sempre come tag sha256[:12]);
- JSON-serializzabile (niente `set` esposti);
- read-only (nessuna mutazione delle mappe di stato);
- robustezza su mappe assenti / malformate.
"""
import json
import os
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
 t@x.com,m-a,groq,https://a.test/v1,free,64,8000,0,K-A,text
 t@x.com,m-b,groq,https://b.test/v1,free,64,4000,0,K-B,text
 t@x.com,mgo,groq,https://g.test/v1,,64,8000,0,K-G,text
 t@x.com,m-200,groq,https://c.test/v1,free,200,200000,0,K-200,text
"""


@pytest.fixture()
def mk():
    def _mk(**pol):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w") as f:
            f.write(CSV)
        r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                   Policy.from_dict(pol))
        r._tmp_path = path
        return r
    return _mk


def _dep(r, key):
    for lst in r.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d
    raise AssertionError(key)


def _u(r, key):
    return _dep(r, key)["unique"]


# ------------------------------------------------------------ naked Router
def _naked():
    from types import SimpleNamespace
    r = Router.__new__(Router)
    r.policy = SimpleNamespace(
        go_stick_ttl_sec=600, rate_hint_ttl_sec=20.0,
        warm_pool_enabled=True, warm_borrow_enabled=True,
        warm_borrow_idle_sec=240.0, model_circuit_enabled=True,
        model_circuit_open_sec=60, model_circuit_window_sec=60,
        model_circuit_keys=3, circuit_breaker_threshold=5,
        circuit_breaker_timeout=60.0, circuit_breaker_half_open_requests=3,
        provider_alternation_enabled=True)
    r.config = SimpleNamespace(go_suffix="-go", fallback_suffix="-fallback",
                               deployment_by_unique=lambda u: None)
    return r


def test_views_non_sollevano_su_router_nudo():
    r = _naked()
    outs = [
        r.slow_timer_view(), r.go_refund_view(), r.last_go_view(),
        r.ctx_frontier_view(), r.prefix_fp_view(), r.session_dep_guard_view(),
        r.warm_pool_view(), r.key_soft_view(), r.model_circuits_view(),
        r.circuit_breakers_view(), r.provider_alternation_view(),
        r.probes_in_flight_view(), r.sess_est_view(),
    ]
    for o in outs:
        json.dumps(o)                    # JSON-serializzabile, niente set


# ------------------------------------------------------------- slow timer
def test_slow_timer_view_ttl(mk):
    r = mk()
    now = time.time()
    r._sess_slow_timer()["S-A"] = {
        "fresh": now - 10,
        "stale": now - 99999,
    }
    out = r.slow_timer_view(now=now)
    by_u = {x["unique"]: x for x in out}
    assert set(by_u) == {"fresh"}
    assert by_u["fresh"]["session_id"] == "S-A"
    assert by_u["fresh"]["ttl_left_sec"] > 0


def test_slow_timer_view_mappa_malformata(mk):
    r = mk()
    r._session_slow_timer = {"S-A": {"u1": "non-numero", "u2": time.time()},
                             "S-B": "non-dict"}
    out = r.slow_timer_view()
    assert [x["unique"] for x in out] == ["u2"]


# --------------------------------------------------------- go_refund / -go
def test_go_refund_view_e_status(mk):
    r = mk()
    now = time.time()
    r._sess_turns_map()["S-A"] = {"n": 3, "go_until": 5, "ts": now}
    out = r.go_refund_view(now=now)
    assert len(out) == 1
    row = out[0]
    assert row["session_id"] == "S-A" and row["turns"] == 3
    assert row["active"] is True and row["refund_left"] == 2
    # la vista non muta la mappa
    assert r._sess_turns_map()["S-A"]["n"] == 3
    # go_refund_status usa la chiave `session` (non `session_id`)
    st = r.go_refund_status("S-A")
    assert st["session"] == "S-A" and st["go_until"] == 5


def test_last_go_view_read_only(mk):
    r = mk()
    now = time.time()
    go = _u(r, "K-G")
    r._last_go_map()["S-A"] = (go, now - 10)
    r._last_go_map()["S-OLD"] = (go, now - 99999)
    out = r.last_go_view(now=now)
    by_sid = {x["session_id"]: x for x in out}
    assert by_sid["S-A"]["valid"] is True
    assert by_sid["S-OLD"]["valid"] is False
    assert by_sid["S-A"]["ttl_sec"] == 600
    # READ-ONLY: `last_go()` espellerebbe gli scaduti, la vista NO
    assert "S-OLD" in r._last_go and "S-A" in r._last_go


# --------------------------------------------- frontiera / impronta prefisso
def test_ctx_frontier_view_filtro_e_read_only(mk):
    r = mk()
    now = time.time()
    r._ctx_frontier["S-A"] = (10, now)
    r._ctx_frontier["S-B"] = (20, now)
    assert [x["session_id"] for x in r.ctx_frontier_view("S-A")] == ["S-A"]
    assert r.ctx_frontier_view("S-A")[0]["boundary"] == 10
    assert len(r.ctx_frontier_view()) == 2
    assert "S-A" in r._ctx_frontier


def test_prefix_fp_view_filtro(mk):
    r = mk()
    now = time.time()
    r._prefix_fp["S-A"] = ("hbody", "hsys", now)
    out = r.prefix_fp_view("S-A")
    assert out[0]["body_fp"] == "hbody" and out[0]["sys_fp"] == "hsys"
    assert r.prefix_fp_view("S-ALTRO") == []


# ------------------------------------------------------- session dep guard
def test_session_dep_guard_view(mk):
    r = mk()
    a = _u(r, "K-A")
    r.note_session_success("S-A", a)
    now = time.time()
    out = r.session_dep_guard_view(now=now)
    row = next(x for x in out if x["unique"] == a)
    assert row["session_id"] == "S-A" and row["expired"] is False
    assert row["guard_sec"] == r._guard_sec()
    r._dep_sess()[_u(r, "K-B")] = ("S-B", now - 99999)
    out = r.session_dep_guard_view(now=now)
    assert out[0]["expired"] is True           # ordinati per age decrescente
    assert out[0]["age_sec"] >= out[-1]["age_sec"]


# --------------------------------------------------------------- warm pool
def test_warm_pool_view_owned_lendable_e_holder(mk):
    r = mk(warm_borrow_idle_sec=0)         # idle>=0 -> prestabile subito
    a = _u(r, "K-A")
    r.note_session_success("S-A", a)
    v = r.warm_pool_view(session_id="S-A")
    assert v["totals"]["sessions"] == 1
    owned = v["sessions"]["S-A"]["owned"]
    assert [o["unique"] for o in owned] == [a]
    assert owned[0]["holder"] is True
    assert owned[0]["model"] == "m-a"
    assert a in v["lendable"]
    assert v["totals"]["owned"] == 1
    json.dumps(v)                          # niente set
    # filtro per sessione
    assert r.warm_pool_view(session_id="S-ALTRA")["sessions"] == {}


# ------------------------------------------------------------ soft per key
def test_key_soft_view_tag_e_no_raw(mk, monkeypatch):
    r = mk()
    now = time.time()
    raw = "sk-RAW-SECRET-QUOTA"
    tag = r._tag_key(raw)
    assert len(tag) == 12 and all(c in "0123456789abcdef" for c in tag)
    monkeypatch.setattr("app.autoprobe._key_quota_day", {raw: now + 100})
    r._key_soft["aabbccddeeff"] = now + 50
    r._key_hints["001122334455"] = (now - 5, 2)
    out = r.key_soft_view()
    assert "aabbccddeeff" in out["soft"]
    assert out["hints"]["001122334455"]["remaining"] == 2
    assert tag in out["quota_day"]
    text = json.dumps(out)
    assert raw not in text
    assert "aabbccddeeff" in text


# --------------------------------------------------------------- circuits
def test_model_circuits_view(mk):
    r = mk()
    now = time.time()
    r._model_cb["groq|m-a"] = {"tags": {"aaaaaaaaaaaa", "bbbbbbbbbbbb"},
                               "ts": now, "opened": now}
    v = r.model_circuits_view()
    assert v["open_total"] == 1
    m = v["models"]["groq|m-a"]
    assert m["distinct_keys"] == 2 and m["opened"] is True
    json.dumps(v)
    r._model_cb["groq|m-b"] = {"tags": set(), "ts": now, "opened": 0.0}
    assert r.model_circuits_view()["open_total"] == 1


def test_circuit_breakers_view_no_raw_key(mk):
    r = mk()
    now = time.time()
    raw = "sk-RAW-KEY-BREAKER"
    tag = r._tag_key(raw)
    r._circuit_breakers[raw] = {"state": "open", "failures": 4,
                                "last_failure": now, "opened_at": now}
    r._dep_circuit_breakers["dep-unique-x"] = {
        "state": "half_open", "failures": 1, "last_failure": now - 5,
        "opened_at": now - 60}
    v = r.circuit_breakers_view()
    assert tag in v["keys"] and raw not in json.dumps(v)
    assert v["keys"][tag]["failures"] == 4
    assert "dep-unique-x" in v["deployments"]
    assert v["config"]["threshold"] == 5


# ------------------------------------------------- provider alternation / probes
def test_provider_alternation_view_3tuple(mk):
    r = mk()
    now = time.time()
    r._last_attempt = ("groq", "m-a", 64)   # 3-tuple (annotato 2-tuple)
    r._prov_last["groq"] = now - 10
    v = r.provider_alternation_view()
    assert v["last_attempt"]["provider"] == "groq"
    assert v["last_attempt"]["model"] == "m-a"
    assert v["last_attempt"]["dim_k"] == 64
    assert v["last_attempt"]["age_sec"] >= 9
    # 2-tuple: campi extra difensivamente None
    r._last_attempt = ("groq", "m-a")
    assert r.provider_alternation_view()["last_attempt"]["dim_k"] is None


def test_probes_in_flight_view(mk):
    r = mk()
    now = time.time()
    r._probes()["S-A"] = {"u1": now, "u2": now - 99999}
    v = r.probes_in_flight_view(now=now)
    assert v["total"] == 1
    assert v["sessions"]["S-A"]["count"] == 1
    assert "u2" not in v["sessions"]["S-A"]["uniques"]


def test_sess_est_view(mk):
    r = mk()
    now = time.time()
    r._sess_est()["S-A"] = {"pre": 100, "post": 80, "pt": 50, "n": 2, "ts": now}
    out = r.sess_est_view("S-A")
    assert out[0]["cpt_pre"] == 2.0 and out[0]["cpt_post"] == 1.6
    assert out[0]["samples"] == 2
    assert r.sess_est_view("S-ALTRA") == []


# ============================================================ HTTP admin
CSV_HTTP = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,model-a,groq,https://a.test/v1,free,500,8000,5,K-A,
t@x,model-b,groq,https://b.test/v1,free,500,8000,5,K-B,
"""
MKH = {"Authorization": "Bearer test-master-p3"}
GROUP = f"{BASE}-500k"


@pytest.fixture()
def client(monkeypatch, tmp_path):
    os.environ.setdefault("GATEWAY_MASTER_KEY", "test-master-not-default")
    csv = tmp_path / "k3.csv"
    csv.write_text(CSV_HTTP)
    import app.main as m
    orig_mk = m.authn.master_key
    orig_csv = m.config.csv_path
    m.authn.master_key = "test-master-p3"
    m.config.csv_path = csv
    m.config.reload()
    m.router._cooldown.clear()
    yield TestClient(m.app), m
    # cleanup esteso: le viste non devono inquinare i test successivi
    for attr in ("_key_soft", "_key_hints", "_circuit_breakers",
                 "_dep_circuit_breakers", "_model_cb", "_sess_ratio",
                 "_probes_flight", "_last_go", "_session_slow_timer",
                 "_ctx_frontier", "_prefix_fp", "_session_turns"):
        d = getattr(m.router, attr, None)
        if isinstance(d, dict):
            d.clear()
    m.config.csv_path = orig_csv
    m.config.reload()
    m.authn.master_key = orig_mk
    m.router._cooldown.clear()
    m.router._key_leases().clear()


def test_new_endpoints_master_only(client):
    c, _m = client
    for path in ("/admin/warm", "/admin/keys/soft", "/admin/circuits"):
        assert c.get(path).status_code == 401
        assert c.get(path, headers={"Authorization": "Bearer sbagliata"}
                     ).status_code in (401, 403)
        assert c.get(path, headers=MKH).status_code == 200


def test_warm_endpoint_shape(client):
    c, m = client
    uid = m.config.groups[GROUP][0]["unique"]
    m.router.note_session_success("SESS-1", uid)
    out = c.get("/admin/warm", headers=MKH).json()
    assert "sessions" in out and "lendable" in out and "totals" in out
    assert out["enabled"] is True


def test_keys_soft_endpoint_no_raw(client, monkeypatch):
    c, m = client
    from app import autoprobe as ap
    monkeypatch.setattr(ap, "_key_quota_day",
                        {"sk-RAW-HTTP-KEY": time.time() + 100})
    m.router._key_soft["aabbccddeeff"] = time.time() + 50
    out = c.get("/admin/keys/soft", headers=MKH).json()
    text = json.dumps(out)
    assert "sk-RAW-HTTP-KEY" not in text
    assert "aabbccddeeff" in out["soft"]
    assert len(next(iter(out["quota_day"]))) == 12


def test_circuits_endpoint_no_raw(client):
    c, m = client
    now = time.time()
    m.router._circuit_breakers["sk-RAW-HTTP-CB"] = {
        "state": "open", "failures": 2, "last_failure": now, "opened_at": now}
    out = c.get("/admin/circuits", headers=MKH).json()
    assert "models" in out and "keys" in out and "deployments" in out
    assert "sk-RAW-HTTP-CB" not in json.dumps(out)
    assert m.router._tag_key("sk-RAW-HTTP-CB") in out["keys"]


def test_state_additivo_con_nuove_viste(client):
    c, m = client
    uid = m.config.groups[GROUP][0]["unique"]
    m.router.note_session_success("SESS-X", uid)
    st = c.get("/admin/state", headers=MKH).json()
    ad = st["adaptive"]
    assert "entries" in ad["session_dep_guard"]
    assert "provider_alternation" in ad and "probes_in_flight" in ad
    # chiavi preesistenti intatte
    for k in ("key_leases", "quirks", "degraded", "warm_pool"):
        assert k in ad
    assert ad["session_dep_guard"]["tracked"] == \
        len(getattr(m.router, "_dep_last_session", {}) or {})


def test_sessions_list_additivo(client):
    c, m = client
    uid = m.config.groups[GROUP][0]["unique"]
    m.router.note_session_success("SESS-1", uid)
    m.router.note_session_turn("SESS-1")
    out = c.get("/admin/sessions", headers=MKH).json()
    for k in ("sticky_sessions", "dep_sticky_sessions", "session_deps",
              "cache_holders", "slow_demoted"):
        assert k in out
    for k in ("slow_timer", "go_refund", "last_go"):
        assert k in out and k in out["totals"]
    assert out["totals"]["go_refund"] == len(out["go_refund"])


def test_session_detail_additivo(client):
    c, m = client
    uid = m.config.groups[GROUP][0]["unique"]
    m.router.note_session_success("SESS-1", uid)
    m.router.note_compact_boundary("SESS-1", 7)
    m.router.audit_prefix("SESS-1", [{"role": "system"}, {"role": "user"},
                                     {"role": "assistant"}], 2)
    m.router.note_session_turn("SESS-1")
    out = c.get("/admin/sessions/SESS-1", headers=MKH).json()
    assert out["session_id"] == "SESS-1"
    for k in ("slow_timer", "go_refund", "last_go", "ctx_frontier",
              "prefix_fp", "sess_est"):
        assert k in out
    assert isinstance(out["go_refund"], dict)
    assert out["ctx_frontier"][0]["boundary"] == 7
    json.dumps(out)
