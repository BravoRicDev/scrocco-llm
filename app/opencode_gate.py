"""Gate per-client degli upstream opencode.ai (zen / zen/go).

opencode.ai/zen e /zen/go accettano richieste solo da client opencode reali
(header nativi + sessione `ses_...`); per tutti gli altri rispondono 403
FreeTierError. Il forwarder sa sintetizzare (o fare passthrough de)gli header
giusti, ma il ROUTER deve saperlo PRIMA di scegliere un deployment: altrimenti
conta gli upstream opencode.ai come disponibili e "caldi" (anche in prestito
da sessioni opencode) e li propone a client che non possono usarli, sprecando
tentativi, canary e cooldown.

Questo modulo e' la fonte di verita' del gate, condivisa da router e
forwarder. Lo stato vive in ContextVar per-request:

  - una RICHIESTA CLIENT imposta `set_allow_opencode(client_can_use_opencode(
    attribution))` (vedi main.py): True se il client e' opencode oppure se lo
    spoof e' attivo (env `OPENCODE_SPOOF_HEADERS`);
  - i contesti INTERNI (probe / autoprobe / admin / background) NON impostano
    nulla: il default `None` ricade sulla env `OPENCODE_SPOOF_HEADERS`, cosi'
    le probe non toccano opencode.ai quando lo spoof e' off (evita 403 e
    ritiri di chiave spuri).

MODALITA' CAUTA (spoof ON): quando stiamo "spoofando" (client NON-opencode con
`OPENCODE_SPOOF_HEADERS` attivo) trattiamo gli upstream **zen** (free tier,
rischioso) come ULTIMA SCELTA, raggiungibili solo a esaurimento degli altri
provider; gli upstream **go** (a pagamento, legittimi) restano normali. La
cautela NON si applica ai client opencode reali. Vedi `cautious_enabled()`,
`spoofing_request()` e `dep_usable(..., last=...)`.
"""

from __future__ import annotations

import contextvars
import os
import re
from typing import Any, Mapping

# Default None = "nessuna decisione per-request": i contesti interni (probe,
# autoprobe, admin, background) ricadono su OPENCODE_SPOOF_HEADERS.
_ALLOW_OPENCODE: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "scrocco_allow_opencode", default=None)

# Default False = "non stiamo spoofando": i contesti interni non sono mai in
# cautela per-request (le loro probe sono disattivate a monte, vedi
# `cautious_enabled()`).
_SPOOFING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "scrocco_spoofing_request", default=False)

# Host degli upstream opencode: copre zen (/zen/v1) e go (/zen/go/v1).
_OPENCODE_HOST = "opencode.ai"

# Session id NATIVO opencode: "ses_" + 12 hex lowercase + 14 base62 (26 char
# dopo il prefisso). opencode.ai/zen e /zen/go ACCETTANO solo questo formato:
# il fingerprint interno "fq_..." (cosi' come "abc" o "ses_x") viene rifiutato
# con 403 FreeTierError.
_NATIVE_SESSION_RE = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")

_FALSEY = {"0", "false", "no", "n", "off", ""}


def _env_bool(name: str) -> bool | None:
    """Valore booleano della env `name`, o None se non impostata."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() not in _FALSEY


def is_opencode_dep(dep: Mapping[str, Any] | None) -> bool:
    """Vero se il deployment parla con opencode.ai (zen / zen/go)."""
    if not dep:
        return False
    base = (dep.get("api_base") or "").lower()
    return _OPENCODE_HOST in base


def is_opencode_zen_dep(dep: Mapping[str, Any] | None) -> bool:
    """Vero se il deployment e' un upstream zen (free tier opencode).

    Detection conservativa come `config._classify`: SOLO il provider esplicito
    `opencode-zen` (il nome provider contiene "zen"). Gli endpoint /zen/go NON
    contano: quelli sono "go".
    """
    if not dep:
        return False
    return "zen" in str(dep.get("provider") or "").lower()


def is_opencode_go_dep(dep: Mapping[str, Any] | None) -> bool:
    """Vero se il deployment e' un upstream go (a pagamento) di opencode.ai."""
    return is_opencode_dep(dep) and not is_opencode_zen_dep(dep)


def is_native_session(value: str | None) -> bool:
    """Vero se `value` e' nel formato session id nativo opencode (`ses_...`)."""
    return bool(_NATIVE_SESSION_RE.match(value or ""))


def spoof_enabled() -> bool:
    """POC 'trucco': env `OPENCODE_SPOOF_HEADERS` truthy."""
    return bool(os.environ.get("OPENCODE_SPOOF_HEADERS"))


def cautious_enabled() -> bool:
    """Modalita' cauta globale: zen come ultima scelta + niente probe/background.

    Derivata dallo spoof (`OPENCODE_SPOOF_HEADERS`) salvo override esplicito
    della env `OPENCODE_CAUTIOUS`, cosi' si puo' tenere lo spoof ON e la
    cautela OFF (o viceversa) senza toccare il codice.
    """
    override = _env_bool("OPENCODE_CAUTIOUS")
    if override is not None:
        return override
    return spoof_enabled()


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


def set_spoofing_request(flag: bool) -> None:
    """Marca il task corrente come 'stiamo spoofando' (client non-opencode)."""
    _SPOOFING.set(bool(flag))


def spoofing_request() -> bool:
    """Vero se la richiesta corrente e' servita spoofando un client non-opencode."""
    return bool(_SPOOFING.get())


def dep_usable(dep: Mapping[str, Any] | None, *, last: bool = False) -> bool:
    """Vero se `dep` e' utilizzabile nel contesto di richiesta corrente.

    - gli upstream opencode.ai sono utilizzabili solo se `allow_opencode()`;
    - in modalita' cauta, gli upstream **zen** sono ammessi solo come ULTIMA
      SCELTA (`last=True`): nei percorsi normali (pick, warm, canary, ...) un
      dep zen e' "non usabile". Gli upstream **go** restano sempre normali.
    """
    if not is_opencode_dep(dep):
        return True
    if not allow_opencode():
        return False
    if not last and spoofing_request() and is_opencode_zen_dep(dep):
        return False
    return True
