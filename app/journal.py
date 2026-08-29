"""Backup CSV pre-scrittura (rotazione) + journal append-only delle operazioni.

Garanzie:
- i backup vivono in <var>/backups/ (MAI versionati: var/ è gitignored);
- il journal è JSONL append-only (<var>/operations.jsonl): una voce per
  operazione admin riuscita, con NESSUN valore di chiave (solo flag);
- crash/corruzione del journal non blocca mai il servizio (append best-effort).
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

# FIX: rotazione per DIMENSIONE del journal (prima cresceva senza limiti).
JOURNAL_MAX_BYTES = 20 * 1024 * 1024        # 20MB prima della rotazione
JOURNAL_KEEP = 2                              # segmenti ruotati conservati (.1, .2)


def _paths(var_dir: str | Path) -> tuple[Path, Path]:
    var = Path(var_dir)
    return var / "backups", var / "operations.jsonl"


def backup_csv(csv_path: str | Path, var_dir: str | Path,
               keep: int = 20) -> Path | None:
    """Copia il CSV corrente nei backup PRIMA di una riscrittura, poi
    ruota mantenendo gli ultimi `keep`. Best-effort: un fallimento non
    deve impedire l'operazione amministrativa."""
    try:
        bdir, _ = _paths(var_dir)
        bdir.mkdir(parents=True, exist_ok=True)
        src = Path(csv_path)
        if not src.exists():
            return None
        dst = bdir / f"keys_rotation-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        shutil.copy2(src, dst)
        # rotazione: i più vecchi fuori
        backups = sorted(bdir.glob("keys_rotation-*.csv"))
        for old in backups[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)
        return dst
    except Exception:                        # noqa: BLE001 — mai bloccare admin
        return None


def record(var_dir: str | Path, op: str, details: dict | None = None) -> None:
    """Appende una voce {ts, op, ...details} al journal (best-effort)."""
    try:
        _, jpath = _paths(var_dir)
        jpath.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": round(time.time(), 3), "op": op}
        if details:
            entry.update(details)
        _rotate_if_needed(jpath)
        with open(jpath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:                        # noqa: BLE001
        pass


def _rotate_if_needed(jpath: Path) -> None:
    """Ruota il journal oltre JOURNAL_MAX_BYTES mantenendo JOURNAL_KEEP
    segmenti (.1, .2). Best-effort: un fallimento non blocca l'append."""
    try:
        if not jpath.exists():
            return
        if jpath.stat().st_size < JOURNAL_MAX_BYTES:
            return
        for i in range(JOURNAL_KEEP - 1, 0, -1):
            src = Path(f"{jpath}.{i}")
            if src.exists():
                os.replace(src, f"{jpath}.{i + 1}")
        os.replace(jpath, f"{jpath}.1")
    except Exception:                        # noqa: BLE001
        pass


def history(var_dir: str | Path, limit: int = 50) -> dict:
    """Ultime `limit` voci (più recenti prime) + totale. Legge anche i
    segmenti ruotati (.1, .2) così la rotazione non perde la storia."""
    _, jpath = _paths(var_dir)
    entries: list[dict] = []
    paths = [Path(f"{jpath}.{i}") for i in range(JOURNAL_KEEP, 0, -1)]
    paths.append(jpath)
    for p in paths:
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue                  # riga corrotta: salta
        except OSError:
            pass
    entries.reverse()                         # più recenti prime
    return {"total": len(entries),
            "entries": entries[: max(0, int(limit))][:100]}


