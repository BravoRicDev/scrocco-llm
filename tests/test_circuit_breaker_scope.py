"""Circuit breaker ibrido (dep + key): un fallimento di modello non deve
oscurare i deployment che condividono la stessa API key; solo i segnali di
chiave (401/402/403/429) aprono il breaker per-chiave."""
import time
from unittest.mock import MagicMock

from app.policy import Policy
from app.router import Router


def _dep(unique, api_key="k1", model="m-a"):
    return {"unique": unique, "group": "test-128k", "model": model,
            "api_base": "https://api.groq.com/openai/v1", "api_key": api_key,
            "tier": 0, "max_input_tokens": 128000,
            "needs_openai_provider": True, "priority": 0,
            "caps": frozenset({"text"}), "provider": "groq",
            "effort_capable": False, "intelligence": 5, "media_defer": True,
            "tool_repair": "", "model_preference": 0}


A, B, C = _dep("dA", "k1", "m-a"), _dep("dB", "k1", "m-b"), _dep("dC", "k2", "m-c")


def _mk_router(deps):
    r = Router.__new__(Router)
    r.policy = Policy()
    r.config = MagicMock()
    r.config.go_suffix = "-go"
    r.config.fallback_suffix = "-fallback"
    r.config.proxy_prefix = "scrocco-llm-"
    r.config.groups = {"test-128k": list(deps)}
    r.config.group_caps = {}
    r.config.deployment_by_unique = lambda u: next(
        (d for d in r.config.groups["test-128k"] if d["unique"] == u), None)
    for a in ("_base_scores", "_provider_scores", "_key_scores", "_avg_latencies",
              "_cooldown", "_cooldown_since", "_stats", "_sticky_dep", "_sticky",
              "_session_group", "_defer_active", "_cap_strikes",
              "_circuit_breakers"):
        setattr(r, a, {})
    r._dep_circuit_breakers = {}
    r._circuit_breaker_threshold = 2
    r._circuit_breaker_timeout = 60.0
    r._circuit_breaker_half_open_requests = 1
    r.policy.circuit_breaker_threshold = 2
    r.policy.circuit_breaker_half_open_requests = 1
    r.policy.circuit_breaker_timeout = 60.0
    return r


def test_model_failure_opens_only_dep():
    r = _mk_router([A, B, C])
    r._update_circuit_breaker_on_failure("dA", key_level=False)
    r._update_circuit_breaker_on_failure("dA", key_level=False)
    assert r._is_circuit_open("dA") is True
    assert r._is_circuit_open("dB") is False        # stesso k1, NON oscurato
    assert r._is_circuit_open("dC") is False
    assert (r._circuit_breakers.get("k1") or {}).get("state", "closed") == "closed"


def test_auth_failure_opens_key_blocks_siblings():
    r = _mk_router([A, B, C])
    r._update_circuit_breaker_on_failure("dA", key_level=True)
    r._update_circuit_breaker_on_failure("dA", key_level=True)
    assert r._is_circuit_open("dB") is True         # fratello k1 bloccato
    assert r._is_circuit_open("dC") is False        # k2 intatto


def test_scope_dep_only():
    r = _mk_router([A, B, C])
    r.policy.circuit_breaker_scope = "dep"
    r._update_circuit_breaker_on_failure("dA", key_level=True)
    r._update_circuit_breaker_on_failure("dA", key_level=True)
    assert r._is_circuit_open("dA") is True
    assert r._is_circuit_open("dB") is False
    assert r._circuit_breakers.get("k1") is None


def test_scope_key_legacy():
    r = _mk_router([A, B, C])
    r.policy.circuit_breaker_scope = "key"
    r._update_circuit_breaker_on_failure("dA", key_level=False)
    r._update_circuit_breaker_on_failure("dA", key_level=False)
    assert r._is_circuit_open("dB") is True


def test_half_open_then_close_dep():
    r = _mk_router([A, B])
    r.policy.circuit_breaker_scope = "dep"
    r.policy.circuit_breaker_timeout = 0.01
    r._update_circuit_breaker_on_failure("dA", key_level=False)
    r._update_circuit_breaker_on_failure("dA", key_level=False)
    assert r._is_circuit_open("dA") is True
    time.sleep(0.02)
    assert r._is_circuit_open("dA") is False        # -> half-open (1 passaggio)
    r._update_circuit_breaker_on_success("dA")
    assert r._dep_circuit_breakers["dA"]["state"] == "closed"
    assert r._dep_circuit_breakers["dA"]["failures"] == 0


def test_is_key_level_failure():
    assert Router._is_key_level_failure(401, "http_401") is True
    assert Router._is_key_level_failure(-429, "") is True
    assert Router._is_key_level_failure(402, "http_402") is True
    assert Router._is_key_level_failure(400, "model_missing") is False
    assert Router._is_key_level_failure(500, "provider_error") is False
