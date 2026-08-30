"""Test anti-provider: escalation cooldown, Retry-After, success EMA,
auto-learn strikes e sanity QC."""
import time

import pytest

from app.policy import Policy, QcSanity
from app.qc import check_sanity
from app.router import Router

POL = Policy()
POL.cooldown_sec = 10
POL.max_cooldown_sec = 100
POL.cooldown_mode = "exponential"     # il test verifica l'escalation esponenziale


def test_escalation_doubling_and_cap():
    r = Router.__new__(Router)          # senza config: servono solo policy/stats
    r.policy = POL
    r._cooldown = {}
    r._cooldown_since = {}
    r._stats = {}
    r._cap_strikes = {}
    d1 = r.mark_failed("u")
    d2 = r.mark_failed("u")
    d3 = r.mark_failed("u")
    d4 = r.mark_failed("u")
    assert (d1, d2, d3, d4) == (10, 20, 40, 80)
    d5 = r.mark_failed("u")
    assert d5 == 100                    # cap max_cooldown_sec


def test_explicit_seconds_wins():
    r = Router.__new__(Router)
    r.policy = POL
    r._cooldown = {}
    r._cooldown_since = {}
    r._stats = {}
    r._cap_strikes = {}
    assert r.mark_failed("u", seconds=37) == 37
    exp = r._cooldown["u"]
    assert 30 < exp - time.time() <= 37


def test_success_resets_streak_and_raises_ema():
    r = Router.__new__(Router)
    r.policy = POL
    r._cooldown = {}
    r._cooldown_since = {}
    r._stats = {}
    r._cap_strikes = {}
    r.mark_failed("u")
    s = r.stats_for("u")
    assert s.fail_streak == 1 and s.success_ema == pytest.approx(0.8)
    r.note_result("u", 100)
    assert s.fail_streak == 0
    assert s.success_ema == pytest.approx(0.8 * 0.8 + 0.2)


def test_score_penalizes_low_success_rate():
    r = Router.__new__(Router)
    r.policy = POL
    r._cooldown = {}
    r._cooldown_since = {}
    r._stats = {}
    r._cap_strikes = {}
    good = {"unique": "good", "priority": 0}
    bad = {"unique": "bad", "priority": 0}
    r.note_result("good", 100)
    for _ in range(5):
        r.mark_failed("bad")            # ema -> ~0.33*... molto basso
    now = time.time()
    assert r._score(good, now) > r._score(bad, now) * 2


def test_strike_threshold_and_window():
    r = Router.__new__(Router)
    p = Policy()
    p.cap_auto_learn_threshold = 2
    r.policy = p
    r._stats = {}
    r._cooldown = {}
    r._cooldown_since = {}
    r._cap_strikes = {}
    hits1 = r.note_cap_strike("m/x", ["vision"], "boom image")
    assert hits1 == []
    hits2 = r.note_cap_strike("m/x", ["vision"], "boom image 2")
    assert hits2 == ["vision"]
    # finestra scaduta -> riparte da zero
    key = "m/x|vision"
    r._cap_strikes[key]["last"] -= 8 * 86400
    assert r.note_cap_strike("m/x", ["vision"], "di nuovo") == []


def test_strikes_persistence_roundtrip():
    r = Router.__new__(Router)
    r.policy = Policy()
    r._stats = {}
    r._cooldown = {}
    r._cooldown_since = {}
    r._cap_strikes = {}
    r.note_cap_strike("a/b", ["tts"], "no tts here")
    dump = r.dump_stats()
    r2 = Router.__new__(Router)
    r2.policy = Policy()
    r2._stats = {}
    r2._cooldown = {}
    r2._cooldown_since = {}
    r2._cap_strikes = {}
    r2.load_stats(dump)
    view = r2.cap_strikes_view()
    assert view and view[0]["model"] == "a/b" and view[0]["cap"] == "tts"
    assert "tts" in view[0]["evidence"]


def test_strip_cap_from_map_preserves_glob():
    from app.admin import strip_cap_from_map
    m = {"*vl-*": ["text", "vision"], "other": ["text"]}
    out = strip_cap_from_map(m, "qwen/qwen2.5-vl-72b", "vision")
    # entry esplicita SENZA vision...
    assert out["qwen/qwen2.5-vl-72b"] == ["text"]
    # ...ma la glob resta intatta per gli altri modelli
    assert out["*vl-*"] == ["text", "vision"]
    assert out["other"] == ["text"]
    # floor: rimozione dell'ultima capacità -> ["text"]
    m2 = {"solo": ["stt"]}
    out2 = strip_cap_from_map(m2, "x/y", "stt")
    assert out2["x/y"] == ["text"]      # floor, mai vuoto


# ------------------------------------------------------------------ sanity QC
def _resp(content, **extra):
    return {"choices": [{"message": {"content": content, **extra}}]}


def test_sanity_flags_empty():
    san = QcSanity(enabled=True, min_chars=1)
    assert check_sanity(_resp(""), {}, san) is not None
    assert check_sanity(_resp("   "), {}, san) is not None
    assert check_sanity(_resp("ok"), {}, san) is None


def test_sanity_respects_tool_calls_and_images():
    san = QcSanity(enabled=True, min_chars=1)
    assert check_sanity(_resp(None, tool_calls=[{"id": "1"}]), {}, san) is None
    assert check_sanity(_resp("", images=["b64"]), {}, san) is None


def test_sanity_disabled_and_min_chars():
    san = QcSanity(enabled=False)
    assert check_sanity(_resp(""), {}, san) is None
    san2 = QcSanity(enabled=True, min_chars=5)
    assert check_sanity(_resp("ab"), {}, san2) is not None
    assert check_sanity(_resp("abcde "), {}, san2) is None


def test_policy_new_knobs_validation():
    p = Policy.from_dict({
        "cooldown_escalation": False,
        "max_cooldown_sec": 3600,
        "qc_sanity": {"enabled": False, "min_chars": 3},
        "capability_routing": {"auto_learn": "suggest",
                               "auto_learn_threshold": 5},
    })
    assert p.cooldown_escalation is False
    assert p.max_cooldown_sec == 3600
    assert p.qc_sanity.enabled is False and p.qc_sanity.min_chars == 3
    assert p.cap_auto_learn == "suggest" and p.cap_auto_learn_threshold == 5


def test_policy_rejects_bad_auto_learn_mode():
    with pytest.raises(ValueError):
        Policy.from_dict({"capability_routing": {"auto_learn": "yolo"}})
