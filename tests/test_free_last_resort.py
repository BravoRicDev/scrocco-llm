"""ULTIMA RISORSA FREE: quando un bucket -go/-fallback e' esaurito si scende
ai free-dims (warm di chiunque cap-ok -> canary -> cooled/retired estremo)
invece di consegnare un 503.

Copre:
  - descent da -go e da -fallback;
  - warm "di chiunque" (prestato da un'altra sessione) preferito al canary;
  - canary quando non c'e' warm;
  - estrema: free in cooldown pur di non arrendersi;
  - None solo se nessun free soddisfa il cap;
  - knob `free_last_resort_enabled` off.
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.opencode_gate import set_allow_opencode_zen, set_spoofing_request
from app.policy import Policy
from app.router import Router
from app.session_ctx import set_current_session

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,order,scrocco-llm-test
t@x.com,m/p1,groq,https://api.groq.com/openai/v1,free,100,100000,5,10,K-P1
t@x.com,m/z1,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,5,0,K-Z1
t@x.com,m/g1,opencode-go,https://opencode.ai/zen/go/v1,15,200,200000,5,20,K-G1
t@x.com,m/f1,groq,https://api.groq.com/openai/v1,fallback,100,100000,5,5,K-F1
"""

POLICY = {"capability_routing": {"model_capabilities": {
    "m/p1": ["text"], "m/z1": ["text"], "m/g1": ["text"], "m/f1": ["text"]}},
    "ladder_skip_after": 20, "ladder_stale_max": 10,
    "dims_ladder_floor": True, "deployment_sticky": False}

NEED = frozenset({"text"})


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
    set_current_session(None)


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    pol = Policy.from_dict(POLICY)
    pol.warm_borrow_idle_sec = 0.0     # prestito immediato nei test
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, pol)
    yield r
    os.unlink(path)


def _grp(r, suffix):
    return next(g for g in r.config.groups if g.endswith(suffix))


def _exhaust_esc(r, go):
    fb = r.config.groups[f"{BASE}-fallback"][0]
    return {go["unique"], fb["unique"]}


def test_go_exhausted_descends_to_warm_of_anyone(router):
    go = router.config.groups[f"{BASE}-go"][0]
    tried = _exhaust_esc(router, go)
    # un'ALTRA sessione ha servito con successo il free -> warm prestabile
    free = router.config.groups[f"{BASE}-100k"][0]
    router.note_session_success("other-sess", free["unique"], 100, ctx_est=100)
    set_current_session("req-sess")
    nxt = router.fallback_next("test", go, NEED, "chain", ctx=100,
                               tried=tried, out_tokens=100)
    assert nxt is not None
    assert nxt["group"] == f"{BASE}-100k"      # sceso al free, non 503


def test_go_exhausted_uses_canary_without_warm(router):
    go = router.config.groups[f"{BASE}-go"][0]
    tried = _exhaust_esc(router, go)
    set_current_session("req-sess")
    nxt = router.fallback_next("test", go, NEED, "chain", ctx=100,
                               tried=tried, out_tokens=100)
    assert nxt is not None
    assert nxt["group"] == f"{BASE}-100k"


def test_fallback_bucket_also_descends(router):
    fb = router.config.groups[f"{BASE}-fallback"][0]
    set_current_session("req-sess")
    nxt = router.fallback_next("test", fb, NEED, "chain", ctx=100,
                               tried={fb["unique"]}, out_tokens=100)
    assert nxt is not None
    assert nxt["group"] == f"{BASE}-100k"


def test_extreme_uses_cooled_free(router):
    go = router.config.groups[f"{BASE}-go"][0]
    free = router.config.groups[f"{BASE}-100k"][0]
    tried = _exhaust_esc(router, go)
    # l'unico free e' in cooldown: la risorsa estrema lo riprende comunque
    router.mark_failed(free["unique"], seconds=600)
    set_current_session("req-sess")
    nxt = router.fallback_next("test", go, NEED, "chain", ctx=100,
                               tried=tried, out_tokens=100)
    assert nxt is not None
    assert nxt["group"] == f"{BASE}-100k"


def test_none_when_no_capable_free(router):
    go = router.config.groups[f"{BASE}-go"][0]
    set_current_session("req-sess")
    # ctx oltre il max_input di TUTTI i free -> nessun candidato
    nxt = router.fallback_next("test", go, NEED, "chain", ctx=150000,
                               tried={go["unique"]}, out_tokens=100)
    assert nxt is None


def test_disabled_knob_returns_none(router):
    go = router.config.groups[f"{BASE}-go"][0]
    tried = _exhaust_esc(router, go)
    router.policy.free_last_resort_enabled = False
    set_current_session("req-sess")
    nxt = router.fallback_next("test", go, NEED, "chain", ctx=100,
                               tried=tried, out_tokens=100)
    assert nxt is None
