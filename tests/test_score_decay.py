"""Time-decay dei punteggi di reputazione (halflife configurabile)."""
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text
"""
GRP = "scrocco-llm-test-32k"


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    yield r
    os.unlink(path)


def test_decay_halflife(router):
    router.policy.reputation_decay_halflife_sec = 3600.0     # 1h
    router._base_scores["a"] = 10.0
    router._provider_scores["p"] = 8.0
    router._key_scores["k"] = 4.0
    router._scores_decay_ts = 1000.0
    f = router._decay_scores(now=1000.0 + 3600.0)
    assert abs(f - 0.5) < 1e-9
    assert abs(router._base_scores["a"] - 5.0) < 1e-9
    assert abs(router._provider_scores["p"] - 4.0) < 1e-9
    assert abs(router._key_scores["k"] - 2.0) < 1e-9


def test_decay_partial_window(router):
    router.policy.reputation_decay_halflife_sec = 7200.0
    router._base_scores["a"] = 1.0
    router._scores_decay_ts = 0.0
    f = router._decay_scores(now=3600.0)
    assert abs(f - 0.5 ** 0.5) < 1e-9
    assert abs(router._base_scores["a"] - 0.5 ** 0.5) < 1e-9


def test_halflife_zero_disables(router):
    router.policy.reputation_decay_halflife_sec = 0.0
    router._base_scores["a"] = 10.0
    router._scores_decay_ts = 0.0
    assert router._decay_scores(now=10 ** 6) == 1.0
    assert router._base_scores["a"] == 10.0


def test_decay_prunes_tiny(router):
    router.policy.reputation_decay_halflife_sec = 1.0
    router._base_scores["a"] = 0.001
    router._scores_decay_ts = 0.0
    router._decay_scores(now=1000.0)
    assert "a" not in router._base_scores


def test_decay_purge_expired_invokes(router):
    router.policy.reputation_decay_halflife_sec = 3600.0
    router._base_scores["a"] = 8.0
    router._scores_decay_ts = router._scores_decay_ts - 3600.0
    router.purge_expired()
    assert router._base_scores.get("a", 0.0) < 8.0


def test_parsing_reputation_decay():
    pol = Policy.from_dict({"reputation_decay_halflife_sec": 86400})
    assert pol.reputation_decay_halflife_sec == 86400.0
    pol2 = Policy.from_dict({})
    assert pol2.reputation_decay_halflife_sec == 129600.0
