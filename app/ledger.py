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
import asyncio

log = logging.getLogger("nx.ledger")

from .atomic_store import load_json as _load_json, save_json as _save_json

LEDGER_MAX_BYTES = int(
    os.environ.get("LEDGER_MAX_BYTES", str(20 * 1024 * 1024)) or
    (20 * 1024 * 1024))                     # 20MB per file prima della rotazione
LEDGER_KEEP = int(
    os.environ.get("LEDGER_KEEP", "2") or "2")   # file ruotati conservati (.1, .2)
# Aggregazione giornaliera: se un segmento ruotato supera questa soglia di
# righe viene compresso in usage_summary.json (aggregato per profilo+modello+
# deployment+kind+giorno) e rimosso. Overridabile via env per i test.
LEDGER_SUMMARY_MIN_ROWS = int(
    os.environ.get("LEDGER_SUMMARY_MIN_ROWS", "50000") or "50000")


class Ledger:
    """Buffer + flush atomico su JSONL. Thread-safe via lock (il watcher e
    l'event loop condividono l'istanza)."""

    def __init__(self, var_dir: str | os.PathLike):
        self.path = os.path.join(str(var_dir), "usage_ledger.jsonl")
        self.summary_path = os.path.join(str(var_dir), "usage_summary.json")
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
                need_flush = len(self._buf) >= 200       # safety net anti-ram
            if need_flush:
                # FUORI dal lock: flush() riacquisisce self._lock (non
                # rientrante) -> dentro il with sarebbe stato un deadlock.
                self.flush()
        except Exception:                       # mai bloccare la risposta
            log.error("[ledger] record error", exc_info=True)

    # -------------------------------------------------------------- flush --
    async def flush_async(self) -> int:
        """Variante non bloccante: esegue il flush su un thread separato così
        l'I/O su disco (append/rotazione) non ferma mai l'event loop di
        FastAPI. Usata dal watcher periodico."""
        try:
            return await asyncio.to_thread(self.flush)
        except Exception:                       # noqa: BLE001 - best effort
            log.error("[ledger] flush_async error", exc_info=True)
            return 0

    def flush_sync(self) -> int:
        """Flush SINCRONO e bloccante per il graceful shutdown
        (SIGTERM/SIGINT): scrive ogni record rimasto nel buffer senza
        dipendere dall'event loop, che durante lo shutdown puo' essere gia'
        in chiusura. Best-effort come `flush()`."""
        return self.flush()

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
            # I4: le righe erano gia' state estratte dal buffer: senza
            # rimetterle, un errore disco (o di serializzazione) le perdeva
            # per sempre. Si riaccodano in testa (ordine preservato) e si
            # ritenta al prossimo flush (watcher/shutdown).
            with self._lock:
                self._buf = rows + self._buf
            log.warning("[ledger] flush FALLITO (%d righe rimesse in coda)",
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
            # il segmento piu' vecchio (sul punto di uscire dalla finestra di
            # retention) viene compresso nel summary giornaliero, non scartato.
            self._aggregate_oldest()
        except Exception:                       # noqa: BLE001 - mai bloccare
            log.error("[ledger] rotate error", exc_info=True)

    # -------------------------------------------------------- aggregazione --
    def _aggregate_oldest(self) -> None:
        """Comprime il segmento piu' vecchio (.2 se esiste, altrimenti .1) se
        supera la soglia di righe; se compresso lo rimuove. Il dato storico
        resta consultabile via iter_rows (summary) senza intasare la lettura
        calda."""
        for i in range(LEDGER_KEEP, 0, -1):
            p = f"{self.path}.{i}"
            if os.path.exists(p):
                if self._aggregate_segment(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                return

    def _aggregate_segment(self, seg_path: str) -> bool:
        """Comprime un segmento ruotato in usage_summary.json: per
        profilo+modello+deployment+gruppo+kind+giorno salva
        {count, token totali, costi, durata media, fb/qc/wd}. Ritorna True se
        compresso (il chiamante puo' rimuovere il segmento). Best-effort."""
        try:
            with open(seg_path, encoding="utf-8") as f:
                nlines = sum(1 for _ in f)
        except OSError:
            return False
        if nlines < LEDGER_SUMMARY_MIN_ROWS:
            return False
        agg: dict[str, dict] = {}
        tot = 0
        try:
            with open(seg_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    tot += 1
                    day = time.strftime("%Y-%m-%d",
                                        time.localtime(r.get("ts") or 0))
                    key = "|".join(str(x) for x in (
                        r.get("profile") or "-", r.get("model") or "-",
                        r.get("dep") or "-", r.get("grp") or "-",
                        r.get("kind") or "chat", day))
                    a = agg.setdefault(key, {
                        "count": 0, "prompt_tokens": 0, "completion_tokens": 0,
                        "total_tokens": 0, "cost": 0.0, "cost_est": 0.0,
                        "dur_ms": 0, "fb": 0, "qc": 0, "wd_fail": 0,
                        "wd_ok": 0})
                    u = r.get("usage") or {}
                    a["count"] += 1
                    a["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
                    a["completion_tokens"] += int(u.get("completion_tokens") or 0)
                    a["total_tokens"] += int(
                        u.get("total_tokens") or ((u.get("prompt_tokens") or 0)
                                                  + (u.get("completion_tokens")
                                                     or 0)))
                    a["cost"] += float(u.get("cost") or 0)
                    a["cost_est"] += float(u.get("cost_est") or 0)
                    a["dur_ms"] += int(r.get("dur_ms") or 0)
                    if (r.get("fb") or 0) and r["fb"] > 0:
                        a["fb"] += 1
                    if r.get("qc"):
                        a["qc"] += 1
                    _wd = r.get("wd")
                    if _wd:
                        if _wd == "tier2-no-done":
                            a["wd_ok"] += 1
                        else:
                            a["wd_fail"] += 1
        except OSError:
            return False
        if not agg:
            return False
        import calendar
        summary = _load_json(self.summary_path, dict)
        for key, a in agg.items():
            parts = key.split("|")
            y, mo, d = (int(x) for x in parts[5].split("-"))
            row = {
                "count": a["count"],
                "ts": calendar.timegm((y, mo, d, 0, 0, 0)),
                "profile": parts[0], "model": parts[1], "dep": parts[2],
                "grp": parts[3], "kind": parts[4],
                "dur_ms": a["dur_ms"], "fb": a["fb"], "qc": a["qc"],
                "wd_fail": a["wd_fail"], "wd_ok": a["wd_ok"],
                "usage": {"prompt_tokens": a["prompt_tokens"],
                          "completion_tokens": a["completion_tokens"],
                          "total_tokens": a["total_tokens"],
                          "cost": round(a["cost"], 6),
                          "cost_est": round(a["cost_est"], 6)},
            }
            old = summary.get(key)
            if isinstance(old, dict):
                old["count"] += row["count"]
                old["dur_ms"] += row["dur_ms"]
                old["fb"] += row["fb"]
                old["qc"] += row["qc"]
                old["wd_fail"] += row["wd_fail"]
                old["wd_ok"] += row["wd_ok"]
                uo = old.setdefault("usage", {})
                uo["prompt_tokens"] = int(uo.get("prompt_tokens") or 0) + \
                    row["usage"]["prompt_tokens"]
                uo["completion_tokens"] = int(uo.get("completion_tokens")
                                              or 0) + \
                    row["usage"]["completion_tokens"]
                uo["total_tokens"] = int(uo.get("total_tokens") or 0) + \
                    row["usage"]["total_tokens"]
                uo["cost"] = round(float(uo.get("cost") or 0)
                                   + row["usage"]["cost"], 6)
                uo["cost_est"] = round(float(uo.get("cost_est") or 0)
                                       + row["usage"]["cost_est"], 6)
            else:
                summary[key] = row
        if not _save_json(self.summary_path, summary):
            return False
        log.info("[ledger] segmento %s compresso (%d righe -> %d summary)",
                 os.path.basename(seg_path), tot, len(agg))
        return True

    # --------------------------------------------------------------- read --
    def iter_rows(self) -> "list[dict]":
        """Tutte le righe (per /admin/insights): prima il summary aggregato
        (storico, ordini di grandezza piu' piccolo), poi i segmenti recenti
        (.2/.1) e il file corrente. Le righe summary portano `count` (numero
        di chiamate reali rappresentate)."""
        out: list[dict] = []
        sm = _load_json(self.summary_path, dict)
        for _k, r in (sm or {}).items():
            if isinstance(r, dict):
                out.append(dict(r))
        paths = [f"{self.path}.{i}" for i in range(LEDGER_KEEP, 0, -1)]
        paths.append(self.path)
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

    async def iter_rows_async(self) -> "list[dict]":
        """Variante non bloccante di `iter_rows` (I/O su thread separato):
        evita di fermare l'event loop durante la lettura dei segmenti."""
        return await asyncio.to_thread(self.iter_rows)


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
