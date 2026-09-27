"""Golden master della pipeline di fallback (caratterizzazione).

Congela il comportamento VISTO DAL CLIENT di `/v1/chat/completions` (stream e
non-stream) su una matrice di scenari di upstream: status, header rilevanti,
body completo (normalizzato: id/timestamp) e sequenza dei deployment
tentati. Serve come rete di sicurezza per i refactoring di
`_stream_with_fallback` e `Forwarder.call_with_fallback`: qualunque
differenza osservabile fa fallire il test.

Rigenerare il riferimento SOLO per un cambio di comportamento voluto:
    GOLDEN_UPDATE=1 pytest tests/golden/test_fallback_golden.py
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
from pathlib import Path

import pytest

import app.state as gw_state

GOLDEN = Path(__file__).with_name("fallback_golden.json")
MK = "test-master-golden"
PROFILE = "gold"

CSV = (
    "commento,modello,provider,endpoint,data,context,max_input,priority,"
    f"scrocco-llm-{PROFILE},caps,intelligence_score\n"
    "a@x.com,m/alpha,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-A1,,5\n"
    "b@x.com,m/beta,mistral,https://api.mistral.ai/v1,free,32,32000,5,K-B1,,5\n"
    "c@x.com,m/gamma,openrouter,https://openrouter.ai/api/v1,free,32,32000,5,K-C1,,5\n"
    "e@x.com,m/epsilon,cerebras,https://api.cerebras.ai/v1,free,32,32000,5,K-E1,,5\n"
    "f@x.com,m/zeta,mistral,https://api.mistral.ai/v1,free,32,32000,5,K-F1,,5\n"
    "d@x.com,m/delta,groq,https://api.groq.com/openai/v1,free,128,128000,5,K-D1,,5\n"
    "g@x.com,m/eta,openrouter,https://openrouter.ai/api/v1,free,128,128000,5,K-G1,,5\n"
)


# ------------------------------------------------------------ upstream fakes
def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _delta(**d):
    return _sse({"choices": [{"index": 0, "delta": d, "finish_reason": None}]})


def _finish(reason="stop", usage=True):
    o = {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}
    if usage:
        o["usage"] = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    return _sse(o)


DONE = b"data: [DONE]\n\n"

STREAMS = {
    "ok": [_delta(role="assistant"), _delta(content="Ciao "), _delta(content="mondo"), _finish(), DONE],
    "empty": [],
    "reasoning_only": [_delta(reasoning_content="penso..."), _finish(), DONE],
    "reasoning_then_answer": [_delta(reasoning_content="penso"), _delta(content="42"), _finish(), DONE],
    "length_empty": [_finish("length"), DONE],
    "length_partial": [_delta(content="parziale"), _finish("length"), DONE],
    "tool_call": [
        _delta(role="assistant", tool_calls=[{"index": 0, "id": "call_1", "type": "function",
                                              "function": {"name": "get_weather", "arguments": ""}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": "{\"city\":"}}]),
        _delta(tool_calls=[{"index": 0, "function": {"arguments": " \"Roma\"}"}}]),
        _finish("tool_calls"), DONE],
    "error_chunk": [_sse({"error": {"message": "provider overloaded", "code": 503}}), DONE],
    "json_ok": [_delta(content="{\"a\": 1}"), _finish(), DONE],
    "json_bad": [_delta(content="{\"a\": 1"), _finish(), DONE],
    "think_tags": [_delta(content="<think>hmm</think>Risposta"), _finish(), DONE],
    "no_done": [_delta(content="senza done"), _finish()],
    "many_chunks": [_delta(role="assistant")] + [_delta(content=f"t{i} ") for i in range(40)] + [_finish(), DONE],
}
SLOW = "slow"                      # primo byte dopo 1.2s (oltre la deadline breve)

MIDSTREAM = "midstream_error"      # contenuto poi eccezione a meta' stream


def _msg(content=None, tool_calls=None, finish="stop", reasoning=None):
    m = {"role": "assistant", "content": content}
    if tool_calls:
        m["tool_calls"] = tool_calls
    if reasoning:
        m["reasoning_content"] = reasoning
    return {"id": "chatcmpl-up", "object": "chat.completion", "created": 1,
            "model": "up", "choices": [{"index": 0, "message": m, "finish_reason": finish}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}


CALLS = {
    "ok": _msg("Ciao mondo"),
    "empty": _msg(""),
    "none_content": _msg(None),
    "reasoning_only": _msg("", reasoning="penso..."),
    "length_empty": _msg("", finish="length"),
    "tool_call": _msg(None, tool_calls=[{"id": "call_1", "type": "function",
                                          "function": {"name": "get_weather",
                                                       "arguments": "{\"city\": \"Roma\"}"}}],
                      finish="tool_calls"),
    "json_ok": _msg("{\"a\": 1}"),
    "json_bad": _msg("{\"a\": 1"),
    "think_tags": _msg("<think>hmm</think>Risposta"),
}

ERRORS = {  # status, detail
    "e429": (429, '{"error":{"message":"rate limit exceeded","type":"rate_limit"}}'),
    "e500": (500, '{"error":{"message":"internal error"}}'),
    "e503": (503, '{"error":{"message":"service unavailable"}}'),
    "e401": (401, '{"error":{"message":"invalid api key"}}'),
    "e400": (-400, '{"error":{"message":"bad request: unsupported parameter foo"}}'),
    "e404model": (404, '{"error":{"message":"The model `m/x` does not exist"}}'),
    "enone": (None, "connect timeout"),
}


def _plan_iter(plan):
    it = iter(plan)
    last = plan[-1] if plan else "ok"
    while True:
        yield next(it, last)


# ----------------------------------------------------------------- scenarios
BASE_MSGS = [{"role": "user", "content": "ciao"}]
TOOLS = [{"type": "function", "function": {"name": "get_weather", "parameters": {
    "type": "object", "properties": {"city": {"type": "string"}}}}}]

SCENARIOS = []
for stream in (True, False):
    table = STREAMS if stream else CALLS
    plans = [
        ["ok"], ["empty", "ok"], ["reasoning_only", "ok"], ["length_empty", "ok"],
        ["e429", "ok"], ["e500", "e500", "ok"], ["e503", "ok"], ["e401", "ok"],
        ["e400", "ok"], ["e404model", "ok"], ["enone", "ok"],
        ["e429"], ["e500"], ["empty"], ["length_empty"], ["reasoning_only"],
        ["think_tags"], ["tool_call"],
    ]
    if stream:
        plans += [["reasoning_then_answer"], ["length_partial"], ["error_chunk", "ok"],
                  [MIDSTREAM], ["no_done"], ["error_chunk"]]
    else:
        plans += [["none_content", "ok"]]
    for plan in plans:
        SCENARIOS.append({"stream": stream, "plan": plan, "extra": {}})
        if stream and len(plan) > 1:
            SCENARIOS.append({"stream": stream, "plan": plan, "extra": {}, "variant": "nohold"})
    SCENARIOS.append({"stream": stream, "plan": ["tool_call"], "extra": {"tools": TOOLS}})
    SCENARIOS.append({"stream": stream, "plan": ["json_ok"],
                      "extra": {"response_format": {"type": "json_object"}}})
    SCENARIOS.append({"stream": stream, "plan": ["json_bad", "json_ok"],
                      "extra": {"response_format": {"type": "json_object"}}})
    SCENARIOS.append({"stream": stream, "plan": ["ok"], "extra": {"max_tokens": 50, "temperature": 0.3}})
    # catene lunghe (tutti i deployment falliscono in modi diversi)
    SCENARIOS.append({"stream": stream, "plan": ["e429", "e500", "empty", "e401", "enone", "e503", "ok"], "extra": {}})
    SCENARIOS.append({"stream": stream, "plan": ["e429", "e500", "empty", "e401", "enone", "e503", "e429", "e500"],
                      "extra": {}})
    # gruppo esplicito e sessione: 3 richieste consecutive (sticky/cache holder)
    SCENARIOS.append({"stream": stream, "plan": ["ok"], "extra": {}, "model": f"scrocco-llm-{PROFILE}-128k"})
    SCENARIOS.append({"stream": stream, "plan": ["ok", "e500", "ok", "ok"], "extra": {}, "requests": 3})
    SCENARIOS.append({"stream": stream, "plan": ["e429", "ok", "e429", "ok", "ok"], "extra": {}, "requests": 3})
SCENARIOS.append({"stream": True, "plan": ["many_chunks"], "extra": {}})
SCENARIOS.append({"stream": True, "plan": ["many_chunks"], "extra": {}, "variant": "nohold"})
SCENARIOS.append({"stream": True, "plan": [SLOW, "ok"], "extra": {}, "variant": "deadline"})
SCENARIOS.append({"stream": True, "plan": [SLOW], "extra": {}, "variant": "deadline"})
SCENARIOS.append({"stream": True, "plan": [MIDSTREAM], "extra": {}, "variant": "nohold"})
SCENARIOS.append({"stream": True, "plan": ["length_partial", "ok"], "extra": {}, "variant": "nohold"})


def _sid(sc):
    ex = "+".join(sorted(sc["extra"])) or "plain"
    tail = "".join(f":{k}={sc[k]}" for k in ("variant", "model", "requests") if sc.get(k))
    return f"{'stream' if sc['stream'] else 'json'}:{'>'.join(sc['plan'])}:{ex}{tail}"


# ------------------------------------------------------------- normalization
_NORM = [
    (re.compile(r'"id":\s*"(chatcmpl|resp|call)[^"]*"'), r'"id":"<\1-id>"'),
    (re.compile(r'"created":\s*\d+'), '"created":0'),
    (re.compile(r'"(latency_ms|ttfb_ms|duration_ms|elapsed_ms|ms)":\s*[0-9.]+'), r'"\1":0'),
    (re.compile(r"\b\d{13}\b"), "<ts-ms>"),
    (re.compile(r"\b\d{10}(\.\d+)?\b"), "<ts>"),
]


def _norm(text: str) -> str:
    for rx, rep in _NORM:
        text = rx.sub(rep, text)
    return text


_HEADERS = ("content-type", "retry-after", "x-scrocco-trail", "x-scrocco-deployment",
            "x-scrocco-model", "x-scrocco-group", "x-scrocco-fallbacks")


# ------------------------------------------------------------------ harness
@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    import app.main as m
    from fastapi.testclient import TestClient

    from app.ledger import Ledger
    from app.router import Router

    tmp = tmp_path_factory.mktemp("golden")
    csv = tmp / "k.csv"
    csv.write_text(CSV)
    mp = pytest.MonkeyPatch()
    mp.setattr(gw_state.authn, "master_key", MK)
    mp.setattr(gw_state, "LEDGER", Ledger(tmp))
    mp.setattr(gw_state.config, "csv_path", csv)
    gw_state.config.reload()
    orig_router = gw_state.router
    yield m, TestClient(m.app), mp, orig_router
    mp.undo()
    gw_state.router = orig_router
    gw_state.config.reload()


def _run(gateway, sc, monkeypatch):
    m, client, _mp, orig_router = gateway
    from app.forwarder import UpstreamError
    from app.router import Router

    random.seed(1234)
    router = Router(gw_state.config, orig_router.policy)
    monkeypatch.setattr(gw_state, "router", router)
    pol = router.policy
    for k, v in (("warm_refill_enabled", False), ("request_coalescing_enabled", False)):
        monkeypatch.setattr(pol, k, v)
    monkeypatch.setattr(pol.qc_json, "stream_hedge_delay_ms", 0)
    variant = sc.get("variant")
    if variant == "nohold":
        monkeypatch.setattr(pol.qc_json, "stream_hold_until_finish", False)
    if variant == "deadline":
        monkeypatch.setattr(pol.qc_json, "stream_first_content_ms", 250)
    attempts: list[str] = []
    plan = _plan_iter(sc["plan"])

    def _maybe_raise(kind):
        if kind in ERRORS:
            st, det = ERRORS[kind]
            raise UpstreamError(st, det)

    async def stream_response(dep, payload, **kw):
        kind = next(plan)
        attempts.append(f"{dep['model']}:{kind}")
        _maybe_raise(kind)
        chunks = STREAMS.get(kind, [])

        async def gen():
            if kind == SLOW:
                await asyncio.sleep(1.2)
                for c in STREAMS["ok"]:
                    yield c
                return
            if kind == MIDSTREAM:
                yield _delta(content="inizio ")
                raise UpstreamError(502, "connection reset mid-stream")
            for c in chunks:
                yield c
        return gen()

    async def call(dep, payload, **kw):
        kind = next(plan)
        attempts.append(f"{dep['model']}:{kind}")
        _maybe_raise(kind)
        return json.loads(json.dumps(CALLS.get(kind, CALLS["ok"])))

    monkeypatch.setattr(gw_state.forwarder, "stream_response", stream_response)
    monkeypatch.setattr(gw_state.forwarder, "call", call)
    out = []
    for i in range(sc.get("requests", 1)):
        msgs = BASE_MSGS + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": f"ancora {i}"}] * i
        body = {"model": sc.get("model") or f"scrocco-llm-{PROFILE}", "messages": msgs,
                "stream": sc["stream"], **sc["extra"]}
        r = client.post("/v1/chat/completions", json=body,
                        headers={"Authorization": f"Bearer {MK}", "X-Session-Id": "golden"})
        hdrs = {h: _norm(r.headers[h]) for h in _HEADERS if h in r.headers}
        cooled = sorted(u.split("__")[1] if "__" in u else u for u in router._cooldown)
        out.append({"status": r.status_code, "headers": hdrs, "body": _norm(r.text),
                    "attempts": list(attempts), "cooled": cooled})
    return out[0] if len(out) == 1 else out


def test_fallback_pipeline_matches_golden(gateway, monkeypatch):
    got = {}
    for sc in SCENARIOS:
        with monkeypatch.context() as mp:
            got[_sid(sc)] = _run(gateway, sc, mp)
    if os.environ.get("GOLDEN_UPDATE") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
        pytest.skip("golden rigenerato")
    want = json.loads(GOLDEN.read_text())
    assert sorted(got) == sorted(want)
    diffs = [k for k in want if got[k] != want[k]]
    assert not diffs, "scenari cambiati:\n" + "\n".join(
        f"- {k}\n  atteso: {json.dumps(want[k], ensure_ascii=False)[:600]}\n"
        f"  ottenuto: {json.dumps(got[k], ensure_ascii=False)[:600]}" for k in diffs[:5])
