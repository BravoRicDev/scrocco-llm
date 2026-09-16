"""Tracciamento delle riparazioni/salvataggi applicati alle risposte.

[IT] COSA: ogni volta che una risposta upstream viene "aggiustata" prima di
essere servita al client, si registra l'evento (a) nel LOG a schermo con un
prefisso distinguibile e (b) in modo PERSISTENTE su var/repair_ledger.jsonl
(append-only, rotazione per dimensione) cosi' i conteggi sopravvivono ai
restart.

Tre TIPOLOGIE (family), con sotto-tipi (kind):
  - "repair"  : una tool-call STRUTTURATA con argomenti JSON rotti viene
                riparata.
                kind = repair_args          (moves reali di repair_arguments)
                kind = repair_trunc_close   (JSON chiuso perche' troncato)
  - "salvage" : una tool-call NON strutturata viene RECUPERATA.
                kind = salvage_text         (resa come testo, texttoolparse)
                kind = salvage_truncated    (tag tool-call rotto/troncato)
  - "struct"  : l'OUTPUT STRUTTURATO (JSON/JSON-Schema) viene aggiustato.
                kind = struct_cleaned       (JSON puro estratto da fence/prosa)
                kind = struct_repaired      (JSON riparato schema-driven)
                kind = struct_invalid       (non conforme, non riparabile)
                kind = struct_corrective    (retry correttivo inviato)

`outcome`: "ok" = aggiustamento applicato, "fail" = non riuscito,
"abort" = chiusura forzata di emergenza.

[EN] WHAT: unified accounting of served repairs/salvages: screen log +
persistent JSONL ledger (survives restarts), split by family/kind.
WHY: the streaming repair path used to be silent, so the number of
successful repairs could not be answered from logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import defaultdict

from . import metrics

log = logging.getLogger("nx.repair")

# Le TIPOLOGIE di riparazione (family) e i loro sotto-tipi (kind).
FAMILY: dict[str, str] = {
    "repair_args": "repair",            # argomenti JSON riparati
    "repair_trunc_close": "repair",     # JSON chiuso perche' troncato
    "salvage_text": "salvage",          # tool-call recuperata dal testo
    "salvage_truncated": "salvage",     # tool-call recuperata da tag rotto
    "struct_cleaned": "struct",         # JSON puro estratto da fence/prosa
    "struct_repaired": "struct",        # JSON riparato schema-driven
    "struct_invalid": "struct",         # output strutturato non recuperabile
    "struct_corrective": "struct",      # retry correttivo inviato
}
KINDS: tuple[str, ...] = tuple(FAMILY)
FAMILIES: tuple[str, ...] = ("repair", "salvage", "struct")

_LEDGER_MAX_BYTES = int(
    os.environ.get("REPAIR_LEDGER_MAX_BYTES", str(4 * 1024 * 1024)) or
    (4 * 1024 * 1024))
_LEDGER_KEEP = int(os.environ.get("REPAIR_LEDGER_KEEP", "2") or "2")
_LEDGER_NAME = "repair_ledger.jsonl"


class RepairLog:
    """Buffer in memoria + append JSONL su disco (stesso schema del ledger
    usage): il flush periodico lo fa il watcher di main.py, piu' uno finale
    allo shutdown."""

    def __init__(self, var_dir=None):
        self.path: str | None = None
        self._buf: list[dict] = []
        self._lock = threading.Lock()
        if var_dir:
            self.configure(var_dir)

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def configure(self, var_dir) -> None:
        if var_dir:
            self.path = os.path.join(str(var_dir), _LEDGER_NAME)

    # -------------------------------------------------------------- note --
    def note(self, kind: str, *, source: str, outcome: str = "ok",
             dep: str = "", model: str = "", detail: str = "",
             count: int = 1, **extra) -> None:
        """Registra UNA riparazione/salvataggio.

        `kind` e' uno dei sotto-tipi; `family` e' derivato. Non solleva MAI
        eccezioni: il tracking non deve mai mordere la risposta.
        """
        try:
            family = FAMILY.get(kind, "repair")
            n = max(1, int(count or 1))
            tail = f" x{n}" if n != 1 else ""
            msg = ("[%s] %s source=%s outcome=%s dep=%s%s%s"
                   % (family, kind, source, outcome, dep or "?", tail,
                      (" :: " + detail) if detail else ""))
            if outcome == "ok":
                log.info("%s", msg)
            else:
                log.warning("%s", msg)
            try:
                metrics.inc("nx_repair_events_total",
                            (family, kind, outcome), float(n))
            except Exception:                       # noqa: BLE001
                pass
            entry = {
                "ts": int(time.time()),
                "kind": kind, "family": family, "source": source,
                "outcome": outcome, "dep": dep, "model": model,
                "count": n, "detail": str(detail)[:400],
            }
            for k, v in extra.items():
                if v not in (None, ""):
                    entry[k] = v
            if self.path:
                with self._lock:
                    self._buf.append(entry)
        except Exception:                           # noqa: BLE001
            log.debug("[repair] note error", exc_info=True)

    # ------------------------------------------------------------- flush --
    def flush(self) -> int:
        """Scrive il buffer su disco (append); ritorna le righe scritte."""
        with self._lock:
            if not self._buf:
                return 0
            rows = self._buf
            self._buf = []
        try:
            self._rotate()
            with open(self.path, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False,
                                       separators=(",", ":"), default=str)
                            + "\n")
            return len(rows)
        except Exception:                           # noqa: BLE001
            with self._lock:                        # rimetti in coda
                self._buf = rows + self._buf
            log.debug("[repair] flush error", exc_info=True)
            return 0

    def flush_sync(self) -> int:
        return self.flush()

    async def flush_async(self) -> int:
        try:
            return await asyncio.to_thread(self.flush)
        except Exception:                           # noqa: BLE001
            return 0

    def _rotate(self) -> None:
        try:
            if not self.path or not os.path.exists(self.path):
                return
            if os.path.getsize(self.path) < _LEDGER_MAX_BYTES:
                return
            for i in range(_LEDGER_KEEP - 1, 0, -1):
                src = f"{self.path}.{i}"
                if os.path.exists(src):
                    os.replace(src, f"{self.path}.{i + 1}")
            os.replace(self.path, f"{self.path}.1")
            log.info("[repair] rotazione ledger")
        except Exception:                           # noqa: BLE001
            log.debug("[repair] rotate error", exc_info=True)


REPAIRLOG = RepairLog(None)


def configure(var_dir) -> None:
    REPAIRLOG.configure(var_dir)


def note(kind: str, **kw) -> None:
    REPAIRLOG.note(kind, **kw)


def flush_sync() -> int:
    return REPAIRLOG.flush_sync()


async def flush_async() -> int:
    return await REPAIRLOG.flush_async()


def _paths() -> list[str]:
    if not REPAIRLOG.path:
        return []
    out: list[str] = []
    for i in range(_LEDGER_KEEP, 0, -1):
        p = f"{REPAIRLOG.path}.{i}"
        if os.path.exists(p):
            out.append(p)
    if os.path.exists(REPAIRLOG.path):
        out.append(REPAIRLOG.path)
    return out


def read_all(limit: int = 0) -> list[dict]:
    rows: list[dict] = []
    for p in _paths():
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    if limit and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def aggregate(limit: int = 0) -> dict:
    """Conteggi per tipologia/sotto-tipo/esito/giorno (dai file persistenti)."""
    rows = read_all(limit=limit)
    by_kind: dict[str, int] = defaultdict(int)
    by_family: dict[str, int] = defaultdict(int)
    by_day: dict[str, int] = defaultdict(int)
    by_dep: dict[str, int] = defaultdict(int)
    by_source: dict[str, int] = defaultdict(int)
    ok = fail = 0
    for r in rows:
        n = int(r.get("count") or 1)
        k = str(r.get("kind") or "?")
        fam = str(r.get("family") or FAMILY.get(k, "?"))
        by_kind[k] += n
        by_family[fam] += n
        by_source[str(r.get("source") or "?")] += n
        if r.get("outcome") == "ok":
            ok += n
        else:
            fail += n
        day = time.strftime("%Y-%m-%d",
                            time.localtime(int(r.get("ts") or 0)))
        by_day[day] += n
        dep = str(r.get("dep") or "")
        if dep:
            by_dep[dep] += n
    return {
        "events": len(rows),
        "total": sum(by_kind.values()),
        "outcomes": {"ok": ok, "fail": fail},
        "by_family": dict(sorted(by_family.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "by_source": dict(sorted(by_source.items())),
        "by_day": dict(sorted(by_day.items())),
        "top_dep": dict(sorted(by_dep.items(),
                               key=lambda kv: -kv[1])[:20]),
    }
