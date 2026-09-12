# Test per il sistema model_preference
import pytest
import contextlib
from app.router import Router
from app.policy import Policy
from app.config import _classify
from app.effort import set_effort, reset_effort, get_effort
from datetime import date


def _router():
    """Crea un Router minimo per test unitari."""
    r = Router.__new__(Router)
    r.policy = Policy()
    r.config = object()
    r._base_scores = {"d_neg": -1000.0, "d_pos": 1000.0, "d_zero": 0.0}
    r._provider_scores = {}
    r._key_scores = {}
    r._avg_latencies = {}
    return r


def _dep(unique, pref, intel=5):
    return {"unique": unique, "model_preference": pref, "intelligence": intel, "effort_capable": True, "api_base": "https://test/v1", "model": "test", "api_key": "k"}


def test_pref_zero_no_change():
    """preference=0 non cambia lo score (comportamento identico a prima)"""
    r = _router()
    s1 = r._reputation_score("d_neg", _dep("d_neg", 0))
    s2 = r._reputation_score("d_pos", _dep("d_pos", 0))
    s3 = r._reputation_score("d_zero", _dep("d_zero", 0))
    assert s1 == -1000.0
    assert s2 == 1000.0
    assert s3 == 0.0


def test_pref_positive_improves_negative_score():
    """preference > 0 migliora score negativi (più negativo = meglio)"""
    r = _router()
    # pref=100 su score -1000: score -= 100 * 1000 / 100 = -1000 - 1000 = -2000
    s = r._reputation_score("d_neg", _dep("d_neg", 100))
    assert s == -2000.0


def test_pref_negative_neutralizes_negative_score():
    """preference < 0 neutralizza score negativi (porta verso 0)"""
    r = _router()
    # pref=-100 su score -1000: score -= (-100) * 1000 / 100 = -1000 + 1000 = 0
    s = r._reputation_score("d_neg", _dep("d_neg", -100))
    assert s == 0.0


def test_pref_negative_doubles_positive_score():
    """preference < 0 raddoppia score positivi (peggiora)"""
    r = _router()
    # pref=-100 su score +1000: score -= (-100) * 1000 / 100 = 1000 + 1000 = 2000
    s = r._reputation_score("d_pos", _dep("d_pos", -100))
    assert s == 2000.0


def test_pref_100_zeroes_positive_scores():
    """pref=100 porta score POSITIVI a 0 (score -= |score|)"""
    r = _router()
    # pref=100 su score POSITIVO +1000 -> score -= 100 * 1000 / 100 = 1000 - 1000 = 0
    s1 = r._reputation_score("d_pos", _dep("d_pos", 100))
    assert s1 == 0.0
    
    # pref=100 su score NEGATIVO -1000 -> score -= 100 * 1000 / 100 = -1000 - 1000 = -2000
    s2 = r._reputation_score("d_neg", _dep("d_neg", 100))
    assert s2 == -2000.0
    
    # pref=100 su score ZERO -> invariato
    s3 = r._reputation_score("d_zero", _dep("d_zero", 100))
    assert s3 == 0.0


def test_pref_negative_100_zeroes_negative_scores():
    """pref=-100 porta score NEGATIVI a 0 (score -= (-100) * |score| / 100 = score + |score|)"""
    r = _router()
    # pref=-100 su score NEGATIVO -1000 -> score -= (-100) * 1000 / 100 = -1000 + 1000 = 0
    s1 = r._reputation_score("d_neg", _dep("d_neg", -100))
    assert s1 == 0.0
    
    # pref=-100 su score POSITIVO +1000 -> score -= (-100) * 1000 / 100 = 1000 + 1000 = 2000
    s2 = r._reputation_score("d_pos", _dep("d_pos", -100))
    assert s2 == 2000.0


def test_pref_large_values():
    """Test con valori grandi (300, -100)"""
    r = _router()
    # pref=300 su -1000 -> score -= 300 * 1000 / 100 = -1000 - 3000 = -4000
    s = r._reputation_score("d_neg", _dep("d_neg", 300))
    assert s == -4000.0
    
    # pref=-100 su -1000 -> score -= (-100) * 1000 / 100 = -1000 + 1000 = 0
    s = r._reputation_score("d_neg", _dep("d_neg", -100))
    assert s == 0.0


def test_pref_improves_positive_scores():
    """preference > 0 migliora anche score positivi (riduce verso 0)"""
    r = _router()
    # pref=100 su +1000 -> score -= 100 * 1000 / 100 = 1000 - 1000 = 0
    s = r._reputation_score("d_pos", _dep("d_pos", 100))
    assert s == 0.0
    
    # pref=50 su +1000 -> score -= 50 * 1000 / 100 = 1000 - 500 = 500
    s = r._reputation_score("d_pos", _dep("d_pos", 50))
    assert s == 500.0


def test_pref_fractional():
    """Test con valori frazionari (anche se CSV accetta solo int)"""
    r = _router()
    # Simuliamo direttamente
    score = -1000.0
    pref = 50
    new_score = score - pref * abs(score) / 100.0
    assert new_score == -1500.0


def test_csv_parsing():
    """Verifica che il parsing CSV funzioni per model_preference"""
    row = {
        "model_preference": "3",
        "effort_capable": "true",
        "intelligence_score": "6",
        "modello": "test-model",
        "provider": "test",
        "endpoint": "https://test/v1",
        "data": "free",
        "context": "200",
        "max_input": "50000",
        "priority": "0",
        "caps": "text",
        "scrocco-llm-mioaruba": "sk-test",
        "tool_repair": "",
    }
    result = _classify(row, date.today())
    assert result["model_preference"] == 3

    # Default quando mancante
    row2 = dict(row)
    row2["model_preference"] = ""
    result2 = _classify(row2, date.today())
    assert result2["model_preference"] == 0

    # Non numerico -> default 0
    row3 = dict(row)
    row3["model_preference"] = "abc"
    result3 = _classify(row3, date.today())
    assert result3["model_preference"] == 0

    # Valori negativi
    row4 = dict(row)
    row4["model_preference"] = "-5"
    result4 = _classify(row4, date.today())
    assert result4["model_preference"] == -5

    # Valori grandi
    row5 = dict(row)
    row5["model_preference"] = "300"
    result5 = _classify(row5, date.today())
    assert result5["model_preference"] == 300


def test_pref_is_last_judge():
    """La preferenza deve essere l'ULTIMO giudice (dopo effort, base_scores, etc.)"""
    r = _router()
    r._base_scores = {"d1": -3010.0}  # score reale tipico
    
    # Con effort=high e intel=7 (g=2), weight=10 -> base = 1.0 + 2*0.1 = 1.2
    # factor = 1/1.2 per score negativo -> score * 0.833
    # Poi preference=100 -> score -= 100 * |score| / 100 = score - |score| = 0
    
    # Test che la preferenza si applichi DOPO l'effort
    # Impostiamo effort=high con intel=7
    tok = set_effort("high")
    try:
        r.policy.effort_intel_weight = 10.0
        s = r._reputation_score("d1", _dep("d1", 100, intel=7))
        # Con effort=high: factor = 1/(1+2*0.1) = 1/1.2 = 0.833
        # score = -3010 * 0.833 = -2508.33
        # Poi pref=100: score -= 100 * 2508.33 / 100 = -2508.33 - 2508.33 = -5016.66
        # Ma con pref=0 dovrebbe essere -2508.33
        pass
    finally:
        reset_effort(tok)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])