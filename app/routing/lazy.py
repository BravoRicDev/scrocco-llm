"""Accessor lazy per lo stato d'istanza dei mixin del router.

[IT] I test costruiscono Router "nudi" (`Router.__new__(Router)` senza
`__init__`), quindi ogni mappa di stato e' letta tramite un accessor che la
crea al primo uso. Prima ogni accessor ripeteva lo stesso corpo di 4 righe;
ora delegano tutti a `lazy_dict`.

[EN] Shared lazy-init accessor for router state dicts (bare-Router tests).
"""
from __future__ import annotations


def lazy_dict(obj: object, attr: str) -> dict:
    """Ritorna `obj.<attr>`, creandolo come dict vuoto se assente o None."""
    d = getattr(obj, attr, None)
    if d is None:
        d = {}
        setattr(obj, attr, d)
    return d
