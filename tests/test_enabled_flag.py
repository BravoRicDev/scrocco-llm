"""Disabilitazione dichiarativa dei deployment (colonna CSV `enabled`).

Default true (vuoto/assente = attivo). `false`/`0`/`no`/`off` = disabilitato:
la riga RESTA nel CSV (chiave e coppia provider/chiave conservate) ma e' esclusa
da tutti i bucket di routing. Senza la colonna il comportamento e' invariato.
"""
import os
import tempfile
from datetime import date

from app import csv_store
from app.config import GatewayConfig, _classify
from app.csv_store import apply_payload, ensure_enabled_column, row_id

CSV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,enabled
,aaaa,ProvA,https://a.example/v1,free,128,8000,0,sk-A,text,
,bbbb,ProvB,https://b.example/v1,free,128,8000,0,sk-B,text,false
,cccc,ProvC,https://c.example/v1,free,128,8000,0,sk-C,text,true
,dddd,ProvD,https://d.example/v1,free,128,8000,0,sk-D,text,off
"""

CSV_NO_COL = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
,zzz,ProvZ,https://z.example/v1,free,128,8000,0,sk-Z,text
"""


def _write(rows: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(rows)
    return path


def _models(cfg: GatewayConfig) -> set[str]:
    return {d["model"] for g in cfg.groups.values() for d in g}


def test_disabled_rows_excluded_from_routing():
    path = _write(CSV)
    try:
        cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-")
        assert _models(cfg) == {"aaaa", "cccc"}
        # nessuna catena contiene le righe disabilitate
        assert all("bbbb" not in u and "dddd" not in u
                   for u in cfg.chains["test"])
        # la riga disabilitata resta nel CSV: chiave e provider conservati
        with open(path) as f:
            content = f.read()
        assert "bbbb" in content and "sk-B" in content
        assert "dddd" in content and "sk-D" in content
    finally:
        os.unlink(path)


def test_default_enabled_without_column():
    path = _write(CSV_NO_COL)
    try:
        cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-")
        assert _models(cfg) == {"zzz"}
    finally:
        os.unlink(path)


def test_classify_enabled_parsing():
    cases = [("", True), ("true", True), ("yes", True), ("1", True),
             ("0", False), ("false", False), ("no", False), ("off", False),
             ("n", False)]
    for raw, expected in cases:
        meta = _classify({"modello": "m", "provider": "p", "endpoint": "e",
                          "data": "free", "context": "128", "enabled": raw},
                         date.today())
        assert meta["enabled"] is expected, raw


def test_payload_enabled_bool_normalization():
    row: dict = {}
    apply_payload(row, {"enabled": False}, "scrocco-llm-", current_profile="test")
    assert row["enabled"] == "false"
    apply_payload(row, {"enabled": "yes"}, "scrocco-llm-", current_profile="test")
    assert row["enabled"] == "true"
    apply_payload(row, {"enabled": ""}, "scrocco-llm-", current_profile="test")
    assert row["enabled"] == ""


def test_ensure_enabled_column():
    header = ["modello", "provider"]
    out = ensure_enabled_column(header)
    assert "enabled" in out
    assert ensure_enabled_column(out) is out  # idempotente


def test_row_id_stable_with_enabled():
    base = {"modello": "m", "provider": "p", "endpoint": "https://x.example/v1",
            "data": "free", "context": "128", "max_input": "0",
            "priority": "0", "caps": "", "scrocco-llm-test": "sk-secret123"}
    disabled = dict(base)
    disabled["enabled"] = "false"
    assert row_id(base, base["endpoint"]) == row_id(disabled, disabled["endpoint"])
