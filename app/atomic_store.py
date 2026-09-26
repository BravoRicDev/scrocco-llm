"""Persistenza JSON atomica con copia di backup per il recupero.

[IT] COSA: helper condiviso per i piccoli file di stato su disco
(`key_health.json`, `adaptive_stats.json`, `thought_sigs.json`). La scrittura
e' ATOMICA (tmp -> os.replace) e mantiene una copia `path.bak`: se il processo
muore durante la scrittura il file principale resta valido; se al caricamento
il file principale e' vuoto/corrotto, si tenta il `.bak` prima di ripartire da
zero (evita di perdere la cronologia retirement/failimenti per una singola
scrittura interrotta).

[EN] Atomic JSON persistence with a `.bak` recovery copy.
"""
from __future__ import annotations

import json
import logging
import os
import shutil

log = logging.getLogger("nx.atomic")


def save_json(path, obj, *, indent=None, backup: bool = True) -> bool:
    """Scrive `obj` in `path` atomicamente, aggiornando `path.bak`.

    Ritorna True su successo. Su errore non solleva (best-effort): lo stato
    persistente non deve mai far cadere il gateway.
    """
    p = str(path)
    tmp = p + ".tmp"
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent)
        os.replace(tmp, p)                   # atomico
        if backup:
            try:
                shutil.copy2(p, p + ".bak")
            except OSError:
                pass
        return True
    except (OSError, TypeError, ValueError):
        log.error("[atomic] save %s fallito", p, exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def load_json(path, default_factory=dict):
    """Carica `path`; se vuoto/corrotto prova `path.bak`, poi il default.

    `default_factory` e' una callable senza argomenti (es. `dict`/`list`).
    """
    p = str(path)
    for candidate, is_bak in ((p, False), (p + ".bak", True)):
        try:
            with open(candidate, encoding="utf-8") as f:
                txt = f.read()
        except (FileNotFoundError, OSError):
            continue
        if not txt.strip():
            continue
        try:
            obj = json.loads(txt)
        except ValueError:
            log.warning("[atomic] %s illeggibile%s", candidate,
                        " (provo .bak)" if not is_bak else "")
            continue
        if is_bak:
            log.warning("[atomic] %s corrotto: recuperato da .bak", p)
        return obj
    return default_factory()
