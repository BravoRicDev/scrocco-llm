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
from app import chat_stream
from app import runtime_persistence
import app.state as gw_state
from app import chat_completions, chat_helpers, chat_stream


@pytest.fixture()
def M():
    import app.main as _M
    qj = gw_state.router.policy.qc_json
    snap = (qj.stream_hold_until_finish, gw_state.router.policy.nonstream_hold_redirect)
    groups_keys = set(gw_state.config.groups)
    cooldown_keys = set(gw_state.router._cooldown)
    orig_fb = gw_state.router.fallback_next
    yield _M
    (qj.stream_hold_until_finish,
     gw_state.router.policy.nonstream_hold_redirect) = snap
    gw_state.router.fallback_next = orig_fb
    for k in list(gw_state.config.groups):
        if k not in groups_keys:
            gw_state.config.groups.pop(k, None)
    for k in list(gw_state.router._cooldown):
        if k not in cooldown_keys:
            gw_state.router._cooldown.pop(k, None)


def _dep(M, name, idx=0):
    dep = {"unique": "%s__fake__%d" % (name, idx), "group": name,
           "model": "fake-model", "api_key": "sk-fake-%d" % idx,
           "api_base": "https://fake.test/v1"}
    gw_state.config.groups.setdefault(name, []).append(dep)
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
    qj = gw_state.router.policy.qc_json
    pol = gw_state.router.policy
    dep = {"hold_until_finish": False}
    # stream -> mai redirect
    assert runtime_persistence._nonstream_hold_redirect(True, dep, qj, pol) is False
    # non-stream + hold policy ON -> redirect
    qj.stream_hold_until_finish = True
    pol.nonstream_hold_redirect = True
    assert runtime_persistence._nonstream_hold_redirect(False, dep, qj, pol) is True
    # kill-switch OFF -> no redirect
    pol.nonstream_hold_redirect = False
    assert runtime_persistence._nonstream_hold_redirect(False, dep, qj, pol) is False
    # hold OFF (policy) -> no redirect
    pol.nonstream_hold_redirect = True
    qj.stream_hold_until_finish = False
    assert runtime_persistence._nonstream_hold_redirect(False, dep, qj, pol) is False
    # hold per-deployment ON (policy OFF) -> redirect
    qj.stream_hold_until_finish = False
    assert runtime_persistence._nonstream_hold_redirect(
        False, {"hold_until_finish": True}, qj, pol) is True


# --------------------------------------------------- composizione stream->json
def test_stream_composition_clean_body(M, monkeypatch):
    dep = _dep(M, "scrocco-llm-test-redirect")
    monkeypatch.setattr(gw_state.forwarder, "stream_response", _stream(CLEAN))
    meta: dict = {}

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        resp = await chat_stream._stream_with_fallback(
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
    monkeypatch.setattr(gw_state.forwarder, "stream_response", _stream(trunc))
    meta: dict = {}

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await chat_stream._stream_with_fallback(
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
    monkeypatch.setattr(gw_state.forwarder, "stream_response", _stream_response)
    gw_state.router.fallback_next = lambda *a, **k: d1
    meta: dict = {}

    async def _run():
        payload = {"model": d0["model"],
                   "messages": [{"role": "user", "content": "dammi json"}]}
        resp = await chat_stream._stream_with_fallback(
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
    monkeypatch.setattr(gw_state.forwarder, "stream_response", _stream(CLEAN))

    class _A:
        ok = True
        profile = "test"
        error = None
    monkeypatch.setattr(gw_state.authn, "authenticate", lambda h: _A())
    monkeypatch.setattr(gw_state.authn, "authorize_model", lambda a, m: True)
    monkeypatch.setattr(gw_state.router, "resolve_group_for_request",
                        lambda *a, **k: dep["group"])
    seen: dict = {}

    def _pick(*a, **k):
        seen.update(k)
        return dep
    monkeypatch.setattr(gw_state.router, "initial_pick", _pick)
    monkeypatch.setattr(gw_state.router, "fallback_next", lambda *a, **k: None)

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
        return await chat_completions.chat_completions(Request(scope, receive), Response())
    out = asyncio.run(_run())
    assert isinstance(out, dict)
    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["content"] == "ciao"
    assert out["nx_deployment"] == dep["unique"]
    # parita' stream/non-stream: sotto hold il non-stream ordina come lo stream
    assert seen.get("prefer_fast") is False


def test_endpoint_nonstream_hold_repairs_toolcall(M, monkeypatch):
    """E2E: non-stream sotto hold con tool-call malformata (virgola finale)
    -> il body assemblato e' riparato (stessa riparazione del non-stream)."""
    import json as _json
    from starlette.requests import Request
    from starlette.responses import Response
    dep = _dep(M, "scrocco-llm-test-ep-tr")
    chunks = [
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        b'"id":"c1","type":"function","function":{"name":"search",'
        b'"arguments":""}}]}}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        b'"function":{"arguments":"{\\"q\\": \\"hi\\",}"}}]}}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{},'
        b'"finish_reason":"tool_calls"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    monkeypatch.setattr(gw_state.forwarder, "stream_response", _stream(chunks))

    class _A:
        ok = True
        profile = "test"
        error = None
    monkeypatch.setattr(gw_state.authn, "authenticate", lambda h: _A())
    monkeypatch.setattr(gw_state.authn, "authorize_model", lambda a, m: True)
    monkeypatch.setattr(gw_state.router, "resolve_group_for_request",
                        lambda *a, **k: dep["group"])
    monkeypatch.setattr(gw_state.router, "initial_pick", lambda *a, **k: dep)
    monkeypatch.setattr(gw_state.router, "fallback_next", lambda *a, **k: None)

    payload = {"model": dep["model"], "stream": False,
               "messages": [{"role": "user", "content": "cerca"}],
               "tools": [{"type": "function", "function": {
                   "name": "search", "parameters": {"type": "object"}}}]}
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
        return await chat_completions.chat_completions(Request(scope, receive), Response())
    out = asyncio.run(_run())
    tcs = out["choices"][0]["message"]["tool_calls"]
    assert _json.loads(tcs[0]["function"]["arguments"]) == {"q": "hi"}
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_immediate_upstream_error_no_unboundlocal(M, monkeypatch):
    """Regressione: se `stream_response` solleva UpstreamError SUBITO (es. 429
    all'apertura, prima che `qcp` fosse assegnato) il path d'errore NON deve
    andare in UnboundLocalError('qcp') -> 500, ma restituire una risposta
    d'errore JSON (503 o status azionabile)."""
    dep = _dep(M, "scrocco-llm-test-redirect-early")
    monkeypatch.setattr(gw_state.router, "fallback_next", lambda *a, **k: None)
    from app.forwarder import UpstreamError

    async def _boom(dep, payload, **kwargs):
        raise UpstreamError(429, "rate limit exceeded")
    monkeypatch.setattr(gw_state.forwarder, "stream_response", _boom)
    meta: dict = {}

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await chat_stream._stream_with_fallback(
            "test", dep, payload, scope="chain",
            result_box=meta, client_stream=False)
    resp = asyncio.run(_run())
    assert isinstance(resp, JSONResponse)
    assert resp.status_code != 500


def test_nonstream_hold_redirect_single_summary(M, monkeypatch):
    """Bug 3: non-stream sotto hold redirect (_redirect=True) NON deve
    emettere doppio _emit_summary.

    Il motore stream (in hold) emette gia' il suo [summary] via _summary().
    Il codice non-stream post-redirect deve saltare _emit_summary e il blocco
    note_estimate_error/note_session_estimate quando _redirect == True.
    """
    import json as _json
    from starlette.requests import Request
    from starlette.responses import Response

    # hold redirect ON
    gw_state.router.policy.qc_json.stream_hold_until_finish = True
    gw_state.router.policy.nonstream_hold_redirect = True

    dep = _dep(M, "scrocco-llm-test-single-summary")
    CLEAN = [
        b'data: {"id":"x","choices":[{"index":0,"delta":{"content":"ciao"}}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    async def _stream_response(dep, payload, **kwargs):
        async def _gen():
            for c in CLEAN:
                yield c
        return _gen()
    monkeypatch.setattr(gw_state.forwarder, "stream_response", _stream_response)

    class _A:
        ok = True; profile = "test"; error = None
    monkeypatch.setattr(gw_state.authn, "authenticate", lambda h: _A())
    monkeypatch.setattr(gw_state.authn, "authorize_model", lambda a, m: True)
    monkeypatch.setattr(gw_state.router, "resolve_group_for_request",
                        lambda *a, **k: dep["group"])
    monkeypatch.setattr(gw_state.router, "initial_pick", lambda *a, **k: dep)
    monkeypatch.setattr(gw_state.router, "fallback_next", lambda *a, **k: None)

    # count _emit_summary calls
    calls = []
    _orig = chat_helpers._emit_summary
    def _spy(**f):
        calls.append(f)
        return _orig(**f)
    # il riepilogo parte dal motore non-stream o da quello stream
    monkeypatch.setattr(chat_completions, "_emit_summary", _spy)
    monkeypatch.setattr(chat_stream, "_emit_summary", _spy)

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
        return await chat_completions.chat_completions(Request(scope, receive), Response())
    out = asyncio.run(_run())

    assert out["object"] == "chat.completion"
    assert out["choices"][0]["message"]["content"] == "ciao"
    # ESATTAMENTE 1 chiamata _emit_summary (quella del motore stream)
    assert len(calls) == 1, f"attesi 1 _emit_summary, trovati {len(calls)}: {calls}"
    assert calls[0].get("stream") is False
    assert calls[0].get("dep") == dep["unique"]
