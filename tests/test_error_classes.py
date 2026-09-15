"""F17/F18/F19: Retry-After anche dal BODY JSON, cooldown e pene separate per
CLASSE d'errore (429 quota / 5xx transitorio / 401 key), jitter DETERMINISTICO
anti-herd. F20/F21: estimator image-aware nei call-site senza router e stall
guard dello stream calibrato sul TTFT di bucket."""
import os
import tempfile
import time

import pytest

import app.forwarder as F
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-ONE,text
t@x,m-b,groq,https://api.groq.com/openai/v1,free,64,8000,0,K-ONE,text
t@x,m-c,nvidia,https://api.nvidia.com/v1,free,64,8000,0,K-TWO,text
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    r = Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
               Policy.from_dict({}))
    yield r
    os.unlink(path)


def _u(r, model):
    return next(d["unique"] for deps in r.config.groups.values()
                for d in deps if d.get("model") == model)


def _cd(r, u):
    return r._cooldown.get(u, 0.0) - time.time()


class _Resp:
    """Minimo sindacale di httpx.Response per i parser header/body."""

    def __init__(self, headers=None):
        self.headers = headers or {}


# ------------------------------------------------------------------- F17
@pytest.mark.parametrize("body,want", [
    ('{"error":{"code":429,"message":"rate limited","retry_after":20}}', 20.0),
    ('{"retryAfter":"23s"}', 23.0),
    ('{"retryDelay":"58s"}', 58.0),
    ('{"error":{"details":[{"retryDelay":{"seconds":58}}]}}', 58.0),
    ('{"error":{"message":"Retry in 58.93s"}}', 58.93),
    ('{"error":{"message":"Please retry after 30s"}}', 30.0),
    ('{"error":{"message":"Please try again in 45s"}}', 45.0),
])
def test_retry_after_body_patterns(body, want):
    assert F._retry_after_from(_Resp(), body, "groq") == pytest.approx(want)


def test_retry_after_header_wins_over_body():
    r = _Resp({"retry-after": "40"})
    assert F._retry_after_from(r, '{"retry_after": 20}', "groq") == 40.0


def test_retry_after_body_absent_low_retry_gets_floor():
    # 2s nel body: il floor anti-loop (10s) alza
    assert F._retry_after_from(_Resp(), '{"retry_after": 2}', "groq") == 10.0


# ------------------------------------------------------------------- F18
def test_429_quota_no_reputation_penalty(router):
    u = _u(router, "m-a")
    k0, p0 = dict(router._key_scores), dict(router._provider_scores)
    b0 = router._base_scores.get(u, 0.0)
    router.mark_failed(u, seconds=45, reason="http_429", status=429)
    assert router._key_scores == k0
    assert router._provider_scores == p0
    assert router._base_scores.get(u, 0.0) == b0
    # ...ma il soft per-chiave F7 scatta comunque (tutte le twin)
    assert router._key_soft
    assert router.is_slow_for_session(u, "S", 40000) is True


def test_503_transient_short_cooldown_key_intact(router):
    u = _u(router, "m-a")
    k0, p0 = dict(router._key_scores), dict(router._provider_scores)
    # DERIVATO (nessun `seconds` esplicito): la classe lo accorcia
    router.mark_failed(u, reason="http_503", status=503)
    assert _cd(router, u) <= 15 + 2.5          # cooldown_transient_sec
    assert router._key_scores == k0 and router._provider_scores == p0


def test_500_transient_light_deployment_penalty(router):
    u = _u(router, "m-a")
    k0 = dict(router._key_scores)
    b0 = router._base_scores.get(u, 0.0)
    router.mark_failed(u, reason="http_500", status=500)
    assert _cd(router, u) <= 15 + 2.5
    assert router._key_scores == k0            # chiave intatta
    from app.constants import SCORING_WEIGHTS as _SW
    assert router._base_scores.get(u, 0.0) == pytest.approx(
        b0 + _SW["FAIL_TRANSIENT"])            # penale lieve solo dep


def test_timeout_capped_short(router):
    u = _u(router, "m-a")
    router.mark_failed(u, reason="timeout")   # DERIVATO
    assert _cd(router, u) <= 60 + 2.5          # cooldown_timeout_sec


def test_key_class_401_penalizes_key(router):
    u = _u(router, "m-a")
    k0 = dict(router._key_scores)
    router.mark_failed(u, seconds=30, reason="http_401", status=401)
    assert router._key_scores != k0


def test_error_class_cooldowns_off_restores_legacy(router):
    u = _u(router, "m-a")
    router.policy.error_class_cooldowns = False
    router.mark_failed(u, seconds=600, reason="timeout")
    assert _cd(router, u) > 100                # moltiplicatore x10 legacy
    router.policy.error_class_cooldowns = True


def test_chronic_floor_still_applies(router):
    u = _u(router, "m-a")
    router.mark_failed(u, seconds=15, reason="http_503", status=503)
    s = router.stats_for(u)
    s.fail_count_24h = 50                      # oltre la soglia cronica
    router.mark_failed(u, seconds=15, reason="http_503", status=503)
    assert _cd(router, u) > 600


def test_error_class_mapping():
    assert Router._error_class(429, "http_429") == "quota"
    assert Router._error_class(-429, None) == "quota"
    assert Router._error_class(-402, "http_402") == "key"
    assert Router._error_class(0, "timeout") == "transient"
    assert Router._error_class(-500, "http_500") == "transient"
    assert Router._error_class(-400, "other_4xx") == "generic"


# ------------------------------------------------------------------- F19
def test_jitter_deterministic_bounded(router):
    a = router._jitter_spread("u1")
    assert a == router._jitter_spread("u1")    # stabile tra chiamate
    assert 0.0 <= a <= 2.0
    assert router._jitter_spread("u2") != a    # spread per-unique


def test_jitter_off(router):
    router.policy.cooldown_jitter_sec_max = 0.0
    assert router._jitter_spread("u1") == 0.0


def test_jitter_applied_to_cooldown(router):
    u = _u(router, "m-a")
    router.policy.cooldown_jitter_sec_max = 2.0
    router.mark_failed(u, seconds=15, reason="http_503", status=503)
    # 15s + spread deterministico (0..2) - il puro 15.0 non e' garantito
    assert 15.0 <= _cd(router, u) <= 17.5


# ------------------------------------------------------------------- F20
@pytest.fixture()
def est_defaults():
    F.set_estimate_defaults(4, 0)
    yield F
    F.set_estimate_defaults(4, 0)


def _img_body(n_img: int) -> dict:
    content = [{"type": "text", "text": "a" * 4000}]
    for i in range(n_img):
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,AAA{i}"}})
    return {"messages": [{"role": "user", "content": content}],
            "max_tokens": 100000}


def test_clamp_counts_every_image(est_defaults):
    dep = {"unique": "d", "max_input_tokens": 20000}
    F.set_estimate_defaults(4, 0)
    b0 = _img_body(2)
    F.clamp_max_tokens(b0, dep)
    mt_noimg = b0["max_tokens"]
    F.set_estimate_defaults(4, 800)
    b1 = _img_body(2)
    F.clamp_max_tokens(b1, dep)
    assert mt_noimg - b1["max_tokens"] == 1600   # 2 immagini x 800


def test_clamp_image_aware_but_single_when_zero(est_defaults):
    dep = {"unique": "d", "max_input_tokens": 20000}
    F.set_estimate_defaults(4, 800)
    b = _img_body(0)
    F.clamp_max_tokens(b, dep)
    assert b["max_tokens"] > 0


# ------------------------------------------------------------------- F21
@pytest.fixture()
def stall_env():
    F.set_stream_stall_sec(8.0)
    F.set_ttft_lookup(None)
    F.set_stall_bucket(multiplier=2.5, max_sec=60.0)
    yield F
    F.set_ttft_lookup(None)
    F.set_stream_stall_sec(20.0)
    F.set_stall_bucket(multiplier=2.5, max_sec=60.0)


def test_stall_base_without_lookup(stall_env):
    assert F._stall_sec_for("u", 200000) == 8.0


def test_stall_scales_with_ttft_bucket(stall_env):
    F.set_ttft_lookup(lambda u, ctx=None: 5000.0)
    assert F._stall_sec_for("u", 200000) == 12.5


def test_stall_capped_by_max(stall_env):
    F.set_ttft_lookup(lambda u, ctx=None: 100000.0)
    assert F._stall_sec_for("u", 200000) == 60.0


def test_stall_lookup_errors_fall_back(stall_env):
    def _boom(u, ctx=None):
        raise RuntimeError("no data")
    F.set_ttft_lookup(_boom)
    assert F._stall_sec_for("u", 200000) == 8.0
    F.set_ttft_lookup(lambda u, ctx=None: 0.0)
    assert F._stall_sec_for("u", 200000) == 8.0


def test_stall_multiplier_off(stall_env):
    F.set_ttft_lookup(lambda u, ctx=None: 5000.0)
    F.set_stall_bucket(multiplier=0.0)
    assert F._stall_sec_for("u", 200000) == 8.0
