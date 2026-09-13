"""Terminal logging utilities for scrocco-llm.
Provides colored, readable terminal output without polluting log files.

Usage (in app/main.py):
    from app.terminal_logging import setup_colored_logging
    console_handler = setup_colored_logging()  # returns a handler with color

The colored output is only for the terminal/stdout. File handlers in
_install_file_logging() must keep a PLAIN (uncolored) formatter so that
var/gateway.log remains parseable by app/logview.py's regex.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
import zlib
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# ANSI escape codes
# ---------------------------------------------------------------------------
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_ITALIC = "\033[3m"

ANSI_COLORS = {
    "black": "\033[30m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "gray": "\033[90m",
    "bright_red": "\033[91m",
    "bright_green": "\033[92m",
    "bright_yellow": "\033[93m",
    "bright_blue": "\033[94m",
    "bright_magenta": "\033[95m",
    "bright_cyan": "\033[96m",
}

# ---------------------------------------------------------------------------
# Color palettes — chosen so level colors and tag colors do NOT clash.
# ---------------------------------------------------------------------------

# Log level -> color (distinct from tag colors)
LEVEL_COLORS = {
    "DEBUG": ANSI_COLORS["bright_cyan"],
    "INFO": ANSI_COLORS["green"],
    "WARNING": ANSI_COLORS["bright_yellow"],
    "ERROR": ANSI_COLORS["bright_red"],
    "CRITICAL": ANSI_COLORS["red"],
}

# Tag -> color (each tag category gets a distinct hue). Tag sconosciuti
# ricevono un colore deterministico dal fallback (mai un log incolore).
TAG_COLORS = {
    # lifecycle / richiesta
    "start": ANSI_COLORS["bright_green"],
    "summary": ANSI_COLORS["bright_blue"],
    "request": ANSI_COLORS["blue"],
    "route": ANSI_COLORS["magenta"],
    "pick": ANSI_COLORS["bright_magenta"],
    "pick-final": ANSI_COLORS["bright_magenta"],
    "identity": ANSI_COLORS["bright_magenta"],
    "session": ANSI_COLORS["cyan"],
    "sticky": ANSI_COLORS["cyan"],
    "cache": ANSI_COLORS["bright_cyan"],
    "ctxcompact": ANSI_COLORS["bright_cyan"],
    "truncation": ANSI_COLORS["bright_cyan"],
    # routing / budget
    "ladder": ANSI_COLORS["blue"],
    "caps": ANSI_COLORS["blue"],
    "defer": ANSI_COLORS["gray"],
    "budget": ANSI_COLORS["yellow"],
    "cooldown": ANSI_COLORS["bright_red"],
    "cooldown-wakeup": ANSI_COLORS["yellow"],
    "circuit-breaker": ANSI_COLORS["bright_red"],
    "latency-cross": ANSI_COLORS["yellow"],
    "esc-pin": ANSI_COLORS["cyan"],
    "esc-pin-probe": ANSI_COLORS["cyan"],
    "fallback": ANSI_COLORS["cyan"],
    "upstream": ANSI_COLORS["bright_magenta"],
    # qualità contenuto / riparazione
    "stream": ANSI_COLORS["magenta"],
    "watchdog": ANSI_COLORS["bright_yellow"],
    "stall": ANSI_COLORS["bright_yellow"],
    "qc": ANSI_COLORS["cyan"],
    "repair": ANSI_COLORS["bright_blue"],
    "toolrepair": ANSI_COLORS["bright_blue"],
    "histnorm": ANSI_COLORS["bright_blue"],
    "thought_sig": ANSI_COLORS["bright_magenta"],
    "sampling": ANSI_COLORS["blue"],
    "texttoolcall": ANSI_COLORS["blue"],
    "loop": ANSI_COLORS["bright_yellow"],
    "fakecall": ANSI_COLORS["bright_yellow"],
    # provider / chiavi
    "auth": ANSI_COLORS["bright_green"],
    "keyhealth": ANSI_COLORS["bright_green"],
    "health": ANSI_COLORS["green"],
    "models": ANSI_COLORS["green"],
    "strike": ANSI_COLORS["bright_red"],
    "vigile": ANSI_COLORS["bright_magenta"],
    # ops / persistenza
    "config": ANSI_COLORS["gray"],
    "policy": ANSI_COLORS["blue"],
    "sniff": ANSI_COLORS["bright_cyan"],
    "ledger": ANSI_COLORS["bright_green"],
    "atomic": ANSI_COLORS["gray"],
    "shutdown": ANSI_COLORS["bright_yellow"],
    "admin": ANSI_COLORS["magenta"],
    "csv": ANSI_COLORS["gray"],
    "erroraudit": ANSI_COLORS["bright_red"],
    "log": ANSI_COLORS["gray"],
    "purge": ANSI_COLORS["gray"],
    "retry": ANSI_COLORS["yellow"],
    "estimate": ANSI_COLORS["blue"],
    "effort": ANSI_COLORS["blue"],
    "images": ANSI_COLORS["bright_magenta"],
    "videos": ANSI_COLORS["bright_magenta"],
    "tts": ANSI_COLORS["bright_magenta"],
    "stt": ANSI_COLORS["bright_magenta"],
    "text": ANSI_COLORS["white"],
}

_FALLBACK_COLORS = [
    ANSI_COLORS["bright_cyan"], ANSI_COLORS["bright_magenta"],
    ANSI_COLORS["bright_blue"], ANSI_COLORS["bright_green"],
    ANSI_COLORS["cyan"], ANSI_COLORS["magenta"], ANSI_COLORS["blue"],
    ANSI_COLORS["yellow"],
]


def _stable_index(text: str, n: int) -> int:
    return zlib.crc32(text.encode("utf-8")) % max(1, n)


def tag_color(tag: str) -> str:
    """Colore del tag: dalla mappa, oppure deterministico (mai vuoto)."""
    return TAG_COLORS.get(tag) or _FALLBACK_COLORS[
        _stable_index(tag, len(_FALLBACK_COLORS))]


def module_color(name: str) -> str:
    return MODULE_COLORS.get(name) or _FALLBACK_COLORS[
        _stable_index(name, len(_FALLBACK_COLORS))]


# Module name -> color (nx.xxx modules)
MODULE_COLORS = {
    "nx.main": ANSI_COLORS["bright_cyan"],
    "nx.router": ANSI_COLORS["cyan"],
    "nx.forwarder": ANSI_COLORS["bright_magenta"],
    "nx.config": ANSI_COLORS["gray"],
    "nx.policy": ANSI_COLORS["blue"],
    "nx.auth": ANSI_COLORS["bright_green"],
    "nx.admin": ANSI_COLORS["magenta"],
    "nx.health": ANSI_COLORS["green"],
    "nx.keyhealth": ANSI_COLORS["bright_green"],
    "nx.ledger": ANSI_COLORS["bright_green"],
    "nx.sniff": ANSI_COLORS["bright_cyan"],
    "nx.ctxcompact": ANSI_COLORS["bright_cyan"],
    "nx.toolrepair": ANSI_COLORS["bright_blue"],
    "nx.histnorm": ANSI_COLORS["bright_blue"],
    "nx.capabilities": ANSI_COLORS["blue"],
    "nx.csv": ANSI_COLORS["gray"],
    "nx.erroraudit": ANSI_COLORS["bright_red"],
    "nx.atomic": ANSI_COLORS["gray"],
    "nx.provider_models": ANSI_COLORS["green"],
    "nx.sampling": ANSI_COLORS["blue"],
    "nx.texttoolparse": ANSI_COLORS["blue"],
    "nx.thought_sig": ANSI_COLORS["bright_magenta"],
    "nx.observability": ANSI_COLORS["gray"],
    "httpx": ANSI_COLORS["gray"],
}


def colorize_tag(tag: str) -> str:
    """Return a colorized tag string, e.g. '\\033[94m[summary]\\033[0m'."""
    return f"{tag_color(tag)}[{tag}]{ANSI_RESET}"


def colorize_level(level: str) -> str:
    """Return a colorized level name."""
    color = LEVEL_COLORS.get(level, "")
    if color:
        return f"{color}{level}{ANSI_RESET}"
    return level


def colorize_module(name: str) -> str:
    """Return a colorized module/logger name."""
    return f"{module_color(name)}{name}{ANSI_RESET}"


def is_terminal_stream(stream) -> bool:
    """Check if the stream is a real TTY (supports colors)."""
    try:
        return hasattr(stream, "isatty") and stream.isatty()
    except Exception:
        return False


# Regex per i tag [xxx] (minuscole, MAIUSC, trattini) e per le chiavi k=v
_TAG_RE = re.compile(r"(?<!\w)\[([A-Za-z][A-Za-z0-9_\-]{1,24})\]")
_KV_RE = re.compile(r"(?<![\w.\-])([A-Za-z_][\w.\-]*)=")

# Emoji per livello (quando il tag non ha un'emoji dedicata)
LEVEL_EMOJI = {
    "DEBUG": "🔍",
    "INFO": "•",
    "WARNING": "⚠️",
    "ERROR": "❌",
    "CRITICAL": "🛑",
}

# Emoji per tag: rende il log "scansionabile" a colpo d'occhio
TAG_EMOJI = {
    "start": "🚀", "shutdown": "🛑", "summary": "📊",
    "request": "📥", "route": "🧭", "pick": "🎯", "pick-final": "🎯",
    "identity": "🪪", "session": "🧵", "sticky": "📌",
    "cache": "💾", "ctxcompact": "🗜️", "truncation": "✂️",
    "ladder": "🪜", "caps": "🧩", "defer": "⏸️", "budget": "💰",
    "cooldown": "🧊", "cooldown-wakeup": "🌅", "circuit-breaker": "🔌",
    "latency-cross": "🐢", "esc-pin": "📍", "esc-pin-probe": "📍",
    "fallback": "🔁", "upstream": "☁️",
    "stream": "🌊", "watchdog": "⏱️", "stall": "🥶", "qc": "🔎",
    "repair": "🔧", "toolrepair": "🔧", "histnorm": "🧹",
    "thought_sig": "🧠", "sampling": "🎛️", "texttoolcall": "⌨️",
    "loop": "🔁", "fakecall": "🎭",
    "auth": "🔑", "keyhealth": "❤️", "health": "❤️", "models": "🧠",
    "strike": "⚡", "vigile": "👁️",
    "config": "⚙️", "policy": "📜", "sniff": "🐽", "ledger": "📒",
    "atomic": "🗄️", "admin": "🛠️", "csv": "📄", "erroraudit": "🚨",
    "log": "📝", "purge": "🗑️", "retry": "🔂", "estimate": "🧮",
    "effort": "🎚️", "images": "🖼️", "videos": "🎬", "tts": "🔊",
    "stt": "🎙️", "text": "💬",
}


class ColoredFormatter(logging.Formatter):
    """Console formatter nello stile `clog` (WHATSAPP-NEWv4):

        [2026-09-13 17:36:15] [nx.main] ⚠️ [watchdog] messaggio chiave=valore

    - timestamp fra parentesi (dim), modulo colorato fra parentesi;
    - emoji per tag (fallback: emoji del livello), tag e livello colorati;
    - colonne `key=value`: chiave in dim per non disturbare la lettura.
    I file restano PLAIN (make_plain_formatter) per logview.py.
    """

    TIME_FMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self,
                 fmt: str = "%(asctime)s %(levelname)s %(name)s %(message)s",
                 use_color: bool = True):
        super().__init__(fmt=fmt)
        self.use_color = use_color

    def formatTime(self, record: logging.LogRecord,
                   datefmt: Optional[str] = None) -> str:
        if datefmt:
            return datetime.fromtimestamp(record.created).strftime(datefmt)
        return datetime.fromtimestamp(record.created).strftime(self.TIME_FMT)

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record)
        msg = record.getMessage()
        if not self.use_color:
            return f"{ts} {record.levelname} {record.name} {msg}"
        tag = None
        m = _TAG_RE.search(msg)
        if m:
            tag = m.group(1)
        emoji = TAG_EMOJI.get(tag) or LEVEL_EMOJI.get(record.levelname, "")
        lvl_color = LEVEL_COLORS.get(record.levelname, "")
        head = f"{lvl_color}{emoji}{ANSI_RESET}" if emoji else ""
        rest = _TAG_RE.sub(lambda mm: colorize_tag(mm.group(1)), msg)
        rest = _KV_RE.sub(
            lambda mm: f"{ANSI_DIM}{mm.group(1)}{ANSI_RESET}=", rest)
        ts_s = f"{ANSI_DIM}[{ts}]{ANSI_RESET}"
        mod_s = f"{module_color(record.name)}[{record.name}]{ANSI_RESET}"
        return f"{ts_s} {mod_s} {head} {rest}".rstrip()


def setup_colored_logging(level: int = logging.INFO,
                           stream=None) -> logging.Handler:
    """Set up colored terminal logging and return the console handler.

    Pass ``use_color=False`` semantics via stream check: colors are emitted
    unconditionally; a TTY-aware caller can check is_terminal_stream().
    The file handler in _install_file_logging() uses a PLAIN formatter so
    that var/gateway.log remains parseable by app/logview.py.
    """
    if stream is None:
        stream = sys.stdout

    console_handler = logging.StreamHandler(stream)
    console_handler.setLevel(level)
    console_handler.setFormatter(ColoredFormatter())
    return console_handler


def make_plain_formatter() -> logging.Formatter:
    """Return a non-colored formatter identical in field order to the colored
    one, safe for file output that logview.py must parse."""
    return logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S,%f",
    )
