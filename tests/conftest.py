"""Isolamento dello stato PERSISTENTE per la suite.

Senza isolamento i test girano contro `<repo>/var/` (VAR_DIR di app.main): i
percorsi REALI di retirement (keyhealth) e di warm-start (routing/cooldown)
scrivono su var/key_health.json ecc. Un test che provoca un retirement di un
dep di test lo PERSISTE, e i test successivi (nella stessa run e in quelle
seguenti) lo vedono come "retired": failure spurie e run lentissimi
(probe/timeout su dep fantasma).

Questa fixture autouse, per OGNI test, installa un KeyHealth nuovo su una dir
temporanea e redirige i file di stato persistente, cosi' nessun test puo'
inquinare gli altri ne' il repository.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_gateway_state(tmp_path, monkeypatch):
    try:
        from app import main as M
    except Exception:                                  # pragma: no cover
        return

    try:
        from app.keyhealth import KeyHealth
        monkeypatch.setattr(M, "KEYHEALTH", KeyHealth(str(tmp_path)),
                            raising=False)
    except Exception:                                  # pragma: no cover
        pass

    for attr, name in (("_stats_file", "adaptive_stats.json"),
                       ("_cooldown_file", "cooldown_state.json"),
                       ("_routing_file", "routing_state.json"),
                       ("_thought_sigs_file", "thought_sigs.json")):
        if hasattr(M, attr):
            monkeypatch.setattr(M, attr, tmp_path / name, raising=False)
