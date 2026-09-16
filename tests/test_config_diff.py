"""Hot-reload seriale con diff strutturato.

Ogni reload del CSV produce un evento [CONFIG_DIFF] nel CLOG: righe
classificate ADDED / REMOVED / MODIFIED / UNCHANGED (+ RENAME modello), con i
campi cambiati evidenziati (endpoint, model, key MAI in chiaro). reload()
e' serializzato da un lock (niente stati misti con reload sovrapposti)."""
import os
import tempfile

from app.config import (_config_diff, _csv_field_rows, GatewayConfig,
                        maybe_reload)

CSV_BASE = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,model_preference
t@x.com,ds-a,prov,https://x/v1,20,32,8000,0,K1,text,100
t@x.com,mimo,prov,https://x/v1,20,32,8000,0,K2,text,50
"""


def _write(path, text):
    with open(path, "w") as f:
        f.write(text)


def test_diff_key_revoked_is_modified(tmp_path):
    _write(tmp_path / "a.csv", CSV_BASE)
    old_rows = _csv_field_rows(tmp_path / "a.csv")
    # key REVOCATA (cambia solo la colonna profilo = la key)
    _write(tmp_path / "a.csv",
           CSV_BASE.replace("K1", "K9-new"))
    d = _config_diff(old_rows, _csv_field_rows(tmp_path / "a.csv"))
    assert len(d["modified"]) == 1
    m = d["modified"][0]
    assert m["model"] == "ds-a"
    # identita' (modello, endpoint) invariata, ma il valore della key cambia
    assert m["old"]["scrocco-llm-test"] == "K1"
    assert m["new"]["scrocco-llm-test"] == "K9-new"
    assert m["old"]["modello"] == m["new"]["modello"]
    assert not d["added"] and not d["removed"]


def test_diff_added_removed(tmp_path):
    csvp = tmp_path / "k.csv"
    _write(csvp, CSV_BASE)
    old_rows = _csv_field_rows(csvp)
    _write(csvp, CSV_BASE + "t@x.com,new-m,prov,https://x/v1,20,32,8000,0,K3,text,0\n")
    d = _config_diff(old_rows, _csv_field_rows(csvp))
    assert [r["model"] for r in d["added"]] == ["new-m"]
    assert d["unchanged"] == 2

    _write(csvp, CSV_BASE.replace("t@x.com,ds-a,", "#"))
    # riga ds-a rimossa
    d = _config_diff(old_rows, _csv_field_rows(csvp))
    assert [r["model"] for r in d["removed"]] == ["ds-a"]


def test_diff_model_rename_is_modified(tmp_path):
    csvp = tmp_path / "k.csv"
    _write(csvp, CSV_BASE)
    old_rows = _csv_field_rows(csvp)
    _write(csvp, CSV_BASE.replace("ds-a", "ds-b"))
    d = _config_diff(old_rows, _csv_field_rows(csvp))
    assert len(d["renamed"]) == 1
    r, a = d["renamed"][0]
    assert r["model"] == "ds-a" and a["model"] == "ds-b"
    assert d["removed"] == [] and d["added"] == []


def test_endpoint_change_is_added_removed(tmp_path):
    """Cambio endpoint = deployment diverso (identita' modello+endpoint):
    compare come REMOVED+ADDED, non come MODIFIED."""
    csvp = tmp_path / "k.csv"
    _write(csvp, CSV_BASE)
    old_rows = _csv_field_rows(csvp)
    _write(csvp, CSV_BASE.replace("https://x/v1", "https://y/v1"))
    d = _config_diff(old_rows, _csv_field_rows(csvp))
    assert len(d["removed"]) == 2 and len(d["added"]) == 2
    assert d["modified"] == []


def test_reload_swaps_and_snapshots(tmp_path):
    csvp = tmp_path / "keys.csv"
    _write(csvp, CSV_BASE)
    cfg = GatewayConfig(csvp, proxy_prefix="scrocco-llm-", seed=1)
    assert len(cfg.groups) >= 1
    assert len(cfg._last_rows) == 2

    m0 = maybe_reload(cfg, None)
    assert m0 is not None

    _write(csvp, CSV_BASE + "t@x.com,new-m,prov,https://x/v1,20,32,8000,0,K3,text,0\n")
    # Garantisci un mtime STRETTAMENTE maggiore: su alcuni filesystem la
    # risoluzione dei timestamp e' grossolana e due scritture ravvicinate
    # possono condividere lo stesso st_mtime_ns (race di timing, non di logica).
    _st = os.stat(csvp)
    os.utime(csvp, ns=(_st.st_atime_ns, _st.st_mtime_ns + 1_000_000_000))
    m1 = maybe_reload(cfg, m0)
    assert m1 != m0
    assert len(cfg._last_rows) == 3
    assert cfg._last_rows[-1]["model"] == "new-m"


def test_reload_invalid_keeps_state(tmp_path):
    csvp = tmp_path / "keys.csv"
    _write(csvp, CSV_BASE)
    cfg = GatewayConfig(csvp, proxy_prefix="scrocco-llm-", seed=1)
    before = len(cfg.groups)
    m0 = maybe_reload(cfg, None)
    _write(csvp, "modello,provider\nbroken")
    m1 = maybe_reload(cfg, m0)
    # reload rifiutato: stato precedente intatto e _last_rows invariato
    assert len(cfg.groups) == before
    assert len(cfg._last_rows) == 2
    assert m1 == m0