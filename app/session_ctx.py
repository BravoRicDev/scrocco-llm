"""Contesto di sessione per-task (async-safe).

[IT] COSA: espone la sessione "corrente" legata al task asincrono che sta
servendo la richiesta. WHY: piu' componenti (router, pipeline di streaming)
devono conoscere la sessione senza passarla a mano lungo ogni chiamata;
`contextvars` garantisce isolamento tra richieste concorrenti.
[EN] Per-task session context. Kept in a leaf module so routing/streaming
components can import it without creating import cycles through router.py.
"""

from __future__ import annotations

import contextvars

_SESSION_CTX: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "scrocco_session", default=None)


def set_current_session(session_id: str | None) -> None:
    """Imposta la sessione corrente per il task (async-safe)."""
    _SESSION_CTX.set(session_id)


def current_session() -> str | None:
    return _SESSION_CTX.get()
