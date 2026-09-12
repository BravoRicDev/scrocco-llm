"""Feature: effort/reasoning nel routing e nel forward.

Copre:
- normalizzazione/estrazione dell'`effort` (body `reasoning_effort`, header);
- `apply_effort_policy`: iniezione/rimozione `reasoning_effort` per provider,
  override temperatura (con precedenza al client);
- parsing delle colonne CSV `effort_capable` e `intelligence_score`;
- bias di intelligence nel pick del router.
"""
import contextlib
from datetime import date

from app.config import _classify
from app.effort import (normalize_effort, effort_from_request, set_effort,
                        reset_effort, get_effort)
from app.forwarder import apply_effort_policy
from app.policy import Policy
from app.router import Router, EFFORT_CAPABLE_BONUS, EFFORT_INTEL_WEIGHT


@contextlib.contextmanager
def effort_ctx(effort, *, temp_enabled=False, temp_overrides=None):
    tok = set_effort(effort, temp_enabled=temp_enabled,
                     temp_overrides=temp_overrides)
    try:
        yield
    finally:
        reset_effort(tok)


# --------------------------------------------------------------- normalizza

def test_normalize_effort():
    assert normalize_effort("high") == "high"
    assert normalize_effort("LOW") == "low"
    assert normalize_effort("Medium") == "medium"
    assert normalize_effort("minimal") == "low"
    assert normalize_effort(None) == "default"
    assert normalize_effort("boh") == "default"
    assert normalize_effort("") == "default"


def test_effort_from_request_body_and_header():
    assert effort_from_request({"reasoning_effort": "high"}) == "high"
    assert effort_from_request({"effort": "low"}) == "low"
    # il body vince sull'header
    assert effort_from_request({"reasoning_effort": "medium"},
                               {"x-effort": "high"}) == "medium"
    # header come fallback
    assert effort_from_request({}, {"x-effort": "low"}) == "low"
    assert effort_from_request({}, {}) == "default"


# ------------------------------------------------------- apply_effort_policy

def _dep(*, capable=False, base="https://openrouter.ai/api/v1"):
    return {"api_base": base, "effort_capable": capable, "model": "m",
            "provider": "p", "api_key": "k"}


def test_default_effort_leaves_body_untouched():
    with effort_ctx("default"):
        body = {"model": "m", "messages": []}
        assert apply_effort_policy(body, _dep(capable=True)) == {
            "model": "m", "messages": []}


def test_effort_capable_gets_reasoning_effort():
    with effort_ctx("high"):
        body = {"model": "m"}
        apply_effort_policy(body, _dep(capable=True))
        assert body["reasoning_effort"] == "high"


def test_non_capable_strips_reasoning_effort():
    with effort_ctx("high"):
        body = {"model": "m", "reasoning_effort": "high"}
        apply_effort_policy(body, _dep(capable=False))
        assert "reasoning_effort" not in body


def test_groq_strips_even_if_capable():
    with effort_ctx("high"):
        body = {"model": "m"}
        apply_effort_policy(
            body, _dep(capable=True, base="https://api.groq.com/openai/v1"))
        assert "reasoning_effort" not in body


def test_client_reasoning_effort_wins():
    with effort_ctx("high"):
        body = {"model": "m", "reasoning_effort": "low"}
        apply_effort_policy(body, _dep(capable=True))
        assert body["reasoning_effort"] == "low"


def test_temperature_override_applied_when_enabled_and_absent():
    with effort_ctx("high", temp_enabled=True,
                    temp_overrides={"high": 0.2}):
        body = {"model": "m"}
        apply_effort_policy(body, _dep(capable=False))
        assert body["temperature"] == 0.2


def test_temperature_override_disabled():
    with effort_ctx("high", temp_enabled=False,
                    temp_overrides={"high": 0.2}):
        body = {"model": "m"}
        apply_effort_policy(body, _dep(capable=False))
        assert "temperature" not in body


def test_client_temperature_wins_over_override():
    with effort_ctx("high", temp_enabled=True,
                    temp_overrides={"high": 0.2}):
        body = {"model": "m", "temperature": 0.9}
        apply_effort_policy(body, _dep(capable=False))
        assert body["temperature"] == 0.9


# ------------------------------------------------------------- config parse

def _row(**over):
    row = {"modello": "m", "provider": "p", "endpoint": "e", "data": "free"}
    row.update(over)
    return row


def test_classify_reads_effort_columns():
    meta = _classify(_row(effort_capable="true", intelligence_score="8"),
                     date.today())
    assert meta["effort_capable"] is True
    assert meta["intelligence"] == 8


def test_classify_defaults_when_columns_missing():
    meta = _classify(_row(), date.today())
    assert meta["effort_capable"] is False
    assert meta["intelligence"] == 5


def test_classify_clamps_intelligence():
    assert _classify(_row(intelligence_score="99"), date.today())["intelligence"] == 10
    assert _classify(_row(intelligence_score="0"), date.today())["intelligence"] == 1


# --------------------------------------------------------------- router bias

def _router():
    r = Router.__new__(Router)
    r.policy = Policy()
    r.config = object()          # non-None: il bias deve essere calcolato
    r._base_scores = {}
    r._provider_scores = {}
    r._key_scores = {}
    r._avg_latencies = {}
    return r


def _score(r, dep):
    return r._reputation_score(dep["unique"], dep)


def _d(unique, intel, capable=False):
    return {"unique": unique, "intelligence": intel,
            "effort_capable": capable, "api_base": "https://x/v1",
            "model": "m", "api_key": "k"}


def test_default_effort_no_intelligence_bias():
    r = _router()
    with effort_ctx("default"):
        lo = _score(r, _d("lo", 2))
        hi = _score(r, _d("hi", 10))
    assert lo == hi == 0.0


def test_high_effort_prefers_high_intelligence():
    r = _router()
    with effort_ctx("high"):
        hi = _score(r, _d("hi", 9))
        lo = _score(r, _d("lo", 2))
    assert hi < lo


def test_low_effort_prefers_low_intelligence():
    r = _router()
    with effort_ctx("low"):
        hi = _score(r, _d("hi", 9))
        lo = _score(r, _d("lo", 2))
    assert lo < hi


def test_medium_effort_prefers_center():
    r = _router()
    with effort_ctx("medium"):
        mid = _score(r, _d("mid", 5))
        hi = _score(r, _d("hi", 10))
        lo = _score(r, _d("lo", 1))
    assert mid < hi and mid < lo


def test_high_effort_bonus_for_capable():
    r = _router()
    with effort_ctx("high"):
        cap = _score(r, _d("cap", 5, capable=True))
        nocap = _score(r, _d("nocap", 5, capable=False))
    assert cap == nocap - EFFORT_CAPABLE_BONUS
    assert cap < nocap


def test_bias_weight_scales():
    r = _router()
    with effort_ctx("high"):
        s2 = _score(r, _d("s2", 2))
        s8 = _score(r, _d("s8", 8))
    assert round(s2 - s8, 3) == round(6.0 * EFFORT_INTEL_WEIGHT, 3)
