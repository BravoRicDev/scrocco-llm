"""ESCALATION WINNER (scorciatoia solo-in-salita, trasversale alla sessione).

Una richiesta partita da un bucket (es. -200k) che fallisce e SALTA verso un
gruppo piu' alto (-go/-fallback) trovando un winner: lo ricordiamo PER QUEL
BUCKET e nelle richieste successive lo usiamo come scorciatoia del FALLBACK
(mai del primo pick). Pin a finestra scorrevole (TTL rinnovato a ogni salita
buona), purificato su mark_failed o guarigione del bucket richiesto.
"""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m/small,groq,https://api.groq.com/openai/v1,free,200,8000,5,K-SMALL,
t@x,m/go,groq,https://api.groq.com/openai/v1,,0,0,5,K-GO,
"""
BASE = "scrocco-llm-test"
G200 = f"{BASE}-200k"
GGO = f"{BASE}-go"


def _mkrouter(**polkw):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}},
                            **polkw})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, pol)
    os.unlink(path)
    return r


@pytest.fixture()
def router():
    return _mkrouter()


def _dep(r, group, key):
    return next(d for d in r.config.groups[group] if d.get("api_key") == key)


# ---------------------------------------------------------------- record
def test_record_only_on_uphill(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    assert router._esc_win.get(G200, ("", 0))[0] == go["unique"]


def test_record_nominal_clears_pin(router):
    go = _dep(router, GGO, "K-GO")
    small = _dep(router, G200, "K-SMALL")
    router.record_escalation_win(G200, go)          # salita -> pin
    assert G200 in router._esc_win
    # il bucket richiesto ora serve da solo -> deve SBLICCARE il pin
    router.record_escalation_win(G200, small)
    assert G200 not in router._esc_win


def test_record_slides_ttl(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    ts1 = router._esc_win[G200][1]
    router._esc_win[G200] = (go["unique"], time.time() - 100)  # invecchia
    router.record_escalation_win(G200, go)                     # salita buona
    assert router._esc_win[G200][1] > ts1
    # e' "scivolato" a now, non il vecchio ts-100
    assert time.time() - router._esc_win[G200][1] < 1.0


def test_record_disabled_by_policy():
    r = _mkrouter(escalation_pin=False)
    go = _dep(r, GGO, "K-GO")
    r.record_escalation_win(G200, go)
    assert G200 not in r._esc_win


# ---------------------------------------------------------------- try
def test_try_returns_fresh_valid(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    got = router._try_esc_win(G200, None, ctx=100)
    assert got is not None and got["unique"] == go["unique"]


def test_try_none_when_stale(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    router._esc_win[G200] = (go["unique"], time.time() - 9999)   # oltre TTL
    assert router._try_esc_win(G200, None, ctx=100) is None
    # entry stale purificata
    assert G200 not in router._esc_win


def test_try_none_when_cooled(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    router.mark_failed(go["unique"], seconds=600)   # il winner cade -> pin pulito
    # mark_failed ha gia' rimosso il pin; prova diretta che un cooled non passa
    router._esc_win[G200] = (go["unique"], time.time())
    assert router._try_esc_win(G200, None, ctx=100) is None


def test_try_none_when_ctx_too_big(router):
    # K-GO max_input=0 (nessun limite dichiarato) quindi passa; usiamo small?
    # per il test serve un dep con max_input > 0 e ctx maggiore
    go = _dep(router, GGO, "K-GO")
    go_lim = dict(go)
    go_lim["max_input_tokens"] = 1000
    # registra a mano puntando a un dict con limite basso
    router.record_escalation_win(G200, go)          # pin -> unique di go
    # forziamo il winner ad avere max_input piccolo nel config
    for d in router.config.groups[GGO]:
        if d["api_key"] == "K-GO":
            d["max_input_tokens"] = 500
    assert router._try_esc_win(G200, None, ctx=5000) is None  # 5000 > 500


def test_try_none_when_caps_mismatch(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    got = router._try_esc_win(G200, frozenset({"vision"}), ctx=100)
    # K-GO non dichiara vision -> deve essere scartato
    assert got is None


def test_try_none_when_already_tried(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    assert router._try_esc_win(G200, None, ctx=100, tried={go["unique"]}) is None


def test_try_none_when_dep_removed(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    router._esc_win[G200] = ("drow_does_not_exist", time.time())
    assert router._try_esc_win(G200, None, ctx=100) is None


# ------------------------------------------------------- mark_failed clears
def test_mark_failed_clears_pin(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    assert G200 in router._esc_win
    router.mark_failed(go["unique"], seconds=60)
    assert G200 not in router._esc_win


def test_mark_failed_double_residual_clears_pin(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    router.mark_failed_double_residual(go["unique"], reason="x")
    assert G200 not in router._esc_win


# --------------------------------------------------- purge_expired cleans
def test_purge_removes_stale_pin(router):
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    router._esc_win[G200] = (go["unique"], time.time() - 9999)
    router.purge_expired()
    assert G200 not in router._esc_win


# --------------------------------------------------- fallback integration
def test_fallback_next_uses_pin_shortcut(router):
    small = _dep(router, G200, "K-SMALL")
    go = _dep(router, GGO, "K-GO")
    # pin: quando la -200k fallisce, atterra sul -go winner
    router.record_escalation_win(G200, go)
    nxt = router.fallback_next("test", small, None, "chain", ctx=100)
    assert nxt is not None and nxt["unique"] == go["unique"]


def test_fallback_next_shortcut_respects_ctx(router):
    small = _dep(router, G200, "K-SMALL")
    go = _dep(router, GGO, "K-GO")
    for d in router.config.groups[GGO]:
        if d["api_key"] == "K-GO":
            d["max_input_tokens"] = 500
    router.record_escalation_win(G200, go)
    # ctx enorme > max_input del winner -> scorciatoia ignorata (fallback normale)
    nxt = router.fallback_next("test", small, None, "chain", ctx=999999)
    # non deve ritornare il winner (non regge il contesto)
    assert not (nxt is not None and nxt["unique"] == go["unique"])


def test_fallback_no_pin_unchanged_behavior(router):
    # senza pin, fallback_next su dims cade nella scala (go raggiungibile)
    small = _dep(router, G200, "K-SMALL")
    nxt = router.fallback_next("test", small, None, "chain", ctx=100)
    assert nxt is not None                        # scala funziona comunque


# ---------------------------------------------------- initial_pick shortcut
def test_initial_pick_does_not_preempt_live_nominal(router):
    # c'e' un candidato vivo nel bucket richiesto -> NON si usa il pin
    go = _dep(router, GGO, "K-GO")
    router.record_escalation_win(G200, go)
    dep = router.initial_pick("test", G200, ctx=100)
    assert dep["api_key"] == "K-SMALL"            # bucket economico ha lapriorita'


def test_initial_pick_uses_pin_when_nominal_dead(router):
    go = _dep(router, GGO, "K-GO")
    small = _dep(router, G200, "K-SMALL")
    router.record_escalation_win(G200, go)
    # pick_deployment ha "ultima spiaggia" che ignora cooldown MA non _cap_fits.
    # ctx enorme rende il piccolo non-candidabile: pick_deployment -> None
    # e il flusso cade nel hook esc-pin.
    dep = router.initial_pick("test", G200, ctx=10_000_000)
    assert dep is not None and dep["unique"] == go["unique"]


# -------------------------------------------------------- ttl knob respected
def test_custom_ttl():
    r = _mkrouter(escalation_pin_ttl_sec=1)
    go = _dep(r, GGO, "K-GO")
    r.record_escalation_win(G200, go)
    r._esc_win[G200] = (go["unique"], time.time() - 2)   # oltre il ttl=1
    assert r._try_esc_win(G200, None, ctx=100) is None
