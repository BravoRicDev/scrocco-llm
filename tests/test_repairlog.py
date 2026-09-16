"""Test del tracking delle riparazioni tool-call (log a schermo + persistente)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import httpx
import pytest

from app import repairlog
from app.config import GatewayConfig
from app.forwarder import Forwarder
from app.policy import Policy
from app.router import Router
from app.toolrepair import (ToolRepairConfig, ToolRepairSSEFilter,
                            repair_tool_calls)


@pytest.fixture
def rep(tmp_path):
    """Configura il singleton su una dir temporanea e ripulisce al termine."""
    old = repairlog.REPAIRLOG.path
    repairlog.configure(str(tmp_path))
    repairlog.REPAIRLOG._buf = []          # isola da altri test
    yield repairlog, tmp_path
    repairlog.flush_sync()
    repairlog.REPAIRLOG.path = old
    repairlog.REPAIRLOG._buf = []


# ------------------------------------------------------------------ unit --
def test_family_mapping():
    assert repairlog.FAMILY["repair_args"] == "repair"
    assert repairlog.FAMILY["repair_trunc_close"] == "repair"
    assert repairlog.FAMILY["salvage_text"] == "salvage"
    assert repairlog.FAMILY["salvage_truncated"] == "salvage"
    assert repairlog.FAMILY["struct_cleaned"] == "struct"
    assert repairlog.FAMILY["struct_repaired"] == "struct"
    assert repairlog.FAMILY["struct_invalid"] == "struct"
    assert repairlog.FAMILY["struct_corrective"] == "struct"
    assert "struct" in repairlog.FAMILIES


def test_note_persiste_e_logga(rep, caplog):
    _rl, tmp = rep
    with caplog.at_level("INFO"):
        repairlog.note("repair_args", source="stream", outcome="ok",
                       dep="d1", model="m1", detail="moves=['x']", count=2)
    repairlog.flush_sync()
    p = tmp / "repair_ledger.jsonl"
    assert p.exists()
    rows = [json.loads(x) for x in p.read_text().splitlines() if x]
    assert len(rows) == 1
    assert rows[0]["family"] == "repair"
    assert rows[0]["kind"] == "repair_args"
    assert rows[0]["count"] == 2
    assert any("outcome=ok" in m and "repair_args" in m for m in caplog.messages)


def test_note_fail_non_solleva(rep, caplog):
    _rl, _ = rep
    with caplog.at_level("WARNING"):
        repairlog.note("salvage_truncated", source="nostream", outcome="fail",
                       dep="d2")
    repairlog.flush_sync()
    assert any("outcome=fail" in m for m in caplog.messages)


def test_aggregate_somma_famiglie_e_esiti(rep):
    _rl, _ = rep
    repairlog.note("repair_args", source="stream", outcome="ok", dep="a",
                   count=3)
    repairlog.note("repair_args", source="nostream", outcome="fail", dep="b")
    repairlog.note("salvage_text", source="stream", outcome="ok", dep="a",
                   count=2)
    repairlog.flush_sync()
    agg = repairlog.aggregate()
    assert agg["by_family"]["repair"] == 4
    assert agg["by_family"]["salvage"] == 2
    assert agg["outcomes"] == {"ok": 5, "fail": 1}
    assert agg["by_kind"]["repair_args"] == 4
    assert agg["top_dep"]["a"] == 5


# ------------------------------------------------------------ integrazione --
def _dep():
    return {"unique": "test-dep", "model": "m", "provider": "p",
            "api_key": "k", "api_base": "https://example.invalid/v1"}


def _payload():
    return {"tools": [{"type": "function",
                       "function": {"name": "f", "parameters": {}}}]}


def test_non_stream_repair_tracciata(rep):
    _rl, _ = rep
    data = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "f", "arguments": '{"a": 1,}'}}]}}]}
    res = repair_tool_calls(data, _payload(), _dep(), ToolRepairConfig())
    assert res["repaired"] is True
    repairlog.flush_sync()
    rows = repairlog.read_all()
    assert rows and rows[-1]["kind"] == "repair_args"
    assert rows[-1]["source"] == "nostream"
    assert rows[-1]["outcome"] == "ok"


def _sse(obj) -> bytes:
    return ("data: " + json.dumps(obj) + "\n\n").encode()


def test_stream_repair_tracciata(rep):
    _rl, _ = rep
    f = ToolRepairSSEFilter(ToolRepairConfig(), _dep())
    f.feed(_sse({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "c1", "type": "function",
         "function": {"name": "f", "arguments": '{"a": 1,'}}]}}]}))
    f.feed(_sse({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "}"}}]},
        "finish_reason": "tool_calls"}]}))
    f.finalize()
    repairlog.flush_sync()
    rows = repairlog.read_all()
    assert any(r["kind"] == "repair_args" and r["source"] == "stream"
               and r["outcome"] == "ok" for r in rows)


def test_stream_trunc_close_tracciata(rep):
    _rl, _ = rep
    f = ToolRepairSSEFilter(ToolRepairConfig(), _dep())
    f.feed(_sse({"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "c1", "type": "function",
         "function": {"name": "f", "arguments": '{"a": '}}]}}]}))
    f.abort_finalize()
    repairlog.flush_sync()
    rows = repairlog.read_all()
    assert any(r["kind"] == "repair_trunc_close"
               and r["outcome"] == "abort" for r in rows)


# ------------------------------------------------------- output strutturato --
_HDR = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n")
_ROW_GOOD = "a,good,groq,https://ok.test/v1,paid,128,8000,5,K2,text\n"
_ROW_BROKEN = "a,broken,cloudflare,https://cf.test/v1,paid,128,8000,5,K1,\n"
_GRP = "scrocco-llm-test-fallback"
_SCHEMA = {"type": "object", "required": ["n"],
           "properties": {"n": {"type": "integer"}}}


def _mk_router(csv_text, *, strict=False, corrective=True):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(csv_text)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    pol.cooldown_jitter_ratio = 0
    pol.qc_json.strict_schema = strict
    pol.corrective_retry_enabled = corrective
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, pol)
    os.unlink(path)
    return router


def _resp(content):
    return httpx.Response(200, json={"choices": [
        {"message": {"content": content}, "finish_reason": "stop"}]})


def _call(router, handler, payload, first):
    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    return asyncio.run(
        fwd.call_with_fallback(router, "test", first, payload))


def _row_for(rows, kind):
    return next((r for r in rows if r["kind"] == kind), None)


def test_struct_cleaned_tracciata(rep):
    """Content con fence JSON -> pulito e registrato come struct_cleaned."""
    _rl, _ = rep
    router = _mk_router(_HDR + _ROW_GOOD)
    first = router.config.groups[_GRP][0]

    def handler(_request):
        return _resp('```json\n{"a": 1}\n```')

    payload = {"model": "x",
               "messages": [{"role": "user", "content": "dammi json"}],
               "response_format": {"type": "json_object"}}
    data, _used = _call(router, handler, payload, first)
    assert data["choices"][0]["message"]["content"] == '{"a": 1}'
    repairlog.flush_sync()
    row = _row_for(repairlog.read_all(), "struct_cleaned")
    assert row and row["family"] == "struct"
    assert row["source"] == "nostream" and row["outcome"] == "ok"


def test_struct_repaired_tracciata(rep):
    """JSON non conforme allo schema -> riparato (coerce) e registrato."""
    _rl, _ = rep
    router = _mk_router(_HDR + _ROW_GOOD, strict=True)
    first = router.config.groups[_GRP][0]

    def handler(_request):
        return _resp('{"n": "7"}')

    payload = {"model": "x",
               "messages": [{"role": "user", "content": "dammi json"}],
               "response_format": {"type": "json_schema",
                                   "json_schema": {"schema": _SCHEMA}}}
    data, _used = _call(router, handler, payload, first)
    assert data["choices"][0]["message"]["content"] == '{"n": 7}'
    repairlog.flush_sync()
    row = _row_for(repairlog.read_all(), "struct_repaired")
    assert row and row["outcome"] == "ok" and "coerce_int" in row["detail"]


def test_struct_invalid_tracciato_e_ruota(rep):
    """JSON non riparabile -> struct_invalid (fail) + rotazione al buono."""
    _rl, _ = rep
    router = _mk_router(_HDR + _ROW_BROKEN + _ROW_GOOD, strict=True,
                        corrective=False)
    broken = next(d for d in router.config.groups[_GRP]
                  if d["api_key"] == "K1")
    good = next(d for d in router.config.groups[_GRP]
                if d["api_key"] == "K2")

    def handler(request):
        if request.url.host == "cf.test":
            return _resp('{"n": "zzz"}')
        return _resp('{"n": 7}')

    router.fallback_next = lambda *a, **k: good
    payload = {"model": "x",
               "messages": [{"role": "user", "content": "dammi json"}],
               "response_format": {"type": "json_schema",
                                   "json_schema": {"schema": _SCHEMA}}}
    _data, used = _call(router, handler, payload, broken)
    assert used["unique"] == good["unique"]
    repairlog.flush_sync()
    row = _row_for(repairlog.read_all(), "struct_invalid")
    assert row and row["outcome"] == "fail"
    assert row["dep"] == broken["unique"]
