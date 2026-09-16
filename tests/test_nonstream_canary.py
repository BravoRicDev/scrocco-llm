"""Un dep che IGNORA stream:true (risponde JSON, adattato a SSE) resta
eleggibile come canario / sveglia / sostituto in gara.

Regola utente: "Deve usare sia stream che non stream indistintamente. Tanto ci
basiamo sulla consegna finale". Il knob `nonstream_canary_allowed` (default
True) tiene nel pool dei sostituti anche i dep con `json_fallback >= 2`;
con False si torna alla semantica storica (pool solo SSE nativo).
"""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,ns-small,openrouter,https://openrouter.ai/api/v1,free,32,32000,5,K-S,
t@x.com,ns-big,openrouter,https://openrouter.ai/api/v1,free,262,262000,5,K-B,
"""


def _mk(**pol):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    try:
        return Router(GatewayConfig(path, proxy_prefix="scrocco-llm-",
                                    seed=1), Policy.from_dict(pol))
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _dep(r, key):
    for lst in r.config.groups.values():
        for d in lst:
            if d.get("api_key") == key:
                return d
    raise AssertionError(key)


def _mark_nonstream(r, dep, n=2):
    r.stats_for(dep["unique"]).json_fallback = n


# ------------------------------------------------------------------ knob
def test_knob_default_true_e_parse():
    assert Policy.from_dict({}).nonstream_canary_allowed is True
    assert Policy.from_dict(
        {"nonstream_canary_allowed": False}).nonstream_canary_allowed is False
    assert Policy.from_dict(
        {"warm_pool": {"nonstream_canary_allowed": False}}
    ).nonstream_canary_allowed is False
    assert Policy.from_dict(
        {"warm_pool": {"nonstream_canary_allowed": True}}
    ).nonstream_canary_allowed is True


def test_nonstream_blocked_rispetta_il_knob():
    r = _mk()
    d = _dep(r, "K-B")
    assert r._nonstream_blocked(d["unique"]) is False   # 0 fallback: mai bloccato
    _mark_nonstream(r, d)
    assert r._is_known_nonstream(d["unique"]) is True
    assert r._nonstream_blocked(d["unique"]) is False   # default: ammesso
    r2 = _mk(nonstream_canary_allowed=False)
    d2 = _dep(r2, "K-B")
    _mark_nonstream(r2, d2)
    assert r2._nonstream_blocked(d2["unique"]) is True


# --------------------------------------------------------------- canary
def _canary(r, requested=BASE + "-262k"):
    cur = _dep(r, "K-S")
    return r.warm_fill_canary("test", cur, frozenset(), 100, 4096,
                              tried=set(), requested_group=requested)


def test_canary_include_il_dep_nonstream_per_default():
    r = _mk()
    big = _dep(r, "K-B")
    _mark_nonstream(r, big)
    got = _canary(r)
    assert got is not None and got["unique"] == big["unique"]


def test_canary_esclude_il_dep_nonstream_col_knob_false():
    r = _mk(nonstream_canary_allowed=False)
    big = _dep(r, "K-B")
    _mark_nonstream(r, big)
    assert _canary(r) is None


# --------------------------------------------------------------- hedge
def test_hedge_canaries_include_il_nonstream_per_default():
    r = _mk()
    big = _dep(r, "K-B")
    _mark_nonstream(r, big)
    cur = _dep(r, "K-S")
    out = r.hedge_canaries("test", cur, frozenset(), 100, set(),
                           BASE + "-262k", k=2)
    assert big["unique"] in [d["unique"] for d in out]


def test_hedge_canaries_esclude_col_knob_false():
    r = _mk(nonstream_canary_allowed=False)
    big = _dep(r, "K-B")
    _mark_nonstream(r, big)
    cur = _dep(r, "K-S")
    out = r.hedge_canaries("test", cur, frozenset(), 100, set(),
                           BASE + "-262k", k=2)
    assert big["unique"] not in [d["unique"] for d in out]


# --------------------------------------------------------------- sveglia
def test_sveglia_include_il_nonstream_per_default(monkeypatch):
    r = _mk()
    big = _dep(r, "K-B")
    _mark_nonstream(r, big)
    # dormiente con 429 "maturo": la sveglia lo puo' prendere
    # 429 SENZA Retry-After => provenance "heuristic" (svegliabile)
    r.mark_failed(big["unique"], reason="http_429")
    # cooldown heuristic ~1800s, ma 'vissuto' da 1500s: maturo e ancora attivo
    r._cooldown_since[big["unique"]] = time.time() - 1500.0
    cur = _dep(r, "K-S")
    got = r.warm_wake_canary("test", cur, frozenset(), 100, 4096,
                             tried=set(), requested_group=BASE + "-262k",
                             min_age_sec=1200.0)
    assert got is not None and got["unique"] == big["unique"]


def test_sveglia_esclude_col_knob_false():
    r = _mk(nonstream_canary_allowed=False)
    big = _dep(r, "K-B")
    _mark_nonstream(r, big)
    # 429 SENZA Retry-After => provenance "heuristic" (svegliabile)
    r.mark_failed(big["unique"], reason="http_429")
    # cooldown heuristic ~1800s, ma 'vissuto' da 1500s: maturo e ancora attivo
    r._cooldown_since[big["unique"]] = time.time() - 1500.0
    cur = _dep(r, "K-S")
    got = r.warm_wake_canary("test", cur, frozenset(), 100, 4096,
                             tried=set(), requested_group=BASE + "-262k",
                             min_age_sec=1200.0)
    assert got is None
