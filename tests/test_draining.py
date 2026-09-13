"""CONNECTION DRAINING su hot-reload: i deployment rimossi dal CSV con
richieste in volo restano marcati draining (ignorati da pick_deployment)
finche' l'inflight non torna a zero o scade il TTL; poi rimossi anche dalla
config. Zero richieste orfane: riferimenti/retry dello stesso ciclo restano
validi perche' il dep draining resta raggiungibile in config.groups."""
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,model_preference
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text,100
t@x.com,m-b,deepinfra,https://api.deepinfra.com/v1/openai,free,32,8000,0,K-B,text,50
t@x.com,m-c,together,https://api.together.xyz/v1,free,32,8000,0,K-C,text,-50
"""
GRP = "scrocco-llm-test-32k"


@pytest.fixture()
def router():
    import os
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    r.policy.adaptive_pick = True
    yield r
    os.unlink(path)


def _dep(router, model):
    return next(d for d in router.config.groups[GRP] if d["model"] == model)


def test_start_draining_marks_and_readds_to_config(router):
    a = _dep(router, "m-a")
    # simula hot-reload: m-a tolto dalla config, ma con richieste in volo
    router.config.groups[GRP] = [
        d for d in router.config.groups[GRP] if d["unique"] != a["unique"]]
    assert router.config.deployment_by_unique(a["unique"]) is None
    router.start_draining(a["unique"], a, inflight=2)
    # il dep draining torna raggiungibile in config (riferimenti/retry validi)
    assert router.is_draining(a["unique"])
    assert router.config.deployment_by_unique(a["unique"]) is not None


def test_pick_skips_draining(router):
    a, b = _dep(router, "m-a"), _dep(router, "m-b")
    router.start_draining(a["unique"], a, inflight=2)
    picks = {router.pick_deployment(GRP, need=frozenset({"text"}))["model"]
             for _ in range(60)}
    assert "m-a" not in picks
    assert picks <= {"m-b", "m-c"}
    assert "m-b" in picks or "m-c" in picks


def test_note_end_drains_counter_and_finishes(router):
    a = _dep(router, "m-a")
    router.stats_for(a["unique"]).inflight = 2
    router.start_draining(a["unique"], a, inflight=2)
    router.note_end(a["unique"])
    assert router.is_draining(a["unique"])          # ancora 1 inflight
    router.note_end(a["unique"])
    assert not router.is_draining(a["unique"])      # 0 -> rimosso
    assert router.config.deployment_by_unique(a["unique"]) is None


def test_ttl_purge_forces_removal(router):
    a = _dep(router, "m-a")
    router.stats_for(a["unique"]).inflight = 5
    router.policy.hotreload_drain_ttl_sec = 1.0
    router.start_draining(a["unique"], a, inflight=5)
    router._draining[a["unique"]]["ts"] = time.time() - 10  # gia' scaduto
    assert router.purge_draining() == 1
    assert not router.is_draining(a["unique"])
    assert router.config.deployment_by_unique(a["unique"]) is None


def test_no_dup_when_start_draining_already_present(router):
    a = _dep(router, "m-a")
    n = len(router.config.groups[GRP])
    router.start_draining(a["unique"], a, inflight=1)
    router.start_draining(a["unique"], a, inflight=1)  # secondo reload
    assert len(router.config.groups[GRP]) == n         # nessun duplicato