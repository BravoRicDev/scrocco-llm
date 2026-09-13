"""Tests for: dynamic scoring, circuit breaker, provider health, observability."""

from __future__ import annotations

import math
import time
from unittest.mock import MagicMock

import pytest

from app.router import Router, DepStats, DYNAMIC_SCORING_DEFAULTS
from app.policy import Policy


# ---- Helpers ----

def _dep(unique="dep-1", provider="groq", model="test/model", api_key="k1",
          caps="text", api_base="https://api.groq.com/openai/v1"):
    return {
        "unique": unique,
        "group": "test-128k",
        "model": model,
        "api_base": api_base,
        "api_key": api_key,
        "tier": provider,
        "max_input_tokens": 128000,
        "needs_openai_provider": True,
        "priority": 0,
        "caps": frozenset([c.strip() for c in caps.split(",") if c]),
        "provider": provider,
        "effort_capable": False,
        "intelligence": 5,
        "media_defer": True,
        "tool_repair": "",
        "model_preference": 0,
    }


def _mk_router():
    """Router con mínimo stato (compatibile con test pattern)."""
    r = Router.__new__(Router)
    r.policy = Policy()
    r.config = MagicMock()
    r.config.go_suffix = "-go"
    r.config.fallback_suffix = "-fallback"
    r.config.proxy_prefix = "scrocco-llm-"
    r.config.groups = {"test-128k": [_dep()]}
    r.config.group_caps = {}
    r._base_scores = {}
    r._provider_scores = {}
    r._key_scores = {}
    r._avg_latencies = {}
    r._cooldown = {}
    r._cooldown_since = {}
    r._stats = {}
    r._sticky_dep = {}
    r._sticky = {}
    r._session_group = {}
    r._defer_active = {}
    r._cap_strikes = {}
    r._circuit_breakers = {}
    r._circuit_breaker_threshold = 5
    r._circuit_breaker_timeout = 60.0
    r._circuit_breaker_half_open_requests = 3
    r._gen_last_model = {}
    r._session_last_ok = {}
    r._session_compact = {}
    r.media_deferred = {}
    r.gen_cross_model = {}
    # Wire deployment_by_unique on config mock
    def _lookup(unique):
        for lst in r.config.groups.values():
            for d in lst:
                if d["unique"] == unique:
                    return d
        return None
    r.config.deployment_by_unique = _lookup
    return r


# ============================================================
# DYNAMIC SCORING TESTS
# ============================================================

class TestDynamicScoring:
    def test_no_stats_means_no_adjustment(self):
        """Senza stats, dynamic scoring non modifica il punteggio."""
        r = _mk_router()
        d = _dep()
        score = r._reputation_score(d["unique"], d)
        # Senza stats, score = 0.0
        assert score == 0.0

    def test_high_latency_penalizes(self):
        """Deployment con latenza alta riceve malus."""
        r = _mk_router()
        d = _dep(unique="dep-slow")
        s = r.stats_for("dep-slow")
        # Simula 50 latenze alte (10s = 10000ms)
        s.latency_history = [10000.0] * 50
        s.recent_attempts = 50
        s.recent_failures = 0
        score = r._reputation_score("dep-slow", d)
        # 10000/1000 * 1.0 weight = 10.0 penalty
        assert score > 0  # penalità positiva (peggio)

    def test_low_error_rate_benefits(self):
        """Deployment con basso error rate riceve meno malus."""
        r = _mk_router()
        d_good = _dep(unique="dep-good")
        s_good = r.stats_for("dep-good")
        s_good.latency_history = [500.0] * 10
        s_good.recent_attempts = 10
        s_good.recent_failures = 0
        score_good = r._reputation_score("dep-good", d_good)

        d_bad = _dep(unique="dep-bad")
        s_bad = r.stats_for("dep-bad")
        s_bad.latency_history = [500.0] * 10
        s_bad.recent_attempts = 10
        s_bad.recent_failures = 5  # 50% error rate
        score_bad = r._reputation_score("dep-bad", d_bad)

        assert score_bad > score_good  # alto error rate = peggiore

    def test_throughput_benefits(self):
        """Deployment con alto throughput riceve bonus."""
        r = _mk_router()
        d_fast = _dep(unique="dep-fast")
        s_fast = r.stats_for("dep-fast")
        s_fast.latency_history = [500.0] * 10
        s_fast.recent_attempts = 10
        s_fast.recent_failures = 0
        s_fast.total_tokens = 100000
        s_fast.total_duration_ms = 1000.0  # 100000 tok/s -> capped at 1000
        score_fast = r._reputation_score("dep-fast", d_fast)

        d_slow = _dep(unique="dep-slow2")
        s_slow = r.stats_for("dep-slow2")
        s_slow.latency_history = [500.0] * 10
        s_slow.recent_attempts = 10
        s_slow.recent_failures = 0
        s_slow.total_tokens = 100
        s_slow.total_duration_ms = 100000.0  # 1 tok/s
        score_slow = r._reputation_score("dep-slow2", d_slow)

        assert score_fast < score_slow  # alto throughput = migliore (più negativo)

    def test_disabled_dynamic_scoring(self):
        """Quando disabilitato, non modifica il punteggio."""
        r = _mk_router()
        r.policy.dynamic_scoring_enabled = False
        d = _dep(unique="dep-1")
        s = r.stats_for("dep-1")
        s.latency_history = [10000.0] * 50
        s.recent_attempts = 50
        s.recent_failures = 50
        score = r._reputation_score("dep-1", d)
        # Dynamic scoring disabilitato -> score rimane 0
        assert score == 0.0


# ============================================================
# CIRCUIT BREAKER TESTS
# ============================================================

class TestCircuitBreaker:
    def test_initial_state_closed(self):
        """Nuova chiave parte in stato closed."""
        r = _mk_router()
        cb = r._get_circuit_breaker("dep-1")
        assert cb is not None
        assert cb["state"] == "closed"
        assert cb["failures"] == 0

    def test_opens_after_threshold(self):
        """Si apre dopo N fallimenti consecutivi."""
        r = _mk_router()
        for _ in range(5):
            r._update_circuit_breaker_on_failure("dep-1")
        cb = r._get_circuit_breaker("dep-1")
        assert cb["state"] == "open"

    def test_success_resets_failures(self):
        """Successo resetta i contatori in stato closed."""
        r = _mk_router()
        r._update_circuit_breaker_on_failure("dep-1")
        r._update_circuit_breaker_on_failure("dep-1")
        r._update_circuit_breaker_on_success("dep-1")
        cb = r._get_circuit_breaker("dep-1")
        assert cb["failures"] == 0

    def test_open_blocks_requests(self):
        """Stato open blocca le richieste."""
        r = _mk_router()
        r.policy.circuit_breaker_threshold = 2
        r._update_circuit_breaker_on_failure("dep-1")
        r._update_circuit_breaker_on_failure("dep-1")
        cb = r._get_circuit_breaker("dep-1")
        assert cb["state"] == "open"
        assert r._is_circuit_open("dep-1") is True

    def test_half_open_after_timeout(self):
        """Dopo il timeout, passa a half-open."""
        r = _mk_router()
        r.policy.circuit_breaker_threshold = 2
        r.policy.circuit_breaker_timeout = 1.0
        r._update_circuit_breaker_on_failure("dep-1")
        r._update_circuit_breaker_on_failure("dep-1")
        cb = r._get_circuit_breaker("dep-1")
        assert cb["state"] == "open"
        # Simula timeout
        cb["opened_at"] = time.time() - 2.0
        result = r._is_circuit_open("dep-1")
        # Dopo timeout -> half-open -> permette (ritorna False)
        assert result is False
        cb = r._get_circuit_breaker("dep-1")
        assert cb["state"] == "half_open"

    def test_closes_after_half_open_successes(self):
        """Chiude dopo N successi in half-open."""
        r = _mk_router()
        r.policy.circuit_breaker_threshold = 2
        r.policy.circuit_breaker_timeout = 1.0
        r.policy.circuit_breaker_half_open_requests = 2
        r._update_circuit_breaker_on_failure("dep-1")
        r._update_circuit_breaker_on_failure("dep-1")
        cb = r._get_circuit_breaker("dep-1")
        cb["opened_at"] = time.time() - 2.0
        r._is_circuit_open("dep-1")  # transition to half-open
        r._update_circuit_breaker_on_success("dep-1")
        r._update_circuit_breaker_on_success("dep-1")
        assert cb["state"] == "closed"
        assert cb["failures"] == 0

    def test_half_open_failure_reopens(self):
        """Fallimento in half-open riapre il circuito."""
        r = _mk_router()
        r.policy.circuit_breaker_threshold = 1
        r.policy.circuit_breaker_timeout = 1.0
        r._update_circuit_breaker_on_failure("dep-1")
        cb = r._get_circuit_breaker("dep-1")
        cb["opened_at"] = time.time() - 2.0
        r._is_circuit_open("dep-1")  # -> half-open
        cb = r._get_circuit_breaker("dep-1")
        assert cb["state"] == "half_open"
        r._update_circuit_breaker_on_failure("dep-1")
        cb = r._get_circuit_breaker("dep-1")
        assert cb["state"] == "open"

    def test_different_keys_independent(self):
        """Chiavi diverse hanno circuit breaker indipendenti."""
        r = _mk_router()
        r.policy.circuit_breaker_threshold = 2
        d1 = _dep(unique="dep-1", api_key="broken-key")
        d2 = _dep(unique="dep-2", api_key="good-key")
        r.config.groups = {"test-128k": [d1, d2]}
        # Break dep-1's key
        r._update_circuit_breaker_on_failure("dep-1")
        r._update_circuit_breaker_on_failure("dep-1")
        assert r._is_circuit_open("dep-1") is True
        # dep-2's key is unaffected
        assert r._is_circuit_open("dep-2") is False

    def test_router_blocks_open_circuit_in_pick(self):
        """pick_deployment salta deployment con circuit breaker aperto."""
        r = _mk_router()
        d1 = _dep(unique="dep-1", api_key="broken")
        d2 = _dep(unique="dep-2", api_key="good")
        r.config.groups = {"test-128k": [d1, d2]}
        r.policy.circuit_breaker_enabled = True
        r.policy.circuit_breaker_threshold = 2
        # Open circuit for broken key
        r._update_circuit_breaker_on_failure("dep-1")
        r._update_circuit_breaker_on_failure("dep-1")
        # Reset cooldown so dep-1 would normally be eligible
        r._cooldown = {}
        # Should pick dep-2 (dep-1 circuit is open)
        result = r.pick_deployment("test-128k")
        assert result is not None
        assert result["unique"] == "dep-2"


# ============================================================
# POLICY PARSING TESTS
# ============================================================

class TestPolicyParsing:
    def test_dynamic_scoring_defaults(self):
        """Campi dynamic_scoring hanno i default corretti."""
        p = Policy()
        assert p.dynamic_scoring_enabled is True
        assert p.dynamic_scoring_latency_p95_weight == 1.0
        assert p.dynamic_scoring_error_rate_weight == 2.0
        assert p.dynamic_scoring_throughput_weight == 0.5

    def test_circuit_breaker_defaults(self):
        """Campi circuit_breaker hanno i default corretti."""
        p = Policy()
        assert p.circuit_breaker_enabled is True
        assert p.circuit_breaker_threshold == 5
        assert p.circuit_breaker_timeout == 60.0
        assert p.circuit_breaker_half_open_requests == 3

    def test_parse_dynamic_scoring_from_yaml(self):
        """Parsing dynamic_scoring da dict YAML."""
        raw = {
            "dynamic_scoring": {
                "enabled": False,
                "latency_p95_weight": 2.0,
                "error_rate_weight": 3.0,
                "throughput_weight": 1.5,
            }
        }
        p = Policy.from_dict(raw)
        assert p.dynamic_scoring_enabled is False
        assert p.dynamic_scoring_latency_p95_weight == 2.0
        assert p.dynamic_scoring_error_rate_weight == 3.0
        assert p.dynamic_scoring_throughput_weight == 1.5

    def test_parse_circuit_breaker_from_yaml(self):
        """Parsing circuit_breaker da dict YAML."""
        raw = {
            "circuit_breaker": {
                "enabled": False,
                "threshold": 3,
                "timeout": 30.0,
                "half_open_requests": 5,
            }
        }
        p = Policy.from_dict(raw)
        assert p.circuit_breaker_enabled is False
        assert p.circuit_breaker_threshold == 3
        assert p.circuit_breaker_timeout == 30.0
        assert p.circuit_breaker_half_open_requests == 5

    def test_parse_deployment_sticky_per_cap(self):
        """Parsing deployment_sticky_per_capability."""
        raw = {"deployment_sticky_per_capability": True}
        p = Policy.from_dict(raw)
        assert p.deployment_sticky_per_capability is True

    def test_invalid_dynamic_scoring_raises(self):
        """Valori non validi in dynamic_scoring alzano ValueError."""
        with pytest.raises(ValueError, match="dynamic_scoring"):
            Policy.from_dict({"dynamic_scoring": "invalid"})
        with pytest.raises(ValueError, match="dynamic_scoring"):
            Policy.from_dict({"dynamic_scoring": {"latency_p95_weight": -1}})

    def test_invalid_circuit_breaker_raises(self):
        """Valori non validi in circuit_breaker alzano ValueError."""
        with pytest.raises(ValueError, match="circuit_breaker"):
            Policy.from_dict({"circuit_breaker": "invalid"})
        with pytest.raises(ValueError, match="circuit_breaker"):
            Policy.from_dict({"circuit_breaker": {"threshold": 0}})


# ============================================================
# OBSERVABILITY TESTS
# ============================================================

class TestObservability:
    def test_trace_id_context(self):
        """Trace ID contextual scoping."""
        from app.observability import get_trace_id, set_trace_id, _trace_id_ctx
        # Default is empty
        assert get_trace_id() == ""
        # Set and verify
        token = set_trace_id("test-trace-123")
        assert get_trace_id() == "test-trace-123"
        # Reset
        _trace_id_ctx.reset(token)
        assert get_trace_id() == ""

    def test_metrics_collector_counter(self):
        """MetricsCollector: counter incrementa."""
        from app.observability import MetricsCollector
        mc = MetricsCollector()
        mc.inc_counter("test_counter", 1.0, {"label": "a"})
        mc.inc_counter("test_counter", 2.0, {"label": "a"})
        prom = mc.generate_prometheus()
        assert 'test_counter{label="a"} 3.0' in prom

    def test_metrics_collector_gauge(self):
        """MetricsCollector: gauge setta."""
        from app.observability import MetricsCollector
        mc = MetricsCollector()
        mc.set_gauge("test_gauge", 42.0)
        prom = mc.generate_prometheus()
        assert "test_gauge 42.0" in prom

    def test_metrics_collector_histogram(self):
        """MetricsCollector: histogram registra osservazioni."""
        from app.observability import MetricsCollector
        mc = MetricsCollector()
        for v in [0.1, 0.5, 1.0, 2.0, 5.0]:
            mc.observe_histogram("test_hist", v)
        prom = mc.generate_prometheus()
        assert "test_hist_count 5" in prom

    def test_replay_buffer(self):
        """ReplayBuffer: add/get/clear."""
        from app.observability import ReplayBuffer
        buf = ReplayBuffer(max_entries=3)
        from app.observability import ReplayEntry
        for i in range(5):
            buf.add(ReplayEntry(
                trace_id=f"t{i}", timestamp=time.time(),
                request={"model": "test"}, response=None, error=None, duration_ms=0
            ))
        # Should only have last 3
        recent = buf.get_recent()
        assert len(recent) == 3
        assert recent[0].trace_id == "t2"  # oldest of last 3

    def test_replay_buffer_by_trace_id(self):
        """ReplayBuffer: get_by_trace_id filtra correttamente."""
        from app.observability import ReplayBuffer, ReplayEntry
        buf = ReplayBuffer()
        buf.add(ReplayEntry(trace_id="aaa", timestamp=time.time(),
                            request={}, response=None, error=None, duration_ms=0))
        buf.add(ReplayEntry(trace_id="bbb", timestamp=time.time(),
                            request={}, response=None, error=None, duration_ms=0))
        buf.add(ReplayEntry(trace_id="aaa", timestamp=time.time(),
                            request={}, response=None, error=None, duration_ms=0))
        assert len(buf.get_by_trace_id("aaa")) == 2
        assert len(buf.get_by_trace_id("bbb")) == 1
        assert len(buf.get_by_trace_id("ccc")) == 0


# ============================================================
# CAPABILITY STICKY TESTS
# ============================================================

class TestCapabilitySticky:
    def test_cap_sticky_disabled_by_default(self):
        """Sticky per-capability disabilitato di default."""
        r = _mk_router()
        assert r.policy.deployment_sticky_per_capability is False
        r.dep_cap_sticky_set("s1", frozenset({"text"}), "dep-1")
        assert r.dep_cap_sticky_get("s1", frozenset({"text"})) is None

    def test_cap_sticky_enabled(self):
        """Sticky per-capability funziona quando abilitato."""
        r = _mk_router()
        r.policy.deployment_sticky_per_capability = True
        r.dep_cap_sticky_set("s1", frozenset({"text"}), "dep-1")
        assert r.dep_cap_sticky_get("s1", frozenset({"text"})) == "dep-1"

    def test_different_caps_different_sticky(self):
        """Caps diverse mantengono sticky separati."""
        r = _mk_router()
        r.policy.deployment_sticky_per_capability = True
        r.dep_cap_sticky_set("s1", frozenset({"text"}), "dep-text")
        r.dep_cap_sticky_set("s1", frozenset({"vision"}), "dep-vision")
        assert r.dep_cap_sticky_get("s1", frozenset({"text"})) == "dep-text"
        assert r.dep_cap_sticky_get("s1", frozenset({"vision"})) == "dep-vision"

    def test_cap_sticky_ttl(self):
        """Sticky scade dopo TTL."""
        r = _mk_router()
        r.policy.deployment_sticky_per_capability = True
        r.policy.sticky_ttl_sec = 1
        r.dep_cap_sticky_set("s1", frozenset({"text"}), "dep-1")
        time.sleep(1.1)
        assert r.dep_cap_sticky_get("s1", frozenset({"text"})) is None

    def test_cap_sticky_release(self):
        """Release rimuove lo sticky per-capability."""
        r = _mk_router()
        r.policy.deployment_sticky_per_capability = True
        r.dep_cap_sticky_set("s1", frozenset({"text"}), "dep-1")
        assert r.dep_cap_sticky_get("s1", frozenset({"text"})) == "dep-1"
        r.dep_cap_sticky_release("s1", frozenset({"text"}))
        assert r.dep_cap_sticky_get("s1", frozenset({"text"})) is None

    def test_no_caps_key(self):
        """Sticky senza need (text pura) usa chiave _text."""
        r = _mk_router()
        r.policy.deployment_sticky_per_capability = True
        r.dep_cap_sticky_set("s1", None, "dep-1")
        assert r.dep_cap_sticky_get("s1", None) == "dep-1"
