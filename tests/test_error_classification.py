"""Error classification centralizzata: strategia di recupero per categoria."""
import os
import tempfile
import time

import pytest

import app.main as M
from app.config import GatewayConfig
from app.forwarder import classify_error
from app.policy import Policy
from app.router import ErrorKind, Router

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
    r.policy.cooldown_jitter_ratio = 0.0
    yield r
    os.unlink(path)


def _dep(router):
    return router.config.groups[GRP][0]


class _KH:
    def __init__(self):
        self.calls = []

    def set_state(self, unique, state, reason=None):
        self.calls.append((unique, state, reason))

    def save(self):
        self.calls.append(("save",))


def test_classify_error_mapping():
    assert classify_error(401, None, "invalid api key") == ErrorKind.PERMANENT_DEAD
    assert classify_error(400, None, "The requested model is not available.") \
        == ErrorKind.PERMANENT_DEAD
    assert classify_error(500, None, "internal server error") \
        == ErrorKind.TRANSIENT
    assert classify_error(None, None, "connect timeout") == ErrorKind.TRANSIENT


def test_permanent_dead_retires_without_cooldown(router, monkeypatch):
    kh = _KH()
    monkeypatch.setattr(M, "KEYHEALTH", kh)
    u = _dep(router)["unique"]
    secs = router.mark_failed(u, reason="not_found", status=-404,
                              kind=ErrorKind.PERMANENT_DEAD)
    assert secs == 0.0
    assert not router.is_cooled_down(u)
    retired = [c for c in kh.calls if c[0] != "save"]
    assert retired and retired[0][0] == u and retired[0][1] == "retired"


def test_quota_reset_exact_seconds_no_chronic(router):
    u = _dep(router)["unique"]
    router.stats_for(u).fail_count_24h = 50      # oltre soglia "cronica"
    router.mark_failed(u, seconds=100.0, reason="quota_exhausted",
                       status=429, kind=ErrorKind.QUOTA_RESET)
    cd = router._cooldown[u] - time.time()
    assert 95.0 <= cd <= 105.0


def test_generic_4xx_sets_cooldown(router):
    u = _dep(router)["unique"]
    secs = router.mark_failed(u, seconds=120.0, reason="http_400",
                              status=400, kind=ErrorKind.GENERIC_4XX)
    assert abs(secs - 120.0) < 1e-6
    assert router.is_cooled_down(u)
