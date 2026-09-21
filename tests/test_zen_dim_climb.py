"""Zen-first: la salita di dim di un client opencode NATIVO non deve uscire
dal tier zen (free) quando esiste una dim zen capiente.

Contesto: gli zen (max_input 200k/262k) vivono solo in alcune -dim; le dim
intermedie (es. -256k) e le grandi (-512k/-1000k) sono senza zen. Prima del
fix una richiesta nativa con ctx 210k-262k finiva su -256k/-512k (provider
non-free); ora resta su -262k (zen) e sara' la compattazione in `main` a
decidere se basta.
"""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.opencode_gate import (set_allow_opencode_zen, set_spoofing_request,
                               set_zen_first)
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test
t@x.com,m/z200,opencode-zen,https://opencode.ai/zen/v1,free,200,200000,5,K-Z200
t@x.com,m/g200,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-G200
t@x.com,m/g256,groq,https://api.groq.com/openai/v1,free,256,256000,5,K-G256
t@x.com,m/z262,opencode-zen,https://opencode.ai/zen/v1,free,262,262000,5,K-Z262
t@x.com,m/g262,groq,https://api.groq.com/openai/v1,free,262,262000,5,K-G262
t@x.com,m/g512,groq,https://api.groq.com/openai/v1,free,512,512000,5,K-G512
t@x.com,m/g1000,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-G1000
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    yield r
    os.unlink(path)


@pytest.fixture(autouse=True)
def _clean():
    set_allow_opencode_zen(None)
    set_spoofing_request(False)
    set_zen_first(False)
    yield
    set_allow_opencode_zen(None)
    set_spoofing_request(False)
    set_zen_first(False)


def test_group_has_zen(router):
    r = router
    assert r.group_has_zen(f"{BASE}-200k") is True
    assert r.group_has_zen(f"{BASE}-262k") is True
    assert r.group_has_zen(f"{BASE}-256k") is False
    assert r.group_has_zen(f"{BASE}-512k") is False
    assert r.group_has_zen(None) is False


def test_climb_dim_group_invariato(router):
    """`climb_dim_group` (senza kwarg) resta identico: nessuna regressione."""
    r = router
    assert r.climb_dim_group(f"{BASE}-200k", 250000) == f"{BASE}-256k"
    assert r.climb_dim_group(f"{BASE}-200k", 300000) == f"{BASE}-512k"


def test_nativo_non_esce_dal_tier_zen(router):
    r = router
    set_zen_first(True)
    # 210k e 250k entrano nella dim zen -262k (non -256k, priva di zen).
    assert r.resolve_group_for_request(
        f"{BASE}-200k", [], "ses_x", None, 210000) == f"{BASE}-262k"
    assert r.resolve_group_for_request(
        f"{BASE}-200k", [], "ses_x", None, 250000) == f"{BASE}-262k"
    # 300k: nessuna dim zen capiente -> si resta sulla piu' grande zen
    # (-262k); compattazione/salita non-zen sono decise in `main` (F31).
    assert r.resolve_group_for_request(
        f"{BASE}-200k", [], "ses_x", None, 300000) == f"{BASE}-262k"


def test_non_nativo_invariato(router):
    r = router
    set_zen_first(False)
    assert r.resolve_group_for_request(
        f"{BASE}-200k", [], "ses_x", None, 210000) == f"{BASE}-256k"
    assert r.resolve_group_for_request(
        f"{BASE}-200k", [], "ses_x", None, 300000) == f"{BASE}-512k"


def test_zen_ladder_cross_dim(router):
    """Il canary zen dedicato vede gli zen di TUTTE le dim, non solo di quella
    richiesta (qui -512k, priva di zen)."""
    r = router
    lad = r._zen_ladder_for("test")
    assert any("z200" in u for u in lad)
    assert any("z262" in u for u in lad)
    assert not any("g200" in u or "g256" in u or "g512" in u for u in lad)


def test_canary_zen_cross_dim(router):
    """Regressione del root cause: con `only_zen=True` il canary deve trovare
    uno zen anche se la -dim richiesta (-512k) non ne ha."""
    r = router
    set_allow_opencode_zen(True)
    set_zen_first(True)
    cur = r.config.groups[f"{BASE}-512k"][0]
    z = r.warm_fill_canary("test", cur, None, 10000, 1000,
                           requested_group=f"{BASE}-512k", only_zen=True)
    assert z is not None and "z" in z["unique"]
    # ctx oltre il max_input di ogni zen: nessun probe sprecato.
    assert r.warm_fill_canary("test", cur, None, 400000, 1000,
                              requested_group=f"{BASE}-512k",
                              only_zen=True) is None

