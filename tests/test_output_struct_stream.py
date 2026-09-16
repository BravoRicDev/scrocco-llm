"""Riparazione dell'OUTPUT STRUTTURATO nel percorso STREAMING con HOLD attivo.

Con `hold_until_finish` la risposta e' INTERAMENTE bufferizzata -> viene
trattata come non-streaming: pulizia (A) / riparazione schema-driven (D) prima
di inviare qualunque byte. Rotazione e retry correttivo restano trasparenti al
client (nessun JSON sporco esce mai).
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
_BROKEN = "a,broken,cloudflare,https://cf.test/v1,free,128,8000,9,K-B,\n"
_SCHEMA = {"type": "object", "required": ["n"],
           "properties": {"n": {"type": "integer"}}}
_RF_JSON = {"type": "json_object"}
_RF_SCHEMA = {"type": "json_schema", "json_schema": {"schema": _SCHEMA}}


@pytest.fixture
def rep(tmp_path):
    old = repairlog.REPAIRLOG.path
    repairlog.configure(str(tmp_path))
    repairlog.REPAIRLOG._buf = []
    yield repairlog
    repairlog.flush_sync()
    repairlog.REPAIRLOG.path = old
    repairlog.REPAIRLOG._buf = []


def _mk(csv_text, hold=True, strict=False, corrective=True):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_text)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    pol.cooldown_jitter_ratio = 0
    pol.qc_json.stream_hold_until_finish = hold
    pol.qc_json.stream_commit_min_chars = 4
    pol.qc_json.strict_schema = strict
    pol.corrective_retry_enabled = corrective
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


class _FakeFwd:
    """SSE finte per dep; tiene conto delle chiamate per dep."""

    def __init__(self, by_unique):
        self.by_unique = by_unique      # unique -> list[bytes|Exception]
        self.calls = []

    async def stream_response(self, d, payload, **kwargs):
        n = self.calls.count(d["unique"])
        self.calls.append(d["unique"])
        seq = self.by_unique[d["unique"]]
        item = seq[n] if n < len(seq) else seq[-1]
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


def _rows():
    repairlog.flush_sync()
    return repairlog.read_all()


def _row(rows, kind):
    return next((r for r in rows if r["kind"] == kind), None)


def _content_of(raw) -> str:
    """Concatena i delta.content di uno stream SSE (choices[0])."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    txt = ""
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
            d = ch.get("delta") or {}
            c = d.get("content")
            if isinstance(c, str):
                txt += c
    return txt


# ---------------------------------------------------------------------- unit
def test_collapse_sse_content_sostituisce_una_volta():
    chunks = [
        _chunk({"choices": [{"index": 0, "delta": {"content": "sporco"},
                             "finish_reason": None}]}),
        _chunk({"choices": [{"index": 0, "delta": {"content": "extra"},
                             "finish_reason": None}]}),
        _chunk({"choices": [{"index": 0, "delta": {},
                             "finish_reason": "stop"}]}),
        b"data: [DONE]\n\n",
    ]
    out = M._collapse_sse_content(chunks, '{"a": 1}')
    joined = b"".join(out).decode()
    assert _content_of(b"".join(out)) == '{"a": 1}'
    assert "sporco" not in joined and "extra" not in joined
    assert '"finish_reason": "stop"' in joined
    assert "[DONE]" in joined


# ------------------------------------------------------------------- e2e ---
def test_stream_cleaned_pulisce_e_traccia(rep, monkeypatch):
    cfg, router, by_key = _mk(_HDR + _GOOD)
    good = by_key["K-G"]
    fwd = _FakeFwd({good["unique"]: [_sse('```json\n{"a": 1}\n```')]})
    payload = {"model": "x",
               "messages": [{"role": "user", "content": "dammi json"}],
               "response_format": _RF_JSON}
    out = _stream(monkeypatch, cfg, router, fwd, good, payload)
    assert _content_of(out) == '{"a": 1}'
    row = _row(_rows(), "struct_cleaned")
    assert row and row["family"] == "struct"
    assert row["source"] == "stream" and row["outcome"] == "ok"


def test_stream_repaired_riscrive_e_traccia(rep, monkeypatch):
    cfg, router, by_key = _mk(_HDR + _GOOD, strict=True)
    good = by_key["K-G"]
    fwd = _FakeFwd({good["unique"]: [_sse('{"n": "7"}')]})
    payload = {"model": "x",
               "messages": [{"role": "user", "content": "dammi json"}],
               "response_format": _RF_SCHEMA}
    out = _stream(monkeypatch, cfg, router, fwd, good, payload)
    assert _content_of(out) == '{"n": 7}'
    row = _row(_rows(), "struct_repaired")
    assert row and row["outcome"] == "ok"
    assert "coerce_int" in row["detail"]


def test_stream_invalid_ruota_senza_cooldown(rep, monkeypatch):
    cfg, router, by_key = _mk(_HDR + _BROKEN + _GOOD, strict=True,
                              corrective=False)
    broken, good = by_key["K-B"], by_key["K-G"]
    fwd = _FakeFwd({broken["unique"]: [_sse('{"n": "zzz"}')],
                    good["unique"]: [_sse('{"n": 7}')]})
    payload = {"model": "x",
               "messages": [{"role": "user", "content": "dammi json"}],
               "response_format": _RF_SCHEMA}
    out = _stream(monkeypatch, cfg, router, fwd, broken, payload)
    assert _content_of(out) == '{"n": 7}'
    assert good["unique"] in fwd.calls
    row = _row(_rows(), "struct_invalid")
    assert row and row["outcome"] == "fail" and row["dep"] == broken["unique"]
    assert router.is_cooled_down(broken["unique"]) is False


def test_stream_corrective_retry_salva(rep, monkeypatch):
    cfg, router, by_key = _mk(_HDR + _GOOD, strict=True, corrective=True)
    good = by_key["K-G"]
    fwd = _FakeFwd({good["unique"]: [_sse('{"n": "zzz"}'),
                                     _sse('{"n": "7"}')]})
    payload = {"model": "x",
               "messages": [{"role": "user", "content": "dammi json"}],
               "response_format": _RF_SCHEMA}
    out = _stream(monkeypatch, cfg, router, fwd, good, payload)
    assert _content_of(out) == '{"n": 7}'
    assert fwd.calls.count(good["unique"]) == 2      # retry correttivo
    rows = _rows()
    assert _row(rows, "struct_corrective")
    assert _row(rows, "struct_repaired")


def test_stream_senza_hold_non_ripara(rep, monkeypatch):
    cfg, router, by_key = _mk(_HDR + _GOOD, hold=False)
    good = by_key["K-G"]
    fwd = _FakeFwd({good["unique"]: [_sse('```json\n{"a": 1}\n```')]})
    payload = {"model": "x",
               "messages": [{"role": "user", "content": "dammi json"}],
               "response_format": _RF_JSON}
    out = _stream(monkeypatch, cfg, router, fwd, good, payload)
    assert "```" in _content_of(out)                  # NON riparato
    assert not [r for r in _rows() if r["family"] == "struct"]
