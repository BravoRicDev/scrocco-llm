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


# --------------------------------------------------------------------------
# "skip after N" REALE: dopo ladder_skip_after key fallite dello STESSO gruppo
# nella richiesta, il resto del gruppo si salta e la scala sale di dim (invece
# di rovistare decine di key free finché scade stream_total_deadline_ms).
CSV_BIGPOOL = (
    "commento,modello,provider,endpoint,data,context,max_input,priority,"
    "scrocco-llm-test,caps\n"
    + "".join(
        f"t,m/s{i},groq,https://api.groq.com/openai/v1,free,200,8000,5,K-S{i},\n"
        for i in range(6)
    )
    + "t,m/big,groq,https://api.groq.com/openai/v1,free,1000,8000,5,K-BIG,\n"
)


def _router_bigpool():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_BIGPOOL)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    os.unlink(path)
    return Router(cfg, pol)


def test_skip_after_n_climbs_dim_instead_of_grinding_pool():
    r = _router_bigpool()
    small = _uniques(r, f"{BASE}-200k")
    big = _uniques(r, f"{BASE}-1000k")
    assert len(small) == 6 and len(big) == 1
    skip = int(r.policy.ladder_skip_after or 4)

    # meno di `skip` key -200k fallite: la scala resta nel pool -200k
    tried = set(small[:skip - 1])
    nxt = r._walk_chain(small + big, small[skip - 2], tried=tried, limit=skip)
    assert nxt["group"] == f"{BASE}-200k"

    # raggiunto `skip`: il gruppo -200k è "esaurito" per questa richiesta ->
    # si sale al -1000k anche se restano key -200k mai provate
    tried = set(small[:skip])
    nxt = r._walk_chain(small + big, small[skip - 1], tried=tried, limit=skip)
    assert nxt is not None and nxt["group"] == f"{BASE}-1000k"


def test_skip_after_n_not_applied_on_stale_retry_walk():
    """Il gate vale SOLO sul walk vivo (step 1): lo step di riesumazione
    cooldown (min_cooldown_age) deve ancora poter riprovare le key untried di
    un gruppo già battuto, prima di sforare sul -fallback a pagamento."""
    r = _router_bigpool()
    small = _uniques(r, f"{BASE}-200k")
    skip = int(r.policy.ladder_skip_after or 4)
    tried = set(small[:skip])
    # una key -200k mai provata, in cooldown STANTIO
    victim = small[skip]
    r.mark_failed(victim, seconds=600)
    r._cooldown_since[victim] = time.time() - 9999
    nxt = r._walk_chain(small, small[skip - 1], tried=tried,
                        min_cooldown_age=300, limit=3)
    assert nxt is not None and nxt["unique"] == victim
