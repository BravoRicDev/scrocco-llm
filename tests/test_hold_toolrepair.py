"""Tool repair sotto HOLD: parita' stream/non-stream.

Con `hold_until_finish` la risposta e' INTERAMENTE bufferizzata -> si applica
la STESSA riparazione del percorso non-streaming (`repair_tool_calls`) + la
pulizia del contenuto, sull'output GREZZO totale (il filtro SSE incrementale
NON gira: `defer_tool_repair`). Poi lo stream bufferizzato viene riscritto.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

import app.main as M
from app import repairlog
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

_HDR = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n")
_GOOD = "a,good,groq,https://ok.test/v1,free,128,8000,5,K-G,\n"
_ARTIFACT = "<previous_reasoning_empty/>Done."


@pytest.fixture
def rep(tmp_path):
    old = repairlog.REPAIRLOG.path
    repairlog.configure(str(tmp_path))
    repairlog.REPAIRLOG._buf = []
    yield repairlog
    repairlog.flush_sync()
    repairlog.REPAIRLOG.path = old
    repairlog.REPAIRLOG._buf = []


def _mk(csv_text, hold=True):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_text)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    pol.cooldown_jitter_ratio = 0
    pol.qc_json.stream_hold_until_finish = hold
    pol.qc_json.stream_commit_min_chars = 4
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, pol)
    os.unlink(path)
    by_key = {}
    for deps in cfg.groups.values():
        for d in deps:
            by_key[d["api_key"]] = d
    return cfg, router, by_key


def _chunk(obj):
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _sse(content, finish="stop"):
    out = []
    if content is not None:
        out.append(_chunk({"choices": [
            {"index": 0, "delta": {"content": content},
             "finish_reason": None}]}))
    out.append(_chunk({"choices": [
        {"index": 0, "delta": {}, "finish_reason": finish}]}))
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


def _sse_toolcall(name, args, finish="tool_calls"):
    """SSE con una tool-call i cui argomenti arrivano frammentati."""
    out = []
    out.append(_chunk({"choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                        "function": {"name": name, "arguments": ""}}]},
        "finish_reason": None}]}))
    out.append(_chunk({"choices": [{"index": 0, "delta": {
        "tool_calls": [{"index": 0,
                        "function": {"arguments": args}}]},
        "finish_reason": None}]}))
    out.append(_chunk({"choices": [
        {"index": 0, "delta": {}, "finish_reason": finish}]}))
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


class _FakeFwd:
    def __init__(self, by_unique):
        self.by_unique = by_unique
        self.calls = []
        self.kwargs = []
        self.payload_snapshot = []

    async def stream_response(self, d, payload, **kwargs):
        self.calls.append(d["unique"])
        self.kwargs.append(kwargs)
        self.payload_snapshot.append(
            {"messages": [dict(m) for m in payload.get("messages", [])]})
        seq = self.by_unique[d["unique"]]
        item = seq[min(len(self.calls) - 1, len(seq) - 1)]
        if isinstance(item, Exception):
            raise item

        async def _g():
            yield item

        return _g()


def _stream(monkeypatch, cfg, router, fwd, first, payload):
    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "config", cfg)
    monkeypatch.setattr(M, "forwarder", fwd)

    async def _run():
        resp = await M._stream_with_fallback(
            "test", first, payload, need=frozenset({"text"}), scope="chain")
        out = b""
        async for chunk in resp.body_iterator:
            out += chunk if isinstance(chunk, bytes) else chunk.encode()
        return out

    return asyncio.run(_run())


def _iter_deltas(raw):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    for line in raw.split("\n"):
        s = line.strip()
        if not s.startswith("data:"):
            continue
        body = s[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            obj = json.loads(body)
        except Exception:
            continue
        for ch in (obj.get("choices") or []):
            yield ch


def _content_of(raw) -> str:
    txt = ""
    for ch in _iter_deltas(raw):
        c = (ch.get("delta") or {}).get("content")
        if isinstance(c, str):
            txt += c
    return txt


def _toolcall_args_of(raw) -> str:
    args = ""
    for ch in _iter_deltas(raw):
        for tc in ((ch.get("delta") or {}).get("tool_calls") or []):
            fn = tc.get("function") or {}
            if isinstance(fn.get("arguments"), str):
                args += fn["arguments"]
    return args


def _finish_of(raw):
    for ch in _iter_deltas(raw):
        if ch.get("finish_reason"):
            return ch["finish_reason"]
    return None


def _rows():
    repairlog.flush_sync()
    return repairlog.read_all()


def _payload(tools=True, content="cerca"):
    p = {"model": "x", "messages": [{"role": "user", "content": content}]}
    if tools:
        p["tools"] = [{"type": "function", "function": {
            "name": "search", "parameters": {"type": "object"}}}]
    return p


# --------------------------------------------------------------- deferral ---
def test_hold_defers_sse_tool_repair(rep, monkeypatch):
    cfg, router, by_key = _mk(_HDR + _GOOD)
    good = by_key["K-G"]
    fwd = _FakeFwd({good["unique"]: [_sse("ciao")]})
    _stream(monkeypatch, cfg, router, fwd, good, _payload())
    assert fwd.kwargs and fwd.kwargs[0]["defer_tool_repair"] is True


def test_no_hold_non_defer(rep, monkeypatch):
    cfg, router, by_key = _mk(_HDR + _GOOD, hold=False)
    good = by_key["K-G"]
    fwd = _FakeFwd({good["unique"]: [_sse("ciao")]})
    _stream(monkeypatch, cfg, router, fwd, good, _payload())
    assert fwd.kwargs and fwd.kwargs[0]["defer_tool_repair"] is False


# ------------------------------------------------------- whole-output repair
def test_hold_repairs_tool_call_on_raw_output(rep, monkeypatch):
    cfg, router, by_key = _mk(_HDR + _GOOD)
    good = by_key["K-G"]
    raw = _sse_toolcall("search", '{"q": "hello",}')
    fwd = _FakeFwd({good["unique"]: [raw]})
    out = _stream(monkeypatch, cfg, router, fwd, good, _payload())
    assert json.loads(_toolcall_args_of(out)) == {"q": "hello"}
    assert _finish_of(out) == "tool_calls"
    assert b"[DONE]" in out
    row = next((r for r in _rows()
                if r["kind"] == "repair_args" and r["source"] == "stream"), None)
    assert row and row["outcome"] == "ok"


def test_hold_sanitizes_content_artifact(rep, monkeypatch):
    cfg, router, by_key = _mk(_HDR + _GOOD)
    good = by_key["K-G"]
    raw = _sse("fatto.\n\n" + _ARTIFACT * 3)
    fwd = _FakeFwd({good["unique"]: [raw]})
    out = _stream(monkeypatch, cfg, router, fwd, good, _payload(tools=False))
    assert _content_of(out) == "fatto."
    assert _finish_of(out) == "stop"


def test_hold_clean_output_untouched(rep, monkeypatch):
    cfg, router, by_key = _mk(_HDR + _GOOD)
    good = by_key["K-G"]
    raw = _sse("risposta pulita")
    fwd = _FakeFwd({good["unique"]: [raw]})
    out = _stream(monkeypatch, cfg, router, fwd, good, _payload(tools=False))
    assert _content_of(out) == "risposta pulita"


def test_corrective_kind_toolcall_vs_json():
    from app.forwarder import _corrective_kind
    assert _corrective_kind(
        "tool_calls.function.arguments JSON non valido (x)") == "toolcall"
    assert _corrective_kind(
        "tool_calls.function.arguments non è una stringa") == "toolcall"
    assert _corrective_kind("JSON non valido (x)") == "json"
    assert _corrective_kind(None) == "json"


def test_hold_unrepairable_toolcall_uses_toolcall_note(rep, monkeypatch):
    """Argomenti tool-call NON riparabili -> il retry correttivo deve chiedere
    di riemettere la tool-call col MECCANISMO previsto (non 'solo JSON'), e il
    client deve continuare a ricevere una tool-call."""
    cfg, router, by_key = _mk(_HDR + _GOOD)
    good = by_key["K-G"]
    # 1o tentativo: argomenti con doppia virgola (non riparabili)
    bad = _sse_toolcall("search", '{"alias":"x",,"command":"y"}')
    # 2o tentativo (dopo la nota correttiva): tool-call VALIDA
    fixed = _sse_toolcall("search", '{"alias":"x","command":"y"}')
    fwd = _FakeFwd({good["unique"]: [bad, fixed]})
    out = _stream(monkeypatch, cfg, router, fwd, good, _payload())
    assert fwd.calls == [good["unique"], good["unique"]]
    note = [m["content"] for m in fwd.payload_snapshot[-1]["messages"]
            if m.get("role") == "system"][-1]
    assert "meccanismo di tool-call" in note
    assert "SOLO con un oggetto JSON" not in note
    assert json.loads(_toolcall_args_of(out)) == {"alias": "x", "command": "y"}
    assert _finish_of(out) == "tool_calls"
    row = next((r for r in _rows()
                if r["kind"] == "struct_corrective"
                and r["source"] == "stream"), None)
    assert row and row["detail"] == "toolcall"
