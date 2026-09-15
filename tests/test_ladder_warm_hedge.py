"""Warm ammette i lenti (P1/P3), wakeup solo-429 a finestra, canary cross-tier
(hedge_canaries) e warm-ownership dal canary (P4)."""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router, set_current_session

BASE = "scrocco-llm-test"
D64 = f"{BASE}-64k"
D200 = f"{BASE}-200k"
GO = f"{BASE}-go"
FB = f"{BASE}-fallback"

CSV = f"""commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,64000,0,K-A,text
t@x,m-b,groq,https://api.groq.com/openai/v1,free,200,200000,0,K-B,text
t@x,m-c,groq,https://api.groq.com/openai/v1,free,200,200000,0,K-C,text
t@x,m-g,groq,https://api.groq.com/openai/v1,,64,64000,0,K-G,text
t@x,m-f,groq,https://api.groq.com/openai/v1,fallback,64,64000,0,K-F,text
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    set_current_session("S1")
    yield r
    os.unlink(path)


def _peers(r, value=3000.0, n=5):
    """Flotta 'normale' a `value` ms (soglia size-aware bassa)."""
    us = [d["unique"] for g in r.config.groups.values() for d in g]
    for u in us[:n]:
        r._avg_latencies[u] = float(value)
    r._fleet_cache.clear()


def _u(r, group, key):
    return next(d for d in r.config.groups[group] if d.get("api_key") == key)[
        "unique"]


# --------------------------------------------------------------- wakeup
def _stale(r, u, reason, status, age=2000.0, total=10000.0):
    """Rende un dep cooled E stantio: registra l'evidenza (`reason`/`status`)
    e poi fissa i tre dati di cooldown in modo che il residuo efficace resti
    > 0 mentre l'eta' supera `stale_cooldown_retry_sec`."""
    r.mark_failed(u, seconds=int(total), reason=reason, status=status)
    now = time.time()
    r._cooldown[u] = now + (total - age)
    r._cooldown_since[u] = now - age
    r._cooldown_full_map()[u] = float(total)
    r._key_soft.clear()            # finestra di quota riaperta
    r._key_hints.clear()


def _cool_all_but(r, keep=()):
    """Raffredda (non-stantii) TUTTI i dim della scala tranne `keep`."""
    for g in (D64, D200):
        for d in r.config.groups.get(g, []):
            if d["unique"] in keep:
                continue
            r.mark_failed(d["unique"], seconds=600, reason="http_503",
                          status=503)


def test_wakeup_solo_cooldown_da_429(router):
    r = router
    u503 = _u(r, D64, "K-A")
    u429 = _u(r, D200, "K-B")
    _cool_all_but(r, keep=(u503, u429))
    r.mark_failed(u503, seconds=600, reason="http_503", status=503)
    _stale(r, u429, "http_429", 429)
    got = r._walk_ladder_resilient(r.config.chains["test"], None, None, None,
                                   set())
    assert got is not None and got["unique"] == u429   # il 503 NON e' svegliato
    assert len(r._wake_times.get(u429, ())) == 1
    assert not r._wake_times.get(u503)


def test_wakeup_non_parte_con_chiave_satura(router):
    """Finche' la chiave e' in soft-429 il wakeup NON parte (protezione
    quota): il 429 non deve diventare un tentativo a vuoto."""
    r = router
    u429 = _u(r, D200, "K-B")
    _cool_all_but(r, keep=(u429,))
    _stale(r, u429, "http_429", 429)
    r._key_soft[u429] = None
    # reinstalla il soft-429 sulla chiave (simula quota ancora chiusa)
    import hashlib
    tag = hashlib.sha256(b"K-B").hexdigest()[:12]
    r._key_soft[tag] = time.time() + 600
    r._walk_ladder_resilient(r.config.chains["test"], None, None, None, set())
    assert not r._wake_times.get(u429)


def test_wakeup_budget_a_finestra(router):
    r = router
    r.policy.ladder_cooldown_wakeups = 1
    u429 = _u(r, D200, "K-B")
    _cool_all_but(r, keep=(u429,))
    _stale(r, u429, "http_429", 429)
    ladder = r.config.chains["test"]
    r._walk_ladder_resilient(ladder, None, None, None, set())
    assert len(r._wake_times.get(u429, ())) == 1
    r._walk_ladder_resilient(ladder, None, None, None, set())
    assert len(r._wake_times.get(u429, ())) == 1   # budget esaurito


def test_prelast_prima_del_wakeup(router):
    """Un dim VIVO servito da un'altra sessione vince sul cooldown-wakeup."""
    r = router
    u_shared = _u(r, D64, "K-A")
    r.note_session_success("S2", u_shared)
    u429 = _u(r, D200, "K-B")
    _cool_all_but(r, keep=(u_shared, u429))
    _stale(r, u429, "http_429", 429)
    got = r._walk_ladder_resilient(r.config.chains["test"], None, None, 60000,
                                   set())
    assert got is not None and got["unique"] == u_shared
    assert not r._wake_times.get(u429)              # nessun wakeup consumato


# ------------------------------------------------------- P1 warm + lenti
def test_warm_ammette_i_lenti(router):
    r = router
    u = _u(r, D64, "K-A")
    _peers(r, 3000.0)
    r.note_session_success("S1", u)
    r._avg_latencies[u] = 200000.0                  # lento (flotta a 3s)
    assert r.session_holder("S1") == u
    assert r._warm_pool("S1", None) == [r.config.deployment_by_unique(u)] \
        or [d["unique"] for d in r._warm_pool("S1", None)] == [u]
    r.policy.warm_pool_allow_slow = False
    assert r._warm_pool("S1", None) == []
    assert r.cache_holder(None, None, "S1") is None


def test_prefer_fast_su_non_stream(router):
    """Con un caldo lento holder e un caldo NON lento disponibile, il
    non-stream (prefer_fast) prende il non lento."""
    r = router
    u_fast = _u(r, D64, "K-A")
    u_slow = _u(r, D200, "K-B")
    _peers(r, 3000.0)
    r.note_session_success("S1", u_fast)
    r.note_session_success("S1", u_slow)            # holder = slow
    r._avg_latencies[u_slow] = 200000.0
    d_fast = r.initial_pick("test", D64, None, 64000, session_id="S1",
                            prefer_fast=True)
    assert d_fast["unique"] == u_fast
    d_slow = r.initial_pick("test", D64, None, 64000, session_id="S1")
    assert d_slow["unique"] == u_slow


# ------------------------------------------------------ hedge_canaries
def test_hedge_canaries_cross_tier_e_pagati(router):
    r = router
    a = r.config.groups[D64][0]
    cands = r.hedge_canaries("test", a, None, 64000, {a["unique"]}, None, k=2)
    assert 1 <= len(cands) <= 2
    ids = [d["unique"] for d in cands]
    assert a["unique"] not in ids and len(set(ids)) == len(ids)
    for d in cands:
        assert not d["group"].endswith("-go")
        assert not d["group"].endswith("-fallback")
    # tier crescente: il primo e' un 200k (tier diverso e piu' grande di A)
    assert cands[0]["group"] == D200


def test_hedge_canaries_floor_e_fresh_only(router):
    r = router
    a = r.config.groups[D64][0]
    # floor 200k: solo la dim 200k
    cands = r.hedge_canaries("test", a, None, 64000, {a["unique"]}, D200, k=2)
    assert cands and all(d["group"] == D200 for d in cands)
    # fresh_only: i caldi della sessione sono esclusi
    r.note_session_success("S1", cands[0]["unique"])
    cands2 = r.hedge_canaries("test", a, None, 64000, {a["unique"]}, D200, k=2,
                              fresh_only=True)
    assert cands2 and cands2[0]["unique"] != cands[0]["unique"]


def test_hedge_canaries_esclude_non_stream(router):
    r = router
    a = r.config.groups[D64][0]
    b = r.config.groups[D200][0]
    r.stats_for(b["unique"]).json_fallback = 2
    assert r._is_known_nonstream(b["unique"]) is True
    cands = r.hedge_canaries("test", a, None, 64000, {a["unique"]}, None, k=2)
    assert all(d["unique"] != b["unique"] for d in cands)


# ------------------------------------------------------ warm ownership
def test_note_warm_owner_senza_holder(router):
    r = router
    u = _u(r, D64, "K-A")
    r.note_warm_owner("S1", u)
    assert [d["unique"] for d in r._warm_pool("S1", None)] == [u]
    assert r.session_holder("S1") is None        # NON diventa holder
