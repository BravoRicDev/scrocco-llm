"""Scala testo RESILIENTE: prima di arrendersi (notice) la rotazione fa
escalation graduale del rilassamento cooldown.

  1) free+go non in cooldown
  2) free+go con cooldown STANTIO (> stale_cooldown_retry_sec)
  3) -fallback (a pagamento) non in cooldown
  4) ULTIMA SPIAGGIA: qualsiasi rung, cooldown ignorato
"""
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x,m/a1000,groq,https://api.groq.com/openai/v1,free,1000,8000,5,K-A,
t@x,m/b1000,groq,https://api.groq.com/openai/v1,free,1000,8000,5,K-B,
t@x,m/g1,groq-go,https://api.groq.com/openai/v1,,0,0,5,K-GO,
t@x,m/f1,groq,https://api.groq.com/openai/v1,paid,0,0,5,K-FB,
"""
BASE = "scrocco-llm-test"


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    pol.stale_cooldown_retry_sec = 300
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def _dep(r, group, key):
    return next(d for d in r.config.groups[group] if d.get("api_key") == key)


def _uniques(r, group):
    return [d["unique"] for d in r.config.groups[group]]


def test_tier1_prefers_non_cooled_free(router):
    a = _dep(router, f"{BASE}-1000k", "K-A")
    nxt = router.fallback_next("test", a, None, "group", ctx=1000)
    assert nxt["api_key"] == "K-B"          # l'altro 1000k, non cotto


def test_tier3_escalates_to_paid_fallback(router):
    # tutti i free+go in cooldown FRESCO (< soglia stantio)
    for u in _uniques(router, f"{BASE}-1000k") + _uniques(router, f"{BASE}-go"):
        router.mark_failed(u, seconds=600)
    a = _dep(router, f"{BASE}-1000k", "K-A")
    nxt = router.fallback_next("test", a, None, "group", ctx=1000)
    assert nxt["group"] == f"{BASE}-fallback"   # salta al tier a pagamento


def test_tier2_retries_stale_free_before_paid(router):
    b = _dep(router, f"{BASE}-1000k", "K-B")
    go = _dep(router, f"{BASE}-go", "K-GO")
    fb = _dep(router, f"{BASE}-fallback", "K-FB")
    # B in cooldown STANTIO (messo 10 min fa), go e fallback freschi
    router.mark_failed(b["unique"], seconds=600)
    router._cooldown_since[b["unique"]] = time.time() - 600      # 10 min fa
    router.mark_failed(go["unique"], seconds=600)
    router.mark_failed(fb["unique"], seconds=600)
    a = _dep(router, f"{BASE}-1000k", "K-A")
    nxt = router.fallback_next("test", a, None, "group", ctx=1000)
    assert nxt["api_key"] == "K-B"          # stantio: ri-provato PRIMA del paid


def test_tier4_last_resort_ignores_cooldown(router):
    # TUTTO in cooldown fresco: nessuna alternativa "pulita"
    for g in (f"{BASE}-1000k", f"{BASE}-go", f"{BASE}-fallback"):
        for u in _uniques(router, g):
            router.mark_failed(u, seconds=600)
    a = _dep(router, f"{BASE}-1000k", "K-A")
    nxt = router.fallback_next("test", a, None, "group", ctx=1000)
    assert nxt is not None                  # NON si arrende: ultima spiaggia
    assert nxt["api_key"] in ("K-B", "K-GO", "K-FB")


def test_returns_none_only_when_truly_nothing(router):
    # un solo deployment nel mondo, ed e' quello fallito -> davvero niente
    only = _dep(router, f"{BASE}-1000k", "K-A")
    for g in (f"{BASE}-1000k", f"{BASE}-go", f"{BASE}-fallback"):
        for u in _uniques(router, g):
            router.mark_failed(u, seconds=600)
    # walk a chain che contiene SOLO il fallito
    assert router._walk_ladder_resilient([only["unique"]], only["unique"],
                                         None, None) is None
