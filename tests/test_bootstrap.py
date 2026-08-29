"""Bootstrap playbook + probe one-shot (no spreco chiamate)."""
import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """App montata su CSV temporaneo vuoto (fresh install) e var/ isolata."""
    import os
    os.environ["GATEWAY_MASTER_KEY"] = "test-master-not-default"
    csv = tmp_path / "k.csv"
    csv.write_text("commento,modello,provider,endpoint,data,context,"
                   "max_input,priority,scrocco-llm-test,caps\n")
    import app.main as m
    orig_csv_path = m.config.csv_path
    orig_var_dir = getattr(m, "VAR_DIR", None)
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    m.config.csv_path = csv
    m.config.reload()                       # ricarica dal CSV vuoto temporaneo
    yield TestClient(m.app)
    # teardown: riporta config/router allo stato reale (nessun leak)
    m.router._cooldown.clear()
    m.config.csv_path = orig_csv_path
    m.config.reload()
    if orig_var_dir is None:
        delattr(m, "VAR_DIR")


def test_bootstrap_public_and_complete(client):
    r = client.get("/bootstrap")            # NO auth header: deve funzionare
    assert r.status_code == 200
    j = r.json()
    steps = [s["id"] for s in j["steps"]]
    assert steps == [0, 1, 2, 3, 4, 5, 6, 7]
    assert "CALLS" in j["cost_warning"].upper()
    # inglese: nessun carattere non-ascii nel corpo principale
    assert all(ord(ch) < 128 for ch in r.text[:4000])


def test_providers_stable_facts_only(client):
    j = client.get("/bootstrap/providers").json()
    ids = {p["id"] for p in j["providers"]}
    assert {"groq", "openrouter", "google"} <= ids
    for p in j["providers"]:
        assert "signup_url" in p and "api_base" in p
        # niente modelli hardcodati che invecchiano
        assert not any(k in p for k in ("models", "recommended"))


def test_status_fresh_install(client):
    j = client.get("/bootstrap/status").json()
    codes = [i["code"] for i in j["issues"]]
    assert "no_deployments" in codes
    assert j["deployments"] == 0
    acts = [a["endpoint"] for a in j["actions"]]
    assert "/bootstrap" in acts


def test_status_masks_keys_and_never_probes(client):
    import time as _t
    import app.main as m
    # serve almeno un deployment: il blocco "suspicious" e' nel ramo != 0
    m.config.csv_path.write_text(
        "commento,modello,provider,endpoint,data,context,max_input,priority,"
        "scrocco-llm-test,caps\n"
        "t@x,m-a,groq,https://p.test/v1,free,32,8000,0,sk-KK,\n")
    m.config.reload()

    class _S:
        fail_streak = 5
    m.router._cooldown["scrocco-llm-test-32k__m-a__0"] = _t.time() + 999
    monkey_stats = {"scrocco-llm-test-32k__m-a__0": _S()}
    monkey_orig = m.router.stats_for
    m.router.stats_for = lambda u: monkey_stats.get(u, monkey_orig(u))
    try:
        j = client.get("/bootstrap/status").json()
        blob = __import__("json").dumps(j)
        assert "sk-KK" not in blob              # mai chiavi intere
        codes = [i["code"] for i in j["issues"]]
        assert "suspicious_keys" in codes
        sus = next(i for i in j["issues"]
                   if i["code"] == "suspicious_keys")
        assert sus["items"][0]["key_masked"].endswith("*") \
            or "..." in sus["items"][0]["key_masked"]
    finally:
        m.router._cooldown.clear()
        m.router.stats_for = monkey_orig


def test_master_key_default_warning(client, monkeypatch):
    import app.main as m
    monkeypatch.delenv("GATEWAY_MASTER_KEY", raising=False)
    j = client.get("/bootstrap/status").json()
    codes = [i["code"] for i in j["issues"]]
    assert "master_key_is_default" in codes


# ---------------------------------------------------------------- probe ---

def test_probe_cached_no_waste(client, monkeypatch):
    """Il successo e' cachato: la seconda chiamata NON tocca la rete."""
    import app.admin as adm
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "A"}}]})

    dep = {"unique": "scrocco-llm-test-32k__m-a__0",
           "group": "scrocco-llm-test-32k", "model": "m-a",
           "api_base": "https://probe.test/v1", "api_key": "sk-KK"}
    monkeypatch.setitem(__import__("app.main", fromlist=["config"])
                        .config.groups, dep["group"], [dep])
    real_cls = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(adm.httpx, "AsyncClient",
                        lambda *a, **k: real_cls(transport=transport))

    mk = {"Authorization": "Bearer test-master-not-default"}
    r1 = client.post("/admin/deployments/probe", json={"unique": dep["unique"]},
                     headers=mk).json()
    assert r1["ok"] is True and r1["cached"] is False
    r2 = client.post("/admin/deployments/probe", json={"unique": dep["unique"]},
                     headers=mk).json()
    assert r2["cached"] is True and calls["n"] == 1     # ZERO chiamate extra
    r3 = client.post("/admin/deployments/probe",
                     json={"unique": dep["unique"], "force": True},
                     headers=mk).json()
    assert r3["cached"] is False and calls["n"] == 2    # force ritesta


def test_probe_failure_not_cached(client, monkeypatch):
    """I falliti NON si cachano: un blip si puo' ritentare senza force."""
    import app.admin as adm
    ok = {"v": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if not ok["v"]:
            return httpx.Response(503, text="blip")
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "A"}}]})

    dep = {"unique": "scrocco-llm-test-32k__m-b__0",
           "group": "scrocco-llm-test-32k", "model": "m-b",
           "api_base": "https://probe2.test/v1", "api_key": "sk-KB"}
    main_mod = __import__("app.main", fromlist=["config"])
    monkeypatch.setitem(main_mod.config.groups, dep["group"], [dep])
    real_cls = httpx.AsyncClient
    real = httpx.MockTransport(handler)
    monkeypatch.setattr(adm.httpx, "AsyncClient",
                        lambda *a, **k: real_cls(transport=real))

    mk = {"Authorization": "Bearer test-master-not-default"}
    body = {"unique": dep["unique"]}
    r1 = client.post("/admin/deployments/probe", json=body, headers=mk).json()
    assert r1["ok"] is False and r1["error_class"] == "http_503"
    ok["v"] = True
    r2 = client.post("/admin/deployments/probe", json=body, headers=mk).json()
    assert r2["ok"] is True and r2["cached"] is False   # ritentato senza force


def test_probe_does_not_touch_cooldowns(client, monkeypatch):
    """Il probe e' informativo: mai mark_failed / cooldown."""
    import app.main as m
    before = dict(m.router._cooldown)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    dep = {"unique": "scrocco-llm-test-32k__m-c__0",
           "group": "scrocco-llm-test-32k", "model": "m-c",
           "api_base": "https://probe3.test/v1", "api_key": "sk-KC"}
    monkeypatch.setitem(m.config.groups, dep["group"], [dep])
    import app.admin as adm2
    real_cls = httpx.AsyncClient
    real = httpx.MockTransport(handler)
    monkeypatch.setattr(adm2.httpx, "AsyncClient",
                        lambda *a, **k: real_cls(transport=real))
    client.post("/admin/deployments/probe",
                json={"unique": dep["unique"]},
                headers={"Authorization": "Bearer test-master-not-default"})
    assert dict(m.router._cooldown) == before


def test_probe_stt_uses_models_get(client, monkeypatch):
    """Deployment stt (non-chat): NIENTE POST /chat/completions; si verifica
    con GET /models e passa con status 200 (probe_kind='models')."""
    import app.admin as adm
    import app.main as m
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        # una GET /models con la chiave del deployment
        assert request.method == "GET"
        assert request.url.path.endswith("/models")
        assert request.headers["authorization"] == "Bearer sk-KS"
        return httpx.Response(200, json={"data": [{"id": "m-a"}]})

    dep = {"unique": "scrocco-llm-test-32k-stt__m-a__0",
           "group": "scrocco-llm-test-32k-stt", "model": "m-a",
           "api_base": "https://stt.test/v1", "api_key": "sk-KS"}
    monkeypatch.setitem(m.config.groups, dep["group"], [dep])
    monkeypatch.setitem(m.config.group_caps, dep["group"], "stt")
    real_cls = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(adm.httpx, "AsyncClient",
                        lambda *a, **k: real_cls(transport=transport))

    mk = {"Authorization": "Bearer test-master-not-default"}
    r1 = client.post("/admin/deployments/probe",
                     json={"unique": dep["unique"]}, headers=mk).json()
    assert r1["ok"] is True and r1["cached"] is False
    assert r1["probe_kind"] == "models"
    assert len(calls) == 1                       # una sola GET, zero POST chat


def test_probe_chat_uses_chat_completions_post(client, monkeypatch):
    """Deployment chat (cap assente): comportamento invariato, POST
    /chat/completions con body max_tokens=1 (probe_kind='chat')."""
    import app.admin as adm
    import app.main as m
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "POST"
        assert request.url.path.endswith("/chat/completions")
        body = request.read()
        payload = __import__("json").loads(body) if body else {}
        assert payload.get("max_tokens") == 1
        assert payload.get("model") == "m-b"
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "A"}}]})

    dep = {"unique": "scrocco-llm-test-32k__m-b__0",
           "group": "scrocco-llm-test-32k", "model": "m-b",
           "api_base": "https://chat.test/v1", "api_key": "sk-KH"}
    monkeypatch.setitem(m.config.groups, dep["group"], [dep])
    # nessun group_caps -> chat (comportamento storico invariato)
    real_cls = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(adm.httpx, "AsyncClient",
                        lambda *a, **k: real_cls(transport=transport))

    mk = {"Authorization": "Bearer test-master-not-default"}
    r1 = client.post("/admin/deployments/probe",
                     json={"unique": dep["unique"]}, headers=mk).json()
    assert r1["ok"] is True and r1["cached"] is False
    assert r1["probe_kind"] == "chat"
    assert len(calls) == 1                       # una sola POST chat, zero GET


def test_gateway_boots_without_csv(tmp_path):
    """FIX bootstrap: repo clonato con var/ vuota NON deve crashare:
    parte con 0 deployment (playbook /bootstrap guida da li')."""
    import app.config as cfg_mod
    cfg = cfg_mod.GatewayConfig(tmp_path / "inesistente.csv",
                                proxy_prefix="scrocco-llm-", seed=1)
    assert sum(len(v) for v in cfg.groups.values()) == 0
    assert cfg.profiles == []
