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
from app.router import Router, set_current_session

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
    # tried={small}: il bucket richiesto ha gia' fallito; senza dim intermedie
    # (e senza altre key -200k) il probe non trova nulla -> winner.
    nxt = router.fallback_next("test", small, None, "chain", ctx=100,
                               tried={small["unique"]}, requested_group=G200)
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


# ================================================== RICAMPIONAMENTO PRE-PIN
# CSV con dim intermedie 256k/1000k + una seconda key -200k. max_input: le
# -200k piccole (100) per poterle escludere con un ctx grande nei test del
# "bucket morto"; intermedie ampie (100000).
CSV_MULTI = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m/small,groq,https://api.groq.com/openai/v1,free,200,100,5,K-SMALL,
t@x,m/small2,groq,https://api.groq.com/openai/v1,free,200,100,5,K-SMALL2,
t@x,m/mid,groq,https://api.groq.com/openai/v1,free,256,100000,5,K-MID,
t@x,m/big,groq,https://api.groq.com/openai/v1,free,1000,100000,5,K-BIG,
t@x,m/go,groq,https://api.groq.com/openai/v1,,0,0,5,K-GO,
"""
G256 = f"{BASE}-256k"
G1000 = f"{BASE}-1000k"


def _mkrouter_multi(**polkw):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_MULTI)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}},
                            **polkw})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, pol)
    os.unlink(path)
    return r


def _cool(r, dep):
    r._cooldown[dep["unique"]] = time.time() + 9999


def test_pin_probe_one_per_tier_then_dims_then_winner():
    r = _mkrouter_multi()
    small = _dep(r, G200, "K-SMALL")
    go = _dep(r, GGO, "K-GO")
    r.record_escalation_win(G200, go)
    _cool(r, small)                      # come dopo un fallimento reale
    tried = {small["unique"]}
    # la -200k ha UN solo tier (order default) e quell'unico candidato e' gia'
    # stato provato: con '1 per tier' NON si ritenta un'altra key dello stesso
    # tier -> si passa subito alle dim intermedie.
    n1 = r.fallback_next("test", small, None, "chain", ctx=100,
                         tried=tried, requested_group=G200)
    assert n1 is not None and n1["group"] in (G256, G1000)
    tried.add(n1["unique"])
    # seconda dim intermedia, diversa dalla prima
    n2 = r.fallback_next("test", n1, None, "chain", ctx=100,
                         tried=tried, requested_group=G200)
    assert n2 is not None and n2["group"] in (G256, G1000)
    assert n2["group"] != n1["group"]
    tried.add(n2["unique"])
    # esaurite le 2 intermedie -> winner
    n3 = r.fallback_next("test", n2, None, "chain", ctx=100,
                         tried=tried, requested_group=G200)
    assert n3 is not None and n3["unique"] == go["unique"]


def test_pin_probe_disabled_returns_winner():
    r = _mkrouter_multi(escalation_pin_probe_dims=0)
    small = _dep(r, G200, "K-SMALL")
    go = _dep(r, GGO, "K-GO")
    r.record_escalation_win(G200, go)
    _cool(r, small)
    nxt = r.fallback_next("test", small, None, "chain", ctx=100,
                          tried={small["unique"]}, requested_group=G200)
    assert nxt is not None and nxt["unique"] == go["unique"]


def test_pin_probe_skips_cooled_intermediates():
    r = _mkrouter_multi()
    small = _dep(r, G200, "K-SMALL")
    small2 = _dep(r, G200, "K-SMALL2")
    go = _dep(r, GGO, "K-GO")
    r.record_escalation_win(G200, go)
    _cool(r, small)
    _cool(r, small2)
    for g in (G256, G1000):              # intermedie TUTTE in cooldown
        for d in r.config.groups[g]:
            _cool(r, d)
    nxt = r.fallback_next("test", small2, None, "chain", ctx=100,
                          tried={small["unique"], small2["unique"]},
                          requested_group=G200)
    assert nxt is not None and nxt["unique"] == go["unique"]


def test_pin_probe_no_retry_after_two_nominal():
    r = _mkrouter_multi()
    small = _dep(r, G200, "K-SMALL")
    small2 = _dep(r, G200, "K-SMALL2")
    go = _dep(r, GGO, "K-GO")
    r.record_escalation_win(G200, go)
    _cool(r, small)
    _cool(r, small2)
    # gia' 2 tentativi nella dim richiesta -> niente terzo retry: si va
    # direttamente alle intermedie / winner.
    nxt = r.fallback_next("test", small2, None, "chain", ctx=100,
                          tried={small["unique"], small2["unique"]},
                          requested_group=G200)
    assert nxt is not None and nxt["group"] in (G256, G1000)


def test_initial_pick_dead_bucket_probes_intermediates():
    r = _mkrouter_multi()
    go = _dep(r, GGO, "K-GO")
    r.record_escalation_win(G200, go)
    # ctx 50000: il bucket -200k (max_input 100) NON e' candidabile -> pin;
    # il probe deve partire da una dim intermedia VIVA (256/1000).
    dep = r.initial_pick("test", G200, ctx=50000)
    assert dep is not None and dep["group"] in (G256, G1000)
    assert dep["unique"] != go["unique"]


# ============================== UNO PER TIER (richiesta C) ===================
# Bucket -200k con DUE tier (order 0 e order 6): il probe pre-pin deve provare
# 1 candidato per OGNI tier vivo, non solo il primo del tier minimo.
CSV_TIERS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,order
t@x,m/a0,groq,https://api.groq.com/openai/v1,free,200,100000,5,K-A0,,0
t@x,m/a1,groq,https://api.groq.com/openai/v1,free,200,100000,5,K-A1,,0
t@x,m/b6,groq,https://api.groq.com/openai/v1,free,200,100000,5,K-B6,,6
t@x,m/go,groq,https://api.groq.com/openai/v1,,0,0,5,K-GO,
"""


def _mkrouter_tiers(**polkw):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_TIERS)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}},
                            **polkw})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, pol)
    os.unlink(path)
    return r


def test_pick_tiered_one_per_tier():
    r = _mkrouter_tiers()
    first = r._pick_tiered(G200, tried=set())
    assert first is not None and int(first["order"]) == 0
    second = r._pick_tiered(G200, tried={first["unique"]})
    assert second is not None and int(second["order"]) == 6
    third = r._pick_tiered(G200, tried={first["unique"], second["unique"]})
    assert third is None


def test_pick_tiered_skips_cooled_tier():
    r = _mkrouter_tiers()
    for d in r.config.groups[G200]:
        if int(d["order"]) == 0:
            _cool(r, d)                       # tier 0 tutto in cooldown
    got = r._pick_tiered(G200, tried=set())
    assert got is not None and int(got["order"]) == 6


def test_pick_tiered_none_when_all_tried():
    r = _mkrouter_tiers()
    allu = {d["unique"] for d in r.config.groups[G200]}
    assert r._pick_tiered(G200, tried=allu) is None


def test_pin_probe_walks_all_tiers_of_requested_dim():
    """Con il pin attivo, i primi tentativi restano nella dim richiesta
    scorrendo TUTTI i suoi tier (0, 6) e solo dopo (nessuna intermedia qui)
    si lascia il posto al winner."""
    r = _mkrouter_tiers()
    go = _dep(r, GGO, "K-GO")
    r.record_escalation_win(G200, go)
    tried = set()
    c1 = r._esc_pin_probe(G200, go, need=None, ctx=100, tried=tried)
    assert c1 is not None and int(c1["order"]) == 0
    tried.add(c1["unique"])
    c2 = r._esc_pin_probe(G200, go, need=None, ctx=100, tried=tried)
    assert c2 is not None and int(c2["order"]) == 6
    tried.add(c2["unique"])
    # tier esauriti e nessuna dim intermedia -> None (il chiamante usa il winner)
    assert r._esc_pin_probe(G200, go, need=None, ctx=100, tried=tried) is None


# ============ SKIP winner==detentore SOLO stesso bucket (regressione live) ===
# note_session_success registra il detentore cache su QUALSIASI successo,
# renewal/pagati inclusi: una sessione che una volta e' salita a -go ha
# holder=-go. Se il pin winner (-go) == holder (-go) saltava il probe ANCHE
# per una richiesta free -Nk, la sessione restava incollata a -go senza mai
# riprovare la dim richiesta (in log: [esc-pin] 'salto la scala' con
# 'winner==detentore: salto il probe' a ripetizione). Lo skip deve valere
# solo quando winner e holder sono NELLO STESSO bucket della richiesta.

def test_probe_retry_not_skipped_when_holder_is_paid_bucket():
    r = _mkrouter_tiers()
    go = _dep(r, GGO, "K-GO")
    r.note_session_success("s1", go["unique"])     # holder = -go (scalata)
    r.record_escalation_win(G200, go)
    set_current_session("s1")
    try:
        cand = r._esc_pin_probe(G200, go, need=None, ctx=100, tried=set())
    finally:
        set_current_session(None)
    assert cand is not None                        # il probe NON va saltato
    assert cand["group"] == G200                   # e resta nella free-dim


def test_probe_retry_still_skipped_when_holder_same_bucket():
    r = _mkrouter_tiers()
    a0 = _dep(r, G200, "K-A0")
    r.note_session_success("s1", a0["unique"])     # holder nel bucket richiesto
    r.record_escalation_win(G200, a0)
    set_current_session("s1")
    try:
        assert r._esc_pin_probe(G200, a0, need=None, ctx=100, tried=set()) is None
    finally:
        set_current_session(None)

