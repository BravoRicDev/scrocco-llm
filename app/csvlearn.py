"""Apprendimento automatico di flag per-deployment scritti nel CSV.

Quando il path di risposta scopre un'informazione sul PROVIDER (es. "esige il
replay del reasoning_content nei turni thinking"), la salviamo nella colonna
del deployment su TUTTE le righe dello stesso modello (gemelli con chiavi
diverse, in qualunque dim): cosi' la volta dopo la richiesta parte GIA'
corretta — niente piu' errori continui e niente ritocchi reattivi.

Prima in memoria (immediato, nessun reload: i tentativi successivi della
stessa richiesta sono gia' a posto), poi persistenza su CSV in background
(single-flight per modello, best-effort: un errore di scrittura non deve mai
toccare la risposta al client)."""
import asyncio
import logging
import threading
from pathlib import Path

from . import csv_store, journal, metrics
from .config import THINKING_REPLAY_HEADER

log = logging.getLogger("nx.csvlearn")

_LOCK = threading.Lock()
_PERSISTED: set[tuple[str, str]] = set()   # (flag, modello) gia' persistiti
_TASKS: set[asyncio.Task] = set()


def _csv_paths(config) -> tuple[Path | None, Path | None]:
    p = getattr(config, "csv_path", None)
    if not p:
        return None, None
    p = Path(p)
    return p, p.parent


def mark_twins_in_memory(config, model: str,
                         flag: str = "thinking_replay") -> int:
    """Marca il flag su OGNI deployment dello stesso modello (tutte le chiavi
    e tutte le dim). Ritorna quante righe sono state marcate ora."""
    n = 0
    for deps in (getattr(config, "groups", {}) or {}).values():
        for d in deps:
            if d.get("model") == model and not d.get(flag):
                d[flag] = True
                n += 1
    return n


def _persist(config, csv_path: Path, var_dir: Path, model: str) -> int:
    header, rows = csv_store.load_table(csv_path)
    if not header:
        return 0
    csv_store.ensure_flag_column(header)
    n = 0
    for r in rows:
        if (r.get("modello") or "").strip() == model:
            r[THINKING_REPLAY_HEADER] = "true"
            n += 1
    if not n:
        return 0
    journal.backup_csv(csv_path, var_dir)
    csv_store.save_table(csv_path, header, rows, like=config)
    config.reload()
    return n


async def _persist_bg(config, csv_path: Path, var_dir: Path, model: str):
    try:
        n = await asyncio.to_thread(_persist, config, csv_path, var_dir, model)
        if n:
            metrics.inc("nx_thinking_replay_total", ("learned",))
            log.info("[thinking-replay] flag salvato su %d righe "
                     "(modello %s)", n, model)
    except Exception as exc:                      # noqa: BLE001
        log.warning("[thinking-replay] persistenza flag fallita per %s: %s",
                    model, exc)


def learn_thinking_replay(router_or_config, model: str | None) -> int:
    """Impara il flag `thinking_replay` per un modello: subito in memoria
    (tutti i gemelli), poi scrittura CSV in background (una volta sola)."""
    if not model:
        return 0
    config = getattr(router_or_config, "config", router_or_config)
    n = mark_twins_in_memory(config, model)
    key = ("thinking_replay", model)
    with _LOCK:
        if key in _PERSISTED:
            return n
        _PERSISTED.add(key)
    csv_path, var_dir = _csv_paths(config)
    if not csv_path:
        return n
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return n                              # fuori da un loop: solo memoria
    t = loop.create_task(_persist_bg(config, csv_path, var_dir, model))
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return n
