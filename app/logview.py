"""Pure, stdlib-only helpers for reading/parsing gateway log files.

Read-only log views for the admin API (/admin/logs/*). No imports from
app.main: this module must stay importable without booting the service.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

# Line shape (summary/route/identity/fallback trails):
#   YYYY-MM-DD HH:MM:SS,mmm LEVEL nx.<mod> [<tag>] <rest>
_LINE_RE = re.compile(
    r"^(?P<ts>.{23})\s+(?P<level>\S+)\s+(?P<logger>\S+)\s+"
    r"\[(?P<tag>[^\]]+)\]\s+(?P<rest>.*)$"
)

# Error-audit line shape:
#   YYYY-MM-DD HH:MM:SS,mmm WARNING nx.erroraudit status=<N> :: <json>
_ERROR_RE = re.compile(
    r"^(?P<ts>.{23})\s+\S+\s+\S+\s+status=(?P<st>-?\d+)\s+::\s+(?P<js>.*)$"
)


def _read_tail_lines(paths: list[str], max_lines: int) -> list[str]:
    """Return the most recent `max_lines` lines from `paths`, in chronological
    ascending order.

    `paths` must be ordered oldest-first then newest (e.g. [log.1, log]). We
    read from the newest file backwards: tail of the main log first, and if it
    has fewer than `max_lines` lines we fill the remainder from the tail of the
    older (.1) file. Files under 5 MB are read whole; larger files use only a
    trailing ~1 MB block.
    """
    if max_lines <= 0:
        return []
    collected: list[list[str]] = []
    remaining = max_lines
    for path in reversed(paths):
        if remaining <= 0:
            break
        if not os.path.exists(path):
            continue
        chunk = _tail_of_file(path, remaining)
        if chunk:
            collected.append(chunk)
            remaining -= len(chunk)
    # collected is newest-first; reverse it for ascending chronological order.
    out: list[str] = []
    for chunk in reversed(collected):
        out.extend(chunk)
    return out


def _tail_of_file(path: str, max_lines: int) -> list[str]:
    """Read at most the last `max_lines` lines of a single file."""
    size = os.path.getsize(path)
    if size == 0:
        return []
    if size < 5 * 1024 * 1024:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    else:
        block = 1024 * 1024
        with open(path, "rb") as f:
            f.seek(max(0, size - block))
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        # The first element may be a fragment cut mid-line; drop it.
        if size > block and lines:
            lines = lines[1:]
    if max_lines > 0 and len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


def _parse_ts(prefix: str) -> float | None:
    """Convert the 23-char timestamp prefix to epoch float, or None."""
    try:
        return datetime.strptime(prefix[:23], "%Y-%m-%d %H:%M:%S,%f").timestamp()
    except Exception:
        return None


def _get_level_color_name(level: str) -> str:
    """Return color name for a log level (for UI display)."""
    _LEVEL_COLOR_NAMES = {
        "INFO": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "red",
        "DEBUG": "cyan",
    }
    return _LEVEL_COLOR_NAMES.get(level, "")


def _get_tag_color_name(tag: str) -> str:
    """Return color name for a log tag (for UI display)."""
    _TAG_COLOR_NAMES = {
        "summary": "cyan",
        "route": "blue",
        "identity": "magenta",
        "auth": "green",
        "fallback": "yellow",
        "cooldown": "red",
        "ladder": "blue",
        "vigile": "magenta",
        "session": "cyan",
        "caps": "blue",
        "budget": "yellow",
        "defer": "gray",
        "stream": "cyan",
        "qc": "yellow",
        "config": "gray",
        "erroraudit": "red",
    }
    return _TAG_COLOR_NAMES.get(tag, "")


def parse_summary_lines(text_lines, tags: set[str], since: float | None,
                        tail: int) -> list[dict]:
    """Parse gateway.log lines whose tag is in `tags`.

    For tag=summary the trailing payload is decoded as JSON; lines that fail
    JSON are skipped. Other tags keep their raw text (raw={"text": rest}) and
    leave the structured fields None. Returns events sorted by ts ascending,
    truncated to the last `tail`.
    """
    events: list[dict] = []
    for line in text_lines:
        if not line or len(line) < 23:
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        tag = m.group("tag")
        if tag not in tags:
            continue
        ts = _parse_ts(m.group("ts"))
        rest = m.group("rest")
        if tag == "summary":
            try:
                raw = json.loads(rest)
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            event = {
                "ts": ts,
                "level": m.group("level"),
                "tag": tag,
                "profile": raw.get("profile"),
                "grp": raw.get("grp"),
                "dep": raw.get("dep"),
                "model": raw.get("model"),
                "dur_ms": raw.get("dur_ms"),
                "tries": raw.get("tries"),
                "fb": raw.get("fb"),
                "qc": raw.get("qc"),
                "status": raw.get("status"),
                # campi extra rilevanti, con default - quando assenti
                "via": raw.get("via"),
                "ttfb_ms": raw.get("ttfb_ms"),
                "wd": raw.get("wd"),
                "stream": raw.get("stream"),
                "kind": raw.get("kind"),
                "extra": raw.get("extra"),
                "raw": raw,
                # Campi color-coded per terminale
                "tag_color": _get_tag_color_name(tag),
                "level_color": _get_level_color_name(m.group("level")),
                "tag_bracket": f"[{tag}]" if tag else "[]",
            }
        else:
            event = {
                "ts": ts,
                "level": m.group("level"),
                "tag": tag,
                "profile": None,
                "grp": None,
                "dep": None,
                "model": None,
                "dur_ms": None,
                "tries": None,
                 "fb": None,
                "qc": None,
                "status": None,
                "via": None,
                "ttfb_ms": None,
                "wd": None,
                "stream": None,
                "kind": None,
                "extra": None,
                "raw": {"text": rest},
                # Campi color-coded per terminale
                "tag_color": _get_tag_color_name(tag),
                "level_color": _get_level_color_name(m.group("level")),
                "tag_bracket": f"[{tag}]" if tag else "[]",
            }
        events.append(event)
    if since is not None:
        events = [e for e in events
                  if e["ts"] is not None and e["ts"] > since]
    events.sort(key=lambda e: (e["ts"] is None, e["ts"] if e["ts"] is not None else 0))
    if tail and tail > 0 and len(events) > tail:
        events = events[-tail:]
    return events


def parse_error_lines(text_lines, filt: str | None, since: float | None,
                      tail: int) -> list[dict]:
    """Parse var/error-audit.log lines (status=N :: <json>).

    filt: None -> no filter. All-numeric (optional '-') -> keep only
    status == int(filt). Otherwise case-insensitive substring match on
    error_type or error_message. Returns events sorted by ts ascending,
    truncated to the last `tail`.
    """
    events: list[dict] = []
    for line in text_lines:
        if not line or len(line) < 23:
            continue
        m = _ERROR_RE.match(line)
        if not m:
            continue
        ts = _parse_ts(m.group("ts"))
        js_str = m.group("js")
        try:
            js = json.loads(js_str)
        except Exception:
            js = None
        status = int(m.group("st"))
        error_type = None
        error_message = None
        if isinstance(js, dict):
            err = js.get("error")
            if isinstance(err, dict):
                error_type = err.get("type")
                error_message = err.get("message")
        if error_type is None and error_message is None:
            error_message = (js_str or "")[:300]
        events.append({
            "ts": ts,
            "status": status,
            "error_type": error_type,
            "error_message": error_message,
        })
    if filt is not None:
        f = filt.strip()
        if re.fullmatch(r"-?\d+", f):
            target = int(f)
            events = [e for e in events if e["status"] == target]
        else:
            needle = f.lower()
            events = [e for e in events
                      if (needle in str(e["error_type"] or "").lower())
                      or (needle in str(e["error_message"] or "").lower())]
    if since is not None:
        events = [e for e in events
                  if e["ts"] is not None and e["ts"] > since]
    events.sort(key=lambda e: (e["ts"] is None, e["ts"] if e["ts"] is not None else 0))
    if tail and tail > 0 and len(events) > tail:
        events = events[-tail:]
    return events
