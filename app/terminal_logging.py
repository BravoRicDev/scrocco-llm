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

# Tag -> color (each tag category gets a distinct hue)
TAG_COLORS = {
    "summary": ANSI_COLORS["bright_blue"],
    "route": ANSI_COLORS["magenta"],
    "identity": ANSI_COLORS["bright_magenta"],
    "auth": ANSI_COLORS["bright_green"],
    "fallback": ANSI_COLORS["cyan"],
    "cooldown": ANSI_COLORS["bright_red"],
    "ladder": ANSI_COLORS["blue"],
    "vigile": ANSI_COLORS["bright_magenta"],
    "session": ANSI_COLORS["cyan"],
    "caps": ANSI_COLORS["blue"],
    "budget": ANSI_COLORS["yellow"],
    "defer": ANSI_COLORS["gray"],
    "stream": ANSI_COLORS["magenta"],
    "qc": ANSI_COLORS["cyan"],
    "config": ANSI_COLORS["gray"],
    "erroraudit": ANSI_COLORS["bright_red"],
    "log": ANSI_COLORS["gray"],
}

# Module name -> color (nx.xxx modules)
MODULE_COLORS = {
    "nx.main": ANSI_COLORS["bright_cyan"],
    "nx.router": ANSI_COLORS["cyan"],
    "nx.auth": ANSI_COLORS["bright_green"],
    "nx.config": ANSI_COLORS["gray"],
    "nx.policy": ANSI_COLORS["blue"],
    "nx.forwarder": ANSI_COLORS["bright_magenta"],
    "nx.erroraudit": ANSI_COLORS["bright_red"],
    "httpx": ANSI_COLORS["gray"],
}


def colorize_tag(tag: str) -> str:
    """Return a colorized tag string, e.g. '\\033[94m[summary]\\033[0m'."""
    color = TAG_COLORS.get(tag, "")
    if color:
        return f"{color}[{tag}]{ANSI_RESET}"
    return f"[{tag}]"


def colorize_level(level: str) -> str:
    """Return a colorized level name."""
    color = LEVEL_COLORS.get(level, "")
    if color:
        return f"{color}{level}{ANSI_RESET}"
    return level


def colorize_module(name: str) -> str:
    """Return a colorized module/logger name."""
    color = MODULE_COLORS.get(name, "")
    if color:
        return f"{color}{name}{ANSI_RESET}"
    return name


def is_terminal_stream(stream) -> bool:
    """Check if the stream is a real TTY (supports colors)."""
    try:
        return hasattr(stream, "isatty") and stream.isatty()
    except Exception:
        return False


# Regex to find [tag] patterns in log messages
_TAG_RE = re.compile(r"(?<!\w)\[([a-z][a-z_]*)\]")


class ColoredFormatter(logging.Formatter):
    """Formatter that adds ANSI colors to log records for terminal output.

    - Level name colored by LEVEL_COLORS.
    - Module name colored by MODULE_COLORS.
    - [tag] patterns found in the message are colored by TAG_COLORS.

    The file handlers should use a PLAIN (uncolored) formatter to keep
    var/gateway.log parseable by app/logview.py.
    """

    TIME_FMT = "%Y-%m-%d %H:%M:%S"

    def __init__(self,
                 fmt: str = "%(asctime)s %(levelname)s %(name)s %(message)s",
                 use_color: bool = True):
        super().__init__(fmt=fmt)
        self.use_color = use_color

    # -- timestamp with milliseconds: 2026-08-29 08:42:00,050 -----------------
    def formatTime(self, record: logging.LogRecord,
                   datefmt: Optional[str] = None) -> str:
        ct = datetime.fromtimestamp(record.created)
        if datefmt:
            return ct.strftime(datefmt)
        s = ct.strftime(self.TIME_FMT)
        ms = int(record.msecs)  # already fractional milliseconds
        return f"{s},{ms:03d}"

    def format(self, record: logging.LogRecord) -> str:
        # Let the parent format the record first (plain).
        base = super().format(record)

        if not self.use_color:
            return base

        # Apply colors to the structured fields by splitting on whitespace.
        # Record shape: "<ts> <LEVEL> <module> <rest>"
        parts = base.split(" ", 3)
        if len(parts) < 4:
            # Fallback: just color the whole line by level.
            color = LEVEL_COLORS.get(record.levelname, "")
            return f"{color}{base}{ANSI_RESET}"

        ts, level, module, rest = parts

        level_colored = (
            f"{LEVEL_COLORS.get(record.levelname, '')}{level}{ANSI_RESET}"
        )
        module_colored = colorize_module(module)

        # Colorize [tag] patterns inside rest.
        def _paint_tag(m):
            tag = m.group(1)
            return colorize_tag(tag)
        rest_colored = _TAG_RE.sub(_paint_tag, rest)

        # Optional dim timestamp
        ts_dim = f"{ANSI_DIM}{ts}{ANSI_RESET}"

        line = f"{ts_dim} {level_colored} {module_colored} {rest_colored}"
        return line


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
