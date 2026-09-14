"""WARM POOL (tier "caldi"): PRIMA del -dim richiesto e della scala si
esauriscono i free-dims che QUESTA sessione ha gia' servito con successo
(ancora vivi, non in cooldown, compatibili need+max_input). Ordine:
cache-holder, poi MRU, poi order, poi max_input. Target: prima di stressare
altri deployment si sfrutta la cache calda propria."""
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


def _allowed(router):
    return set(router.config.chains["test"])


def test_group_profile_scoping(router):
    assert router._group_profile(GROUP) == "test"
    assert router._group_profile(GO) == "test"
    assert router._group_profile("scrocco-llm-unknown-64k") is None
    assert router._group_profile("scrocco-llm-test-vision") is None


def test_warm_pool_returns_own_success(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b)
    assert router._warm_pool("S-A", _allowed(router)) == \
        [router.config.deployment_by_unique(b)]


def test_warm_pool_cache_holder_first(router):
    a = _u(router, GROUP, "K-A")   # max_input 8000
    b = _u(router, GROUP, "K-B")   # max_input 4000
    router.note_session_success("S-A", b)
    router.note_session_success("S-A", a)   # holder = A (ultimo successo)
    got = router._warm_pool("S-A", _allowed(router))
    assert [d["unique"] for d in got] == [a, b]


def test_warm_pool_respects_ctx(router):
    b = _u(router, GROUP, "K-B")   # max_input 4000
    router.note_session_success("S-A", b)
    assert router._warm_pool("S-A", _allowed(router), ctx=3000)
    assert router._warm_pool("S-A", _allowed(router), ctx=5000) == []


def test_warm_pool_excludes_cooled(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b)
    router.mark_failed(b, seconds=600)
    assert router._warm_pool("S-A", _allowed(router)) == []


def test_warm_pool_excludes_other_session_takeover(router):
    """`a` resta in _session_deps di S-A ma l'ultima sessione e' S-B: non e'
    piu' un caldo di S-A e va escluso."""
    a = _u(router, GROUP, "K-A")
    router.note_session_success("S-A", a)
    router.note_session_success("S-B", a)
    assert a in router._session_deps.get("S-A", set())
    assert router._warm_pool("S-A", _allowed(router)) == []


def test_warm_pool_disabled(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b)
    router.policy.warm_pool_enabled = False
    assert router._warm_pool("S-A", _allowed(router)) == []


def test_warm_pool_allowed_filter(router):
    a = _u(router, GROUP, "K-A")
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", a)
    router.note_session_success("S-A", b)
    got = router._warm_pool("S-A", {b})
    assert [d["unique"] for d in got] == [b]


def test_warm_pool_expired(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b)
    router._dep_last_session[b] = ("S-A", time.time() - 1000)
    assert router._warm_pool("S-A", _allowed(router)) == []


def test_initial_pick_uses_warm(router):
    """initial_pick sceglie il caldo prima del -dim (ctx/need compatibili)."""
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b)
    set_current_session("S-A")
    got = router.initial_pick("test", GROUP, None, 1000, session_id="S-A")
    assert got is not None and got["unique"] == b


def test_initial_pick_warm_flag_gates_pool(router, monkeypatch):
    """warm=False non consulta affatto il pool caldi."""
    calls = []

    def spy(*args, **kwargs):
        calls.append(1)
        return []

    monkeypatch.setattr(router, "_warm_pool", spy)
    router.initial_pick("test", GROUP, None, 1000,
                        session_id="S-A", warm=False)
    assert calls == []
    router.initial_pick("test", GROUP, None, 1000,
                        session_id="S-A", warm=True)
    assert calls


def test_warm_step_in_ladder(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b)
    set_current_session("S-A")
    got = router._walk_ladder_resilient(router.config.chains["test"],
                                        None, None, None)
    assert got is not None and got["unique"] == b


def test_warm_step_skips_escalation_only_scope(router):
    """Se lo scope contiene solo il bucket -go (escalation deliberata), il
    caldo dim NON deve essere scelto: resta confinato al proprio mondo."""
    b = _u(router, GROUP, "K-B")
    go = _u(router, GO, "K-G")
    router.note_session_success("S-A", b)
    set_current_session("S-A")
    assert router._warm_pool("S-A", {go}) == []
    got = router._walk_ladder_resilient([go], None, None, 1000)
    assert got is not None and got["unique"] == go
