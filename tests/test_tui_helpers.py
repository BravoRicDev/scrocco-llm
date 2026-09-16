"""Helper della TUI indipendenti da textual (girano sempre).

Il bug storico: GET /admin/backups ritorna [{filename,size,mtime}] e la
schermata Operazioni confrontava direttamente una stringa con i dict, quindi
il ripristino non trovava mai il nome. Qui si fissa la normalizzazione.
"""


def test_backup_names_normalizza_dict_e_stringhe():
    """GET /admin/backups ritorna [{filename,size,mtime}]: la schermata
    Operazioni deve normalizzare a filename, altrimenti il ripristino non
    trova mai il nome scelto (bug: confronto str vs dict)."""
    from tui.gateway_client import backup_filenames
    assert backup_filenames(
        [{"filename": "keys_rotation-2026.csv", "size": 9, "mtime": 1}]
    ) == ["keys_rotation-2026.csv"]
    assert backup_filenames(["gateway.yaml-2026.yaml"]) == \
        ["gateway.yaml-2026.yaml"]
    assert backup_filenames(
        [{"name": "x.csv"}, {}, None, "y.yaml", {"filename": ""}]
    ) == ["x.csv", "y.yaml"]
    assert backup_filenames(None) == []
