"""Scritture di CSV e policy: uno scrittore alla volta, anche tra worker.

[IT] Admin API, apprendimento dei flag CSV (app/csvlearn.py) e auto-learn
delle capacita' fanno lettura-modifica-scrittura di `keys_rotation.csv` o
`gateway.yaml`. Con piu' worker due scritture contemporanee potevano
sovrascriversi (l'ultima vinceva, la prima spariva). Qui c'e' UN lock tra
processi (`var/.config-write.lock`) che copre l'intera lettura-modifica-
scrittura, e l'avviso agli altri worker di ricaricare subito invece di
aspettare il prossimo giro del watcher (GATEWAY_WATCH_SECONDS).

Con un solo processo il lock non e' mai conteso e l'avviso non parte.

[EN] Single writer for CSV/policy across workers + immediate reload notice.
"""
from __future__ import annotations

from pathlib import Path

from . import cluster
from . import state as gw_state
from .jsonl_store import async_file_lock, path_lock

LOCK_NAME = ".config-write.lock"


def lock_path(var_dir: str | Path | None = None) -> Path:
    return Path(var_dir if var_dir is not None else gw_state.VAR_DIR) / LOCK_NAME


def locked(var_dir: str | Path | None = None):
    """Lock SINCRONO: solo fuori dall'event loop (thread) o per sezioni senza await."""
    return path_lock(lock_path(var_dir))


def locked_async(var_dir: str | Path | None = None):
    """Lock per il codice async: attende senza bloccare l'event loop."""
    return async_file_lock(lock_path(var_dir))


def changed() -> None:
    """CSV/policy riscritti: gli altri worker ricaricano subito."""
    cluster.notify_config_changed()
