"""Rifiniture multi-worker e prestazioni (batch dopo il cluster).

Ogni test fissa un comportamento "sotto il cofano": nessuno cambia cio' che
vede il client.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest

from app import cluster, metrics, offload


# ------------------------------------------------ keyhealth: scritture --
def test_keyhealth_save_async_skips_unchanged_content(tmp_path, monkeypatch):
    from app import keyhealth as kh_mod

    writes: list[str] = []
    real = kh_mod.save_json_text

    def counting(path, snap, **kw):
        writes.append(str(path))
        return real(path, snap, **kw)

    monkeypatch.setattr(kh_mod, "save_json_text", counting)
    kh = kh_mod.KeyHealth(str(tmp_path))
    kh.set_state("u1", "retired", reason="t")
    asyncio.run(kh.save_async())
    asyncio.run(kh.save_async())                  # nessun cambiamento: niente disco
    assert len(writes) == 1
    kh.set_state("u2", "retired", reason="t")
    asyncio.run(kh.save_async())
    assert len(writes) == 2


# --------------------------------------------------- body JSON off-loop --
class _Req:
    def __init__(self, body: bytes, scope: dict | None = None):
        self._body = body
        self.scope = scope or {}

    async def body(self):
        return self._body


def test_request_json_matches_starlette_semantics():
    req = _Req(b'{"a": [1, 2]}')
    assert asyncio.run(offload.request_json(req)) == {"a": [1, 2]}
    assert req._json == {"a": [1, 2]}             # cache come Request.json()
    with pytest.raises(ValueError):
        asyncio.run(offload.request_json(_Req(b"{rotto")))


def test_request_json_large_body_goes_to_a_thread(monkeypatch):
    seen: list[int] = []
    real = offload.run

    async def spy(fn, *a, size=0, **kw):
        seen.append(size)
        return await real(fn, *a, size=size, **kw)

    monkeypatch.setattr(offload, "run", spy)
    big = json.dumps({"x": "y" * (offload.OFFLOAD_MIN_BYTES + 10)}).encode()
    assert asyncio.run(offload.request_json(_Req(big)))["x"].startswith("y")
    assert seen == [len(big)]


def test_request_json_reuses_the_affinity_parse_only_for_the_same_body():
    parsed = {"model": "m"}
    req = _Req(b'{"model": "m"}', {offload.PARSED_BODY_KEY: (b'{"model": "m"}', parsed)})
    assert asyncio.run(offload.request_json(req)) is parsed
    assert offload.PARSED_BODY_KEY not in req.scope        # usato una volta sola
    other = _Req(b'{"model": "n"}', {offload.PARSED_BODY_KEY: (b'{"model": "m"}', parsed)})
    assert asyncio.run(offload.request_json(other)) == {"model": "n"}


def test_affinity_hands_the_parsed_body_to_the_local_endpoint():
    from app.affinity import AffinityProxy

    seen: dict = {}

    async def app(scope, receive, send):
        seen["pre"] = scope.get(offload.PARSED_BODY_KEY)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    proxy = AffinityProxy(app, index=0, workers=1)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://gw")
    body = {"model": "m", "user": "ses_1", "messages": []}
    asyncio.run(client.post("/v1/chat/completions", json=body))
    raw, payload = seen["pre"]
    assert payload == body and json.loads(raw) == body


# ------------------------------------------------------------ metriche --
def test_metrics_render_format_is_unchanged():
    metrics.reset()
    metrics.declare("nx_t_total", "kind")
    metrics.inc("nx_t_total", ("b",), 2)
    metrics.inc("nx_t_total", ("a",))
    metrics.set_gauge("nx_t_gauge", 7)
    metrics.observe_latency_ms("dep-x", 100)
    metrics.observe_latency_ms("dep-x", 300)
    lines = metrics.render().splitlines()
    i = lines.index("# TYPE nx_t_total counter")
    assert lines[i + 1:i + 3] == ['nx_t_total{kind="a"} 1.0', 'nx_t_total{kind="b"} 2.0']
    assert lines[lines.index("# TYPE nx_t_gauge gauge") + 1] == "nx_t_gauge 7"
    assert 'nx_upstream_latency_ms{unique="dep-x"} 200' in lines
    metrics.reset()


def test_unreachable_owner_is_logged_once_per_window(caplog):
    from app.affinity import AffinityProxy

    metrics.reset()
    proxy = AffinityProxy(lambda *a: None, index=0, workers=2)
    with caplog.at_level(logging.WARNING, logger="nx.affinity"):
        for _ in range(5):
            proxy._note_unreachable(1, OSError("refused"))
    assert len([r for r in caplog.records if "irraggiungibile" in r.getMessage()]) == 1
    assert metrics.snapshot(("nx_affinity_fallback_total",))["nx_affinity_fallback_total"][("1",)] == 5.0
    metrics.reset()


# ------------------------------------------------------------ cluster --
def test_cluster_view_single_process(monkeypatch):
    monkeypatch.delenv(cluster.ENV_INDEX, raising=False)
    snap = cluster.stats()
    assert snap["enabled"] is False and snap["workers"] == 1 and snap["leader"] is True


def test_admin_cluster_endpoint_requires_master():
    from fastapi.testclient import TestClient

    import app.main as m
    import app.state as gw_state

    orig = gw_state.authn.master_key
    gw_state.authn.master_key = "test-master-cluster"
    try:
        c = TestClient(m.app)
        assert c.get("/admin/cluster").status_code == 401
        r = c.get("/admin/cluster", headers={"Authorization": "Bearer test-master-cluster"})
        assert r.status_code == 200 and r.json()["enabled"] is False
    finally:
        gw_state.authn.master_key = orig


def test_follower_sniff_log_does_not_rotate(tmp_path, monkeypatch):
    from logging.handlers import TimedRotatingFileHandler, WatchedFileHandler

    from app import sniff

    monkeypatch.setenv(cluster.ENV_SIZE, "2")
    monkeypatch.setenv(cluster.ENV_INDEX, "1")
    lg = logging.getLogger("nx.test.sniff.follower")
    lg.handlers.clear()
    assert sniff._install(lg, str(tmp_path / "s.log"), 24)
    assert isinstance(lg.handlers[0], WatchedFileHandler)
    monkeypatch.setenv(cluster.ENV_INDEX, "0")
    lg2 = logging.getLogger("nx.test.sniff.leader")
    lg2.handlers.clear()
    assert sniff._install(lg2, str(tmp_path / "s2.log"), 24)
    assert isinstance(lg2.handlers[0], TimedRotatingFileHandler)
    for h in lg.handlers + lg2.handlers:
        h.close()
    lg.handlers.clear()
    lg2.handlers.clear()


def test_only_the_leader_sweeps_the_shared_image_dir(tmp_path, monkeypatch):
    from app import imagestore

    monkeypatch.setattr(imagestore, "_STORAGE_DIR", tmp_path)
    monkeypatch.setattr(imagestore, "_TTL_SEC", 1)
    (tmp_path / "old.json").write_text(json.dumps({"ts": 1.0}))
    (tmp_path / "old.bin").write_bytes(b"x")
    monkeypatch.setenv(cluster.ENV_SIZE, "2")
    monkeypatch.setenv(cluster.ENV_INDEX, "1")
    assert imagestore._sweep_disk(time.time()) == 0 and (tmp_path / "old.bin").exists()
    monkeypatch.setenv(cluster.ENV_INDEX, "0")
    assert imagestore._sweep_disk(time.time()) == 1 and not (tmp_path / "old.bin").exists()


def test_config_change_wakes_the_current_watcher_only():
    from app import runtime_persistence as rp

    async def main():
        rp._WATCH_WAKE = asyncio.Event()
        rp._wake_watcher()
        return rp._WATCH_WAKE.is_set()

    try:
        assert asyncio.run(main()) is True
        assert cluster._CONFIG_LISTENERS.count(rp._wake_watcher) == 1
    finally:
        rp._WATCH_WAKE = None


def test_resync_keeps_own_inflight_requests(tmp_path, monkeypatch):
    from tests.test_cluster import CSV, _router, _uniques
    from app.cluster import Replicator

    path = tmp_path / "k.csv"
    path.write_text(CSV)
    a, c = _router(path), _router(path)
    rep_a = Replicator(a, None, send=lambda m: None, origin="wA:1")
    rep_c = Replicator(c, None, send=lambda m: None, origin="wC:1")
    rep_a.install()
    rep_c.install()
    try:
        u0 = _uniques(a)[0]
        a.note_start(u0)                          # in volo su A
        c.note_start(u0, ctx_est=100)             # in volo su C (il bus era giu')
        rep_c.begin_sync()
        rep_c.handle({"t": "sync", "to": "wC:1", **rep_a.snapshot()})
        assert c.stats_for(u0).inflight == 2 and c.stats_for(u0).inflight_tokens == 100
        assert rep_c.local_inflight() == 1
    finally:
        rep_a.uninstall()
        rep_c.uninstall()


def test_internal_server_stops_accepting_with_the_public_one():
    from app import serve

    order: list[str] = []

    class _Public:
        should_exit = False

        async def serve(self, sockets=None):
            await asyncio.sleep(0.05)
            self.should_exit = True               # SIGTERM
            await asyncio.sleep(0.5)              # drain del lifespan
            order.append("public-done")

    class _Internal:
        should_exit = False

        async def serve(self):
            while not self.should_exit:
                await asyncio.sleep(0.01)
            order.append("internal-stopped")

    asyncio.run(serve._serve_worker(_Public(), _Internal(), sock=None))
    assert order == ["internal-stopped", "public-done"]
