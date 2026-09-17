"""Gate per-client degli upstream opencode.ai (zen / zen/go).

opencode.ai/zen e /zen/go accettano richieste solo da client opencode reali
(header nativi + sessione `ses_...`); per tutti gli altri rispondono 403
FreeTierError. Il forwarder sa sintetizzare (o fare passthrough de)gli header
giusti, ma il ROUTER deve saperlo PRIMA di scegliere un deployment: altrimenti
conta gli upstream opencode.ai come disponibili e "caldi" (anche in prestito
da sessioni opencode) e li propone a client che non possono usarli, sprecando
tentativi, canary e cooldown.

Questo modulo e' la fonte di verita' del gate, condivisa da router e
forwarder. Lo stato vive in una ContextVar per-request:

  - una RICHIESTA CLIENT imposta `set_allow_opencode(client_can_use_opencode(
    attribution))` (vedi main.py): True se il client e' opencode oppure se lo
    spoof e' attivo (env `OPENCODE_SPOOF_HEADERS`);
  - i contesti INTERNI (probe / autoprobe / admin / background) NON impostano
    nulla: il default `None` ricade sulla env `OPENCODE_SPOOF_HEADERS`, cosi'
    le probe non toccano opencode.ai quando lo spoof e' off (evita 403 e
    ritiri di chiave spuri).
"""

from __future__ import annotations

import contextvars
import os
from typing import Any, Mapping

# Default None = "nessuna decisione per-request": i contesti interni (probe,
# autoprobe, admin, background) ricadono su OPENCODE_SPOOF_HEADERS.
_ALLOW_OPENCODE: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "scrocco_allow_opencode", default=None)

# Host degli upstream opencode: copre zen (/zen/v1) e go (/zen/go/v1).
_OPENCODE_HOST = "opencode.ai"


def is_opencode_dep(dep: Mapping[str, Any] | None) -> bool:
    """Vero se il deployment parla con opencode.ai (zen / zen/go)."""
    if not dep:
        return False
    base = (dep.get("api_base") or "").lower()
    return _OPENCODE_HOST in base


def spoof_enabled() -> bool:
    """POC 'trucco': env `OPENCODE_SPOOF_HEADERS` truthy."""
    return bool(os.environ.get("OPENCODE_SPOOF_HEADERS"))


def client_is_opencode(client_headers: Mapping[str, Any] | None) -> bool:
    """Vero se il client si presenta come opencode reale (senza spoof).

    Il client opencode 1.18.x verso un gateway custom manda `user-agent:
    opencode/<ver> ...` (+ `x-session-affinity`); gli header `x-opencode-*`
    espliciti sono un segnale altrettanto valido.
    """
    h = client_headers or {}
    ua = str(h.get("user-agent") or "").lower()
    if ua.startswith("opencode/"):
        return True
    for k in ("x-opencode-client", "x-opencode-request", "x-opencode-session"):
        if h.get(k):
            return True
    return False


def client_can_use_opencode(client_headers: Mapping[str, Any] | None) -> bool:
    """Vero se il client puo' usare gli upstream opencode.ai.

    Vero per un client opencode reale oppure, in POC, se lo spoof e' attivo.
    """
    return spoof_enabled() or client_is_opencode(client_headers)


def set_allow_opencode(flag: bool | None) -> None:
    """Imposta il gate per il task corrente (async-safe)."""
    _ALLOW_OPENCODE.set(flag)


def allow_opencode() -> bool:
    """Gate effettivo: scelta per-request se presente, altrimenti spoof env."""
    v = _ALLOW_OPENCODE.get()
    if v is None:
        return spoof_enabled()
    return bool(v)


def dep_usable(dep: Mapping[str, Any] | None) -> bool:
    """Vero se `dep` e' utilizzabile nel contesto di richiesta corrente.

    Gli upstream opencode.ai sono utilizzabili solo se `allow_opencode()`.
    """
    if not is_opencode_dep(dep):
        return True
    return allow_opencode()
