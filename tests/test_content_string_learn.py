"""Schema stretto Cloudflare (content array vs string): impara + ritenta.

Il 400 "'array' not in 'string'" / "required properties at '/messages/N' are
'role,content'" NON e' un deployment rotto: il payload e' RIPARABILE. Il
gateway impara il flag `content_string` (tutti i gemelli del modello),
appiattisce gli array di solo testo e RITENTA LO STESSO deployment. Con media
(immagini/audio) la bonifica non si applica e si ricade sulla rotazione.
"""
import asyncio
import os
import tempfile

import pytest

from app.config import GatewayConfig
from app.forwarder import UpstreamError, apply_content_string
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/cs-a,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-A,,5
t@x.com,m/cs-b,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-B,,5
"""

CF_ERR = ('{"errors":[{"code":5006,"message":"AiError: Bad input: Error: '
          'oneOf at \'/\' not met, 0 matches: required properties at \'/\' are '
          '\'prompt\', Type mismatch of \'/messages/0/content\', \'array\' not '
          'in \'string\', required properties at \'/messages/1\' are '
          '\'role,content\'"}]}')


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


def _payload(media=False):
    if media:
        content = [{"type": "text", "text": "guarda"},
                   {"type": "image_url",
                    "image_url": {"url": "http://x/y.png"}}]
    else:
        content = [{"type": "text", "text": "ciao "},
                   {"type": "text", "text": "mondo"}]
    return {"model": "m", "stream": False, "max_tokens": 64,
            "messages": [
                {"role": "user", "content": content},
                {"role": "assistant", "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "read", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            ]}


def _resp(txt="ok"):
    return {"choices": [{"message": {"role": "assistant", "content": txt},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2}}


def _has_array(payload):
    return any(isinstance(m.get("content"), list)
               for m in payload.get("messages", []))


# -------------------------------------------------------------- non-stream
def test_nonstream_impara_e_ritenta_lo_stesso_dep(FW, monkeypatch):
    import app.forwarder as F
    a = FW.config.groups[f"{BASE}-32k"][0]
    b = FW.config.groups[f"{BASE}-200k"][0]
    calls = []

    async def fake_call(self, dep, payload, **kw):
        body = dict(payload)
        n = apply_content_string(body, dep)       # emula il sender reale
        calls.append((dep["unique"], n))
        if _has_array(body):
            raise UpstreamError(-400, CF_ERR)
        return _resp("BONIFICATO")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        return await fwd.call_with_fallback(
            FW, "test", a, _payload(), need=frozenset(), scope="chain",
            ctx=100, attempts_box=[], session="s1", ses="s1", client_ip="",
            attribution=None, requested_group=None)
    data, used = asyncio.run(go())
    assert used["unique"] == a["unique"]            # STESSO dep: mai ruotato
    assert [u for u, _ in calls] == [a["unique"], a["unique"]]
    assert calls[0][1] == 0 and calls[1][1] >= 1    # 1a: niente, 2a: appiattito
    assert data["choices"][0]["message"]["content"] == "BONIFICATO"
    assert a["unique"] not in FW._cooldown          # nessuna penale
    assert a.get("content_string") is True          # flag imparato
    assert b["unique"] not in [u for u, _ in calls]


def test_nonstream_media_ruota_non_bonifica(FW, monkeypatch):
    import app.forwarder as F
    a = FW.config.groups[f"{BASE}-32k"][0]
    b = FW.config.groups[f"{BASE}-200k"][0]
    calls = []

    async def fake_call(self, dep, payload, **kw):
        body = dict(payload)
        apply_content_string(body, dep)
        calls.append(dep["unique"])
        # solo `a` e' a schema stretto (come Cloudflare): con media non si
        # appiattisce -> a resta rotto e si ruota su `b` (tollerante).
        if dep["unique"] == a["unique"] and _has_array(body):
            raise UpstreamError(-400, CF_ERR)
        return _resp("OK")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    FW.fallback_next = lambda *a, **k: b
    fwd = F.Forwarder()

    async def go():
        return await fwd.call_with_fallback(
            FW, "test", a, _payload(media=True), need=frozenset(),
            scope="chain", ctx=100, attempts_box=[], session="s1", ses="s1",
            client_ip="", attribution=None, requested_group=None)
    data, used = asyncio.run(go())
    assert used["unique"] == b["unique"]            # rotazione su media
    assert calls.count(a["unique"]) == 2            # learn+retry, poi rotazione
    assert calls[-1] == b["unique"]
    assert a.get("content_string") is True


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
    pol.warm_refill_enabled = False
    try:
        yield M
    finally:
        (qj.stream_hedge_delay_ms, qj.stream_hold_until_finish,
         pol.warm_refill_enabled) = snap
        M.config.csv_path = orig
        M.config.reload()


def test_streaming_impara_e_ritenta_lo_stesso_dep(SM, monkeypatch):
    a = SM.config.groups[f"{BASE}-32k"][0]
    b = SM.config.groups[f"{BASE}-200k"][0]
    calls = []

    async def sr(dep, payload, **kw):
        body = dict(payload)
        apply_content_string(body, dep)
        calls.append(dep["unique"])
        if _has_array(body):
            raise UpstreamError(-400, CF_ERR)

        async def gen():
            yield b'data: {"choices":[{"delta":{"content":"BONIFICATO"}}]}\n\n'
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
    assert calls == [a["unique"], a["unique"]]      # bonificato e ritentato
    assert b"BONIFICATO" in body
    assert a["unique"] not in SM.router._cooldown
    assert b["unique"] not in calls
