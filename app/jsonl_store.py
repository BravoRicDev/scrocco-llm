"""Primitive condivise per i file JSONL append-only con rotazione a segmenti.

[IT] Ledger usage, repair ledger e journal operazioni ruotavano i file con
tre copie della stessa logica (shift .1 -> .2, corrente -> .1). Qui vive
l'unica implementazione; la gestione degli errori (log/silenzio) resta al
chiamante.

[EN] Shared size-based segment rotation and batched JSONL append.
"""
from __future__ import annotations

import json
import os


def rotate_segments(path: str | os.PathLike, max_bytes: int, keep: int) -> bool:
    """Se `path` esiste e supera `max_bytes`, sposta `.i -> .i+1` (fino a
    `keep`) e il corrente in `.1`. Ritorna True se ha ruotato. Solleva
    OSError: il chiamante decide se loggare o ignorare."""
    path = os.fspath(path)
    if not os.path.exists(path):
        return False
    if os.path.getsize(path) < max_bytes:
        return False
    for i in range(keep - 1, 0, -1):
        src = f"{path}.{i}"
        if os.path.exists(src):
            os.replace(src, f"{path}.{i + 1}")
    os.replace(path, f"{path}.1")
    return True


def append_jsonl(path: str | os.PathLike, rows: list[dict]) -> None:
    """Appende `rows` in formato JSONL compatto (una open per batch)."""
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":"),
                               default=str) + "\n")
