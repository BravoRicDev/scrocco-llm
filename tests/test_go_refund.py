"""Rimborso latenza ("go_refund"): quando un deployment viene marcato lento
per una sessione, le si regalano N turni sul bucket -go.

Copre:
- parsing/validazione della policy `go_refund`;
- conteggio turni + decisione (note_session_turn) e matematica del regalo
  (grant_go_refund) con clamp min/max;
- l'hook nelle funzioni di marcatura "lento" (mark_session_slow);
- la decisione all'atterraggio `_apply_go_refund` (solo dim testo -Nk);
- persistenza in routing_state.
"""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

# un dim testo free (200k) + un bucket -go (data=giorno, provider non-zen)
CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-gr
a@x.com,gpt-oss,groq,https://api.groq.com/v1,free,200,0,0,sk-K1-AAAAAAAAAA
a@x.com,gpt-oss-go,groq,https://api.groq.com/v1,15,200,0,0,sk-K2-AAAAAAAAAA
"""

CSV_NOGO = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-gr
a@x.com,gpt-oss,groq,https://api.groq.com/v1,free,200,0,0,sk-K1-AAAAAAAAAA
"""


def _mk(csv_text=CSV, pol_dict=None):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_text)
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


# --------------------------------------------------------------- policy
def test_policy_defaults():
    p = Policy()
    assert p.go_refund_enabled is True
    assert p.go_refund_pct == 20
    assert p.go_refund_min_turns == 5
    assert p.go_refund_max_turns == 20


def test_policy_parse_block():
    p = Policy.from_dict({"go_refund": {
        "enabled": False, "pct": 50, "min_turns": 2, "max_turns": 9}})
    assert p.go_refund_enabled is False
    assert p.go_refund_pct == 50
    assert p.go_refund_min_turns == 2
    assert p.go_refund_max_turns == 9


def test_policy_parse_invalid():
    with pytest.raises(ValueError, match="go_refund"):
        Policy.from_dict({"go_refund": "no"})
    with pytest.raises(ValueError, match="pct"):
        Policy.from_dict({"go_refund": {"pct": 150}})
    with pytest.raises(ValueError, match="min_turns"):
        Policy.from_dict({"go_refund": {"min_turns": -1}})
    with pytest.raises(ValueError, match="max_turns"):
        Policy.from_dict({"go_refund": {"min_turns": 9, "max_turns": 2}})


# ------------------------------------------------- conteggio e matematica
def test_no_refund_means_no_go(router):
    sid = "s1"
    assert router.note_session_turn(sid) is False
    assert router.note_session_turn(sid) is False
    assert router.go_refund_status(sid)["turns"] == 2
    assert router.go_refund_status(sid)["active"] is False


def test_grant_min_clamp_example_turn3(router):
    # lento al turno 3 -> refund = clamp(0.6, min 5, max 20) = 5 -> go_until 8
    sid = "s1"
    for _ in range(3):
        router.note_session_turn(sid)
    assert router.grant_go_refund(sid) == 5
    st = router.go_refund_status(sid)
    assert st["go_until"] == 8
    # turni in -go: n = 3,4,5,6,7 -> 5 turni, poi stop a n=8
    got = [router.note_session_turn(sid) for _ in range(6)]
    assert got == [True, True, True, True, True, False]


def test_grant_pct_example_turn100(router):
    sid = "s1"
    _set_turns(router, sid, 100)
    assert router.grant_go_refund(sid) == 20
    assert router.go_refund_status(sid)["go_until"] == 120
    got = [router.note_session_turn(sid) for _ in range(21)]
    assert got.count(True) == 20 and got[-1] is False


def test_grant_max_clamp_example_turn200(router):
    sid = "s1"
    _set_turns(router, sid, 200)
    assert router.grant_go_refund(sid) == 20          # 40 -> max 20
    assert router.go_refund_status(sid)["go_until"] == 220


def test_grant_takes_max_never_shrinks(router):
    sid = "s1"
    _set_turns(router, sid, 100)
    router.grant_go_refund(sid)                       # go_until 120
    _set_turns(router, sid, 105, go_until=120)
    router.grant_go_refund(sid)                       # target 125 > 120
    assert router.go_refund_status(sid)["go_until"] == 125


def test_disabled_knob_blocks_grant(router):
    sid = "s1"
    router.policy.go_refund_enabled = False
    _set_turns(router, sid, 100)
    assert router.grant_go_refund(sid) == 0
    assert router.go_refund_status(sid)["go_until"] == 0


def test_mark_session_slow_grants(router):
    sid = "s1"
    _set_turns(router, sid, 100)
    dep = router.config.groups["scrocco-llm-gr-200k"][0]
    router.mark_session_slow(sid, dep["unique"])
    assert router.go_refund_status(sid)["go_until"] == 120
    assert router.note_session_turn(sid) is True


# ------------------------------------------------- atterraggio (_apply_go_refund)
def _main():
    import app.main as M
    return M


def test_landing_redirects_dim_to_go(router):
    M = _main()
    grp, red = M._apply_go_refund(router, "scrocco-llm-gr-200k", "gr", True)
    assert (grp, red) == ("scrocco-llm-gr-go", True)


def test_landing_no_turn_no_redirect(router):
    M = _main()
    grp, red = M._apply_go_refund(router, "scrocco-llm-gr-200k", "gr", False)
    assert (grp, red) == ("scrocco-llm-gr-200k", False)


def test_landing_skips_go_fallback_and_unique(router):
    M = _main()
    for g in ("scrocco-llm-gr-go", "scrocco-llm-gr-fallback",
              "scrocco-llm-gr__gpt-oss-go__0", "scrocco-llm-gr-vision"):
        grp, red = M._apply_go_refund(router, g, "gr", True)
        assert (grp, red) == (g, False), g


def test_landing_skips_when_no_go_bucket():
    M = _main()
    r = _mk(CSV_NOGO)
    try:
        assert "scrocco-llm-gr-go" not in r.config.groups
        grp, red = M._apply_go_refund(r, "scrocco-llm-gr-200k", "gr", True)
        assert (grp, red) == ("scrocco-llm-gr-200k", False)
    finally:
        os.unlink(r._tmp_path)


# --------------------------------------------------------------- persistenza
def test_session_turns_persist_roundtrip():
    r = _mk()
    try:
        _set_turns(r, "s1", 100, go_until=120)
        data = r.dump_routing_state()
        assert data["session_turns"]["s1"][0] == 100
        assert data["session_turns"]["s1"][1] == 120
        r2 = _mk()
        try:
            rep = r2.load_routing_state(data)
            assert rep["session_turns"] == 1
            st = r2.go_refund_status("s1")
            assert st["turns"] == 100 and st["go_until"] == 120
            assert st["active"] is True
        finally:
            os.unlink(r2._tmp_path)
    finally:
        os.unlink(r._tmp_path)
