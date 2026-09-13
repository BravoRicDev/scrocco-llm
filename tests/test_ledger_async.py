"""Ledger non bloccante: record() non deve mai deadlockare col lock (era
riacquisito dentro il with) e il flush I/O gira su un thread separato."""
import asyncio
import json

from app.ledger import Ledger


def test_record_safety_net_no_deadlock(tmp_path):
    ledger = Ledger(tmp_path)
    # 205 righe: supera la soglia di 200 che innesca il flush di sicurezza
    # (prima del fix riacquisiva self._lock dentro il with -> deadlock).
    for i in range(205):
        ledger.record({"kind": "chat", "n": i})
    ledger.flush()                         # svuota il residuo (5 righe)
    rows = ledger.iter_rows()
    assert len(rows) == 205


def test_flush_async_writes(tmp_path):
    ledger = Ledger(tmp_path)
    for i in range(3):
        ledger.record({"kind": "chat", "n": i})
    written = asyncio.run(ledger.flush_async())
    assert written == 3
    assert asyncio.run(ledger.iter_rows_async()) != []
    path = tmp_path / "usage_ledger.jsonl"
    lines = [json.loads(x) for x in path.read_text().splitlines() if x]
    assert len(lines) == 3


def test_flush_async_empty(tmp_path):
    ledger = Ledger(tmp_path)
    assert asyncio.run(ledger.flush_async()) == 0


def test_iter_rows_async_reads_segments(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.record({"kind": "chat", "n": 1})
    ledger.flush()
    (tmp_path / "usage_ledger.jsonl.1").write_text(
        json.dumps({"kind": "chat", "n": 0}) + "\n")
    rows = asyncio.run(ledger.iter_rows_async())
    assert {r["n"] for r in rows} == {0, 1}
