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
    assert p.go_refund_min_turns == 1
    assert p.go_refund_max_turns == 5
    assert p.go_refund_trigger_ms == 20000


def test_policy_parse_block():
    p = Policy.from_dict({"go_refund": {
        "enabled": False, "pct": 50, "min_turns": 2, "max_turns": 9,
        "trigger_ms": 30000}})
    assert p.go_refund_enabled is False
    assert p.go_refund_pct == 50
    assert p.go_refund_min_turns == 2
    assert p.go_refund_max_turns == 9
    assert p.go_refund_trigger_ms == 30000


def test_policy_parse_invalid():
    with pytest.raises(ValueError, match="go_refund"):
        Policy.from_dict({"go_refund": "no"})
    with pytest.raises(ValueError, match="pct"):
        Policy.from_dict({"go_refund": {"pct": 150}})
    with pytest.raises(ValueError, match="min_turns"):
        Policy.from_dict({"go_refund": {"min_turns": -1}})
    with pytest.raises(ValueError, match="max_turns"):
        Policy.from_dict({"go_refund": {"min_turns": 9, "max_turns": 2}})
    with pytest.raises(ValueError, match="trigger_ms"):
        Policy.from_dict({"go_refund": {"trigger_ms": 0}})


# ------------------------------------------------- conteggio e matematica
def test_no_refund_means_no_go(router):
    sid = "s1"
    assert router.note_session_turn(sid) is False
    assert router.note_session_turn(sid) is False
    assert router.go_refund_status(sid)["turns"] == 2
    assert router.go_refund_status(sid)["active"] is False


def test_grant_min_clamp_example_turn3(router):
    # turno 3 -> refund = clamp(0.6, min 1, max 5) = 1 -> go_until 4
    sid = "s1"
    for _ in range(3):
        router.note_session_turn(sid)
    assert router.grant_go_refund(sid) == 1
    st = router.go_refund_status(sid)
    assert st["go_until"] == 4
    # turno in -go: n = 3 -> True, poi stop a n=4
    got = [router.note_session_turn(sid) for _ in range(3)]
    assert got == [True, False, False]


def test_grant_pct_example_turn100(router):
    sid = "s1"
    _set_turns(router, sid, 100)
    assert router.grant_go_refund(sid) == 5
    assert router.go_refund_status(sid)["go_until"] == 105
    got = [router.note_session_turn(sid) for _ in range(6)]
    assert got.count(True) == 5 and got[-1] is False


def test_grant_max_clamp_example_turn200(router):
    sid = "s1"
    _set_turns(router, sid, 200)
    assert router.grant_go_refund(sid) == 5           # 40 -> max 5
    assert router.go_refund_status(sid)["go_until"] == 205


def test_grant_takes_max_never_shrinks(router):
    sid = "s1"
    _set_turns(router, sid, 100)
    router.grant_go_refund(sid)                       # go_until 105
    _set_turns(router, sid, 105, go_until=105)
    router.grant_go_refund(sid)                       # target 110 > 105
    assert router.go_refund_status(sid)["go_until"] == 110


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
    assert router.go_refund_status(sid)["go_until"] == 105
    assert router.note_session_turn(sid) is True


# ------------------------------- regalo INDIPENDENTE dalla demozione "lento"
def test_note_session_slow_refunds_without_demoting(router, monkeypatch):
    """Un dep che serve ~32s regala turni -go ma NON viene demoto (il floor
    "lento" resta a 45s): resta warm/holder e si ritrova al ritorno."""
    sid = "s1"
    _set_turns(router, sid, 10)
    dep = router.config.groups["scrocco-llm-gr-200k"][0]
    u = dep["unique"]
    monkeypatch.setattr(router, "_slow_threshold_ms",
                        lambda *a, **k: 45000.0)
    router._note_session_slow(sid, u, 32000)          # 20s < 32s < 45s
    assert router.go_refund_status(sid)["go_until"] > 10   # regalo concesso
    assert u not in (router._sess_slow().get(sid) or {})   # NON demoto


def test_note_session_slow_no_refund_below_trigger(router):
    sid = "s1"
    _set_turns(router, sid, 10)
    dep = router.config.groups["scrocco-llm-gr-200k"][0]
    router._note_session_slow(sid, dep["unique"], 19999)   # < 20s
    assert router.go_refund_status(sid)["go_until"] == 0


def test_note_session_slow_demotes_only_over_floor(router, monkeypatch):
    sid = "s1"
    dep = router.config.groups["scrocco-llm-gr-200k"][0]
    u = dep["unique"]
    monkeypatch.setattr(router, "_slow_threshold_ms",
                        lambda *a, **k: 45000.0)
    _set_turns(router, sid, 10)
    router._note_session_slow(sid, u, 44000)          # < 45s: regala, non demota
    assert u not in (router._sess_slow().get(sid) or {})
    assert router.go_refund_status(sid)["go_until"] > 10
    _set_turns(router, sid, 10)
    router._note_session_slow(sid, u, 46000)          # > 45s: demota
    assert u in (router._sess_slow().get(sid) or {})


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


# --------------------------------------------------------------- log INFO
def test_log_info_on_grant_and_consumption(router, caplog):
    import logging
    sid = "s1"
    _set_turns(router, sid, 100)
    with caplog.at_level(logging.INFO, logger="nx.router"):
        router.grant_go_refund(sid)          # concessione
        router.note_session_turn(sid)        # consumo
    msgs = [r.getMessage() for r in caplog.records]
    assert any("turni -go" in m for m in msgs), msgs        # concesso
    assert any("servito in -go" in m for m in msgs), msgs   # consumato


def test_log_info_on_landing(router, caplog):
    import logging
    M = _main()
    sid = "s1"
    _set_turns(router, sid, 100, go_until=120)
    router.note_session_turn(sid)
    with caplog.at_level(logging.INFO, logger="nx.api"):
        grp, red = M._apply_go_refund(
            router, "scrocco-llm-gr-200k", "gr", True, sid)
    assert red is True
    msgs = [r.getMessage() for r in caplog.records]
    assert any("atterraggio" in m and "restano" in m for m in msgs), msgs
