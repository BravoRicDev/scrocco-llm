"""Stato per-request dell'`effort` (reasoning_effort) e delle regole collegate.

L'effort arriva dal client nel body (`reasoning_effort`, oppure l'alias
`effort`) o dall'header `x-effort`. Valori normalizzati: default/low/medium/high.

Lo stato e' in una `ContextVar`: il router la legge per il bias di intelligenza
e il forwarder per iniettare/rimuovere `reasoning_effort` e per l'override di
temperatura. Essendo contextvar, resta isolato per-task (una richiesta non
"sporca" le concorrenti).
"""

from __future__ import annotations

import contextvars
from typing import Any

DEFAULT = "default"
_VALID = (DEFAULT, "low", "medium", "high")

# Chiave di default neutra: nessun deployment e' penalizzato, nessuna iniezione.
_state: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "nx_effort_state",
    default={"effort": DEFAULT, "temp_enabled": False, "temp_overrides": {}},
)


def normalize_effort(raw: Any) -> str:
    """normalizza il valore grezzo a uno dei 4 livelli."""
    m = str(raw or "").strip().lower()
    if m in ("low", "medium", "high"):
        return m
    if m in ("minimal", "min"):
        return "low"
    return DEFAULT


def effort_from_request(payload: Any, headers: Any = None) -> str:
    """Estrae l'effort da body (`reasoning_effort`/`effort`) o header `x-effort`."""
    raw = None
    if isinstance(payload, dict):
        raw = payload.get("reasoning_effort")
        if raw is None:
            raw = payload.get("effort")
    if raw is None and headers is not None:
        try:
            raw = headers.get("x-effort")
        except Exception:  # noqa: BLE001
            raw = None
    return normalize_effort(raw)


def set_effort(effort: Any, *, temp_enabled: bool = False,
               temp_overrides: dict | None = None) -> contextvars.Token:
    """Imposta lo stato per la richiesta corrente. Ritorna un token per reset."""
    eff = normalize_effort(effort)
    return _state.set({
        "effort": eff,
        "temp_enabled": bool(temp_enabled),
        "temp_overrides": dict(temp_overrides or {}),
    })


def reset_effort(token: contextvars.Token) -> None:
    try:
        _state.reset(token)
    except (ValueError, LookupError):
        pass


def get_effort() -> str:
    return _state.get()["effort"]


def get_temperature_config() -> tuple[bool, dict]:
    s = _state.get()
    return bool(s.get("temp_enabled")), dict(s.get("temp_overrides") or {})
