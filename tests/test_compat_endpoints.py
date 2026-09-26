"""Compat endpoints: fix dei 404 osservati sui client OSS (Ollama / llama.cpp /
OpenAI SDK) che oggi chiamano path non gestiti dal gateway.

Ogni client vede solo i modello visibili (master => tutti, altrimenti
whitelist del profilo), coerente con /v1/models. Le route statiche
(/version, /props, /v1/props, /api/version) NON richiedono auth perche'
molti client le chiamano prima di avere configurato la chiave.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    csv = tmp_path / "k.csv"
    csv.write_text(
        "commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "seed,openai/gpt-4o-mini,openai,https://api.openai.com/v1,"
        "free,8,8000,1,sk-test-key,\n"
    )
    import app.main as m
    # Patch DETERMINISTICA dell'attributo (come test_insights): app.main puo'
    # essere gia' importato da altri test con altra GATEWAY_MASTER_KEY.
    orig_mk = m.authn.master_key
    m.authn.master_key = "test-master-compat"
    orig_csv = m.config.csv_path
    m.LEDGER.flush()
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m.config, "csv_path", csv)
    m.config.reload()
    from app.ledger import Ledger
    led = Ledger(tmp_path)
    monkeypatch.setattr(m, "LEDGER", led)
    yield TestClient(m.app), m, led
    m.router._cooldown.clear()
    m.authn.master_key = orig_mk
    m.config.csv_path = orig_csv
    m.config.reload()


MK = {"Authorization": "Bearer test-master-compat"}


def _real_unique(m) -> str:
    return next(d["unique"] for deps in m.config.groups.values() for d in deps)


def test_api_tags_with_auth(client):
    c, m, _ = client
    r = c.get("/api/tags", headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert "models" in body and isinstance(body["models"], list)
    assert len(body["models"]) > 0
    for item in body["models"]:
        assert "name" in item and "model" in item


def test_retrieve_real_model(client):
    c, m, _ = client
    uid = _real_unique(m)
    r = c.get(f"/v1/models/{uid}", headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "model"
    assert body["id"] == uid


def test_retrieve_base_and_group_names(client):
    """Regression: il NOME BASE (scrocco-llm-<profilo>) e i gruppi -Nk/-go...
    che il client usa davvero nelle chat devono risolvere 200, non solo gli
    univoci grp__model__idx."""
    c, m, _ = client
    prefix = m.config.proxy_prefix
    prof = m.config.profiles[0]
    r = c.get(f"/v1/models/{prefix}{prof}", headers=MK)
    assert r.status_code == 200 and r.json()["id"] == f"{prefix}{prof}"
    # un gruppo reale del profilo (se esiste un -Nk/-go/...)
    grp = next((g for g in m.config.groups if g.startswith(f"{prefix}{prof}-")),
               None)
    if grp:
        r2 = c.get(f"/v1/models/{grp}", headers=MK)
        assert r2.status_code == 200 and r2.json()["id"] == grp


def test_retrieve_fake_model_404(client):
    c, m, _ = client
    r = c.get("/v1/models/modello-inventato-xyz", headers=MK)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


def test_api_v1_models(client):
    c, m, _ = client
    r = c.get("/api/v1/models", headers=MK)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list) and len(body["data"]) > 0


def test_api_show(client):
    c, m, _ = client
    uid = _real_unique(m)
    r = c.post("/api/show", headers=MK, json={"name": uid})
    assert r.status_code == 200
    body = r.json()
    assert "capabilities" in body


# ------------------------------------------------- discovery: chi vede cosa
def test_master_default_sees_only_uniques(client):
    """Il master di default elenca i DEPLOYMENT: nomi 'veri' ma non stabili.
    Nessun alias, nessun gruppo, nessun nome base."""
    c, m, _ = client
    ids = [x["id"] for x in c.get("/v1/models", headers=MK).json()["data"]]
    assert ids, "il master non vede nessun deployment"
    assert all("__" in i for i in ids), "un nome non-unique nel default master"
    base = m.config.proxy_prefix + m.config.profiles[0]
    assert base not in ids


def test_master_view_stable_sees_names_not_uniques(client):
    """?view=stable: nome base + gruppi, e NON i deployment."""
    c, m, _ = client
    data = c.get("/v1/models?view=stable", headers=MK).json()["data"]
    ids = [x["id"] for x in data]
    base = m.config.proxy_prefix + m.config.profiles[0]
    assert base in ids
    assert not any("__" in i for i in ids), "un unique nella vista stable"
    # la vista ricca porta i campi capability
    entry = next(x for x in data if x["id"] == base)
    assert entry.get("capabilities"), "manca capabilities nella vista stable"
    assert "architecture" in entry and "supported_endpoints" in entry


def test_profile_key_never_sees_uniques(client):
    """Una chiave di profilo (deterministica `sk-<profilo>`, attiva fuori
    produzione) vede i nomi STABILI del proprio profilo e non i deployment:
    `?view=uniques` non la fa uscire dalla vista stable."""
    c, m, _ = client
    prof = m.config.profiles[0]
    h = {"Authorization": f"Bearer sk-{prof}"}
    r = c.get("/v1/models", headers=h)
    assert r.status_code == 200, r.text
    for url in ("/v1/models", "/v1/models?view=uniques",
                "/v1/models?view=stable"):
        ids = [x["id"] for x in c.get(url, headers=h).json()["data"]]
        assert base_name(m) in ids, url
        assert not any("__" in i for i in ids), f"unique visibile in {url}"


def base_name(m) -> str:
    return m.config.proxy_prefix + m.config.profiles[0]


def test_uniques_not_in_stable_but_still_callable(client):
    """Un deployment non e' piu' nella vista stable (nomi stabili), ma resta
    utilizzabile: non si rompe nessuno che lo aveva pinnato."""
    c, m, _ = client
    uid = _real_unique(m)
    stable = [x["id"] for x in
              c.get("/v1/models?view=stable", headers=MK).json()["data"]]
    assert uid not in stable
    # resta recuperabile
    r = c.get(f"/v1/models/{uid}", headers=MK)
    assert r.status_code == 200 and r.json()["id"] == uid
    # resta instradabile: NON viene respinto come modello sconosciuto
    r2 = c.post("/v1/chat/completions", headers=MK, json={
        "model": uid, "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1})
    body = r2.text
    assert "model_not_found" not in body and "non gestito" not in body, body


def test_capability_fields_on_retrieve(client):
    """Il retrieve espone le stesse capability della lista."""
    c, m, _ = client
    base = base_name(m)
    listed = next(x for x in c.get("/v1/models?view=stable", headers=MK)
                  .json()["data"] if x["id"] == base)
    one = c.get(f"/v1/models/{base}", headers=MK).json()
    assert one.get("capabilities") == listed.get("capabilities")
    assert one.get("architecture") == listed.get("architecture")


def test_api_tags_entries_have_capabilities(client):
    c, m, _ = client
    models = c.get("/api/tags", headers=MK).json()["models"]
    entry = next(x for x in models if x["name"] == base_name(m))
    assert entry.get("capabilities"), "manca capabilities in /api/tags"
    # enum Ollama: i valori ufficiali devono essere presenti
    assert "completion" in entry["capabilities"]


def test_api_show_real_capabilities(client):
    """Prima /api/show rispondeva sempre ["completion","chat"]: un client non
    poteva scoprire le capability reali del nome richiesto."""
    c, m, _ = client
    base = base_name(m)
    body = c.post("/api/show", headers=MK, json={"name": base}).json()
    assert body["capabilities"] != ["completion", "chat"]
    assert "completion" in body["capabilities"]
    info = body["model_info"]
    assert "scrocco.caps" in info
    assert "scrocco.endpoints" in info
    assert "/v1/chat/completions" in info["scrocco.endpoints"].values()


def test_model_info_endpoint(client):
    """Endpoint dedicato in stile LiteLLM."""
    c, m, _ = client
    r = c.get("/v1/model/info?view=stable", headers=MK)
    assert r.status_code == 200
    data = r.json()["data"]
    entry = next(x for x in data if x["id"] == base_name(m))
    mi = entry["model_info"]
    assert mi["supports_function_calling"] is True
    assert "/v1/chat/completions" in mi["supported_endpoints"]
    assert mi["max_input_tokens"] > 0
    assert "architecture" in mi


def test_model_info_requires_auth(client):
    c, m, _ = client
    assert c.get("/v1/model/info").status_code == 401


def test_version_no_auth(client):
    c, m, _ = client
    r = c.get("/version")
    assert r.status_code == 200
    assert r.json() == {"version": "0.2.0"}


def test_v1_props_no_auth(client):
    c, m, _ = client
    r = c.get("/v1/props")
    assert r.status_code == 200
    assert "total_slots" in r.json()


def test_api_tags_no_auth_401(client):
    c, m, _ = client
    r = c.get("/api/tags")
    assert r.status_code == 401
