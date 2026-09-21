"""Stima per-sessione dal rapporto REALE char/token del provider.

Il 1o turno di una sessione usa la stima euristica (solo char/divisor); dal 2o
il rapporto appreso (char REALI del payload inviato a monte / prompt_tokens
restituito) applicato ai char della richiesta, con margine di sicurezza.
Qui: fallback iniziale, media cumulativa, margine, clamp, gate, TTL e purge.
"""
import os
import tempfile
import time

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router, estimate_tokens

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-A,text
"""


def _router(**pk):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict({})
    for k, v in pk.items():
        setattr(pol, k, v)
    return Router(cfg, pol), path


def _msgs(nchars):
    return [{"role": "user", "content": "x" * nchars}]


def test_first_turn_falls_back_to_heuristic():
    r, path = _router()
    try:
        assert r.session_chars_per_token("s1") is None
        tokens, used = r.estimate_for_session("s1", _msgs(20000))
        assert used is False
        assert tokens == estimate_tokens(_msgs(20000),
                                         r.policy.estimate_divisor)
    finally:
        os.unlink(path)


def test_second_turn_uses_learned_ratio_with_margin():
    r, path = _router(estimate_divisor=4, session_estimate_margin=1.05)
    try:
        # provider reale: 40000 char -> 10000 token (cpt = 4.0)
        r.note_session_estimate("s1", 40000, 10000)
        assert r.session_chars_per_token("s1") == 4.0
        tokens, used = r.estimate_for_session("s1", _msgs(20000))
        assert used is True
        assert tokens == int(20000 / 4.0 * 1.05)        # 5250, +5%
        # il fallback euristico darebbe 5000: il rapporto cambia la stima
        assert tokens != estimate_tokens(_msgs(20000), 4)
    finally:
        os.unlink(path)


def test_cumulative_volume_weighted_average():
    r, path = _router()
    try:
        r.note_session_estimate("s1", 40000, 10000)     # cpt 4.0
        r.note_session_estimate("s1", 20000, 4000)      # cpt 5.0
        cpt = r.session_chars_per_token("s1")
        assert abs(cpt - 60000 / 14000) < 1e-9          # ~4.2857
        assert r._sess_ratio["s1"]["n"] == 2
    finally:
        os.unlink(path)


def test_margin_is_configurable_and_never_below_one():
    r, path = _router(session_estimate_margin=1.10)
    try:
        r.note_session_estimate("s1", 40000, 10000)
        tokens, _ = r.estimate_for_session("s1", _msgs(20000))
        assert tokens == int(20000 / 4.0 * 1.10)
    finally:
        os.unlink(path)


def test_gate_rejects_unreliable_samples():
    r, path = _router(session_estimate_min_chars=8000,
                      session_estimate_min_tokens=1000)
    try:
        r.note_session_estimate("s1", 4000, 8000)       # chars < min
        r.note_session_estimate("s1", 40000, 500)       # pt < min
        r.note_session_estimate(None, 40000, 10000)     # nessuna sessione
        assert r.session_chars_per_token("s1") is None
        assert "s1" not in r._sess_ratio
    finally:
        os.unlink(path)


def test_ratio_clamped_to_bounds():
    r, path = _router(session_estimate_min_ratio=1.5,
                      session_estimate_max_ratio=8.0)
    try:
        r.note_session_estimate("hi", 1000000, 1000)    # cpt 1000 -> clamp 8
        r.note_session_estimate("lo", 10000, 10000)     # cpt 1.0 -> clamp 1.5
        assert r.session_chars_per_token("hi") == 8.0
        assert r.session_chars_per_token("lo") == 1.5
    finally:
        os.unlink(path)


def test_disabled_does_not_learn_nor_use():
    r, path = _router(session_estimate_enabled=False)
    try:
        r.note_session_estimate("s1", 40000, 10000)
        assert r.session_chars_per_token("s1") is None
        tokens, used = r.estimate_for_session("s1", _msgs(20000))
        assert used is False
        assert tokens == estimate_tokens(_msgs(20000),
                                         r.policy.estimate_divisor)
    finally:
        os.unlink(path)


def test_ttl_expiry_falls_back():
    r, path = _router(session_estimate_ttl_sec=60)
    try:
        r.note_session_estimate("s1", 40000, 10000)
        assert r.session_chars_per_token("s1") == 4.0
        # invecchia artificialmente il campione oltre la TTL
        r._sess_ratio["s1"]["ts"] = time.time() - 3600
        assert r.session_chars_per_token("s1") is None
    finally:
        os.unlink(path)


def test_purge_removes_expired_and_caps_cardinality():
    r, path = _router(session_estimate_ttl_sec=60)
    try:
        now = time.time()
        r._sess_ratio["old"] = {"chars": 40000, "pt": 10000, "n": 1,
                                "ts": now - 3600}
        for i in range(4100):
            r._sess_ratio[f"s{i}"] = {"chars": 40000, "pt": 10000, "n": 1,
                                      "ts": now}
        r.purge_expired()
        assert "old" not in r._sess_ratio
        assert len(r._sess_ratio) <= 4096
    finally:
        os.unlink(path)


def test_policy_parses_session_estimate_fields():
    p = Policy.from_dict({
        "session_estimate_enabled": False,
        "session_estimate_margin": 1.2,
        "session_estimate_min_chars": 100,
        "session_estimate_min_tokens": 50,
        "session_estimate_ttl_sec": 120,
        "session_estimate_min_ratio": 2.0,
        "session_estimate_max_ratio": 6.0,
    })
    assert p.session_estimate_enabled is False
    assert p.session_estimate_margin == 1.2
    assert p.session_estimate_min_chars == 100
    assert p.session_estimate_min_tokens == 50
    assert p.session_estimate_ttl_sec == 120
    assert p.session_estimate_min_ratio == 2.0
    assert p.session_estimate_max_ratio == 6.0
    for bad in ({"session_estimate_margin": 9.0},
                {"session_estimate_min_ratio": 0},
                {"session_estimate_max_ratio": -1}):
        try:
            Policy.from_dict(bad)
            raise AssertionError(f"doveva fallire: {bad}")
        except ValueError:
            pass
