"""Persistenza JSON atomica con copia di backup per il recupero.

[IT] COSA: helper condiviso per i piccoli file di stato su disco
(`key_health.json`, `adaptive_stats.json`, `thought_sigs.json`). La scrittura
e' ATOMICA (tmp -> os.replace) e mantiene una copia `path.bak`: se il processo
muore durante la scrittura il file principale resta valido; se al caricamento
il file principale e' vuoto/corrotto, si tenta il `.bak` prima di ripartire da
zero (evita di perdere la cronologia retirement/failimenti per una singola
scrittura interrotta).

[EN] Atomic JSON persistence with a `.bak` recovery copy.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import shutil
import threading

log = logging.getLogger("nx.atomic")

# Le scritture possono arrivare sia dall'event loop sia da thread (watcher,
# ledger). Il lock evita che due scrittori dello stesso file si contendano
# `path.tmp`; la sequenza degli snapshot evita che uno snapshot VECCHIO
# (es. l'ultimo tick del watcher, finito dopo il flush di shutdown)
# sovrascriva su disco uno piu' recente.
_WRITE_LOCK = threading.Lock()
_SNAPSHOT_SEQ = itertools.count(1)
_WRITTEN_SEQ: dict[str, int] = {}


def encode_json(obj, *, indent=None) -> str:
    """Testo JSON identico, byte per byte, a `json.dump(obj, f, indent=indent)`
    (stesso encoder, stessi pezzi concatenati)."""
    return "".join(json.JSONEncoder(indent=indent).iterencode(obj))


class JsonSnapshot:
    """Stato codificato in JSON in un istante preciso (`seq` crescente)."""

    __slots__ = ("seq", "_text", "_error")

    def __init__(self, obj, *, indent=None):
        self.seq = next(_SNAPSHOT_SEQ)
        self._text: str | None = None
        self._error: Exception | None = None
        try:
            self._text = encode_json(obj, indent=indent)
        except (TypeError, ValueError) as exc:
            self._error = exc

    def text(self) -> str:
        """Il testo; un errore di codifica viene sollevato qui, alla scrittura,
        che lo gestisce come ha sempre fatto `save_json` (log + False)."""
        if self._error is not None:
            raise self._error
        return self._text or ""


def freeze_json(obj, *, indent=None) -> JsonSnapshot:
    """Codifica SUBITO `obj` (fotografia dello stato, da fare sull'event loop);
    la scrittura con `save_json_text` puo' poi girare su un thread."""
    return JsonSnapshot(obj, indent=indent)


def save_json(path, obj, *, indent=None, backup: bool = True) -> bool:
    """Scrive `obj` in `path` atomicamente, aggiornando `path.bak`.

    Ritorna True su successo. Su errore non solleva (best-effort): lo stato
    persistente non deve mai far cadere il gateway.
    """
    return save_json_text(path, freeze_json(obj, indent=indent), backup=backup)


def save_json_text(path, snapshot: JsonSnapshot, *, backup: bool = True) -> bool:
    """Come `save_json` per uno snapshot gia' codificato. Se su disco c'e' gia'
    uno snapshot PIU' RECENTE dello stesso file, non lo sovrascrive (True:
    il dato aggiornato e' gia' salvato)."""
    p = str(path)
    with _WRITE_LOCK:
        if _WRITTEN_SEQ.get(p, 0) > snapshot.seq:
            return True
        ok = _write_atomic(p, snapshot, backup)
        if ok:
            _WRITTEN_SEQ[p] = snapshot.seq
        return ok


def _write_atomic(p: str, snapshot: JsonSnapshot, backup: bool) -> bool:
    tmp = p + ".tmp"
    try:
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(snapshot.text())
        os.replace(tmp, p)                   # atomico
        if backup:
            try:
                shutil.copy2(p, p + ".bak")
            except OSError:
                pass
        return True
    except (OSError, TypeError, ValueError):
        log.error("[atomic] save %s fallito", p, exc_info=True)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def load_json(path, default_factory=dict):
    """Carica `path`; se vuoto/corrotto prova `path.bak`, poi il default.

    `default_factory` e' una callable senza argomenti (es. `dict`/`list`).
    """
    p = str(path)
    for candidate, is_bak in ((p, False), (p + ".bak", True)):
        try:
            with open(candidate, encoding="utf-8") as f:
                txt = f.read()
        except (FileNotFoundError, OSError):
            continue
        if not txt.strip():
            continue
        try:
            obj = json.loads(txt)
        except ValueError:
            log.warning("[atomic] %s illeggibile%s", candidate,
                        " (provo .bak)" if not is_bak else "")
            continue
        if is_bak:
            log.warning("[atomic] %s corrotto: recuperato da .bak", p)
        return obj
    return default_factory()
