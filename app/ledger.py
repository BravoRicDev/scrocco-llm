"""Ledger usage/costi persistente + stima prezzi.

[IT] COSA: append-only JSONL (var/usage_ledger.jsonl) con una riga per
richiesta servita, alimentata da _emit_summary(). WHY: i [summary] nei log
muoiono al restart e non si aggregano; il ledger permette GET
/admin/insights (burn per profilo/modello/giorno, costi stimati vs
riportati). HOW:
  - buffer in memoria + flush periodico dal watcher di main.py (+ flush()
    forzato allo shutdown): append sincrono per OGNI richiesta costerebbe
    una fsync a chiamata.
  - rotazione per DIMENSIONE (>LEDGER_MAX_BYTES -> .1, mantieni 2): il
    disco non cresce all'infinito; l'aggregazione legge tutti i .jsonl*.
  - pricing da policy ("pricing": {"glob": {"prompt_per_1m": x,
    "completion_per_1m": y}}): cost_est calcolato SOLO se il provider non
    ha gia' mandato un costo reale (cost_reported). Default vuoto: senza
    catalogo si vedono i token e i soli costi OpenRouter.

[EN] WHAT: persistent JSONL usage ledger feeding /admin/insights. WHY:
log summaries die at restart; the ledger enables burn/cost analytics.
HOW: buffered writes flushed by the main watcher; size-based rotation;
policy-driven pricing estimates only when the provider reports no cost.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
import time

log = logging.getLogger("nx.ledger")

LEDGER_MAX_BYTES = 20 * 1024 * 1024        # 20MB per file prima della rotazione
LEDGER_KEEP = 2                             # file ruotati conservati (.1, .2)


class Ledger:
    """Buffer + flush atomico su JSONL. Thread-safe via lock (il watcher e
    l'event loop condividono l'istanza)."""

    def __init__(self, var_dir: str | os.PathLike):
        self.path = os.path.join(str(var_dir), "usage_ledger.jsonl")
        self._buf: list[dict] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------- record --
    def record(self, entry: dict, pricing: dict | None = None,
               upstream_model: str | None = None) -> None:
        """Accoda UNA riga; stima il costo se assente e il pricing matcha."""
        try:
            usage = entry.get("usage")
            if isinstance(usage, dict):
                if usage.get("cost") is None and pricing and upstream_model:
                    est = _estimate_cost(usage, pricing, upstream_model)
                    if est is not None:
                        usage["cost_est"] = round(est, 6)
            entry["ts"] = int(time.time())
            with self._lock:
                self._buf.append(entry)
                if len(self._buf) >= 200:       # safety net anti-ram
                    self.flush()
        except Exception:                       # mai bloccare la risposta
            log.debug("[ledger] record error", exc_info=True)

    # -------------------------------------------------------------- flush --
    def flush(self) -> int:
        """Scrive il buffer su disco (append); ritorna le righe scritte."""
        with self._lock:
            if not self._buf:
                return 0
            rows = self._buf
            self._buf = []
        try:
            self._rotate_if_needed(len(rows))
            with open(self.path, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False,
                                       separators=(",", ":"),
                                       default=str) + "\n")
            return len(rows)
        except Exception:                       # noqa: BLE001 - best effort
            log.warning("[ledger] flush FALLITO (%d righe perse)",
                        len(rows), exc_info=True)
            return 0

    def _rotate_if_needed(self, incoming_rows: int) -> None:
        try:
            if not os.path.exists(self.path):
                return
            if os.path.getsize(self.path) < LEDGER_MAX_BYTES:
                return
            # shift: .1 -> .2, corrente -> .1
            for i in range(LEDGER_KEEP - 1, 0, -1):
                src = f"{self.path}.{i}"
                if os.path.exists(src):
                    os.replace(src, f"{self.path}.{i + 1}")
            os.replace(self.path, f"{self.path}.1")
            log.info("[ledger] rotazione: nuovo segmento (righe in arrivo %d)",
                     incoming_rows)
        except Exception:                       # noqa: BLE001 - mai bloccare
            log.debug("[ledger] rotate error", exc_info=True)

    # --------------------------------------------------------------- read --
    def iter_rows(self) -> "list[dict]":
        """Tutte le righe dei segmenti correnti (per /admin/insights)."""
        paths = [f"{self.path}.{i}" for i in range(LEDGER_KEEP, 0, -1)]
        paths.append(self.path)
        out: list[dict] = []
        for p in paths:
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            continue            # riga troncata: salta
            except OSError:
                continue
        return out


def _estimate_cost(usage: dict, pricing: dict, model: str) -> float | None:
    """Costo stimato USD dalla tabella pricing (primo pattern che matcha)."""
    pt = usage.get("prompt_tokens") or 0
    ct = usage.get("completion_tokens") or 0
    for pat, cfgp in pricing.items():
        if not isinstance(cfgp, dict):
            continue
        if fnmatch.fnmatch(model or "", pat):
            pp = float(cfgp.get("prompt_per_1m") or 0)
            cp = float(cfgp.get("completion_per_1m") or 0)
            return (pt / 1_000_000) * pp + (ct / 1_000_000) * cp
    return None
