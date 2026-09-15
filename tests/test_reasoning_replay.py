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
