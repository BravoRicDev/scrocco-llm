#!/usr/bin/env python3
"""Imposta intelligence_score=10 per tutte le righe con modello `deepseek-v4*`.

Uso:  python3 scripts/set_deepseek_intel.py <keys_rotation.csv> [<csv2> ...]

- Header-aware: individua le colonne `modello` e `intelligence_score`.
- Colonna intelligence_score assente -> skip (nessuna modifica).
- Riga il cui modello contiene "deepseek-v4" (case-insensitive) e non ha gia'
  intelligenza 10 -> impostata a 10.
- Backup `.bak-intel` del file originale (solo se non esiste) + scrittura
  atomica (tempfile nella stessa dir + os.replace): il watcher del gateway
  ricarica il CSV al cambio di mtime senza riavvii.
"""
import csv
import os
import shutil
import sys
import tempfile


def _idx(header: list[str], name: str) -> int | None:
    try:
        return header.index(name)
    except ValueError:
        return None


def process(path: str) -> int:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        print(f"[skip] {path}: file vuoto")
        return 0
    header = rows[0]
    icol = _idx(header, "intelligence_score")
    if icol is None:
        print(f"[skip] {path}: colonna 'intelligence_score' assente")
        return 0
    mcol = _idx(header, "modello") or 1
    changed = 0
    for r in rows[1:]:
        if len(r) <= icol:
            continue
        mod = (r[mcol] if len(r) > mcol else "").lower()
        if "deepseek-v4" in mod and r[icol].strip() != "10":
            r[icol] = "10"
            changed += 1
    if not changed:
        print(f"[ok] {path}: nessuna modifica (deepseek-v4 gia' a 10)")
        return 0
    bak = f"{path}.bak-intel"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"[ok] {path}: {changed} righe deepseek-v4 -> intelligence_score=10")
    return changed


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    total = 0
    for p in argv:
        total += process(p)
    return 0 if total >= 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))