"""B3 - il playbook di bootstrap deve essere ESECUTIBILE, non solo leggibile.

Due difetti, entrambi verificati (400 garantito, zero righe scritte perche'
`/admin/deployments/bulk` e' ATOMICO):
  1. `docs/BOOTSTRAP.md` mandava un payload SENZA "profile", campo
     obbligatorio di `_required_create` (app/admin.py);
  2. il playbook LIVE servito da `GET /bootstrap` (app/bootstrap.py) mandava
     "model" invece di "modello" e ometteva "action":"create".

I test estraggono il payload DAI FILE (non da una copia): se il playbook
regredisce, il test va rosso.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin import _required_create

BOOTSTRAP_MD = (Path(__file__).resolve().parents[1]
                / "docs" / "BOOTSTRAP.md")

_CURL = re.compile(
    r"curl -X POST localhost:4001/admin/deployments/bulk.*?-d\s*'(?P<payload>.*?)'",
    re.DOTALL)


# ------------------------------------------------------- docs/BOOTSTRAP.md ---
def test_md_contains_exactly_one_bulk_payload():
    found = _CURL.findall(BOOTSTRAP_MD.read_text(encoding="utf-8"))
    assert len(found) == 1, f"atteso 1 payload bulk nel .md, trovati {len(found)}"
    json.loads(found[0])          # JSON valido


def test_md_payload_is_accepted_by_the_api():
    """Il payload del .md deve superare `_required_create` (=> niente 400)."""
    op = json.loads(_CURL.search(BOOTSTRAP_MD.read_text(
        encoding="utf-8")).group("payload"))["operations"][0]
    assert op.get("action") == "create"
    _required_create(op)          # solleva CsvStoreError se manca un campo
    # `model` non e' il campo del CSV: il suo uso passa in silenzio ma
    # lascia `modello` vuoto -> 400.
    assert "model" not in op
    assert op["modello"]


def test_md_documents_required_fields():
    text = BOOTSTRAP_MD.read_text(encoding="utf-8")
    assert "profile" in text and "modello" in text
    # il campo upstream da usare (non `model`)
    assert re.search(r"`modello`.*not.*`model`|`modello`.*\(non `model`\)", text)
    # la lunghezza minima della chiave (validazione di app/csv_store.py)
    assert re.search(r"8 char", text, re.IGNORECASE)


# --------------------------------------------------- GET /bootstrap (live) ---
@pytest.fixture()
def client(monkeypatch, tmp_path):
    import app.main as m
    csv = tmp_path / "k.csv"
    csv.write_text("commento,modello,provider,endpoint,data,context,"
                   "max_input,priority,scrocco-llm-test,caps\n")
    orig_csv_path = m.config.csv_path
    monkeypatch.setattr(m.authn, "master_key", "test-master-bootstrap-md")
    monkeypatch.setattr(m, "CSV_PATH", str(csv))
    monkeypatch.setattr(m, "VAR_DIR", str(tmp_path))
    m.config.csv_path = csv
    m.config.reload()
    yield TestClient(m.app)
    # teardown: nessun leak verso la config reale
    m.router._cooldown.clear()
    m.config.csv_path = orig_csv_path
    m.config.reload()


def _live_insert_op(j: dict) -> dict:
    step = next(s for s in j["steps"] if s["id"] == 3)
    start = step["how_to"].index("{")
    end = step["how_to"].rindex("}") + 1
    return json.loads(step["how_to"][start:end])


def test_live_playbook_payload_is_accepted_by_the_api(client):
    """Il payload del playbook di GET /bootstrap deve superare
    `_required_create` (=> nessun 400, la riga viene scritta)."""
    op = _live_insert_op(client.get("/bootstrap").json())
    assert op.get("action") == "create"
    _required_create(op)
    assert op["profile"] and op["modello"] and op["key"]


def test_live_field_semantics_says_modello(client):
    sem = next(s for s in client.get("/bootstrap").json()["steps"]
               if s["id"] == 3)["field_semantics"]
    assert "modello" in sem
    assert "model" not in sem


def test_live_playbook_bulk_actually_writes_the_row(client):
    """End-to-end: il payload LIVE del playbook, POSTato a /bulk, scrive la
    riga (prima del fix rispondeva 400 e non scriveva NULLA)."""
    j = client.get("/bootstrap").json()
    op = _live_insert_op(j)
    op["profile"] = "mdprofile"
    op["modello"] = "openai/gpt-oss-120b"
    op["key"] = "gsk_0123456789abcdef"      # >= 8 caratteri (csv_store)
    op["context"] = 128
    r = client.post("/admin/deployments/bulk",
                    json={"operations": [op]},
                    headers={"Authorization": "Bearer test-master-bootstrap-md"})
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["ok"] is True
    rows = client.get("/admin/deployments",
                      headers={"Authorization":
                               "Bearer test-master-bootstrap-md"}).json()
    assert rows["count"] == 1
    assert rows["deployments"][0]["profile"] == "mdprofile"
