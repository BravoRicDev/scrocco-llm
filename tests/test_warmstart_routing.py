"""F2 WARM-START: lo stato di routing legato alle sessioni sopravvive al
restart (holder cache, sticky, ownership warm, demote, pin escalation,
watermark ctxcompact). Voci scadute/corrotte -> scartate, mai crash."""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-A,text
t@x,m-go,groq,https://api.groq.com/openai/v1,,64,8000,0,K-GO,text
"""


def _mk():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
               Policy.from_dict({}))
    return r, path


@pytest.fixture()
def pair():
    r1, p1 = _mk()
    r2, p2 = _mk()
    yield r1, r2
    os.unlink(p1)
    os.unlink(p2)


def _u(r, key):
    return next(d for deps in r.config.groups.values() for d in deps
                if d.get("api_key") == key)["unique"]


def test_roundtrip_families(pair):
    r1, r2 = pair
    ua = _u(r1, "K-A")
    ugo = _u(r1, "K-GO")
    now = time.time()
    r1._sticky["S"] = ("scrocco-llm-test-64k", now)
    r1._session_group["S"] = ("scrocco-llm-test-64k", now)
    r1._sticky_dep["S"] = (ua, now)
    r1._cache_ok()["S"] = (ua, now)
    r1._dep_sess()[ua] = ("S", now)
    r1._sess_deps()["S"] = {ua}
    r1._sess_slow()["S"] = {ua: (now, False)}
    r1._ctx_frontier["S"] = (7, now)
    r1._esc()["scrocco-llm-test-200k"] = (ugo, now)
    snap = r1.dump_routing_state()
    rep = r2.load_routing_state(snap)
    assert rep["sticky"] == 1 and rep["holder"] == 1
    assert rep["warm_owner"] == 1 and rep["session_slow"] == 1
    assert rep["ctx_frontier"] == 1 and rep["esc_win"] == 1
    assert r2.dep_sticky_get("S") == ua
    assert r2.session_holder("S") == ua
    assert r2._sticky.get("S", ("",))[0] == "scrocco-llm-test-64k"
    assert r2.ctx_boundary_floor("S") == 7
    # demote SOFT (hard=False): pesa solo sui contesti > 30k
    assert r2.is_slow_for_session(ua, "S", 50000) is True
    assert r2.is_slow_for_session(ua, "S", 5000) is False
    # ownership: la sessione possiede ancora ua
    assert r2._dep_sess().get(ua, ("",))[0] == "S"
    assert r2._esc().get("scrocco-llm-test-200k", ("",))[0] == ugo


def test_expired_dropped(pair):
    r1, r2 = pair
    ua = _u(r1, "K-A")
    old = time.time() - 99999
    r1._sticky["S"] = ("g", old)
    r1._cache_ok()["S"] = (ua, old)
    snap = r1.dump_routing_state()
    assert "S" not in snap["sticky"] and "S" not in snap["session_last_ok"]
    assert r2.load_routing_state(snap) == {} or \
        sum(r2.load_routing_state(snap).values()) == 0


def test_garbage_never_crashes(pair):
    r1, r2 = pair
    junk = {"sticky": {"S": ["g"]}, "session_last_ok": {"S": "nope"},
            "dep_last_session": {"x@y": ["S", "ts"]},
            "session_slow": {"S": {"u": [1e12, True]}},
            "ctx_frontier": {"S": ["b", "t"]},
            "esc_win": {"g": ("u", time.time())}}
    assert isinstance(r2.load_routing_state(junk), dict)
    # tuple/list esc_win valido (la nostra dump usa liste ma load accetta 2)
    assert r2._esc().get("g", ("",))[0] == "u"


def test_session_deps_rebuilt_from_ownership(pair):
    r1, r2 = pair
    ua = _u(r1, "K-A")
    now = time.time()
    r1._dep_sess()[ua] = ("S", now)
    r1._sess_deps()["S"] = {ua, "fantasma"}       # 'fantasma' non in ownership
    snap = r1.dump_routing_state()
    assert snap["session_deps"].get("S") == [ua]
    r2.load_routing_state(snap)
    assert ua in r2._sess_deps().get("S", set())


def test_naked_router_dump_safe():
    r = Router.__new__(Router)
    r.policy = Policy.from_dict({})
    from app import router as _R
    r._guard_sec = lambda: 900
    r._warm_ttl = lambda: 900
    snap = r.dump_routing_state()
    assert isinstance(snap, dict) and snap["sticky"] == {}
