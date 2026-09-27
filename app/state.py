"""Stato runtime condiviso del gateway (singleton di processo).

[IT] COSA: il contenitore UNICO degli oggetti vivi del gateway (config,
policy, router, forwarder, auth, ledger, keyhealth), dei percorsi dei file di
stato e delle strutture condivise (coalescing, job video, probe in volo).
`app/main.py` li crea all'import e li assegna qui; tutti gli altri moduli li
leggono come `gw_state.<nome>` (import normale in testa al file).

PERCHE': prima vivevano come globali di `app/main.py` e i moduli estratti li
raggiungevano con `import app.main as M` DENTRO ogni funzione (ciclo di
import con il modulo radice, dipendenze nascoste). Ora la dipendenza e'
esplicita e senza cicli: questo modulo non importa nulla del gateway.

Riassegnazioni (hot-reload della policy, test che sostituiscono il router)
si fanno qui: `gw_state.policy = fresh` e' visto da tutti.

[EN] Process-wide runtime state of the gateway, populated by app.main.
"""
from __future__ import annotations

import importlib
from typing import Any


def __getattr__(name: str) -> Any:
    """Un attributo non ancora assegnato significa che `app.main` non e'
    stato importato (es. un test che importa direttamente un modulo
    estratto): lo si importa, come faceva il vecchio `import app.main as M`
    dentro le funzioni, e si rilegge."""
    if name.startswith("__"):
        raise AttributeError(name)
    importlib.import_module("app.main")
    try:
        return globals()[name]
    except KeyError:
        raise AttributeError(f"module 'app.state' has no attribute {name!r}") from None
