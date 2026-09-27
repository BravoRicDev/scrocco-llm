"""Integrazione Jev / TypeSafe System One (`decision`).

`/v1/systemone` e' l'unico endpoint NON-OpenAI del gateway: body nativo
`{model, state, questions}` -> risposta nativa `{model, answers, usage}`.
Qui si blinda che:
  - la capacita' `decision` produca un gruppo dedicato e NON finisca nel testo;
  - l'endpoint validi il payload, autentichi e instradi sulla cap `decision`;
  - il body sia inoltrato nativo (nessuna traduzione chat);
  - il QC ruoti se `answers` non copre tutte le chiavi richieste;
  - la rotazione avvenga anche con master key e sui transient (timeout);
  - l'esaurimento risponda 503 con trail;
  - `[summary]`/ledger usino `kind="systemone"` e mappino input/output tokens.

Per la rotazione il primo pick viene FISSATO (`initial_pick` monkeypatchato) su
`jev-a`: cosi' il test e' deterministico e non dipende dal fatto che
`_walk_chain` sulle catene piatte non faccia wrap (comportamento pre-esistente).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import protocols as P
from app.forwarder import UpstreamError
import app.state as gw_state

_HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
           "priority,scrocco-llm-test,caps,api_style\n")
_ROW_A = ("t1,jev-a,openrouter,https://openrouter.ai/api/v1/systemone,free,24,"
          "24000,1,sk-key-a,decision,jev\n")
_ROW_B = ("t2,jev-b,openrouter,https://openrouter.ai/api/v1/systemone,free,24,"
          "24000,1,sk-key-b,decision,jev\n")

MK = {"Authorization": "Bearer test-master-jev"}
_URL = "/v1/systemone"


def _systemone_body(model="scrocco-llm-test", **extra):
    body = {
        "model": model,
        "state": "Il cliente non riesce a collegare Stripe da 3 giorni",
        "questions": {
            "department": {
                "type": "choice",
                "instructions": "Which team",
                "criteria": {"billing": "payments", "technical": "bugs"},
            },
            "is_urgent": {"type": "noul", "instructions": "urgency"},
        },
    }
    body.update(extra)
    return body


def _answers_for(questions):
    return {k: {"type": "noul", "noul": 0.9} for k in questions}


class _FakeFwd:
    """Forwarder finto determinista per modello.

    `fail` mappa modello -> eccezione da alzare; `incomplete` e' l'insieme dei
    modelli che rispondono con `answers` vuote (QC -> rotazione). Tutti gli
    altri modelli rispondono completo.
    """

    def __init__(self, fail=None, incomplete=()):
        self.fail = dict(fail or {})
        self.incomplete = set(incomplete)
        self.calls: list[dict] = []

    async def call_systemone(self, dep, payload, **kw):
        self.calls.append({"dep": dep, "payload": dict(payload), **kw})
        model = dep["model"]
        if model in self.fail:
            raise self.fail[model]
        if model in self.incomplete:
            return {"model": model, "answers": {},
                    "usage": {"input_tokens": 1, "output_tokens": 1}}
        return {"model": model,
                "answers": _answers_for(payload.get("questions") or {}),
                "usage": {"input_tokens": 312, "output_tokens": 48}}


@pytest.fixture()
def client(monkeypatch, tmp_path):
    m, orig = _client_env(monkeypatch, tmp_path, _ROW_A + _ROW_B)
    yield TestClient(m.app), m
    _client_teardown(m, orig)


def _client_env(monkeypatch, tmp_path, rows, header=_HEADER):
    csv = tmp_path / "k.csv"
    csv.write_text(header + rows)
    import app.main as m
    orig = (gw_state.authn.master_key, gw_state.config.csv_path)
    gw_state.authn.master_key = "test-master-jev"
    gw_state.LEDGER.flush()
    monkeypatch.setattr(gw_state, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(gw_state, "CSV_PATH", str(csv))
    monkeypatch.setattr(gw_state.config, "csv_path", csv)
    gw_state.config.reload()
    gw_state.router.policy.cap_groups_enabled = True
    return m, orig


def _client_teardown(m, orig):
    gw_state.router._cooldown.clear()
    gw_state.router.policy.cap_groups_enabled = False
    gw_state.authn.master_key = orig[0]
    gw_state.config.csv_path = orig[1]
    gw_state.config.reload()


@pytest.fixture()
def client_fallback(monkeypatch, tmp_path):
    """Jev e' SEMPRE a pagamento: le righe reali sono tutte `fallback`."""
    rows = (_ROW_A.replace(",free,", ",fallback,")
            + _ROW_B.replace(",free,", ",fallback,"))
    m, orig = _client_env(monkeypatch, tmp_path, rows)
    yield TestClient(m.app), m
    _client_teardown(m, orig)


@pytest.fixture()
def client_mixed(monkeypatch, tmp_path):
    """Un provider Jev GRATIS (`free`) + uno a pagamento (`fallback`)."""
    rows = _ROW_A + _ROW_B.replace(",free,", ",fallback,")
    m, orig = _client_env(monkeypatch, tmp_path, rows)
    yield TestClient(m.app), m
    _client_teardown(m, orig)


def _pin_initial_pick(monkeypatch, m, model):
    """Fissa il primo deployment scelto da `initial_pick` a `model`."""
    dep = gw_state.config.deployment_by_unique(
        next(u for u in gw_state.config.chains_cap["test"]["decision"]
             if gw_state.config.deployment_by_unique(u)["model"] == model))
    monkeypatch.setattr(gw_state.router, "initial_pick", lambda *a, **k: dep)


def _post(c, monkeypatch, m, fwd, body=None, headers=MK):
    monkeypatch.setattr(gw_state, "forwarder", fwd)
    return c.post(_URL, headers=headers, json=body or _systemone_body())


# ------------------------------------------------------------- capacita'
def test_decision_capability_groups(client):
    _c, m = client
    assert "decision" in gw_state.config.profile_caps["test"]
    assert gw_state.config.group_caps.get("scrocco-llm-test-decision") == "decision"
    chain = gw_state.config.chains_cap["test"].get("decision") or []
    assert len(chain) == 2
    assert all("scrocco-llm-test-decision" in u for u in chain)
    # il gruppo e' richiamabile in whitelist (come -stt/-tts/-image_gen)
    assert "scrocco-llm-test-decision" in gw_state.config.whitelist_for("test")


def test_decision_rows_excluded_from_text_dims(client):
    """Una riga solo `decision` NON compare nei gruppi testo/dims."""
    _c, m = client
    assert "scrocco-llm-test-24k" not in (gw_state.config.chains.get("test") or [])


# ------------------------------------------------------------- protocollo
def test_build_url_jev_is_native_path():
    dep = {"api_base": "https://openrouter.ai/api/v1/systemone",
           "api_style": "jev", "api_key": "k"}
    assert P.build_url(dep, stream=False) == \
        "https://openrouter.ai/api/v1/systemone"


def test_call_systemone_native_url_and_model(monkeypatch):
    """`call_systemone` posta all'endpoint nativo riscrivendo solo il model."""
    from app.forwarder import Forwarder
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"answers": {}}

    class _Client:
        async def post(self, url, **kw):
            captured["url"] = url
            captured.update(kw)
            return _Resp()

    class _Stub:
        def _client_for(self, url, key):
            return _Client()

    dep = {"unique": "u", "group": "g",
           "api_base": "https://openrouter.ai/api/v1/systemone",
           "api_key": "sk-x", "model": "typesafe/jev-1.13",
           "provider": "openrouter", "api_style": "jev"}
    payload = {"model": "scrocco-llm-test", "state": "s",
               "questions": {"q": {"type": "noul", "instructions": "i"}}}
    out = asyncio.run(Forwarder.call_systemone(_Stub(), dep, payload))
    assert out == {"answers": {}}
    assert captured["url"] == "https://openrouter.ai/api/v1/systemone"
    assert captured["json"]["model"] == "typesafe/jev-1.13"
    assert captured["json"]["state"] == "s"
    assert captured["json"]["questions"] == payload["questions"]


# ------------------------------------------------------------- endpoint
def test_systemone_native_success(client, monkeypatch):
    c, m = client
    r = _post(c, monkeypatch, m, _FakeFwd())
    assert r.status_code == 200, r.text
    data = r.json()
    assert "answers" in data and "department" in data["answers"]
    assert data["nx_provider"] == "openrouter"
    assert data["nx_deployment"].startswith("scrocco-llm-test-decision__")
    assert r.headers.get("x-nx-deployment") == data["nx_deployment"]


def test_systemone_payload_forwarded_native(client, monkeypatch):
    c, m = client
    fwd = _FakeFwd()
    body = _systemone_body()
    r = _post(c, monkeypatch, m, fwd, body=body)
    assert r.status_code == 200, r.text
    sent = fwd.calls[0]["payload"]
    # il body viaggia nativo (il `model` e' riscritto dal forwarder reale)
    assert sent["state"] == body["state"]
    assert sent["questions"] == body["questions"]
    assert sent["model"] == "scrocco-llm-test"


def test_systemone_invalid_json_400(client, monkeypatch):
    c, m = client
    monkeypatch.setattr(gw_state, "forwarder", _FakeFwd())
    r = c.post(_URL, content=b"not-json",
               headers={**MK, "content-type": "application/json"})
    assert r.status_code == 400


def test_systemone_missing_state_400(client, monkeypatch):
    c, m = client
    body = _systemone_body()
    body.pop("state")
    r = _post(c, monkeypatch, m, _FakeFwd(), body=body)
    assert r.status_code == 400
    assert "state" in r.json()["error"]["message"]


def test_systemone_missing_or_empty_questions_400(client, monkeypatch):
    c, m = client
    for qs in (None, {}, "x"):
        body = _systemone_body()
        if qs is None:
            body.pop("questions")
        else:
            body["questions"] = qs
        r = _post(c, monkeypatch, m, _FakeFwd(), body=body)
        assert r.status_code == 400, (qs, r.text)


def test_systemone_auth_401(client, monkeypatch):
    c, m = client
    r = _post(c, monkeypatch, m, _FakeFwd(), headers={})
    assert r.status_code == 401


# ------------------------------------------------------------- QC e rotazione
def test_systemone_qc_incomplete_answers_rotates(client, monkeypatch):
    """La prima risposta non copre tutte le chiavi -> rotazione -> seconda ok."""
    c, m = client
    _pin_initial_pick(monkeypatch, m, "jev-a")
    fwd = _FakeFwd(incomplete={"jev-a"})
    r = _post(c, monkeypatch, m, fwd)
    assert r.status_code == 200, r.text
    assert len(fwd.calls) == 2
    assert "is_urgent" in r.json()["answers"]


def test_systemone_rotates_on_429(client, monkeypatch):
    c, m = client
    _pin_initial_pick(monkeypatch, m, "jev-a")
    fwd = _FakeFwd(fail={"jev-a": UpstreamError(429, "rate limited", 2.0)})
    r = _post(c, monkeypatch, m, fwd)
    assert r.status_code == 200, r.text
    assert len(fwd.calls) == 2


def test_systemone_rotates_on_timeout(client, monkeypatch):
    """status None (timeout/rete) e' deployment-side: DEVE ruotare."""
    c, m = client
    _pin_initial_pick(monkeypatch, m, "jev-a")
    fwd = _FakeFwd(fail={"jev-a": UpstreamError(None, "upstream timeout: x")})
    r = _post(c, monkeypatch, m, fwd)
    assert r.status_code == 200, r.text
    assert len(fwd.calls) == 2


def test_systemone_client_error_no_rotation(client, monkeypatch):
    c, m = client
    _pin_initial_pick(monkeypatch, m, "jev-a")
    fwd = _FakeFwd(fail={"jev-a": UpstreamError(-400, "invalid state schema")})
    r = _post(c, monkeypatch, m, fwd)
    assert r.status_code == 400
    assert len(fwd.calls) == 1


def test_systemone_all_fail_503_with_trail(client, monkeypatch):
    c, m = client
    _pin_initial_pick(monkeypatch, m, "jev-a")
    fwd = _FakeFwd(fail={"jev-a": UpstreamError(503, "upstream down"),
                         "jev-b": UpstreamError(503, "upstream down")})
    r = _post(c, monkeypatch, m, fwd)
    assert r.status_code == 503
    err = r.json()["error"]
    assert err.get("code") == "no_healthy_deployment"
    assert len(err.get("attempts") or []) == 2
    assert r.headers.get("X-Scrocco-Attempts") == "2"


# ------------------------------------------------------------- ledger
def test_systemone_summary_kind_and_usage(client, monkeypatch):
    c, m = client
    gw_state.LEDGER.flush()
    r = _post(c, monkeypatch, m, _FakeFwd())
    assert r.status_code == 200
    entries = [e for e in gw_state.LEDGER._buf if e.get("kind") == "systemone"]
    assert entries, "nessuna entry systemone nel ledger"
    e = entries[-1]
    assert e["usage"]["prompt_tokens"] == 312
    assert e["usage"]["completion_tokens"] == 48
    assert e["usage"]["total_tokens"] == 360


def test_systemone_note_end_no_inflight_leak(client, monkeypatch):
    c, m = client
    r = _post(c, monkeypatch, m, _FakeFwd())
    assert r.status_code == 200
    cur = r.json()["nx_deployment"]
    assert gw_state.router.stats_for(cur).inflight == 0


# ------------------------------------------------------- pagamento (fallback)
def test_decision_fallback_only_group_still_routes(client_fallback, monkeypatch):
    """Righe Jev a pagamento tutte `fallback` -> esiste solo -decision-fallback:
    l'endpoint deve comunque trovarle (catena capability free->go->fallback)."""
    c, m = client_fallback
    assert gw_state.config.cap_counts["test"]["decision"]["primary"] == 0
    assert gw_state.config.cap_counts["test"]["decision"]["fallback"] == 2
    r = _post(c, monkeypatch, m, _FakeFwd())
    assert r.status_code == 200, r.text
    assert "department" in r.json()["answers"]
    assert "-decision-fallback" in r.json()["nx_deployment"]


def test_decision_fallback_only_rotates(client_fallback, monkeypatch):
    c, m = client_fallback
    _pin_initial_pick(monkeypatch, m, "jev-a")
    fwd = _FakeFwd(fail={"jev-a": UpstreamError(429, "rate limited", 2.0)})
    r = _post(c, monkeypatch, m, fwd)
    assert r.status_code == 200, r.text
    assert len(fwd.calls) == 2


def test_decision_prefers_free_primary_over_paid_fallback(client_mixed,
                                                          monkeypatch):
    """Con un Jev gratis (`free`) e uno a pagamento (`fallback`), la prima
    scelta e' il primario gratuito; il pagato resta in rotazione."""
    c, m = client_mixed
    assert gw_state.config.cap_counts["test"]["decision"]["primary"] == 1
    assert gw_state.config.cap_counts["test"]["decision"]["fallback"] == 1
    r = _post(c, monkeypatch, m, _FakeFwd())
    assert r.status_code == 200, r.text
    dep = r.json()["nx_deployment"]
    assert "-decision__" in dep and "-decision-fallback__" not in dep


def test_models_alias_exposes_decision_capability(monkeypatch, tmp_path):
    """L'entry alias (`jev-latest`) in /v1/models deve dichiarare la capacita'
    reale (`decision`), risolta dai gruppi-alias, non solo id/owned_by.

    Gli alias stanno nella vista `stable` (il master di default elenca i
    deployment, che non sono nomi stabili)."""
    header = ("commento,modello,provider,endpoint,data,context,max_input,"
              "priority,scrocco-llm-test,caps,api_style,alias\n")
    row = ("t1,jev-a,bynara,https://router.bynara.id/v1/systemone,free,32,"
           "32000,0,sk-k,decision,jev,jev-latest\n")
    m, orig = _client_env(monkeypatch, tmp_path, row, header=header)
    try:
        c = TestClient(m.app)
        r = c.get("/v1/models?view=stable", headers=MK)
        assert r.status_code == 200, r.text
        entry = next((x for x in r.json()["data"] if x["id"] == "jev-latest"),
                     None)
        assert entry is not None, "alias jev-latest assente da /v1/models"
        assert entry.get("capabilities") == ["decision"]
        # forma OpenRouter: il modello decisionale dichiara `decisions`
        assert "decisions" in entry["architecture"]["output_modalities"]
        # forma LiteLLM: la capability dice DOVE chiamare
        assert "/v1/systemone" in entry["supported_endpoints"]
    finally:
        _client_teardown(m, orig)
