"""Test per il detentore cache per-sessione (Router, cache-aware)."""

import time
from types import SimpleNamespace

from app.router import Router, set_current_session


def _router(deps=None, cooled=None, retired=None, capfits=True,
            supports=True, ttl=3600):
    r = Router.__new__(Router)
    r.policy = SimpleNamespace(
        cache_aware_enabled=True, cache_prefer_last_success=True,
        cache_holder_ttl_sec=ttl, cache_skip_probe_when_holder=True,
        escalation_pin=False, dims_ladder_floor=True)
    dep_map = deps or {}
    r.config = SimpleNamespace(
        go_suffix="-go", fallback_suffix="-fallback",
        proxy_prefix="scrocco-llm-mioaruba-",
        deployment_by_unique=lambda u: dep_map.get(u),
        groups={}, group_caps={}, profile_dims={}, chains_cap={})
    r._session_last_ok = {}
    r._session_compact = {}
    r._esc_win = {}
    r.is_cooled_down = lambda u: u in (cooled or set())
    r.is_retired = lambda u: u in (retired or set())
    r._cap_fits = lambda d, ctx: capfits
    r._dep_supports = lambda d, need: supports
    return r


def _dep(unique, group, mi=100000):
    return {"unique": unique, "group": group, "max_input_tokens": mi,
            "caps": set()}


class TestFreeGroup:
    def test_dim_is_free(self):
        assert _router()._free_group("p-200k") is True

    def test_go_is_paid(self):
        assert _router()._free_group("p-go") is False

    def test_fallback_is_paid(self):
        assert _router()._free_group("p-fallback") is False


class TestHolder:
    def test_set_get(self):
        r = _router()
        r.note_session_success("s1", "u1")
        assert r.session_holder("s1") == "u1"

    def test_ttl(self):
        r = _router(ttl=100)
        r._session_last_ok["s1"] = ("u1", time.time() - 10000)
        assert r.session_holder("s1") is None

    def test_exact_unique(self):
        deps = {"u1": _dep("u1", "p-200k")}
        r = _router(deps=deps)
        r.note_session_success("s1", "u1")
        got = r.cache_holder("s1", ctx=1000)
        assert got is not None and got["unique"] == "u1"

    def test_cooled_none(self):
        deps = {"u1": _dep("u1", "p-200k")}
        r = _router(deps=deps, cooled={"u1"})
        r.note_session_success("s1", "u1")
        assert r.cache_holder("s1", ctx=1000) is None

    def test_retired_none(self):
        deps = {"u1": _dep("u1", "p-200k")}
        r = _router(deps=deps, retired={"u1"})
        r.note_session_success("s1", "u1")
        assert r.cache_holder("s1", ctx=1000) is None

    def test_need_unsupported_none(self):
        deps = {"u1": _dep("u1", "p-200k")}
        r = _router(deps=deps, supports=False)
        r.note_session_success("s1", "u1")
        assert r.cache_holder("s1", need=frozenset({"vision"}),
                              ctx=1000) is None


class TestFallbackPreference:
    def test_prefers_holder_free(self):
        holder = _dep("uH", "p-200k")
        cur = _dep("uC", "p-200k")
        r = _router(deps={"uH": holder})
        r.note_session_success("s1", "uH")
        set_current_session("s1")
        nxt = r.fallback_next("p", cur, need=None, scope="group", ctx=1000,
                              tried=set(), requested_group="p-200k")
        assert nxt is not None and nxt["unique"] == "uH"

    def test_no_preference_when_holder_paid(self):
        holder = _dep("uH", "p-go")
        cur = _dep("uC", "p-200k")
        r = _router(deps={"uH": holder})
        r.note_session_success("s1", "uH")
        set_current_session("s1")
        nxt = r.fallback_next("p", cur, need=None, scope="group", ctx=1000,
                              tried=set(), requested_group="p-200k")
        assert nxt is None or nxt["unique"] != "uH"


class TestCompactMode:
    def test_sticky(self):
        r = _router()
        assert r.is_session_compact("s1") is False
        r.mark_session_compact("s1")
        assert r.is_session_compact("s1") is True

    def test_ttl(self):
        r = _router(ttl=100)
        r._session_compact["s1"] = time.time() - 10000
        assert r.is_session_compact("s1") is False
