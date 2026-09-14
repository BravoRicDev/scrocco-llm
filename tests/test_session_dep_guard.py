"""SESSION-DEP GUARD: fra i vivi si IGNORA un free-dims servito con successo
da un'ALTRA sessione negli ultimi session_dep_guard_sec. Torna eleggibile solo
nel tier "pre-ultima-spiaggia" (prima di -go), ordinato per max_input crescente.
Target: rate-limit/accaparramento quando piu' sessioni concorrono."""
import os
import time
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router, set_current_session

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-b,groq,https://api.groq.com/openai/v1,free,64,4000,0,K-B,text
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-A,text
t@x.com,m-c,groq,https://api.groq.com/openai/v1,free,64,128000,0,K-C,text
t@x.com,mgo,groq,https://api.groq.com/openai/v1,,64,8000,0,K-G,text
"""
GROUP = "scrocco-llm-test-64k"
GO = "scrocco-llm-test-go"


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    yield r
    os.unlink(path)


def _dep(router, group, key):
    return next(d for d in router.config.groups[group] if d["api_key"] == key)


def _u(router, group, key):
    return _dep(router, group, key)["unique"]


def test_other_session_recent_blocks_and_expires(router):
    a = _u(router, GROUP, "K-A")
    set_current_session("S-B")
    router.note_session_success("S-A", a)
    assert router.other_session_recent(a) is True
    # stessa sessione che l'ha usato: NON bloccata
    set_current_session("S-A")
    assert router.other_session_recent(a) is False
    # scaduta la finestra: tornato normale
    router._dep_last_session[a] = ("S-A", time.time() - 1000)
    set_current_session("S-B")
    assert router.other_session_recent(a) is False


def test_pick_excludes_other_session_dep(router):
    a = _u(router, GROUP, "K-A")
    set_current_session("S-A")
    router.note_session_success("S-A", a)
    set_current_session("S-B")
    for _ in range(30):
        got = router.pick_deployment(GROUP)
        assert got is not None and got["unique"] != a


def test_same_session_can_reuse(router):
    a = _u(router, GROUP, "K-A")
    b = _u(router, GROUP, "K-B")
    c = _u(router, GROUP, "K-C")
    set_current_session("S-A")
    router.note_session_success("S-A", a)
    # raffreddo gli altri: deve tornare a, usabile dalla sua sessione
    router.mark_failed(b, seconds=600)
    router.mark_failed(c, seconds=600)
    got = router.pick_deployment(GROUP)
    assert got is not None and got["unique"] == a


def test_guard_disabled(router):
    router.policy.session_dep_guard_enabled = False
    a = _u(router, GROUP, "K-A")
    b = _u(router, GROUP, "K-B")
    c = _u(router, GROUP, "K-C")
    set_current_session("S-A")
    router.note_session_success("S-A", a)
    set_current_session("S-B")
    router.mark_failed(b, seconds=600)
    router.mark_failed(c, seconds=600)
    got = router.pick_deployment(GROUP)
    assert got is not None and got["unique"] == a


def test_go_bucket_never_tracked(router):
    g = _u(router, GO, "K-G")
    router.note_session_success("S-A", g)
    assert g not in router._dep_last_session


def test_prelast_shared_orders_by_max_input(router):
    a = _u(router, GROUP, "K-A")   # 8000
    b = _u(router, GROUP, "K-B")   # 4000
    c = _u(router, GROUP, "K-C")   # 128000
    set_current_session("S-A")
    router.note_session_success("S-A", a)
    router.note_session_success("S-B", b)
    router.note_session_success("S-C", c)
    set_current_session("S-D")
    chain = [c, a, b]
    got = router.prelast_shared(chain, None)
    assert got is not None and got["unique"] == b        # 4000: fit migliore
    # ctx 5000: il 4000 non regge -> passa all'8000
    got = router.prelast_shared(chain, None, ctx=5000)
    assert got is not None and got["unique"] == a


def test_prelast_shared_empty_when_only_own(router):
    a = _u(router, GROUP, "K-A")
    set_current_session("S-A")
    router.note_session_success("S-A", a)
    assert router.prelast_shared([a], None) is None        # solo la propria


def test_prelast_shared_respects_disabled(router):
    router.policy.session_dep_guard_enabled = False
    a = _u(router, GROUP, "K-A")
    set_current_session("S-A")
    router.note_session_success("S-A", a)
    set_current_session("S-B")
    assert router.prelast_shared([a], None) is None
