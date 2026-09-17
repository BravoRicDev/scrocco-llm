"""Gate per-client degli upstream opencode.ai nel ROUTER.

opencode.ai/zen e /zen/go rispondono 403 ai client non-opencode. Il router
deve quindi trattarli come NON disponibili (pick a freddo, catena, warm pool
propri e PRESTATI, capacita'/dims, canary, ultima spiaggia) quando il client
non e' opencode e lo spoof (env OPENCODE_SPOOF_HEADERS) e' off.

Fixture: un CSV con due dim distinte, cosi' i gruppi sono deterministici:
  - `-100k` ospita SOLO il deployment opencode-zen;
  - `-200k` ospita SOLO il deployment normale (groq).
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.opencode_gate import set_allow_opencode_zen, set_spoofing_request
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
t@x.com,m/oc,opencode-zen,https://opencode.ai/zen/v1,free,100,100000,5,K-OC
t@x.com,m/plain,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-PL
"""

POLICY = {"capability_routing": {"model_capabilities": {
    "m/oc": ["text"], "m/plain": ["text"]}}}


@pytest.fixture(autouse=True)
def _gate_off(monkeypatch):
    monkeypatch.delenv("OPENCODE_SPOOF_HEADERS", raising=False)
    monkeypatch.delenv("OPENCODE_GO", raising=False)
    monkeypatch.delenv("OPENCODE_CAUTIOUS", raising=False)
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


# --------------------------------------------------------- pick a freddo
def test_pick_excludes_opencode_when_disallowed(router):
    set_allow_opencode_zen(False)
    assert router.pick_deployment(f"{BASE}-100k", frozenset({"text"})) is None
    plain = router.pick_deployment(f"{BASE}-200k", frozenset({"text"}))
    assert plain is not None and plain["model"] == "m/plain"


def test_pick_includes_opencode_when_allowed(router):
    set_allow_opencode_zen(True)
    oc = router.pick_deployment(f"{BASE}-100k", frozenset({"text"}))
    assert oc is not None and oc["model"] == "m/oc"


def test_pick_includes_opencode_with_spoof(router, monkeypatch):
    set_allow_opencode_zen(None)                     # nessuna decisione per-request
    monkeypatch.setenv("OPENCODE_SPOOF_HEADERS", "1")
    oc = router.pick_deployment(f"{BASE}-100k", frozenset({"text"}))
    assert oc is not None and oc["model"] == "m/oc"


# --------------------------------------------------------------- warm
def test_warm_pool_excludes_opencode_when_disallowed(router):
    oc = _dep(router, f"{BASE}-100k", "K-OC")
    plain = _dep(router, f"{BASE}-200k", "K-PL")
    router.note_session_success("s1", oc["unique"], 100, ctx_est=100)
    router.note_session_success("s1", plain["unique"], 100, ctx_est=100)

    set_allow_opencode_zen(False)
    pool = router._warm_pool("s1", None, None, 100)
    assert {d["model"] for d in pool} == {"m/plain"}

    set_allow_opencode_zen(True)
    pool2 = router._warm_pool("s1", None, None, 100)
    assert {d["model"] for d in pool2} == {"m/oc", "m/plain"}


def test_warm_pool_excludes_borrowed_opencode(router):
    oc = _dep(router, f"{BASE}-100k", "K-OC")
    plain = _dep(router, f"{BASE}-200k", "K-PL")
    router.note_session_success("owner", oc["unique"], 100, ctx_est=100)
    router.note_session_success("owner", plain["unique"], 100, ctx_est=100)
    router.policy.warm_borrow_idle_sec = 0.0     # prestabili subito

    set_allow_opencode_zen(False)
    pool = router._warm_pool("newbie", None, None, None, include_borrowed=True)
    assert {d["model"] for d in pool} == {"m/plain"}

    set_allow_opencode_zen(True)
    pool2 = router._warm_pool("newbie", None, None, None, include_borrowed=True)
    assert {d["model"] for d in pool2} == {"m/oc", "m/plain"}


# ------------------------------------------------- capacita' / dims
def test_capable_dims_respects_gate(router):
    set_allow_opencode_zen(False)
    assert router._capable_dims("test", frozenset({"text"})) == [200]
    assert not router._any_capable_in_group(f"{BASE}-100k", frozenset({"text"}))
    assert router._any_capable_in_group(f"{BASE}-200k", frozenset({"text"}))

    set_allow_opencode_zen(True)
    assert router._capable_dims("test", frozenset({"text"})) == [100, 200]


# ------------------------------------------------------------- canary
def test_canary_never_picks_opencode_when_disallowed(router):
    set_allow_opencode_zen(False)
    cur = _dep(router, f"{BASE}-200k", "K-PL")
    got = router.warm_fill_canary("test", cur, frozenset({"text"}), 100,
                                  out_tokens=1000, tried=None,
                                  requested_group=f"{BASE}-200k",
                                  exclude_keys=None, exclude_uniq=None,
                                  sampled_tiers=None)
    assert got is None or got["model"] == "m/plain"
