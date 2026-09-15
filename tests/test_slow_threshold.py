"""Soglia "lento" size-aware (B3 ibrida) + misura token/s + backoff caccia."""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router, _ctx_bucket, set_current_session

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m-a,groq,https://api.groq.com/openai/v1,free,64,64000,0,K-A,text
t@x,m-b,groq,https://api.groq.com/openai/v1,free,200,200000,0,K-B,text
t@x,m-c,groq,https://api.groq.com/openai/v1,free,64,64000,0,K-C,text
t@x,m-d,groq,https://api.groq.com/openai/v1,free,200,200000,0,K-D,text
t@x,m-e,groq,https://api.groq.com/openai/v1,free,200,200000,0,K-E,text
t@x,m-h,groq,https://api.groq.com/openai/v1,free,64,64000,0,K-H,text
t@x,m-i,groq,https://api.groq.com/openai/v1,free,64,64000,0,K-I,text
t@x,m-g,groq,https://api.groq.com/openai/v1,,64,64000,0,K-G,text
t@x,m-f,groq,https://api.groq.com/openai/v1,fallback,64,64000,0,K-F,text
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    set_current_session("S1")
    yield r
    os.unlink(path)


def _seed_peers(r, bucket, value, n=5):
    """Semina la mediana di flotta: n dep con EMA `value` nel bucket."""
    us = [d["unique"] for g in r.config.groups.values() for d in g]
    for i, u in enumerate(us[:n]):
        row = [0.0, 0.0, 0.0, 0.0]
        row[bucket] = float(value)
        r._lat_buckets[u] = row


def test_lento_size_aware_su_128k(router):
    r = router
    b3 = _ctx_bucket(200000)
    _seed_peers(r, b3, 100000.0)
    u = r.config.groups["scrocco-llm-test-200k"][0]["unique"]
    r._lat_buckets[u] = [0.0, 0.0, 0.0, 100000.0]
    assert r._is_slow_dep(u, 200000) is False        # 100k su 128k = normale
    r._lat_buckets[u] = [0.0, 0.0, 0.0, 250000.0]
    assert r._is_slow_dep(u, 200000) is True         # 2.5x la flotta = lento


def test_lento_su_contesto_piccolo(router):
    r = router
    b1 = _ctx_bucket(16000)
    _seed_peers(r, b1, 5000.0)
    u = r.config.groups["scrocco-llm-test-64k"][0]["unique"]
    r._lat_buckets[u] = [0.0, 50000.0, 0.0, 0.0]
    assert r._is_slow_dep(u, 16000) is True          # 50s su 16k e' lento


def test_senza_flotta_erpita_legacy_e_rate(router):
    """Senza flotta: niente baseline propria (mai 2x se stessi, sarebbe
    irraggiungibile). Con rate di prefill si stima; senza rate si ripiega
    sui 90s legacy."""
    r = router
    u = r.config.groups["scrocco-llm-test-200k"][0]["unique"]
    r._lat_buckets[u] = [0.0, 0.0, 0.0, 120000.0]
    r._fleet_cache = {}
    assert r._is_slow_dep(u, 200000) is True         # legacy 90s
    r._prefill_rate = {u: 50.0}                      # ms/1k: 128k -> 6.4s ttft
    r._gen_rate = {u: 20.0}                          # 20 tok/s
    r._fleet_cache = {}
    # atteso total ~ 6400 + 600/20*1000 = 36400 -> soglia 72800
    assert r._is_slow_dep(u, 200000) is True         # 120s > 72.8s
    r._lat_buckets[u] = [0.0, 0.0, 0.0, 50000.0]
    assert r._is_slow_dep(u, 200000) is False        # 50s < soglia


def test_expected_rate_based(router):
    r = router
    u = r.config.groups["scrocco-llm-test-200k"][0]["unique"]
    r._prefill_rate[u] = 50.0                        # ms per 1k token
    r._gen_rate[u] = 20.0                            # token/s
    assert r._expected_latency_ms(u, 128000, "ttft") == pytest.approx(6400.0)
    # total: ttft + 600 token / 20 tps = 6400 + 30000
    assert r._expected_latency_ms(u, 128000, "total") == pytest.approx(36400.0)


def test_note_stream_end_alimenta_gen_rate(router):
    r = router
    u = r.config.groups["scrocco-llm-test-200k"][0]["unique"]
    r.note_stream_end(u, 10000.0, 128000, completion_tokens=100)
    assert r._gen_rate[u] == pytest.approx(10.0)     # 100 token / 10s


def test_hunt_backoff_e_cap(router):
    r = router
    assert r.hunt_allowed("S1", 128000) is True
    r.note_hunt("S1", 128000, gained=False)          # il buono non esiste
    assert r.hunt_allowed("S1", 128000) is False
    # scaduto il backoff torna permesso
    st = r._hunt_state[("S1", _ctx_bucket(128000))]
    st["backoff_until"] = time.time() - 1
    assert r.hunt_allowed("S1", 128000) is True
    # cap per finestra
    for _ in range(5):
        r.note_hunt("S1", 128000, gained=True)
    assert r.hunt_allowed("S1", 128000) is False
