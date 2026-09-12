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
from app.router import Router, EFFORT_CAPABLE_BONUS


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
    # base score non-zero per test moltiplicativo (score=0*moltiplicatore=0 sempre)
    r._base_scores = {"s2": 100.0, "s5": 100.0, "s8": 100.0,
                      "s1": 100.0, "s10": 100.0}
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
        lo = _score(r, _d("s2", 2))
        hi = _score(r, _d("s8", 10))
    # Nessun bias: score = base_score (uguale per entrambi)
    assert lo == hi == 100.0


def test_high_effort_prefers_high_intelligence():
    r = _router()
    with effort_ctx("high"):
        hi = _score(r, _d("s8", 9))
        lo = _score(r, _d("s2", 2))
    # Moltiplicativo: s8=100*(1-4w), s2=100*(1+3w)
    # s8 < s2 (high intel ha score piu' basso = meglio)
    assert hi < lo


def test_low_effort_prefers_low_intelligence():
    r = _router()
    with effort_ctx("low"):
        hi = _score(r, _d("s8", 9))
        lo = _score(r, _d("s2", 2))
    # Moltiplicativo: s8=100*(1+4w), s2=100*(1-3w)
    # s2 < s8 (low intel ha score piu' basso = meglio)
    assert lo < hi


def test_medium_effort_prefers_center():
    r = _router()
    with effort_ctx("medium"):
        mid = _score(r, _d("s5", 5))
        hi = _score(r, _d("s10", 10))
        lo = _score(r, _d("s1", 1))
    # Moltiplicativo: s5=100*(1+0)=100, s10=100*(1+5w), s1=100*(1+4w)
    # mid < hi and mid < lo (centro ha score piu' basso = meglio)
    assert mid < hi and mid < lo


def test_high_effort_bonus_for_capable():
    r = _router()
    with effort_ctx("high"):
        cap = _score(r, _d("s5", 5, capable=True))
        nocap = _score(r, _d("s5", 5, capable=False))
    # Moltiplicativo: s5=100*(1+0)=100, poi -1.5 per capable
    # cap = 100 - 1.5 = 98.5, nocap = 100
    assert cap == nocap - EFFORT_CAPABLE_BONUS
    assert cap < nocap


def test_high_effort_never_inverts_good_score():
    # regressione: punteggio buono (negativo) + intel alto NON deve diventare
    # positivo per via del bias (era il bug della moltiplicazione a segno variabile)
    r = _router()
    r._base_scores = {"good": -20.0}
    with effort_ctx("high"):
        s = _score(r, _d("good", 6))
    assert s < 0 and s <= -20.0


def test_high_effort_favors_intel_among_negative_scores():
    r = _router()
    r._base_scores = {"smart": -10.0, "dumb": -10.0}
    with effort_ctx("high"):
        smart = _score(r, _d("smart", 9))
        dumb = _score(r, _d("dumb", 2))
    assert smart < dumb < 0


def test_low_effort_favors_low_intel_among_negative_scores():
    r = _router()
    r._base_scores = {"low": -10.0, "high": -10.0}
    with effort_ctx("low"):
        low = _score(r, _d("low", 2))
        high = _score(r, _d("high", 9))
    assert low < high < 0


def test_medium_penalizes_extremes_even_with_negative_scores():
    r = _router()
    r._base_scores = {"mid": -10.0, "ext": -10.0}
    with effort_ctx("medium"):
        mid = _score(r, _d("mid", 5))
        ext = _score(r, _d("ext", 10))
    assert mid < ext


def test_bias_weight_scales_gap():
    r = _router()
    w = r.policy.effort_intel_weight
    with effort_ctx("high"):
        s2 = _score(r, _d("s2", 2))
        s8 = _score(r, _d("s8", 8))
    assert s8 < s2
    r.policy.effort_intel_weight = w * 2
    with effort_ctx("high"):
        s2b = _score(r, _d("s2", 2))
        s8b = _score(r, _d("s8", 8))
    assert (s2b - s8b) > (s2 - s8)
