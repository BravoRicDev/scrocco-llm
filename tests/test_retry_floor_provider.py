"""Floor Retry-After specifico per provider (anti-loop 429 proporzionato)."""
import pytest

from app import forwarder as F
from app.policy import Policy


def test_provider_specific_floor():
    try:
        F.set_retry_after_floors(10, {"groq": 5, "google": 30,
                                      "openrouter": 15})
        assert F._apply_retry_floor(1, "groq") == 5
        assert F._apply_retry_floor(1, "google") == 30
        assert F._apply_retry_floor(1, "GROQ") == 5          # case-insensitive
        assert F._apply_retry_floor(1, "opencode-zen") == 10  # default
        assert F._apply_retry_floor(1, None) == 10
        assert F._apply_retry_floor(50, "groq") == 50         # valore piu' alto
    finally:
        F.set_retry_after_floors(10, {})


def test_floor_zero_disables_globally():
    try:
        F.set_retry_after_floors(0, {})
        assert F._apply_retry_floor(1, "groq") == 1
    finally:
        F.set_retry_after_floors(10, {})


def test_provider_floor_survives_zero_default():
    try:
        F.set_retry_after_floors(0, {"google": 30})
        assert F._apply_retry_floor(1, "google") == 30
        assert F._apply_retry_floor(1, "other") == 1
    finally:
        F.set_retry_after_floors(10, {})


def test_policy_parsing_floor_table():
    p = Policy.from_dict({"retry_after_floor_by_provider":
                          {"groq": 5, "google": 30}})
    assert p.retry_after_floor_by_provider == {"groq": 5.0, "google": 30.0}
    assert Policy.from_dict({}).retry_after_floor_by_provider == {}


def test_policy_rejects_bad_floor_table():
    with pytest.raises(ValueError):
        Policy.from_dict({"retry_after_floor_by_provider": {"x": "abc"}})
    with pytest.raises(ValueError):
        Policy.from_dict({"retry_after_floor_by_provider": [1, 2]})
