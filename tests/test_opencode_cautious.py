"""Modalita' cauta OPENCODE (zen in CODA tra i free) e cautela GENERICA.

Decisioni utente:
  - la cautela opencode vale SOLO per il traffico spoofato (client non-opencode
    con `OPENCODE_SPOOF_HEADERS` attivo): un client opencode reale resta
    normale;
  - in cautela gli zen (free) NON vengono esclusi: restano eleggibili nei
    percorsi normali (cold/warm/canary) ma con priorita' ULTIMA tra i free
    (dopo openrouter) e comunque PRIMA dello stadio -go/-fallback. L'ordine e'
    gestito dal router via `_eff_order` + rank in `_walk_chain`;
  - gli upstream go (a pagamento) restano normali e sono regolati
    dall'interruttore dedicato `OPENCODE_GO` (default ON);
  - il warm zen resta utilizzabile con eccezione sull'OWNER: se il warm zen
    appartiene a una sessione NATIVA opencode (`ses_...`) non e' "spoofabile";
    se l'owner e' una sessione fake (`fq_...`, propria o di un'altra) resta un
    warm valido;
  - la cautela GENERICA (`BACKGROUND_CAUTIOUS`, default OFF) e' una
    funzionalita' distinta: spegne probe/background per TUTTI i provider. In
    cautela opencode le probe restano attive ma con gli zen in coda.
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
                               set_allow_opencode_zen, set_spoofing_request,
                               set_zen_first)
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
t@x.com,m/oc,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,5,K-OC
t@x.com,m/oc2,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,6,K-OC2
t@x.com,m/plain,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-PL
t@x.com,m/goc,opencode-go,https://opencode.ai/zen/go/v1,free,300,300000,5,K-GOC
"""

# Stessa -dim con zen (order 0) e un free non-zen (order 10): serve a provare
# che in cautela lo zen viene spostato in CODA (order effettivo ORDER_LAST).
CSV_ORDERED = """commento,modello,provider,endpoint,data,context,max_input,priority,order,scrocco-llm-test
t@x.com,m/oc,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,5,0,K-OC
t@x.com,m/oc2,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,6,0,K-OC2
t@x.com,m/plain,groq,https://api.groq.com/openai/v1,free,100,100000,5,10,K-PL
t@x.com,m/goc,opencode-go,https://opencode.ai/zen/go/v1,free,300,300000,5,20,K-GOC
"""

# Zen con order ALTO (50) e non-zen con order basso (10): serve a provare che
# per un client opencode NATIVO lo zen scavalca comunque l'ordine nativo
# (ORDER_FIRST), mentre per gli altri resta l'ordine del CSV.
CSV_ORDERED2 = """commento,modello,provider,endpoint,data,context,max_input,priority,order,scrocco-llm-test
t@x.com,m/oc,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,5,50,K-OC
t@x.com,m/oc2,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,6,50,K-OC2
t@x.com,m/plain,groq,https://api.groq.com/openai/v1,free,100,100000,5,10,K-PL
t@x.com,m/goc,opencode-go,https://opencode.ai/zen/go/v1,free,300,300000,5,20,K-GOC
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
    set_zen_first(False)
    yield
    set_allow_opencode_zen(None)
    set_spoofing_request(False)
    set_zen_first(False)


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    pol = Policy.from_dict(POLICY)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


@pytest.fixture()
def router_ord():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ORDERED)
    pol = Policy.from_dict(POLICY)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


@pytest.fixture()
def router_ord2():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ORDERED2)
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


def test_dep_usable_zen_usable_under_caution(monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    assert dep_usable(ZEN_DEP)          # eleggibile: l'ordine lo manda in coda
    assert dep_usable(GO_DEP)           # go resta normale


def test_dep_usable_zen_normal_for_real_opencode_client():
    set_allow_opencode_zen(True)
    set_spoofing_request(False)                       # client opencode reale
    assert dep_usable(ZEN_DEP)


def test_dep_usable_zen_client_gate_still_applies():
    set_allow_opencode_zen(False)                     # spoof off, non-opencode
    set_spoofing_request(False)
    assert not dep_usable(ZEN_DEP)
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


# --------------------------------------------- pick: zen in coda tra i free
def test_eff_order_demotes_zen_last(router_ord, monkeypatch):
    from app.config import ORDER_LAST
    zen = _dep(router_ord, f"{BASE}-100k", "K-OC")
    plain = _dep(router_ord, f"{BASE}-100k", "K-PL")
    # fuori cautela: order nativo (zen 0 = primo tier)
    assert router_ord._eff_order(zen) == 0
    assert router_ord._eff_order(plain) == 10
    # in cautela opencode (spoof): zen -> ORDER_LAST, gli altri invariati
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    set_spoofing_request(True)
    assert router_ord._eff_order(zen) == ORDER_LAST
    assert router_ord._eff_order(plain) == 10


def test_pick_zen_last_among_free(router_ord, monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    set_allow_opencode_zen(True)
    need = frozenset({"text"})
    # client opencode reale: nessuna cautela, zen resta primo tier (order 0)
    set_spoofing_request(False)
    d = router_ord.pick_deployment(f"{BASE}-100k", need)
    assert d is not None and d["model"] in {"m/oc", "m/oc2"}
    # traffico spoofato in cautela: zen demoto -> vince il free non-zen
    set_spoofing_request(True)
    d2 = router_ord.pick_deployment(f"{BASE}-100k", need)
    assert d2 is not None and d2["model"] == "m/plain"


def test_walk_chain_zen_in_tail(router_ord, monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    need = frozenset({"text"})
    chain = [d["unique"] for d in router_ord.config.groups[f"{BASE}-100k"]]
    # il primo eleggibile NON e' zen (zen in coda)
    d = router_ord._walk_chain(chain, None, need, None)
    assert d is not None and d["model"] == "m/plain"
    # esaurito il non-zen, lo zen torna raggiungibile
    d2 = router_ord._walk_chain(chain, None, need, None,
                                tried={d["unique"]})
    assert d2 is not None and d2["model"] in {"m/oc", "m/oc2"}


def test_warm_pool_empty_for_spoofed_under_caution(router_ord, monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    zen = _dep(router_ord, f"{BASE}-100k", "K-OC")
    plain = _dep(router_ord, f"{BASE}-100k", "K-PL")
    router_ord.note_session_success(FAKE1, zen["unique"], 100, ctx_est=100)
    router_ord.note_session_success(FAKE1, plain["unique"], 100, ctx_est=100)
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    # spoofato: il warm non e' consultabile (ne' proprio ne' prestato)
    assert router_ord._warm_pool(FAKE1, None, None, None) == []
    assert router_ord._warm_pool(FAKE1, None, None, None,
                                 include_borrowed=True) == []
    # ... ma l'ownership e' comunque registrata
    assert zen["unique"] in (router_ord._sess_deps().get(FAKE1) or set())
    # una sessione opencode REALE la trova calda (nessuna cautela)
    set_spoofing_request(False)
    pool = router_ord._warm_pool(FAKE1, None, None, None)
    assert {d["model"] for d in pool} == {"m/plain", "m/oc"}


def test_pick_zen_normal_for_real_opencode_client(router):
    set_allow_opencode_zen(True)
    set_spoofing_request(False)                       # nessuna cautela
    dep = router.pick_deployment(f"{BASE}-100k", frozenset({"text"}))
    assert dep is not None and dep["model"] in {"m/oc", "m/oc2"}


# ------------------------------------- zen-FIRST per client opencode NATIVO
def test_eff_order_zen_first_for_native(router_ord2):
    from app.config import ORDER_FIRST
    zen = _dep(router_ord2, f"{BASE}-100k", "K-OC")
    plain = _dep(router_ord2, f"{BASE}-100k", "K-PL")
    set_allow_opencode_zen(True)
    set_spoofing_request(False)
    set_zen_first(True)                       # client opencode nativo
    assert router_ord2._eff_order(zen) == ORDER_FIRST
    assert router_ord2._eff_order(plain) == 10


def test_pick_zen_first_for_native(router_ord2):
    need = frozenset({"text"})
    set_allow_opencode_zen(True)
    set_spoofing_request(False)
    # zen ammessi ma NON nativo: vince l'order nativo (plain, order 10)
    set_zen_first(False)
    assert router_ord2.pick_deployment(
        f"{BASE}-100k", need)["model"] == "m/plain"
    # nativo: lo zen scavalca l'ordine nativo (order 50) -> primo tier
    set_zen_first(True)
    assert router_ord2.pick_deployment(
        f"{BASE}-100k", need)["model"] in {"m/oc", "m/oc2"}


def test_nonopencode_never_uses_zen(router_ord2):
    set_allow_opencode_zen(False)             # non-opencode: zen esclusi
    set_spoofing_request(False)
    set_zen_first(False)
    for _ in range(20):
        d = router_ord2.pick_deployment(f"{BASE}-100k", frozenset({"text"}))
        assert d is not None and d["model"] == "m/plain"


def test_native_skips_nonzen_warm_to_search_zen_cold(router_ord2):
    """Nativo: un warm non-zen NON deve battere la ricerca (a freddo) di uno
    zen vivo. Q1 utente: prima cercano uno zen, poi (solo se non c'e') usano
    un warm non-opencode."""
    plain = _dep(router_ord2, f"{BASE}-100k", "K-PL")
    router_ord2.note_session_success(NATIVE, plain["unique"], 100, ctx_est=100)
    set_allow_opencode_zen(True)
    set_spoofing_request(False)
    set_zen_first(True)
    d = router_ord2.initial_pick("test", f"{BASE}-100k",
                                 need=frozenset({"text"}), session_id=NATIVE)
    assert d is not None and is_opencode_zen_dep(d)


def test_native_uses_nonzen_warm_when_no_zen_available(router_ord2,
                                                       monkeypatch):
    """Non esclusione totale: se NON esiste alcuno zen, il warm non-zen resta
    utilizzabile (i nativi ci arrivano solo dopo aver cercato lo zen)."""
    plain = _dep(router_ord2, f"{BASE}-100k", "K-PL")
    router_ord2.note_session_success(NATIVE, plain["unique"], 100, ctx_est=100)
    set_allow_opencode_zen(True)
    set_spoofing_request(False)
    set_zen_first(True)
    monkeypatch.setattr(router_ord2, "_usable_zen_exists",
                        lambda *a, **k: False)
    d = router_ord2.initial_pick("test", f"{BASE}-100k",
                                 need=frozenset({"text"}), session_id=NATIVE)
    assert d is not None and d["model"] == "m/plain"


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


# -------------------------------------- warm/canary/sticky: spoofati
def test_warm_disabled_for_spoofed_borrow(router, monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    oc = _dep(router, f"{BASE}-100k", "K-OC")
    oc2 = _dep(router, f"{BASE}-100k", "K-OC2")
    router.note_session_success(NATIVE, oc["unique"], 100, ctx_est=100)
    router.note_session_success(FAKE1, oc2["unique"], 100, ctx_est=100)
    router.policy.warm_borrow_idle_sec = 0.0
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    # spoofato: niente warm, neanche in prestito
    assert router._warm_pool(FAKE2, None, None, None,
                             include_borrowed=True) == []
    # client opencode reale: il prestito (nativo e fake) resta disponibile
    set_spoofing_request(False)
    pool2 = router._warm_pool(FAKE2, None, None, None, include_borrowed=True)
    assert {d["model"] for d in pool2} == {"m/oc", "m/oc2"}


def test_warm_recorded_but_unusable_by_owner_when_spoofed(router, monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    oc = _dep(router, f"{BASE}-100k", "K-OC")
    router.note_session_success(FAKE1, oc["unique"], 100, ctx_est=100)
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    assert router._warm_pool(FAKE1, None, None, None) == []
    assert oc["unique"] in (router._sess_deps().get(FAKE1) or set())


def test_canaries_disabled_for_spoofed(router, monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    cur = _dep(router, f"{BASE}-100k", "K-OC")
    need = frozenset({"text"})
    assert router.warm_fill_canary("test", cur, need, 100, 0) is None
    assert router.warm_wake_canary("test", cur, need, 100, 0) is None


def test_sticky_not_set_for_spoofed(router, monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    router.policy.deployment_sticky = True
    d = router.initial_pick("test", f"{BASE}-100k",
                            need=frozenset({"text"}), session_id=FAKE1)
    assert d is not None
    assert router.dep_sticky_get(FAKE1) is None


def test_cache_holder_go_only_for_spoofed(router, monkeypatch):
    monkeypatch.setenv("OPENCODE_CAUTIOUS", "1")
    oc = _dep(router, f"{BASE}-100k", "K-OC")          # dim, non -go
    router.note_session_success(FAKE1, oc["unique"], 100, ctx_est=100)
    set_allow_opencode_zen(True)
    set_spoofing_request(True)
    # spoofato: un dim NON aggancia (unico aggancio ammesso: dep -go)
    assert router.session_holder(FAKE1) is None
    assert router.cache_holder(FAKE1) is None
    # client opencode reale: cache-holder invariato
    set_spoofing_request(False)
    assert router.session_holder(FAKE1) == oc["unique"]
    assert router.cache_holder(FAKE1) is not None


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
