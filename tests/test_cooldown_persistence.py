"""Persistenza cooldown su disco (var/cooldown_state.json): sopravvivenza
ai restart con eta' reale (`since`) e durata totale (`full`) ripristinate.
Solo i cooldown NON scaduti vengono riattivati."""
import time

from app.policy import Policy
from app.router import Router


def _router():
    r = Router.__new__(Router)
    r.policy = Policy()
    r._stats = {}
    r._cooldown = {}
    r._cooldown_since = {}
    r._cap_strikes = {}
    r._base_scores = {}
    r._provider_scores = {}
    r._key_scores = {}
    r._avg_latencies = {}
    return r


def test_save_only_non_expired():
    r = _router()
    now = time.time()
    r._cooldown["a"] = now + 100
    r._cooldown_since["a"] = now - 50
    r._cooldown_full_map()["a"] = 150.0
    r._cooldown["b"] = now - 10          # gia' scaduto
    d = r.save_cooldowns()
    assert "a" in d
    assert "b" not in d
    assert d["a"]["expires"] == now + 100
    assert d["a"]["since"] == now - 50
    assert d["a"]["full"] == 150.0


def test_load_restores_non_expired_with_since_and_full():
    r = _router()
    now = time.time()
    n = r.load_cooldowns({
        "a": {"expires": now + 60, "since": now - 30, "full": 120.0},
        "b": {"expires": now - 5, "since": now - 90, "full": 120.0},  # scaduto
        "c": "garbage",                   # ignorata
        "d": {"expires": "x"},            # ignorata
    })
    assert n == 1
    assert r._cooldown["a"] == now + 60
    assert r._cooldown_since["a"] == now - 30
    assert r._cooldown_full_map()["a"] == 120.0
    assert "b" not in r._cooldown
    assert "c" not in r._cooldown
    assert "d" not in r._cooldown


def test_load_handles_missing_since_and_full():
    r = _router()
    now = time.time()
    n = r.load_cooldowns({"a": {"expires": now + 30}})
    assert n == 1
    # since assente -> trattato come "appena impostato"; full assente -> 0
    assert r._cooldown_since["a"] >= now - 1
    assert r._cooldown_full_map().get("a", 0.0) == 0.0


def test_load_non_dict_is_noop():
    r = _router()
    assert r.load_cooldowns([]) == 0
    assert r.load_cooldowns(None) == 0


def test_dump_stats_has_no_cooldown_key():
    """Il cooldown vive nel file dedicato, non piu' in adaptive_stats."""
    r = _router()
    r._cooldown["a"] = time.time() + 10
    assert "cooldown" not in r.dump_stats()