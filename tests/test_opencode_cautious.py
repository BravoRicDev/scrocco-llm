"""Modalita' cauta OPENCODE (zen come ULTIMA SCELTA) e cautela GENERICA.

Decisioni utente:
  - la cautela opencode vale SOLO per il traffico spoofato (client non-opencode
    con `OPENCODE_SPOOF_HEADERS` attivo): un client opencode reale resta
    normale; demotion SOLO degli zen (free), gli upstream go (a pagamento)
    restano normali e sono regolati da un interruttore dedicato (`OPENCODE_GO`,
    default ON);
  - il warm zen resta utilizzabile con eccezione sull'OWNER: se il warm zen
    appartiene a una sessione NATIVA opencode (`ses_...`) non e' "spoofabile";
    se l'owner e' una sessione fake (`fq_...`, propria o di un'altra) resta un
    warm valido;
  - la cautela GENERICA (`BACKGROUND_CAUTIOUS`, default OFF) e' una
    funzionalita' distinta: spegne probe/background per TUTTI i provider. In
    cautela opencode le probe restano attive ma non toccano gli zen.
"""
import os
import tempfile

import pytest

from app import autoprobe
from app.caution import background_cautious_enabled
from app.config import GatewayConfig
from app.opencode_gate import (dep_usable, is_opencode_dep,
                               is_opencode_go_dep, is_opencode_zen_dep,
                               opencode_cautious_enabled,
                               set_allow_opencode_zen, set_spoofing_request)
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
t@x.com,m/oc,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,5,K-OC
t@x.com,m/oc2,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,6,K-OC2
t@x.com,m/plain,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-PL
t@x.com,m/goc,opencode-go,https://opencode.ai/zen/go/v1,free,300,300000,5,K-GOC
"""

POLICY = {"capability_routing": {"model_capabilities": {
    "m/oc": ["text"], "m/oc2": ["text"], "m/plain": ["text"],
    "m/goc": ["text"]}}}

NATIVE = "ses_f5204e4a7ffeBxQjwqn3m1wM0X"
FAKE1 = "fq_1111111111111111"
FAKE2 = "fq_2222222222222222"

ZEN_DEP = {"api_base": "https://opencode.ai/zen/v1", "provider": "opencode-zen"}
GO_DEP = {"api_base": "https://opencode.ai/zen/go/v1", "provider": "opencode-go"}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    monkeypatch.delenv("OPENCODE_CAUTIOUS", raising=False)
    monkeypatch.delenv("OPENCODE_GO", raising=False)
    monkeypatch.delenv("BACKGROUND_CAUTIOUS", raising=False)
    set_allow_opencode_zen(None)
    set_spoofing_request(False)
    yield
    set_allow_opencode_zen(None)
    set_spoofing_request(False)


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    pol = Policy.from_dict(POLICY)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def _dep(r, gname, key):
    return next(d for d in r.config.groups[gname] if d.get("api_key") == key)


# ---------------------------------------------------- helper zen/go/cautious
def test_zen_vs_go_detection():
    assert is_opencode_zen_dep(ZEN_DEP) and is_opencode_dep(ZEN_DEP)
    assert not is_opencode_zen_dep(GO_DEP) and is_opencode_dep(GO_DEP)
    assert is_opencode_go_dep(GO_DEP) and not is_opencode_go_dep(ZEN_DEP)


def test_dep_usable_zen_last_only_under_spoofing(monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    assert not dep_usable(ZEN_DEP)                    # percorso normale: no
    assert dep_usable(ZEN_DEP, last=True)             # ultima scelta: si
    assert dep_usable(GO_DEP)                         # go: mai demoto
    assert dep_usable(GO_DEP, last=True)


def test_dep_usable_zen_normal_for_real_opencode_client():
    set_allow_opencode_zen(True)
    set_spoofing_request(False)                       # client opencode reale
    assert dep_usable(ZEN_DEP)


def test_dep_usable_zen_client_gate_still_applies():
    set_allow_opencode_zen(False)                     # spoof off, non-opencode
    set_spoofing_request(False)
    assert not dep_usable(ZEN_DEP, last=True)
    # go e' indipendente dal gate zen: default ON
    assert dep_usable(GO_DEP)


def test_dep_usable_go_switch(monkeypatch):
    set_allow_opencode_zen(False)                     # zen chiuso...
    assert dep_usable(GO_DEP)                         # ...go comunque ON
    monkeypatch.setenv("OPENCODE_GO", "0")
    assert not dep_usable(GO_DEP)                     # interruttore dedicato


def test_opencode_cautious_derived_and_overridable(monkeypatch):
    assert not opencode_cautious_enabled()
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    assert opencode_cautious_enabled()
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "0")      # override esplicito
    assert not opencode_cautious_enabled()


# --------------------------------------------- pick: zen ultima scelta
def test_pick_excludes_zen_and_chain_admits_only_last(router, monkeypatch):
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")  # cautela attiva
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    need = frozenset({"text"})
    # pick a freddo: nessun zen eleggibile (nemmeno nell'ultima spiaggia locale)
    assert router.pick_deployment(f"{BASE}-100k", need) is None
    # la catena normale non ammette zen...
    chain = [d["unique"] for d in router.config.groups[f"{BASE}-100k"]]
    assert router._walk_chain(chain, None, need, None) is None
    # ...ma lo stadio finale (last=True) si'
    dep = router._walk_chain(chain, None, need, None, allow_slow=True,
                             last=True)
    assert dep is not None and dep["model"] in {"m/oc", "m/oc2"}


def test_pick_zen_normal_for_real_opencode_client(router):
    set_allow_opencode_zen(True)
    set_spoofing_request(False)                       # nessuna cautela
    dep = router.pick_deployment(f"{BASE}-100k", frozenset({"text"}))
    assert dep is not None and dep["model"] in {"m/oc", "m/oc2"}


# --------------------------------------------- pick: go indipendente
def test_pick_go_allowed_with_spoof_off(router):
    set_allow_opencode_zen(False)                     # client non-opencode
    set_spoofing_request(False)
    dep = router.pick_deployment(f"{BASE}-300k", frozenset({"text"}))
    assert dep is not None and dep["model"] == "m/goc"


def test_pick_go_disabled_by_switch(router, monkeypatch):
    monkeypatch.setenv("OPENCODE_GO", "0")
    set_allow_opencode_zen(True)
    assert router.pick_deployment(f"{BASE}-300k", frozenset({"text"})) is None


# ------------------------------------------- warm: eccezione sull'owner
def test_warm_zen_native_owner_excluded_fake_owner_allowed(router, monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")      # cautela opencode attiva
    oc = _dep(router, f"{BASE}-100k", "K-OC")
    oc2 = _dep(router, f"{BASE}-100k", "K-OC2")
    router.note_session_success(NATIVE, oc["unique"], 100, ctx_est=100)
    router.note_session_success(FAKE1, oc2["unique"], 100, ctx_est=100)
    router.policy.warm_borrow_idle_sec = 0.0

    set_allow_opencode_zen(True)
    set_spoofing_request(True)                        # stiamo spoofando
    pool = router._warm_pool(FAKE2, None, None, None, include_borrowed=True)
    assert {d["model"] for d in pool} == {"m/oc2"}    # nativo escluso

    set_spoofing_request(False)                       # client opencode reale
    pool2 = router._warm_pool(FAKE2, None, None, None, include_borrowed=True)
    assert {d["model"] for d in pool2} == {"m/oc", "m/oc2"}


def test_warm_zen_own_fake_session_is_usable(router):
    oc = _dep(router, f"{BASE}-100k", "K-OC")
    router.note_session_success(FAKE1, oc["unique"], 100, ctx_est=100)
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    pool = router._warm_pool(FAKE1, None, None, None)
    assert {d["model"] for d in pool} == {"m/oc"}


# ------------------------------------------- cautela GENERICA (probe/background)
def test_background_and_opencode_caution_are_independent(monkeypatch):
    # spoof ON -> cautela opencode ON, ma la generica resta OFF
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    assert opencode_cautious_enabled()
    assert not background_cautious_enabled()
    # BACKGROUND_CAUTIOUS ON con spoof OFF -> generica ON, opencode OFF
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    monkeypatch.setenv("BACKGROUND_CAUTIOUS", "1")
    assert background_cautious_enabled()
    assert not opencode_cautious_enabled()


def test_probe_ready_disabled_by_background_caution(router, monkeypatch):
    monkeypatch.setattr(router, "cooldown_progress", lambda u: 1.0)
    router.policy.cooldown_probe_enabled = True
    assert router.probe_ready("qualsiasi") is True
    monkeypatch.setenv("BACKGROUND_CAUTIOUS", "1")
    assert router.probe_ready("qualsiasi") is False
    # lo spoof (cautela opencode) NON spegne i re-probe generici
    monkeypatch.delenv("BACKGROUND_CAUTIOUS", raising=False)
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    assert router.probe_ready("qualsiasi") is True


def test_autoprobe_noop_only_with_background_caution(router, monkeypatch):
    calls = {"n": 0}

    def _cfg(_pol):
        calls["n"] += 1
        return (True, "nightly")

    monkeypatch.setattr(autoprobe, "_cfg", _cfg)
    autoprobe.maybe_spawn(router, None, "test")       # default: chiama
    assert calls["n"] == 1
    calls["n"] = 0
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")  # cautela opencode: non basta
    autoprobe.maybe_spawn(router, None, "test")
    assert calls["n"] == 1
    calls["n"] = 0
    monkeypatch.setenv("BACKGROUND_CAUTIOUS", "1")     # cautela generica: no-op
    autoprobe.maybe_spawn(router, None, "test")
    assert calls["n"] == 0
