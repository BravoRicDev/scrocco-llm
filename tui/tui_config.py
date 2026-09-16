"""Parametri configurabili della TUI (prima hardcoded).

Tutti i valori sono sovrascrivibili da variabili d'ambiente con prefisso
`TUI_`; i default coincidono con i valori storici, quindi il comportamento
non cambia. Importare da qui invece di usare letterali sparsi rende ogni
manopola della TUI ispezionabile e regolabile senza toccare il codice.
"""
from __future__ import annotations

import os


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


# --- intervalli di auto-refresh (secondi) ---------------------------------
REFRESH_LIVE_SEC = _env_float("TUI_REFRESH_LIVE_SEC", 2.0)
REFRESH_ERRORS_SEC = _env_float("TUI_REFRESH_ERRORS_SEC", 5.0)
REFRESH_SESSIONS_SEC = _env_float("TUI_REFRESH_SESSIONS_SEC", 10.0)
REFRESH_LEADERBOARD_SEC = _env_float("TUI_REFRESH_LEADERBOARD_SEC", 15.0)
REFRESH_STATS_SEC = _env_float("TUI_REFRESH_STATS_SEC", 30.0)

# --- limiti di righe / troncature -----------------------------------------
LIVE_MAX_ROWS = _env_int("TUI_LIVE_MAX_ROWS", 500)
MODEL_RANKING_MAX = _env_int("TUI_MODEL_RANKING_MAX", 50)
OPS_ROWS_MAX = _env_int("TUI_OPS_ROWS_MAX", 200)
OPS_HISTORY_MAX = _env_int("TUI_OPS_HISTORY_MAX", 100)
OPS_PRESSURE_LIMIT = _env_int("TUI_OPS_PRESSURE_LIMIT", 40)
RESULT_MAX_CHARS = _env_int("TUI_RESULT_MAX_CHARS", 8000)
MCP_RESULT_MAX_CHARS = _env_int("TUI_MCP_RESULT_MAX_CHARS", 4000)
ERROR_MSG_MAX_CHARS = _env_int("TUI_ERROR_MSG_MAX_CHARS", 119)
HTTP_ERR_SNIPPET_CHARS = _env_int("TUI_HTTP_ERR_SNIPPET_CHARS", 300)
