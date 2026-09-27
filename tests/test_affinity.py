"""Multi-worker: instradamento per sessione tra worker (app/affinity.py).

L'AffinityProxy davanti al worker 0 di un cluster da 2: le richieste la cui
sessione appartiene al worker 1 devono arrivare li' (corpo, header e client
intatti), le altre restare locali. I peer sono app ASGI raggiunte con
httpx.ASGITransport al posto del socket unix.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import cluster
from app.affinity import AffinityProxy, InternalEntry, merge_prometheus


def _recorder(name: str, seen: list):
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        body = b""
        while True:
            m = await receive()
            body += m.get("body", b"")
            if not m.get("more_body"):
                break
        seen.append({"who": name, "scope": scope, "body": body})
        payload = json.dumps({"who": name}).encode()
        await send({"type": "http.response.start", "status": 201,
                    "headers": [(b"content-type", b"application/json"), (b"x-who", name.encode())]})
        await send({"type": "http.response.body", "body": payload})
    return app


class _FakePeers:
    def __init__(self, apps: dict[int, object], fail: bool = False):
        self.apps, self.fail = apps, fail

    def get(self, i: int) -> httpx.AsyncClient:
        if self.fail:
            def refuse(request):
                raise httpx.ConnectError("refused", request=request)
            return httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=InternalEntry(self.apps[i]),
                                                               client=("unix", 0)))


def _sid_for(owner: int) -> str:
    return next(f"ses_{i}" for i in range(1000) if cluster.owner_of(f"ses_{i}", 2) == owner)


@pytest.fixture()
def gw():
    seen: list = []
    local, remote = _recorder("w0", seen), _recorder("w1", seen)
    proxy = AffinityProxy(local, index=0, workers=2, peers=_FakePeers({1: remote}))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy, client=("203.0.113.7", 5555)),
                               base_url="https://gw.example")
    return client, seen, proxy


def _run(coro):
    return asyncio.run(coro)


def test_header_session_goes_to_its_owner(gw):
    client, seen, _ = gw

    async def go():
        r1 = await client.post("/v1/chat/completions", headers={"x-session-id": _sid_for(1)},
                               json={"model": "m", "messages": []})
        r0 = await client.post("/v1/chat/completions", headers={"x-session-id": _sid_for(0)},
                               json={"model": "m", "messages": []})
        return r1, r0

    r1, r0 = _run(go())
    assert (r1.status_code, r1.json(), r1.headers["x-who"]) == (201, {"who": "w1"}, "w1")
    assert r0.json() == {"who": "w0"}
    hop = seen[0]
    assert hop["who"] == "w1"
    assert hop["scope"]["client"] == ("203.0.113.7", 5555)         # client originale
    assert hop["scope"]["scheme"] == "https"
    assert hop["scope"]["scrocco.internal"] is True
    assert json.loads(hop["body"]) == {"model": "m", "messages": []}
    assert not [k for k, _ in hop["scope"]["headers"] if k.startswith(b"x-scrocco-")]


def test_body_session_is_used_when_no_header(gw):
    client, seen, _ = gw
    sid = _sid_for(1)
    r = _run(client.post("/v1/chat/completions", json={"model": "m", "user": sid, "messages": []}))
    assert r.json() == {"who": "w1"}
    assert json.loads(seen[0]["body"])["user"] == sid


def test_requests_without_session_stay_local(gw):
    client, seen, _ = gw

    async def go():
        a = await client.get("/v1/models")
        b = await client.post("/v1/audio/transcriptions", content=b"xx",
                              headers={"content-type": "multipart/form-data; boundary=x"})
        c = await client.get("/healthz")
        return a, b, c

    for r in _run(go()):
        assert r.json() == {"who": "w0"}
    assert seen[1]["body"] == b"xx"                    # corpo riconsegnato intatto


def test_admin_session_views_follow_the_session(gw):
    client, _seen, _ = gw

    async def go():
        a = await client.get("/admin/sessions", params={"session_id": _sid_for(1)})
        b = await client.post("/admin/sessions/release", json={"session_id": _sid_for(1)})
        c = await client.get("/admin/state")
        return a, b, c

    a, b, c = _run(go())
    assert a.json() == {"who": "w1"} and b.json() == {"who": "w1"} and c.json() == {"who": "w0"}


def test_client_cannot_inject_private_headers(gw):
    client, seen, _ = gw
    _run(client.post("/v1/chat/completions",
                     headers={"x-session-id": _sid_for(0), "x-scrocco-client": '["10.0.0.1", 1]'},
                     json={"model": "m"}))
    scope = seen[0]["scope"]
    assert scope["client"] == ("203.0.113.7", 5555)
    assert not [k for k, _ in scope["headers"] if k.startswith(b"x-scrocco-")]


def test_unreachable_owner_is_served_locally(gw):
    _client, seen, proxy = gw
    proxy.peers = _FakePeers({}, fail=True)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy, client=("203.0.113.7", 1)),
                               base_url="http://gw")
    r = _run(client.post("/v1/chat/completions", headers={"x-session-id": _sid_for(1)},
                         json={"model": "m", "stream": True}))
    assert r.json() == {"who": "w0"}
    assert json.loads(seen[0]["body"]) == {"model": "m", "stream": True}


def test_merge_prometheus_groups_families_and_labels_worker():
    w0 = ('# TYPE nx_x_total counter\nnx_x_total{kind="a"} 1.0\n'
          '# TYPE nx_up gauge\nnx_up 3\n'
          '# TYPE h histogram\nh_bucket{le="1"} 2\nh_sum 3\nh_count 2\n')
    w1 = '# TYPE nx_x_total counter\nnx_x_total{kind="a"} 5.0\n# TYPE nx_up gauge\nnx_up 4\n'
    out = merge_prometheus({0: w0, 1: w1}).splitlines()
    assert out.count("# TYPE nx_x_total counter") == 1
    i = out.index("# TYPE nx_x_total counter")
    assert out[i + 1:i + 3] == ['nx_x_total{worker="0",kind="a"} 1.0', 'nx_x_total{worker="1",kind="a"} 5.0']
    assert 'nx_up{worker="1"} 4' in out
    j = out.index("# TYPE h histogram")
    assert out[j + 1:j + 4] == ['h_bucket{worker="0",le="1"} 2', 'h_sum{worker="0"} 3', 'h_count{worker="0"} 2']
