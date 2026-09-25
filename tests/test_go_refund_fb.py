"""Regalo -go per i FALLBACK attraversati (#50): secondo trigger di go_refund.

Copre:
- parsing/validazione della policy `go_refund.fb_*` (+ path noti nello schema);
- la matematica `clamp(floor(per_fallback * fb), min, max)` con i clamp;
- i kill-switch (`go_refund.enabled` master e `fb_enabled`), `per<=0`, senza
  sessione, `fb<=0`;
- `go_until` mai ridotto (semantica max);
- l'hook end-to-end: una chat servita con 1 fallback regala 1 turno, un 503 no.

`app.main` va importato SOLO dentro fixture/funzioni.
"""
import asyncio
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy, unknown_yaml_paths
from app.router import Router

# un dim testo free (200k) + un bucket -go (data=giorno, provider non-zen)
CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-gr
a@x.com,gpt-oss,groq,https://api.groq.com/v1,free,200,0,0,sk-K1-AAAAAAAAAA
a@x.com,gpt-oss-go,groq,https://api.groq.com/v1,15,200,0,0,sk-K2-AAAAAAAAAA
"""


def _mk(pol_dict=None):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict(pol_dict or {})
    r = Router(cfg, pol)
    r._tmp_path = path
    return r


@pytest.fixture()
def router():
    r = _mk()
    yield r
    os.unlink(r._tmp_path)


def _set_turns(r, sid, n, go_until=0):
    r._sess_turns_map()[sid] = {"n": n, "go_until": go_until,
                                "ts": time.time()}


# ------------------------------------------------------------------ policy
def test_policy_defaults_fb():
    p = Policy()
    assert p.go_refund_fb_enabled is True
    assert p.go_refund_fb_per_fallback == 0.5
    assert p.go_refund_fb_min_turns == 1
    assert p.go_refund_fb_max_turns == 3


def test_policy_parse_fb_block():
    p = Policy.from_dict({"go_refund": {
        "fb_enabled": False, "fb_per_fallback": 0.25,
        "fb_min_turns": 2, "fb_max_turns": 8}})
    assert p.go_refund_fb_enabled is False
    assert p.go_refund_fb_per_fallback == 0.25
    assert p.go_refund_fb_min_turns == 2
    assert p.go_refund_fb_max_turns == 8


def test_policy_parse_fb_invalid():
    with pytest.raises(ValueError, match="fb_per_fallback"):
        Policy.from_dict({"go_refund": {"fb_per_fallback": -0.1}})
    with pytest.raises(ValueError, match="fb_min_turns"):
        Policy.from_dict({"go_refund": {"fb_min_turns": -1}})
    with pytest.raises(ValueError, match="fb_max_turns"):
        Policy.from_dict({"go_refund": {"fb_min_turns": 5, "fb_max_turns": 2}})


def test_policy_fb_paths_known():
    assert unknown_yaml_paths({"go_refund": {
        "fb_enabled": True, "fb_per_fallback": 0.5,
        "fb_min_turns": 1, "fb_max_turns": 3}}) == []


# --------------------------------------------------------- matematica
@pytest.mark.parametrize("fb,expected", [
    (0, 0), (1, 1), (2, 1), (3, 1), (4, 2), (5, 2), (6, 3), (10, 3),
    (100, 3)])
def test_fb_math_table(fb, expected):
    r = _mk()
    try:
        assert r.grant_go_refund_fb("s", fb) == expected
        assert r.go_refund_status("s")["go_until"] == expected
    finally:
        os.unlink(r._tmp_path)


def test_fb_credits_relative_to_turns(router):
    _set_turns(router, "s1", 10)                      # n = 10
    assert router.note_request_fallbacks("s1", 5) == 2
    st = router.go_refund_status("s1")
    assert st["go_until"] == 12 and st["refund_left"] == 2


def test_fb_never_shrinks_go_until(router):
    _set_turns(router, "s1", 10, go_until=50)
    assert router.note_request_fallbacks("s1", 6) == 3   # target 13 < 50
    assert router.go_refund_status("s1")["go_until"] == 50


def test_fb_disabled_master_and_own_switch():
    for pol in ({"go_refund": {"enabled": False}},
                {"go_refund": {"fb_enabled": False}}):
        r = _mk(pol)
        try:
            assert r.note_request_fallbacks("s", 6) == 0
            assert r.go_refund_status("s")["go_until"] == 0
        finally:
            os.unlink(r._tmp_path)


def test_fb_per_zero_is_off():
    r = _mk({"go_refund": {"fb_per_fallback": 0}})
    try:
        assert r.note_request_fallbacks("s", 6) == 0
    finally:
        os.unlink(r._tmp_path)


def test_fb_no_session(router, monkeypatch):
    import app.routing.sessions as S
    monkeypatch.setattr(S, "current_session", lambda *a, **k: None)
    assert router.note_request_fallbacks(None, 3) == 0


def test_fb_nonpositive(router):
    assert router.note_request_fallbacks("s1", 0) == 0
    assert router.note_request_fallbacks("s1", -2) == 0
    assert router.go_refund_status("s1")["go_until"] == 0


# ------------------------------------------------------------ hook e2e
CLEAN = [
    b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"ciao"}}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
    b"data: [DONE]\n\n",
]


def _dep(M, name, idx=0):
    dep = {"unique": "%s__fake__%d" % (name, idx), "group": name,
           "model": "fake-model", "api_key": "sk-fake-%d" % idx,
           "api_base": "https://fake.test/v1"}
    M.config.groups.setdefault(name, []).append(dep)
    return dep


def _fb_policy_snapshot(M):
    p = M.router.policy
    return (p.go_refund_enabled, p.go_refund_fb_enabled,
            p.go_refund_fb_per_fallback, p.go_refund_fb_min_turns,
            p.go_refund_fb_max_turns)


def _fb_policy_restore(M, snap):
    p = M.router.policy
    (p.go_refund_enabled, p.go_refund_fb_enabled,
     p.go_refund_fb_per_fallback, p.go_refund_fb_min_turns,
     p.go_refund_fb_max_turns) = snap


def test_e2e_served_with_one_fallback_gifts_one(monkeypatch):
    """1 fallback attraversato (d0 -> d1), risposta servita -> +1 turno -go."""
    import app.main as M
    from app.forwarder import UpstreamError
    name = "scrocco-llm-test-fb"
    d0 = _dep(M, name, 0)
    d1 = _dep(M, name, 1)
    cooldowns = set(M.router._cooldown)
    snap = _fb_policy_snapshot(M)
    p = M.router.policy
    p.go_refund_enabled = p.go_refund_fb_enabled = True
    p.go_refund_fb_per_fallback = 0.5
    p.go_refund_fb_min_turns, p.go_refund_fb_max_turns = 1, 3

    async def _stream_response(dep, payload, **kwargs):
        if dep["unique"] == d0["unique"]:
            raise UpstreamError(429, "rate limit exceeded")

        async def _gen():
            for c in CLEAN:
                yield c
        return _gen()
    monkeypatch.setattr(M.forwarder, "stream_response", _stream_response)
    monkeypatch.setattr(M.router, "fallback_next", lambda *a, **k: d1)
    sid = "sess-fb-e2e-1"
    M.router._sess_turns_map().pop(sid, None)
    meta: dict = {}

    async def _run():
        payload = {"model": d0["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        resp = await M._stream_with_fallback(
            "test", d0, payload, scope="chain", ses=sid,
            result_box=meta, client_stream=False)
        if hasattr(resp, "body_iterator"):
            async for _ in resp.body_iterator:
                pass
        return resp
    try:
        asyncio.run(_run())
        assert meta.get("attempts") == [d0["unique"], d1["unique"]]
        st = M.router.go_refund_status(sid)
        assert st["go_until"] == 1, st
        assert st["refund_left"] == 1, st
    finally:
        _fb_policy_restore(M, snap)
        M.router._sess_turns_map().pop(sid, None)
        for k in list(M.router._cooldown):
            if k not in cooldowns:
                M.router._cooldown.pop(k, None)
        M.config.groups.pop(name, None)


def test_e2e_exhausted_keeps_no_gift(monkeypatch):
    """Catena esaurita (503): nessun regalo, perche' non e' una risposta servita."""
    import app.main as M
    from app.forwarder import UpstreamError
    name = "scrocco-llm-test-fb-503"
    d0 = _dep(M, name, 0)
    cooldowns = set(M.router._cooldown)
    snap = _fb_policy_snapshot(M)
    p = M.router.policy
    p.go_refund_enabled = p.go_refund_fb_enabled = True
    p.go_refund_fb_per_fallback = 0.5
    p.go_refund_fb_min_turns, p.go_refund_fb_max_turns = 1, 3

    async def _boom(dep, payload, **kwargs):
        raise UpstreamError(429, "rate limit exceeded")
    monkeypatch.setattr(M.forwarder, "stream_response", _boom)
    monkeypatch.setattr(M.router, "fallback_next", lambda *a, **k: None)
    sid = "sess-fb-e2e-503"
    M.router._sess_turns_map().pop(sid, None)

    async def _run():
        payload = {"model": d0["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await M._stream_with_fallback(
            "test", d0, payload, scope="chain", ses=sid,
            result_box={}, client_stream=False)
    try:
        resp = asyncio.run(_run())
        assert getattr(resp, "status_code", None) == 503
        assert M.router.go_refund_status(sid)["go_until"] == 0
    finally:
        _fb_policy_restore(M, snap)
        M.router._sess_turns_map().pop(sid, None)
        for k in list(M.router._cooldown):
            if k not in cooldowns:
                M.router._cooldown.pop(k, None)
        M.config.groups.pop(name, None)
