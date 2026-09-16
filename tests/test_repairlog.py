"""Test del tracking delle riparazioni tool-call (log a schermo + persistente)."""
from __future__ import annotations

import json

import pytest

from app import repairlog
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
