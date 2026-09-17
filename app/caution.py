"""Cautela GENERICA: spegne le attivita' di background/probe del gateway.

Funzionalita' DISTINTA dalla "cautela opencode" (vedi app/opencode_gate.py):

  - cautela opencode (`OPENCODE_CAUTIOUS`, default = spoof): riguarda SOLO gli
    upstream opencode.ai/zen, che diventano ultima scelta e non vengono sondati;
  - cautela generica (`BACKGROUND_CAUTIOUS`): riguarda TUTTI i provider e
    disattiva le attivita' che NON servono direttamente una richiesta client —
    probe automatiche, health-check periodici, giro notturno, probe hot-reload
    e re-probe dei deployment in cooldown.

Interruttore: env `BACKGROUND_CAUTIOUS` (default OFF = probe attive). Si puo'
tenere lo spoof ON e la cautela generica OFF (o viceversa) senza toccare il
codice.
"""

from __future__ import annotations

import os

_FALSEY = {"0", "false", "no", "n", "off", ""}


def background_cautious_enabled() -> bool:
    """Vero se le attivita' di background/probe sono disattivate (env).

    Default OFF (probe attive); si attiva con `BACKGROUND_CAUTIOUS=1`.
    """
    raw = os.environ.get("BACKGROUND_CAUTIOUS")
    if raw is None:
        return False
    return raw.strip().lower() not in _FALSEY
