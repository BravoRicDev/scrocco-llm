"""Bootstrap delle finestre rolling-24h dai log.

All'avvio ricostruisce due registri dai marker del log:
  - *uso* (tutti i tentativi ok+fail di ogni deployment): righe `[fallback]`
    (fallimento) e `[summary]` (esito finale, `"dep"` = successo);
  - *probe*: righe `[autoprobe] <dep>: probe OK|KO`.

La scansione e' pura (nessuno stato globale) e testabile.
"""
from __future__ import annotations

import calendar
import re
import time
from pathlib import Path

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_FALLBACK_RE = re.compile(r"\[fallback\]\s+stream\s+(\S+)\s")
_SUMMARY_RE = re.compile(r"\[summary\]\s+\{.*?\"dep\":\s*\"([^\"]+)\"")
_AUTOPROBE_RE = re.compile(r"\[autoprobe\]\s+(\S+):\s+probe\s+(OK|KO)")


def scan_log(path, cutoff: float):
    """Ritorna (usage, probes) come liste di `(unique, ts)` con `ts >= cutoff`.

    `usage` = tentativi ok+fail; `probes` = probe effettuati. Se il file non
    esiste ritorna due liste vuote (mai sollevare eccezioni)."""
    usage: list[tuple[str, float]] = []
    probes: list[tuple[str, float]] = []
    p = Path(path)
    if not p.exists():
        return usage, probes
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _TS_RE.match(line)
            if not m:
                continue
            try:
                ts = float(calendar.timegm(time.strptime(
                    m.group(1), "%Y-%m-%d %H:%M:%S")))
            except Exception:
                continue
            if ts < cutoff:
                continue
            fm = _FALLBACK_RE.search(line)
            if fm:
                usage.append((fm.group(1), ts))
                continue
            sm = _SUMMARY_RE.search(line)
            if sm:
                usage.append((sm.group(1), ts))
                continue
            am = _AUTOPROBE_RE.search(line)
            if am:
                probes.append((am.group(1), ts))
    return usage, probes
