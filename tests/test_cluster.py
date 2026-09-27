"""Multi-worker: replica dello stato GLOBALE del router (app/cluster.py).

Due Router reali costruiti dallo stesso CSV (come due worker): le
osservazioni eseguite su A vengono pubblicate, serializzate in JSON e
rieseguite su B. Lo stato globale (stats, cooldown, lease, ...) deve
risultare identico; log e metriche della replica restano spenti.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import pytest

from app import cluster, metrics
from app.admission import limits_from_policy
from app.cluster import BusClient, BusHub, Replicator, Unencodable, decode, encode
from app.config import GatewayConfig
from app.keyhealth import KeyHealth
from app.policy import Policy
from app.router import Router

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t,model-a,groq,https://a.test/v1,free,32,32000,5,K-A,
t,model-b,openrouter,https://b.test/v1,free,32,32000,5,K-B,
t,model-c,cerebras,https://c.test/v1,free,32,32000,5,K-C,
"""


def _router(path, **pol) -> Router:
    return Router(GatewayConfig(str(path), proxy_prefix="scrocco-llm-", seed=1), Policy.from_dict(pol))


@pytest.fixture()
def pair(tmp_path):
    """(A, B, rep_A, rep_B, inbox): cio' che A pubblica arriva a B via JSON."""
    path = tmp_path / "keys.csv"
    path.write_text(CSV)
    pol = {"key_concurrency_enabled": True, "key_concurrency_max": 4}
    a, b = _router(path, **pol), _router(path, **pol)
    inbox: list[dict] = []
    rep_b = Replicator(b, None, send=lambda m: None)

    def deliver(msg):
        msg["o"] = "wA:1"
        wire = json.loads(json.dumps(msg))
        inbox.append(wire)
        rep_b.handle(wire)

    rep_a = Replicator(a, None, send=deliver)
    rep_a.install()
    yield a, b, rep_a, rep_b, inbox
    rep_a.uninstall()


def _uniques(r: Router) -> list[str]:
    return sorted(d["unique"] for deps in r.config.groups.values() for d in deps)


def _frozen_clock(monkeypatch, t0=1_900_000_000.0):
    monkeypatch.setattr(time, "time", lambda: t0)


# --------------------------------------------------------------- codec --
def test_codec_roundtrip_and_no_credentials_on_the_wire(tmp_path):
    path = tmp_path / "k.csv"
    path.write_text(CSV)
    r = _router(path)
    dep = r.config.deployment_by_unique(_uniques(r)[0])
    value = {"dep": dep, "t": (1, "x"), "s": {"a"}, "l": [1.5, None, {"k": True}]}
    wire = json.dumps(encode(value))
    assert "K-A" not in wire and "api_key" not in wire
    back = decode(json.loads(wire), r.config.deployment_by_unique)
    assert back["dep"] is dep
    assert back["t"] == (1, "x") and back["s"] == {"a"} and back["l"] == [1.5, None, {"k": True}]


def test_codec_rejects_unknown_objects_and_missing_deps():
    with pytest.raises(Unencodable):
        encode(object())
    with pytest.raises(Unencodable):
        encode({1: "chiave non stringa"})
    with pytest.raises(LookupError):
        decode({"__dep__": "sconosciuto"}, lambda u: None)


# --------------------------------------------------------- replicazione --
def test_global_state_converges(pair, monkeypatch):
    a, b, _ra, _rb, inbox = pair
    _frozen_clock(monkeypatch)
    u0, u1, u2 = _uniques(a)
    a.note_start(u0, ctx_est=1200)
    a.note_result(u0, 420.0, quality=0.9, ctx_est=1200)
    a.note_stream_end(u0, 900.0, ctx_est=1200, completion_tokens=50)
    a.note_output_tokens(u0, 50)
    a.note_end(u0, ctx_est=1200)
    a.mark_failed(u1, reason="http_429", status=429)
    a.note_rate_limit(u2, {"remaining_requests": 1, "limit_requests": 30})
    a.note_estimate_error(u0, 1000, 1300)
    a.note_cap_strike("model-c", ["vision"], "no image input")
    a.record_escalation_win("scrocco-llm-test-32k", a.config.deployment_by_unique(u2))
    a.note_json_fallback(u2)
    assert inbox, "nessuna osservazione pubblicata"
    assert a.dump_stats() == b.dump_stats()
    assert a.save_cooldowns() == b.save_cooldowns()
    for u in (u0, u1, u2):
        sa, sb = a.stats_for(u), b.stats_for(u)
        assert (sa.inflight, sa.inflight_tokens, sa.minute_calls, sa.day_calls, sa.json_fallback) == \
               (sb.inflight, sb.inflight_tokens, sb.minute_calls, sb.day_calls, sb.json_fallback)


def test_operator_commands_replicate(pair):
    a, b, *_ = pair
    u0, u1, _u2 = _uniques(a)
    a.mark_failed(u0, reason="http_500", status=500)
    a.mark_failed(u1, reason="http_500", status=500)
    assert b.is_cooled_down(u0) and b.is_cooled_down(u1)
    assert a.drop_cooldown(u0) is True
    assert not b.is_cooled_down(u0)
    assert a.drop_all_cooldowns() == [u1]
    assert not b.is_cooled_down(u1)
    a.bump_probe_fail_streak(u1)
    assert b.stats_for(u1).probe_fail_streak == 1
    a.reset_for_unretire(u1)
    assert b.stats_for(u1).fail_streak == 0 and b.stats_for(u1).success_ema is None
    a.set_cooldown_until(u0, time.time() + 300, time.time())
    assert a.save_cooldowns() == b.save_cooldowns()


def test_nested_calls_are_published_once(pair):
    a, _b, _ra, _rb, inbox = pair
    u0 = _uniques(a)[0]
    a.note_start(u0)                      # internamente chiama note_usage
    assert [m["m"] for m in inbox] == ["note_start"]


def test_cooldown_copied_verbatim_even_with_random_jitter(pair):
    a, b, *_ = pair
    a.policy = Policy.from_dict({"cooldown_jitter_ratio": 0.5})
    b.policy = Policy.from_dict({"cooldown_jitter_ratio": 0.5})
    u1 = _uniques(a)[1]
    a.mark_failed(u1, seconds=120, reason="http_429", status=429)
    assert a._cooldown[u1] == b._cooldown[u1]
    assert a._cooldown_since[u1] == b._cooldown_since[u1]


def test_key_leases_replicate_and_release(pair):
    a, b, *_ = pair
    dep = a.config.deployment_by_unique(_uniques(a)[0])
    lease = a.key_lease_acquire(dep)
    assert lease and b.key_inflight(b.config.deployment_by_unique(dep["unique"])) == 1
    a.key_lease_release(lease)
    assert a.key_inflight(dep) == 0 and b.key_inflight(b.config.deployment_by_unique(dep["unique"])) == 0


def test_peer_down_drops_its_inflight_and_leases(pair):
    a, b, _ra, rep_b, _ = pair
    u0 = _uniques(a)[0]
    a.note_start(u0, ctx_est=500)
    a.note_start(u0, ctx_est=500)
    a.key_lease_acquire(a.config.deployment_by_unique(u0))
    assert b.stats_for(u0).inflight == 2 and b.stats_for(u0).inflight_tokens == 1000
    rep_b.handle({"t": "down", "o": "wA:1"})     # il processo A e' morto
    assert b.stats_for(u0).inflight == 0 and b.stats_for(u0).inflight_tokens == 0
    assert b.key_inflight(b.config.deployment_by_unique(u0)) == 0


def test_local_inflight_counts_only_own_requests(pair):
    a, _b, rep_a, rep_b, _ = pair
    u0 = _uniques(a)[0]
    a.note_start(u0)
    a.note_start(u0)
    a.note_end(u0)
    assert rep_a.local_inflight() == 1
    assert rep_b.local_inflight() == 0          # le repliche non sono "sue"


def test_replay_is_silent_logs_and_metrics(tmp_path, caplog):
    path = tmp_path / "k.csv"
    path.write_text(CSV)
    b = _router(path)
    rep_b = Replicator(b, None, send=lambda m: None)
    u = _uniques(b)[0]
    metrics.reset()
    caplog.handler.addFilter(cluster._LOG_FILTER)
    try:
        with caplog.at_level(logging.DEBUG):
            rep_b.handle({"o": "wA:1", "k": "r", "m": "mark_failed",
                          "a": [u], "kw": {"reason": "http_429", "status": 429}})
    finally:
        caplog.handler.removeFilter(cluster._LOG_FILTER)
    assert b.is_cooled_down(u)
    assert not [r for r in caplog.records if r.name.startswith("nx.")]
    assert rep_b.replayed == 1 and rep_b.errors == 0


def test_replay_ignores_unknown_methods_and_missing_deps(tmp_path):
    path = tmp_path / "k.csv"
    path.write_text(CSV)
    b = _router(path)
    rep = Replicator(b, None, send=lambda m: None)
    rep.handle({"o": "x", "k": "r", "m": "pick_deployment", "a": [], "kw": {}})
    rep.handle({"o": "x", "k": "r", "m": "__init__", "a": [], "kw": {}})
    rep.handle({"o": "x", "k": "r", "m": "record_escalation_win",
                "a": ["g", {"__dep__": "non-esiste"}], "kw": {}})
    assert rep.replayed == 0 and rep.skipped == 1


def test_metrics_muted_context():
    metrics.reset()
    with metrics.muted():
        metrics.inc("nx_test_muted_total")
        metrics.set_gauge("nx_test_muted_gauge", 3)
    metrics.inc("nx_test_muted_total")
    snap = metrics.snapshot(("nx_test_muted_total",))
    assert snap["nx_test_muted_total"][()] == 1.0


# ------------------------------------------------------------ topologia --
def test_single_process_by_default(monkeypatch):
    monkeypatch.delenv(cluster.ENV_INDEX, raising=False)
    monkeypatch.setenv(cluster.ENV_SIZE, "8")        # senza indice: ignorato
    assert cluster.size() == 1 and not cluster.enabled() and cluster.is_leader()
    from pathlib import Path
    assert cluster.per_worker_path(Path("/v/routing_state.json")) == Path("/v/routing_state.json")


def test_cluster_topology(monkeypatch):
    monkeypatch.setenv(cluster.ENV_SIZE, "4")
    monkeypatch.setenv(cluster.ENV_INDEX, "2")
    from pathlib import Path
    assert cluster.enabled() and cluster.size() == 4 and cluster.index() == 2
    assert not cluster.is_leader()
    assert cluster.per_worker_path(Path("/v/routing_state.json")) == Path("/v/routing_state.w2.json")
    owners = {cluster.owner_of(f"ses_{i}") for i in range(200)}
    assert owners == {0, 1, 2, 3}
    assert cluster.owner_of("ses_x") == cluster.owner_of("ses_x", 4)


def test_start_is_a_noop_with_one_worker(tmp_path, monkeypatch):
    monkeypatch.delenv(cluster.ENV_INDEX, raising=False)
    path = tmp_path / "k.csv"
    path.write_text(CSV)
    r = _router(path)
    asyncio.run(cluster.start(r, None))
    assert "mark_failed" not in vars(r)            # nessun metodo avvolto
    assert cluster.local_inflight(r) == r.inflight_total()


def test_admission_limits_are_split_between_workers():
    pol = Policy.from_dict({"admission_max_inflight": 128, "admission_max_streams": 48})
    one, three = limits_from_policy(pol, workers=1), limits_from_policy(pol, workers=3)
    assert (one.max_inflight, one.max_streams) == (128, 48)
    assert (three.max_inflight, three.max_streams) == (43, 16)
    zero = limits_from_policy(Policy.from_dict({"admission_max_inflight": 0}), workers=4)
    assert zero.max_inflight == 0


# ------------------------------------------------------------ keyhealth --
def test_keyhealth_file_written_only_by_leader(tmp_path, monkeypatch):
    monkeypatch.setenv(cluster.ENV_SIZE, "2")
    monkeypatch.setenv(cluster.ENV_INDEX, "1")
    follower = KeyHealth(str(tmp_path))
    follower.set_state("u1", "retired", reason="test")
    follower.save()
    assert not os.path.exists(follower.path)
    monkeypatch.setenv(cluster.ENV_INDEX, "0")
    leader = KeyHealth(str(tmp_path))
    leader.set_state("u2", "retired", reason="test")
    leader.save()
    assert os.path.exists(leader.path)
    monkeypatch.setenv(cluster.ENV_INDEX, "1")
    assert follower.reload_if_changed() is True
    assert follower.is_retired("u2") and not follower.reload_if_changed()


def test_keyhealth_mutations_replicate(tmp_path):
    path = tmp_path / "k.csv"
    path.write_text(CSV)
    ra, rb = _router(path), _router(path)
    kha, khb = KeyHealth(str(tmp_path / "a")), KeyHealth(str(tmp_path / "b"))
    rep_b = Replicator(rb, khb, send=lambda m: None)
    rep_a = Replicator(ra, kha, send=lambda m: rep_b.handle(json.loads(json.dumps({**m, "o": "a"}))))
    rep_a.install()
    try:
        kha.set_state("u9", "retired", reason="insufficient_balance")
        assert khb.is_retired("u9")
        kha.clear("u9")
        assert not khb.is_retired("u9")
    finally:
        rep_a.uninstall()


# ------------------------------------------------------------------ bus --
def test_bus_fans_out_to_others_and_reports_down(tmp_path):
    async def main():
        sock = str(tmp_path / "bus.sock")
        hub = BusHub(sock)
        await hub.start()
        got: dict[str, list] = {"a": [], "b": [], "c": []}
        clients = {n: BusClient(sock, f"{n}:1", i, got[n].append) for i, n in enumerate("abc")}
        for c in clients.values():
            await c.start()
        await asyncio.sleep(0.05)
        clients["a"].send({"k": "r", "m": "note_end", "a": ["u"], "kw": {}})
        await asyncio.sleep(0.05)
        assert got["a"] == []
        assert [m["m"] for m in got["b"]] == ["note_end"] == [m["m"] for m in got["c"]]
        assert got["b"][0]["o"] == "a:1"
        await clients["a"].stop()
        await asyncio.sleep(0.05)
        assert {"t": "down", "o": "a:1"} in got["b"]
        for n in "bc":
            await clients[n].stop()
        await hub.stop()

    asyncio.run(main())


# ------------------------------------------------------ config condivisa --
def test_cluster_seed_gives_every_worker_the_same_uniques(tmp_path, monkeypatch):
    """Gli unique (`gruppo__modello__<indice>`) nascono da un mescolamento:
    con il seme di cluster tutti i worker ottengono la STESSA mappa
    unique -> chiave, indipendentemente dallo stato del loro `random`."""
    import random

    rows = "".join(f"r{i},model-a,groq,https://a.test/v1,free,32,32000,5,K-{i},\n" for i in range(12))
    path = tmp_path / "k.csv"
    path.write_text(CSV.splitlines()[0] + "\n" + rows)

    def mapping() -> dict:
        cfg = GatewayConfig(str(path), proxy_prefix="scrocco-llm-")
        return {d["unique"]: d["api_key"] for deps in cfg.groups.values() for d in deps}

    monkeypatch.setenv(cluster.ENV_SEED, "abc123")
    random.seed(1)
    first = mapping()
    random.seed(99)
    assert mapping() == first
    monkeypatch.setenv(cluster.ENV_SEED, "altro-avvio")
    assert mapping() != first          # nuovo cluster: nuovo mescolamento


def test_per_worker_state_files_start_from_the_single_process_file(tmp_path, monkeypatch):
    from app.runtime_persistence import _own_or_shared

    base = tmp_path / "routing_state.json"
    base.write_text("{}")
    monkeypatch.setenv(cluster.ENV_SIZE, "2")
    monkeypatch.setenv(cluster.ENV_INDEX, "1")
    own = cluster.per_worker_path(base)
    assert own.name == "routing_state.w1.json"
    assert _own_or_shared(own) == base            # primo avvio multi-worker
    own.write_text("{}")
    assert _own_or_shared(own) == own
