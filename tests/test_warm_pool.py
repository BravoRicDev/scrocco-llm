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
from app.router import (Router, set_current_session,
                        LATENCY_ROTATE_THRESHOLD_MS)

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
 t@x.com,m-b,groq,https://api.groq.com/openai/v1,free,64,4000,0,K-B,text
 t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-A,text
 t@x.com,m-c,groq,https://api.groq.com/openai/v1,free,64,128000,0,K-C,text
 t@x.com,mgo,groq,https://api.groq.com/openai/v1,,64,8000,0,K-G,text
 t@x.com,mfb,groq,https://api.groq.com/openai/v1,fallback,64,8000,0,K-FB,text
 t@x.com,m-200,groq,https://api.groq.com/openai/v1,free,200,200000,0,K-200,text
 t@x.com,m-1000,groq,https://api.groq.com/openai/v1,free,1000,1000000,0,K-1000,text
 """
GROUP = "scrocco-llm-test-64k"
GO = "scrocco-llm-test-go"
FB = "scrocco-llm-test-fallback"
DIM200 = "scrocco-llm-test-200k"
DIM1000 = "scrocco-llm-test-1000k"


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


# ------------------------------------------------------ guardia di latenza
def test_is_slow_dep_threshold(router):
    b = _u(router, GROUP, "K-B")
    assert router._is_slow_dep(b) is False          # EMA ignota -> non lento
    router._avg_latencies[b] = 3000.0
    assert router._is_slow_dep(b) is False
    router._avg_latencies[b] = LATENCY_ROTATE_THRESHOLD_MS
    assert router._is_slow_dep(b) is False          # soglia inclusa
    router._avg_latencies[b] = LATENCY_ROTATE_THRESHOLD_MS + 1
    assert router._is_slow_dep(b) is True


def test_warm_pool_excludes_slow_dep(router):
    """Un dep con EMA sopra soglia NON resta nel tier caldo: e' riserva nel
    ladder ma non viene riproposto come scelta calda."""
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b)
    router._avg_latencies[b] = LATENCY_ROTATE_THRESHOLD_MS + 1
    assert router._warm_pool("S-A", _allowed(router)) == []


def test_sticky_rejects_slow_dep(router):
    """Lo sticky non deve incollare la sessione a un dep divenuto lento."""
    a = _u(router, GROUP, "K-A")
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b)
    router._avg_latencies[b] = LATENCY_ROTATE_THRESHOLD_MS + 1
    set_current_session("S-A")
    router.dep_sticky_set("S-A", b)
    got = router.initial_pick("test", GROUP, None, 1000, session_id="S-A",
                              warm=False)
    assert got is not None and got["unique"] != b


def test_cache_holder_rejects_slow_dep(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b)
    assert router.cache_holder("S-A", ctx=1000) is not None
    router._avg_latencies[b] = LATENCY_ROTATE_THRESHOLD_MS + 1
    assert router.cache_holder("S-A", ctx=1000) is None


# -------------------------------------------------- primo contenuto adattivo
def test_first_content_deadline_fixed(router):
    router.policy.qc_json.stream_first_content_ms = 180000
    router.policy.qc_json.stream_first_content_adaptive = False
    b = _u(router, GROUP, "K-B")
    router._avg_latencies[b] = 5000.0
    assert router.first_content_deadline_ms(b) == 180000


def test_first_content_deadline_adaptive(router):
    q = router.policy.qc_json
    q.stream_first_content_ms = 180000
    q.stream_first_content_mult = 3.0
    q.stream_first_content_floor_ms = 20000
    b = _u(router, GROUP, "K-B")
    router._avg_latencies[b] = 5000.0      # 3*5s=15s -> floor 20s
    assert router.first_content_deadline_ms(b) == 20000
    router._avg_latencies[b] = 30000.0     # 3*30s=90s
    assert router.first_content_deadline_ms(b) == 90000
    router._avg_latencies[b] = 90000.0     # 3*90s=270s -> cap 180s
    assert router.first_content_deadline_ms(b) == 180000


def test_first_content_deadline_unknown_ema(router):
    q = router.policy.qc_json
    q.stream_first_content_ms = 180000
    b = _u(router, GROUP, "K-B")
    assert router.first_content_deadline_ms(b) == 180000


def test_first_content_deadline_default_cap(router):
    """Senza override la cap resta il default di policy (20s)."""
    b = _u(router, GROUP, "K-B")
    assert router.policy.qc_json.stream_first_content_ms == 20000
    assert router.first_content_deadline_ms(b) == 20000


# ---------------------------------------------- demote "lenti per la sessione"
def test_session_slow_mark_and_clear(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b,
                                latency_ms=LATENCY_ROTATE_THRESHOLD_MS + 1)
    assert router.is_slow_for_session(b, "S-A") is True
    # un successo rapido ripulisce il marchio
    router.note_session_success("S-A", b, latency_ms=1000)
    assert router.is_slow_for_session(b, "S-A") is False


def test_session_slow_is_per_session(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b,
                                latency_ms=LATENCY_ROTATE_THRESHOLD_MS + 1)
    assert router.is_slow_for_session(b, "S-A") is True
    assert router.is_slow_for_session(b, "S-B") is False


def test_session_slow_only_free_dims(router):
    """Un successo lento su un bucket -go NON marchia (solo free-dims)."""
    g = _u(router, GO, "K-G")
    router.note_session_success("S-A", g,
                                latency_ms=LATENCY_ROTATE_THRESHOLD_MS + 1)
    assert router.is_slow_for_session(g, "S-A") is False


def test_warm_pool_excludes_session_slow(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b,
                                latency_ms=LATENCY_ROTATE_THRESHOLD_MS + 1)
    assert router._warm_pool("S-A", _allowed(router)) == []


def test_pick_deployment_excludes_session_slow(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b,
                                latency_ms=LATENCY_ROTATE_THRESHOLD_MS + 1)
    set_current_session("S-A")
    got = router.pick_deployment(GROUP, ctx=1000)
    assert got is not None and got["unique"] != b


def test_walk_chain_excludes_session_slow_but_respects_allow_slow(router):
    b = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b,
                                latency_ms=LATENCY_ROTATE_THRESHOLD_MS + 1)
    set_current_session("S-A")
    assert router._walk_chain([b], None, None, 1000) is None
    got = router._walk_chain([b], None, None, 1000, allow_slow=True)
    assert got is not None and got["unique"] == b


def test_prelast_shared_skips_session_slow(router):
    b = _u(router, GROUP, "K-B")
    # b occupato da un'ALTRA sessione -> eleggibile nel tier prelast...
    router.note_session_success("S-B", b, latency_ms=1000)
    set_current_session("S-A")
    assert router.prelast_shared([b], None, None, 1000) is not None
    # ...ma se e' LENTO per la sessione S-A, sparisce anche da lì.
    router.note_session_success("S-A", b,
                                latency_ms=LATENCY_ROTATE_THRESHOLD_MS + 1)
    assert router.prelast_shared([b], None, None, 1000) is None


def test_ladder_reaches_session_slow_only_at_fallback(router):
    """Il free-dim lento-per-sessione non viene pescato nella scala normale:
    torna solo all'ultimo scaglione (-fallback)."""
    b = _u(router, GROUP, "K-B")
    fb = _u(router, FB, "K-FB")
    router.note_session_success("S-A", b,
                                latency_ms=LATENCY_ROTATE_THRESHOLD_MS + 1)
    set_current_session("S-A")
    got = router._walk_ladder_resilient([b, fb], None, None, 1000)
    assert got is not None and got["unique"] == fb


def test_ladder_uses_dim_when_not_slow(router):
    b = _u(router, GROUP, "K-B")
    fb = _u(router, FB, "K-FB")
    router.note_session_success("S-A", b, latency_ms=3000)
    set_current_session("S-A")
    got = router._walk_ladder_resilient([b, fb], None, None, 1000)
    assert got is not None and got["unique"] == b


# ------------------------------------------------ floor sulla dim richiesta
def test_warm_pool_respects_requested_dim_floor(router):
    """Richiesta esplicita -200k: un caldo -64k della stessa sessione NON va
    pescato (la dim richiesta e' una soglia MINIMA)."""
    b64 = _u(router, GROUP, "K-B")
    router.note_session_success("S-A", b64)   # caldo 64k
    set_current_session("S-A")
    got = router.initial_pick("test", DIM200, None, 1000, session_id="S-A")
    assert got is not None and got["unique"] != b64


def test_warm_pool_uses_warm_at_requested_dim(router):
    b200 = _u(router, DIM200, "K-200")
    router.note_session_success("S-A", b200)
    set_current_session("S-A")
    got = router.initial_pick("test", DIM200, None, 1000, session_id="S-A")
    assert got is not None and got["unique"] == b200


def test_warm_pool_allows_larger_warm_dim(router):
    """Un caldo di dim SUPERIORE alla richiesta resta valido (>= soglia)."""
    b1000 = _u(router, DIM1000, "K-1000")
    router.note_session_success("S-A", b1000)
    set_current_session("S-A")
    got = router.initial_pick("test", DIM200, None, 1000, session_id="S-A")
    assert got is not None and got["unique"] == b1000


def test_warm_allowed_floor_helper(router):
    allowed = router._warm_allowed("test", DIM200)
    assert _u(router, GROUP, "K-B") not in allowed
    assert _u(router, DIM200, "K-200") in allowed
    assert _u(router, DIM1000, "K-1000") in allowed
