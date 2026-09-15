"""Fix "The `reasoning_content` in the thinking mode must be passed back to
the API" (opencode zen "Console Go" / deepseek v4.1 thinking).

Il client agentico DROPPA il reasoning dalla history; il provider pretende il
campo sugli assistant con tool_calls -> 400 bloccante. Non va RUOTATO (tutte le
chiavi dello stesso provider rifiutano lo stesso payload: in produzione
bruciava 26 chiavi) ma RIPARATO e ritentato sullo STESSO deployment.
"""
import asyncio
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.forwarder import (UpstreamError, repair_reasoning_replay,
                           restore_reasoning, is_unclear_error,
                           _REASONING_REPLAY_RE)
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/rr-a,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-A,,5
t@x.com,m/rr-b,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-B,,5
"""

ERR = ('{"error":{"param":null,"type":"invalid_request_error",'
       '"code":"invalid_request_error","message":"Error from provider '
       '(Console Go): Upstream request failed: [invalid_request_error] '
       'The `reasoning_content` in the thinking mode must be passed back '
       'to the API."}}')


def _write_csv():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV)
    return path


@pytest.fixture()
def FW():
    path = _write_csv()
    try:
        yield Router(GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1),
                     Policy.from_dict({"warm_pool": {"refill_enabled": False}}))
    finally:
        os.path.exists(path) and os.unlink(path)


def _has_repair(payload):
    return any(m.get("reasoning_content")
               for m in payload.get("messages", [])
               if m.get("role") == "assistant" and m.get("tool_calls"))


def _payload():
    return {"model": "m", "stream": False, "reasoning_effort": "medium",
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": "leggi /tmp/a.txt"},
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "call_1", "type": "function",
                                 "function": {"name": "read",
                                              "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            ]}


def _resp(txt="ok"):
    return {"choices": [{"message": {"role": "assistant", "content": txt},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2}}


# ------------------------------------------------------------------ unita'
def test_matcher_e_riparazione_idempotente():
    assert _REASONING_REPLAY_RE.search(ERR)
    body = {"messages": [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a", "content": "y"},
        {"role": "assistant", "content": "testo"},            # no tool_calls
        {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}],
         "reasoning_content": "gia' presente"},
    ]}
    assert repair_reasoning_replay(body) == 1
    msgs = body["messages"]
    assert msgs[1]["reasoning_content"]                       # riparato
    assert "reasoning_content" not in msgs[3]                 # intatto
    assert msgs[4]["reasoning_content"] == "gia' presente"    # intatto
    assert repair_reasoning_replay(body) == 0                 # idempotente


# -------------------------------------------------------------- non-stream
def test_nonstream_ripara_e_ritenta_lo_stesso_dep(FW, monkeypatch):
    import app.forwarder as F
    a = FW.config.groups[f"{BASE}-32k"][0]
    b = FW.config.groups[f"{BASE}-200k"][0]
    calls = []

    async def fake_call(self, dep, payload, **kw):
        calls.append(dep["unique"])
        if not _has_repair(payload):
            raise UpstreamError(-400, ERR)
        return _resp("RIPARATO")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        return await fwd.call_with_fallback(
            FW, "test", a, _payload(), need=frozenset(), scope="chain",
            ctx=100, attempts_box=[], session="s1", ses="s1", client_ip="",
            attribution=None, requested_group=None)
    data, used = asyncio.run(go())
    assert used["unique"] == a["unique"]       # STESSO dep: mai ruotato
    assert calls == [a["unique"], a["unique"]]
    assert data["choices"][0]["message"]["content"] == "RIPARATO"
    assert a["unique"] not in FW._cooldown     # nessuna penale
    assert b["unique"] not in calls


# --------------------------------------------------------------- streaming
@pytest.fixture()
def SM(tmp_path):
    import app.main as M
    csv = tmp_path / "k.csv"
    csv.write_text(CSV)
    orig = M.config.csv_path
    qj = M.router.policy.qc_json
    pol = M.router.policy
    snap = (qj.stream_hedge_delay_ms, qj.stream_hold_until_finish,
            pol.warm_refill_enabled)
    M.config.csv_path = csv
    M.config.reload()
    qj.stream_hedge_delay_ms = 0
    qj.stream_hold_until_finish = False
    pol.warm_refill_enabled = False        # isola: solo il percorso in prova
    try:
        yield M
    finally:
        (qj.stream_hedge_delay_ms, qj.stream_hold_until_finish,
         pol.warm_refill_enabled) = snap
        M.config.csv_path = orig
        M.config.reload()


def test_streaming_ripara_e_ritenta_lo_stesso_dep(SM, monkeypatch):
    a = SM.config.groups[f"{BASE}-32k"][0]
    b = SM.config.groups[f"{BASE}-200k"][0]
    calls = []

    async def sr(dep, payload, **kw):
        calls.append(dep["unique"])
        if not _has_repair(payload):
            raise UpstreamError(-400, ERR)

        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"RIPARATO"}}]}\n\n'
            yield (b'data: {"choices":[{"delta":{},'
                   b'"finish_reason":"stop"}]}\n\n')
            yield b"data: [DONE]\n\n"
        return gen()
    monkeypatch.setattr(SM.forwarder, "stream_response", sr)
    payload = _payload()
    payload["stream"] = True

    async def go():
        resp = await SM._stream_with_fallback(
            "test", a, payload, scope="chain", session="s1", ses="s1",
            ctx=100)
        body = b""
        if hasattr(resp, "body_iterator"):
            async for c in resp.body_iterator:
                body += c
        return resp, body
    resp, body = asyncio.run(go())
    assert calls == [a["unique"], a["unique"]]   # riparato e ritentato
    assert b"RIPARATO" in body
    assert a["unique"] not in SM.router._cooldown
    assert b["unique"] not in calls


# ------------------------- errore OSCURO: retry con history NON tagliata ----
ORIG = [
    {"role": "user", "content": "leggi /tmp/a.txt"},
    {"role": "assistant", "content": "", "reasoning_content": "penso passo",
     "tool_calls": [{"id": "call_1", "type": "function",
                     "function": {"name": "read", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
]
WEIRD = '{"error":{"message":"upstream hiccup zzz","type":"oops"}}'


def _trimmed_payload():
    import copy
    msgs = copy.deepcopy(ORIG)
    for m in msgs:
        m.pop("reasoning_content", None)      # come dopo histnorm max_chars=0
    return {"model": "m", "stream": False, "max_tokens": 64, "messages": msgs}


def _has_reasoning(payload):
    return any(m.get("reasoning_content")
               for m in payload.get("messages", [])
               if m.get("role") == "assistant")


def test_is_unclear_error():
    assert is_unclear_error(-400, WEIRD)
    assert is_unclear_error(-400, "")                     # vuoto = oscuro
    assert not is_unclear_error(-429, "quota exhausted")
    assert not is_unclear_error(-404, "model not found")
    assert not is_unclear_error(-400, ERR)                # replay: chiaro
    assert not is_unclear_error(-503, "bad gateway")


def test_restore_reasoning_tocca_solo_il_campo():
    body = {"messages": [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
        {"role": "assistant", "content": "altro turno"},   # non in ORIG
    ]}
    assert restore_reasoning(body, ORIG) == 1
    msgs = body["messages"]
    assert msgs[0]["reasoning_content"] == "penso passo"    # solo il campo
    assert "reasoning_content" not in msgs[1]
    assert "reasoning_content" not in msgs[2]
    assert len(msgs) == 3                                   # lista intatta
    assert restore_reasoning(body, None) == 0
    assert restore_reasoning(body, ORIG) == 0               # idempotente


def test_restore_reasoning_dopo_histnorm_che_scarta():
    """History normalizzata piu' corta (turni scartati): il match per firma
    ritrova comunque l'assistant giusto."""
    normalized = {"messages": [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]}
    assert restore_reasoning(normalized, ORIG) == 1
    assert normalized["messages"][0]["reasoning_content"] == "penso passo"


def test_nonstream_errore_oscuro_ripristina_e_ritenta(FW, monkeypatch):
    import app.forwarder as F
    a = FW.config.groups[f"{BASE}-32k"][0]
    calls = []

    async def fake_call(self, dep, payload, **kw):
        calls.append((dep["unique"], _has_reasoning(payload)))
        if not _has_reasoning(payload):
            raise UpstreamError(-400, WEIRD)
        return _resp("RIPRISTINATO")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        return await fwd.call_with_fallback(
            FW, "test", a, _trimmed_payload(), need=frozenset(),
            scope="chain", ctx=100, attempts_box=[], session="s1", ses="s1",
            client_ip="", attribution=None, requested_group=None,
            orig_messages=ORIG)
    data, used = asyncio.run(go())
    assert used["unique"] == a["unique"]          # stesso dep, mai ruotato
    assert calls == [(a["unique"], False), (a["unique"], True)]
    assert data["choices"][0]["message"]["content"] == "RIPRISTINATO"
    assert a["unique"] not in FW._cooldown


def test_nonstream_errore_CHIARO_non_ripristina(FW, monkeypatch):
    """Errore chiaro (quota 429): nessun tentativo con history originale."""
    import app.forwarder as F
    a = FW.config.groups[f"{BASE}-32k"][0]
    seen = []

    async def fake_call(self, dep, payload, **kw):
        seen.append(_has_reasoning(payload))
        raise UpstreamError(-429, '{"error":{"message":"quota exhausted"}}')
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        try:
            return await fwd.call_with_fallback(
                FW, "test", a, _trimmed_payload(), need=frozenset(),
                scope="chain", ctx=100, attempts_box=[], session="s1",
                ses="s1", client_ip="", attribution=None,
                requested_group=None, orig_messages=ORIG)
        except UpstreamError:
            return None
    asyncio.run(go())
    assert seen and not any(seen)      # mai ripristinata


def test_streaming_errore_oscuro_ripristina_e_ritenta(SM, monkeypatch):
    a = SM.config.groups[f"{BASE}-32k"][0]
    b = SM.config.groups[f"{BASE}-200k"][0]
    calls = []

    async def sr(dep, payload, **kw):
        calls.append((dep["unique"], _has_reasoning(payload)))
        if not _has_reasoning(payload):
            raise UpstreamError(-400, WEIRD)

        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"RIPRISTINATO"}}]}\n\n'
            yield (b'data: {"choices":[{"delta":{},'
                   b'"finish_reason":"stop"}]}\n\n')
            yield b"data: [DONE]\n\n"
        return gen()
    monkeypatch.setattr(SM.forwarder, "stream_response", sr)
    payload = _trimmed_payload()
    payload["stream"] = True

    async def go():
        resp = await SM._stream_with_fallback(
            "test", a, payload, scope="chain", session="s1", ses="s1",
            ctx=100, orig_messages=ORIG)
        body = b""
        if hasattr(resp, "body_iterator"):
            async for c in resp.body_iterator:
                body += c
        return resp, body
    resp, body = asyncio.run(go())
    assert calls == [(a["unique"], False), (a["unique"], True)]
    assert b"RIPRISTINATO" in body
    assert a["unique"] not in SM.router._cooldown
    assert b["unique"] not in [c[0] for c in calls]


# ------------------------- PROATTIVO: flag `thinking_replay` sul deployment --
def test_streaming_proattivo_flag_prima_del_primo_invio(SM, monkeypatch):
    """Con `thinking_replay` attivo il reasoning vero viene rimesso PRIMA del
    primo invio: un solo tentativo, nessun 400."""
    a = SM.config.groups[f"{BASE}-32k"][0]
    a["thinking_replay"] = True
    seen = []

    async def sr(dep, payload, **kw):
        seen.append([dict(m) for m in payload["messages"]])
        if not _has_reasoning(payload):
            raise UpstreamError(-400, ERR)

        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"VERO"}}]}\n\n'
            yield (b'data: {"choices":[{"delta":{},'
                   b'"finish_reason":"stop"}]}\n\n')
            yield b"data: [DONE]\n\n"
        return gen()
    monkeypatch.setattr(SM.forwarder, "stream_response", sr)
    payload = _payload()
    payload["stream"] = True

    async def go():
        resp = await SM._stream_with_fallback(
            "test", a, payload, scope="chain", session="s1", ses="s1",
            ctx=100, orig_messages=ORIG)
        body = b""
        if hasattr(resp, "body_iterator"):
            async for c in resp.body_iterator:
                body += c
        return resp, body
    resp, body = asyncio.run(go())
    assert len(seen) == 1                        # gia' corretto al primo colpo
    assert b"VERO" in body
    asst = [m for m in seen[0] if m.get("role") == "assistant"][0]
    assert asst["reasoning_content"] == "penso passo"    # reasoning VERO
