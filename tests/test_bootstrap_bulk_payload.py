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
import app.state as gw_state

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
    orig_csv_path = gw_state.config.csv_path
    monkeypatch.setattr(gw_state.authn, "master_key", "test-master-bootstrap-md")
    monkeypatch.setattr(gw_state, "CSV_PATH", str(csv))
    monkeypatch.setattr(gw_state, "VAR_DIR", str(tmp_path))
    gw_state.config.csv_path = csv
    gw_state.config.reload()
    yield TestClient(m.app)
    # teardown: nessun leak verso la config reale
    gw_state.router._cooldown.clear()
    gw_state.config.csv_path = orig_csv_path
    gw_state.config.reload()


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


# ================================ Bug 4: bootstrap su CSV INESISTENTE
# `POST /deployments` gia' gestiva il fresh install (header minimale + rows=[]).
# `POST /admin/deployments/bulk` NO: il FileNotFoundError di
# `csv_store.load_table` finiva in `except Exception` -> 400
# ("CSV non valido dopo la modifica: [Errno 2] ...") e il batch atomico non
# applicava NULLA. Il playbook di bootstrap (unica via su fresh install)
# era quindi eseguibile solo DOPO che il singolo create avesse girato.
@pytest.fixture()
def fresh_client(monkeypatch, tmp_path):
    """CSV su un path che NON esiste ne' come file ne' come cartella."""
    import app.main as m
    csv_file = tmp_path / "nuova" / "dir" / "keys_rotation.csv"
    assert not csv_file.exists()
    orig_mk = gw_state.authn.master_key
    gw_state.authn.master_key = "test-master-bootstrap-fresh"
    monkeypatch.setattr(gw_state, "CSV_PATH", str(csv_file))
    monkeypatch.setattr(gw_state, "VAR_DIR", str(tmp_path))
    monkeypatch.setattr(gw_state.config, "csv_path", csv_file)
    gw_state.config.reload()
    assert str(gw_state.CSV_PATH).startswith(str(tmp_path))
    yield TestClient(m.app), csv_file
    gw_state.authn.master_key = orig_mk
    gw_state.router._cooldown.clear()
    gw_state.config.csv_path = gw_state.CSV_PATH
    gw_state.config.reload()


def _create_op(**over):
    op = {"action": "create", "profile": "freshprofile",
          "modello": "openai/gpt-oss-120b", "provider": "openai",
          "endpoint": "https://api.openai.com/v1", "data": "text",
          "context": 128, "key": "gsk_0123456789abcdef"}
    op.update(over)
    return op


def test_bulk_bootstrap_creates_missing_csv(fresh_client):
    """BUG 4: /admin/deployments/bulk su CSV inesistente deve CREARE il file e
    applicare le op, non rispondere 400."""
    c, csv_file = fresh_client
    auth = {"Authorization": "Bearer test-master-bootstrap-fresh"}

    r = c.post("/admin/deployments/bulk",
               json={"operations": [_create_op()]}, headers=auth)

    assert r.status_code == 200, (
        f"il bootstrap su fresh install deve riuscire, non 400: {r.text}")
    body = r.json()
    assert body["ok"] is True
    assert body["applied"] == 1
    assert body["results"][0]["ok"] is True, body["results"]
    # il file e' stato materializzato (cartelle comprese)
    assert csv_file.exists(), "il CSV doveva essere creato dal primo insert"
    assert csv_file.stat().st_size > 0
    # e la riga e' davvero persistita e servibile
    rows = c.get("/admin/deployments", headers=auth).json()
    assert rows["count"] == 1
    assert rows["deployments"][0]["profile"] == "freshprofile"


def test_bulk_bootstrap_multiple_ops_on_missing_csv(fresh_client):
    """Lo stesso su un batch multi-op: tutte applicate, un solo CSV."""
    c, csv_file = fresh_client
    auth = {"Authorization": "Bearer test-master-bootstrap-fresh"}
    ops = [_create_op(profile="p1", modello="openai/gpt-oss-120b"),
           _create_op(profile="p2", modello="openai/gpt-oss-120b")]

    r = c.post("/admin/deployments/bulk", json={"operations": ops},
               headers=auth)

    assert r.status_code == 200, r.text
    assert r.json()["applied"] == 2
    rows = c.get("/admin/deployments", headers=auth).json()
    assert rows["count"] == 2
    assert {d["profile"] for d in rows["deployments"]} == {"p1", "p2"}
    assert csv_file.exists()


def test_create_deployment_bootstrap_still_works(fresh_client):
    """Il percorso gia' corretto (singolo POST /deployments) NON regredisce."""
    c, csv_file = fresh_client
    auth = {"Authorization": "Bearer test-master-bootstrap-fresh"}
    op = _create_op()
    op.pop("action")

    r = c.post("/admin/deployments", json=op, headers=auth)

    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert csv_file.exists()
    rows = c.get("/admin/deployments", headers=auth).json()
    assert rows["count"] == 1
    assert rows["deployments"][0]["profile"] == "freshprofile"
