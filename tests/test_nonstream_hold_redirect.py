"""Redirect non-stream -> MOTORE STREAM sotto hold (parita' stream/non-stream).

Copre:
- la decisione `_nonstream_hold_redirect` (kill-switch incluso);
- la composizione `_stream_with_fallback(result_box=..., client_stream=False)`
  + `sse_to_chat_obj`: una richiesta non-stream sotto hold ottiene il body
  non-stream assemblato dallo stream;
- il QC di contenuto portato nel motore stream-in-hold (JSON non valido ->
  retry correttivo -> rotazione).

`app.main` va importato SOLO dentro fixture/funzioni.
"""
import asyncio

import pytest
from fastapi.responses import JSONResponse, StreamingResponse


@pytest.fixture()
def M():
    import app.main as _M
    qj = _M.router.policy.qc_json
    snap = (qj.stream_hold_until_finish, _M.router.policy.nonstream_hold_redirect)
    groups_keys = set(_M.config.groups)
    cooldown_keys = set(_M.router._cooldown)
    orig_fb = _M.router.fallback_next
    yield _M
    (qj.stream_hold_until_finish,
     _M.router.policy.nonstream_hold_redirect) = snap
    _M.router.fallback_next = orig_fb
    for k in list(_M.config.groups):
        if k not in groups_keys:
            _M.config.groups.pop(k, None)
    for k in list(_M.router._cooldown):
        if k not in cooldown_keys:
            _M.router._cooldown.pop(k, None)


def _dep(M, name, idx=0):
    dep = {"unique": "%s__fake__%d" % (name, idx), "group": name,
           "model": "fake-model", "api_key": "sk-fake-%d" % idx,
           "api_base": "https://fake.test/v1"}
    M.config.groups.setdefault(name, []).append(dep)
    return dep


def _stream(chunks):
    async def _stream_response(dep, payload, **kwargs):
        async def _gen():
            for c in chunks:
                yield c
        return _gen()
    return _stream_response


CLEAN = [
    b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"ciao"}}]}\n\n',
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
    b"data: [DONE]\n\n",
]


async def _drain(resp):
    out = b""
    async for c in resp.body_iterator:
        out += c
    return out


# --------------------------------------------------- decisione redirect
def test_redirect_decision_cases(M):
    qj = M.router.policy.qc_json
    pol = M.router.policy
    dep = {"hold_until_finish": False}
    # stream -> mai redirect
    assert M._nonstream_hold_redirect(True, dep, qj, pol) is False
    # non-stream + hold policy ON -> redirect
    qj.stream_hold_until_finish = True
    pol.nonstream_hold_redirect = True
    assert M._nonstream_hold_redirect(False, dep, qj, pol) is True
    # kill-switch OFF -> no redirect
    pol.nonstream_hold_redirect = False
    assert M._nonstream_hold_redirect(False, dep, qj, pol) is False
    # hold OFF (policy) -> no redirect
    pol.nonstream_hold_redirect = True
    qj.stream_hold_until_finish = False
    assert M._nonstream_hold_redirect(False, dep, qj, pol) is False
    # hold per-deployment ON (policy OFF) -> redirect
    qj.stream_hold_until_finish = False
    assert M._nonstream_hold_redirect(
        False, {"hold_until_finish": True}, qj, pol) is True


# --------------------------------------------------- composizione stream->json
def test_stream_composition_clean_body(M, monkeypatch):
    dep = _dep(M, "scrocco-llm-test-redirect")
    monkeypatch.setattr(M.forwarder, "stream_response", _stream(CLEAN))
    meta: dict = {}

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        resp = await M._stream_with_fallback(
            "test", dep, payload, scope="chain",
            result_box=meta, client_stream=False)
        assert isinstance(resp, StreamingResponse)
        from app.protocols import sse_to_chat_obj
        return sse_to_chat_obj(await _drain(resp))
    obj = asyncio.run(_run())
    assert obj["choices"][0]["message"]["content"] == "ciao"
    assert obj["choices"][0]["finish_reason"] == "stop"
    assert meta["dep"]["unique"] == dep["unique"]
    assert meta["attempts"] == [dep["unique"]]


def test_stream_composition_truncated_is_503(M, monkeypatch):
    dep = _dep(M, "scrocco-llm-test-redirect2")
    trunc = [
        b'data: {"choices":[{"index":0,"delta":{"content":"moncone"}}]}\n\n',
        # niente finish_reason, niente [DONE]: troncato
    ]
    monkeypatch.setattr(M.forwarder, "stream_response", _stream(trunc))
    meta: dict = {}

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await M._stream_with_fallback(
            "test", dep, payload, scope="chain",
            result_box=meta, client_stream=False)
    resp = asyncio.run(_run())
    assert isinstance(resp, JSONResponse) and resp.status_code == 503
    assert meta["dep"]["unique"] == dep["unique"]


def test_hold_qc_invalid_json_rotates(M, monkeypatch):
    """JSON non valido in hold -> retry correttivo sullo stesso dep, poi
    rotazione (nessuna penale) verso il successivo."""
    name = "scrocco-llm-test-redirect3"
    d0 = _dep(M, name, 0)
    d1 = _dep(M, name, 1)

    async def _stream_response(dep, payload, **kwargs):
        async def _gen():
            if dep["unique"] == d0["unique"]:
                yield (b'data: {"choices":[{"index":0,"delta":'
                       b'{"content":"{bad json"}}]}\n\n')
            else:
                yield (b'data: {"choices":[{"index":0,"delta":'
                       b'{"content":"buono"}}]}\n\n')
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"
        return _gen()
    monkeypatch.setattr(M.forwarder, "stream_response", _stream_response)
    M.router.fallback_next = lambda *a, **k: d1
    meta: dict = {}

    async def _run():
        payload = {"model": d0["model"],
                   "messages": [{"role": "user", "content": "dammi json"}]}
        resp = await M._stream_with_fallback(
            "test", d0, payload, scope="chain",
            result_box=meta, client_stream=False)
        assert isinstance(resp, StreamingResponse)
        from app.protocols import sse_to_chat_obj
        return sse_to_chat_obj(await _drain(resp))
    obj = asyncio.run(_run())
    assert obj["choices"][0]["message"]["content"] == "buono"
    assert meta["dep"]["unique"] == d1["unique"]


def test_endpoint_nonstream_hold_redirect_e2e(M, monkeypatch):
    """E2E: una richiesta NON-stream con hold viene servita dal motore stream e
    restituita come `chat.completion` JSON (unico motore per stream/non-stream)."""
    import json as _json
    from starlette.requests import Request
    from starlette.responses import Response
    dep = _dep(M, "scrocco-llm-test-ep")
    monkeypatch.setattr(M.forwarder, "stream_response", _stream(CLEAN))

    class _A:
        ok = True
        profile = "test"
        error = None
    monkeypatch.setattr(M.authn, "authenticate", lambda h: _A())
    monkeypatch.setattr(M.authn, "authorize_model", lambda a, m: True)
    monkeypatch.setattr(M.router, "resolve_group_for_request",
                        lambda *a, **k: dep["group"])
    seen: dict = {}

    def _pick(*a, **k):
        seen.update(k)
        return dep
    monkeypatch.setattr(M.router, "initial_pick", _pick)
    monkeypatch.setattr(M.router, "fallback_next", lambda *a, **k: None)

    payload = {"model": dep["model"], "stream": False,
               "messages": [{"role": "user", "content": "ciao"}]}
    body = _json.dumps(payload).encode()
    sent = {"done": False}

    async def receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "POST",
             "path": "/v1/chat/completions",
             "headers": [(b"authorization", b"Bearer x")],
             "query_string": b""}

    async def _run():
        return await M.chat_completions(Request(scope, receive), Response())
    out = asyncio.run(_run())
    assert isinstance(out, dict)
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["content"] == "ciao"
    assert out["nx_deployment"] == dep["unique"]
    # parita' stream/non-stream: sotto hold il non-stream ordina come lo stream
    assert seen.get("prefer_fast") is False
