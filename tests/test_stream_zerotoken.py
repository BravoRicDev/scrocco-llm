"""Streaming anti-stallo: lo stream verso il client NON parte finche' non
arriva CONTENUTO DI RISPOSTA reale.

- entro `stream_first_content_ms`: upstream vuoto/errore/lento -> rotazione
  trasparente (nessun byte al client);
- catena esaurita -> HTTP 503 retryable (mai un turno/stream finto)
  (contenuto in chiaro + finish_reason=stop + [DONE]); mai un `{"error"}`;
- post-commit (contenuto gia' partito): il watchdog puo' solo raffreddare il
  deployment, mai iniettare byte.

`app.main` va importato SOLO dentro funzioni/fixture (mai a livello di modulo).
"""
import asyncio

import pytest
from fastapi.responses import JSONResponse, StreamingResponse


@pytest.fixture()
def M():
    import app.main as _M
    qj = _M.router.policy.qc_json
    qs = _M.router.policy.qc_sanity
    snap = (qj.stream_first_content_ms, qj.stream_total_deadline_ms,
            qj.stream_commit_include_reasoning, qs.rotate_on_length_empty,
            qj.stream_parachute_no_timeout)
    cooldown_keys = set(_M.router._cooldown)
    groups_keys = set(_M.config.groups)
    yield _M
    (qj.stream_first_content_ms, qj.stream_total_deadline_ms,
     qj.stream_commit_include_reasoning, qs.rotate_on_length_empty,
     qj.stream_parachute_no_timeout) = snap
    for k in list(_M.router._cooldown):
        if k not in cooldown_keys:
            _M.router._cooldown.pop(k, None)
    for k in list(_M.config.groups):
        if k not in groups_keys:
            _M.config.groups.pop(k, None)


def _fake_stream_response(chunks, delay=0.0):
    async def _stream_response(dep, payload, **kwargs):
        async def _gen():
            for c in chunks:
                if delay:
                    await asyncio.sleep(delay)
                yield c
        return _gen()
    return _stream_response


def _a_dep(M):
    for _grp, deps in M.config.groups.items():
        if deps:
            return deps[0]
    raise RuntimeError("nessun deployment nella config globale")


def _set(M, first_ms=20000, deadline_ms=90000, incl_reason=False,
         rotate_length=False):
    qj = M.router.policy.qc_json
    qj.stream_first_content_ms = first_ms
    qj.stream_total_deadline_ms = deadline_ms
    qj.stream_commit_include_reasoning = incl_reason
    M.router.policy.qc_sanity.rotate_on_length_empty = rotate_length


async def _drain(resp):
    out = b""
    async for c in resp.body_iterator:
        out += c
    return out


# ------------------------------------------------------------ _peek_stream
def test_peek_answer_content(M):
    chunk = b'data: {"choices":[{"delta":{"content":"ciao"}}]}\n\n'

    async def gen():
        yield chunk
    v, buf, pend, meta = asyncio.run(M._peek_stream(gen(), 500, False))
    assert v == "content" and buf == [chunk] and pend is None


def test_peek_reasoning_only_is_not_content(M):
    chunk = b'data: {"choices":[{"delta":{"reasoning_content":"penso"}}]}\n\n'

    async def gen():
        yield chunk                       # solo reasoning, poi lo stream resta muto
        await asyncio.sleep(1)
    v, buf, pend, meta = asyncio.run(M._peek_stream(gen(), 60, False))
    assert v == "timeout"                 # niente answer entro il budget
    if pend is not None:
        pend.cancel()


def test_peek_reasoning_counts_when_enabled(M):
    chunk = b'data: {"choices":[{"delta":{"reasoning_content":"penso"}}]}\n\n'

    async def gen():
        yield chunk
    v, buf, pend, meta = asyncio.run(M._peek_stream(gen(), 500, True))
    assert v == "content"


def test_peek_one_tiny_token_then_death_does_not_commit(M):
    """Un solo token poi lo stream muore (niente finish, niente [DONE]) ->
    NON impegna lo stream: timeout -> rotazione."""
    async def gen():
        yield b'data: {"choices":[{"delta":{"content":"E"}}]}\n\n'
        await asyncio.sleep(1)             # poi muto
    v, buf, pend, meta = asyncio.run(M._peek_stream(gen(), 80, False, 40))
    assert v == "timeout"
    if pend is not None:
        pend.cancel()


def test_peek_short_but_complete_answer_commits(M):
    """Risposta breve ma COMPLETA (finish_reason) -> impegna."""
    async def gen():
        yield b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    v, buf, pend, meta = asyncio.run(M._peek_stream(gen(), 500, False, 40))
    assert v == "content"


def test_peek_reaches_min_chars_commits(M):
    long = "x" * 50
    async def gen():
        yield ('data: {"choices":[{"delta":{"content":"%s"}}]}\n\n'
               % long).encode()
    v, buf, pend, meta = asyncio.run(M._peek_stream(gen(), 500, False, 40))
    assert v == "content"


def test_peek_finish_reason_no_content_is_empty_eof(M):
    chunk = b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'

    async def gen():
        yield chunk
    v, buf, pend, meta = asyncio.run(M._peek_stream(gen(), 500, False))
    assert v == "empty_eof" and meta["finish_reason"] == "length"


def test_peek_error_event(M):
    chunk = b'data: {"error":{"message":"boom"}}\n\n'

    async def gen():
        yield chunk
    v, buf, pend, meta = asyncio.run(M._peek_stream(gen(), 500, False))
    assert v == "error"


def test_peek_provider_error_as_delta_content_is_error(M):
    """Provider che mette il proprio envelope d'errore DENTRO delta.content:
    non e' risposta reale -> verdict error (rotazione)."""
    inner = '{"type":"error","error":{"type":"server_error","message":"[404]"}}'
    chunk = ('data: {"choices":[{"delta":{"content":%s}}]}\n\n'
             % __import__("json").dumps(inner)).encode()

    async def gen():
        yield chunk
    v, buf, pend, meta = asyncio.run(M._peek_stream(gen(), 500, False, 40))
    assert v == "error"


def test_peek_empty_eof(M):
    async def gen():
        return
        yield b""
    v, buf, pend, meta = asyncio.run(M._peek_stream(gen(), 200, False))
    assert v == "empty_eof" and buf == []


def test_peek_timeout_keeps_pending(M):
    async def gen():
        await asyncio.sleep(0.5)
        yield b"data: {}\n\n"

    async def _run():
        v, buf, pend, meta = await M._peek_stream(gen(), 50, False)
        assert v == "timeout" and pend is not None
        assert await pend == b"data: {}\n\n"       # non cancellata a meta' frame
    asyncio.run(_run())


# ----------------------------------------------------- helper puri
def test_delta_helpers(M):
    ans = {"choices": [{"delta": {"content": "x"}}]}
    rea = {"choices": [{"delta": {"reasoning_content": "y"}}]}
    tc = {"choices": [{"delta": {"tool_calls": [{"id": "a"}]}}]}
    assert M._delta_has_answer(ans) and not M._delta_has_answer(rea)
    assert M._delta_has_answer(tc)
    assert M._delta_has_content(rea)              # reasoning conta come "content"
    assert M._chunk_finish_reason(
        {"choices": [{"finish_reason": "stop"}]}) == "stop"


# --------------------------------------------------- flusso end-to-end
def test_e2e_happy_stream_passthrough(M, monkeypatch):
    _set(M)
    dep = _a_dep(M)
    M.router._cooldown.pop(dep["unique"], None)
    chunks = [
        b'data: {"choices":[{"delta":{"content":"ris"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"posta"}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    monkeypatch.setattr(M.forwarder, "stream_response",
                        _fake_stream_response(chunks))

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        resp = await M._stream_with_fallback("test", dep, payload, scope="chain")
        assert isinstance(resp, StreamingResponse)
        return await _drain(resp)
    out = asyncio.run(_run())
    assert b'"ris"' in out and b'"posta"' in out and b"[DONE]" in out
    assert b'"error"' not in out
    assert dep["unique"] not in M.router._cooldown


def test_e2e_empty_stream_no_alternative_returns_503(M, monkeypatch):
    """Stream vuoto + nessun deployment alternativo (profile=None) -> errore
    503 RETRYABLE PULITO (mai uno stream/turno finto)."""
    _set(M, first_ms=2000)
    dep = _a_dep(M)

    async def _empty(dep, payload, **kwargs):
        async def _g():
            return
            yield b""
        return _g()
    monkeypatch.setattr(M.forwarder, "stream_response", _empty)

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await M._stream_with_fallback(None, dep, payload, scope="chain")
    resp = asyncio.run(_run())
    assert isinstance(resp, JSONResponse) and resp.status_code == 503
    assert b"upstream_unavailable" in resp.body
    assert resp.headers.get("retry-after") == "2"


def test_e2e_length_empty_returns_503_no_chain_burn(M, monkeypatch):
    """finish_reason=length + zero answer + rotate_on_length_empty=False ->
    503 SUBITO, senza bruciare la catena (nessuna alternativa provata)."""
    _set(M, first_ms=2000, rotate_length=False)
    dep = _a_dep(M)
    seen = []

    async def _len_empty(d, payload, **kwargs):
        seen.append(d["unique"])
        async def _g():
            yield (b'data: {"choices":[{"delta":{},'
                   b'"finish_reason":"length"}]}\n\n')
        return _g()
    monkeypatch.setattr(M.forwarder, "stream_response", _len_empty)

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await M._stream_with_fallback("test", dep, payload, scope="chain")
    resp = asyncio.run(_run())
    assert isinstance(resp, JSONResponse) and resp.status_code == 503
    assert len(seen) == 1                       # un solo deployment interpellato
    assert dep["unique"] not in M.router._cooldown   # length-empty non punisce


def test_e2e_reasoning_then_finish_no_answer_returns_503(M, monkeypatch):
    """Modello che RAGIONA poi CHIUDE con finish_reason ma senza risposta:
    ha completato, non e' rotto -> 503 diretto, un tentativo, no cooldown."""
    _set(M, first_ms=2000)
    dep = _a_dep(M)
    M.router._cooldown.pop(dep["unique"], None)
    seen = []

    async def _reason_only(d, payload, **kwargs):
        seen.append(d["unique"])
        async def _g():
            yield (b'data: {"choices":[{"delta":'
                   b'{"reasoning_content":"sto pensando..."}}]}\n\n')
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"
        return _g()
    monkeypatch.setattr(M.forwarder, "stream_response", _reason_only)

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await M._stream_with_fallback("test", dep, payload, scope="chain")
    resp = asyncio.run(_run())
    assert isinstance(resp, JSONResponse) and resp.status_code == 503
    assert len(seen) == 1
    assert dep["unique"] not in M.router._cooldown


def test_e2e_reasoning_truncated_no_finish_rotates(M, monkeypatch):
    """Reasoning TRONCATO (niente finish_reason): e' un troncamento upstream ->
    si ROTA verso un altro deployment (+ mark_failed sul primo)."""
    _set(M, first_ms=2000)
    dep = _a_dep(M)
    M.router._cooldown.pop(dep["unique"], None)
    seen = []

    async def _trunc(d, payload, **kwargs):
        seen.append(d["unique"])
        async def _g():
            yield (b'data: {"choices":[{"delta":'
                   b'{"reasoning_content":"penso e poi la linea cade"}}]}\n\n')
            # niente finish_reason, niente [DONE]: troncato
        return _g()
    monkeypatch.setattr(M.forwarder, "stream_response", _trunc)

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await M._stream_with_fallback("test", dep, payload, scope="chain")
    resp = asyncio.run(_run())
    # ogni dep tronca -> ha provato >1 dep, poi 503 retryable (mai turno finto)
    assert isinstance(resp, JSONResponse) and resp.status_code == 503
    assert len(seen) >= 2                                 # ha provato >1 dep
    assert dep["unique"] in M.router._cooldown            # primo penalizzato


def test_e2e_post_commit_truncation_only_cools_down(M, monkeypatch):
    """Contenuto -> commit al client -> troncamento: nessun artefatto verso il
    client, ma il deployment va in cooldown."""
    _set(M, first_ms=2000)
    dep = _a_dep(M)
    M.router._cooldown.pop(dep["unique"], None)
    chunks = [
        b'data: {"choices":[{"delta":{"content":"meta"}}]}\n\n',
        # niente [DONE], niente finish_reason -> troncato
    ]
    monkeypatch.setattr(M.forwarder, "stream_response",
                        _fake_stream_response(chunks))

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        resp = await M._stream_with_fallback("test", dep, payload, scope="chain")
        return await _drain(resp)
    out = asyncio.run(_run())
    assert b"meta" in out and b'"error"' not in out
    assert dep["unique"] in M.router._cooldown


# ------------------------------------------- paracadute (-go/-fallback)
def _mute_stream_response():
    """Upstream che NON produce contenuto entro il budget: _peek_stream ->
    timeout. Resta muto oltre il clamp minimo di fc_ms (2s), poi yielda un
    chunk non-answer (che non committa) e termina."""
    async def _stream_response(dep, payload, **kwargs):
        async def _gen():
            await asyncio.sleep(3.0)      # oltre fc_ms clamp (2s) -> timeout
            yield b"data: {}\n\n"          # non-answer, non committa
        return _gen()
    return _stream_response


def _parachute_dep(M, suffix="-go"):
    base = "scrocco-llm-test" + suffix
    dep = {"unique": base + "__fake__0", "group": base,
           "model": "fake-model", "api_key": "sk-fake",
           "api_base": "https://fake.test/v1"}
    M.config.groups.setdefault(base, [dep])
    return dep


def test_parachute_go_timeout_transmits_not_503(M, monkeypatch):
    """Sulla catena -go (paracadute) il timeout primo-contenuto NON produce 503:
    lo stream parte comunque (parametro default True)."""
    qj = M.router.policy.qc_json
    qj.stream_first_content_ms = 60
    qj.stream_parachute_no_timeout = True
    dep = _parachute_dep(M, suffix="-go")
    monkeypatch.setattr(M.forwarder, "stream_response", _mute_stream_response())

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        resp = await M._stream_with_fallback("test", dep, payload, scope="chain")
        assert isinstance(resp, StreamingResponse), "deve trasmettere, non 503"
        await _drain(resp)
    asyncio.run(_run())


def test_parachute_go_timeout_legacy_503(M, monkeypatch):
    """Parametro disattivato (stream_parachute_no_timeout=False): sulla -go il
    timeout resta rotazione/503 (utile con molte chiavi valide in catena)."""
    qj = M.router.policy.qc_json
    qj.stream_first_content_ms = 60
    qj.stream_parachute_no_timeout = False
    dep = _parachute_dep(M, suffix="-go")
    monkeypatch.setattr(M.forwarder, "stream_response", _mute_stream_response())

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        resp = await M._stream_with_fallback("test", dep, payload, scope="chain")
        assert isinstance(resp, JSONResponse) and resp.status_code == 503
    asyncio.run(_run())


def test_parachute_does_not_affect_dims(M, monkeypatch):
    """I -dim NON sono paracadute: il timeout primo-contenuto resta timeout
    (rotazione) anche col parametro attivo — comportamento invariato."""
    qj = M.router.policy.qc_json
    qj.stream_first_content_ms = 60
    qj.stream_parachute_no_timeout = True
    dep = _parachute_dep(M, suffix="-200k")
    monkeypatch.setattr(M.forwarder, "stream_response", _mute_stream_response())

    async def _run():
        payload = {"model": dep["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        resp = await M._stream_with_fallback("test", dep, payload, scope="chain")
        assert isinstance(resp, JSONResponse) and resp.status_code == 503
    asyncio.run(_run())


def test_parachute_verdict_helper(M):
    pol = M.router.policy
    dep_go = {"group": "scrocco-llm-test-go"}
    dep_fb = {"group": "scrocco-llm-test-fallback"}
    dep_dim = {"group": "scrocco-llm-test-200k"}
    pol.qc_json.stream_parachute_no_timeout = True
    assert M._parachute_verdict("timeout", pol.qc_json, dep_go, pol) == "content"
    assert M._parachute_verdict("timeout", pol.qc_json, dep_fb, pol) == "content"
    assert M._parachute_verdict("timeout", pol.qc_json, dep_dim, pol) == "timeout"
    assert M._parachute_verdict("empty_eof", pol.qc_json, dep_go, pol) == "empty_eof"
    pol.qc_json.stream_parachute_no_timeout = False
    assert M._parachute_verdict("timeout", pol.qc_json, dep_go, pol) == "timeout"
