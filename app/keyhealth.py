"""Lifecycle delle chiavi morte: classificazione, retirement, sblocco.

[IT] COSA: evidenza PERSISTENTE (var/key_health.json) dello stato di salute
per deployment, aggiornata a tick dal watcher di main.py. WHY: i cooldown e
le EMA vivono in memoria e muoiono al restart; senza evidenza su disco un
agente non puo' distinguere una chiave momentaneamente rate-limited da uno
zombi da settimane (es. crediti OR esauriti).

Classificazione (conservativa, mai distruttiva):
  - dead_suspect : fail_streak >= 5 E in cooldown ADESSO E success_ema < 0.1
  - retired      : dead_suspect consecutivo da >= policy.retire_after_days

Regole:
  - il retirement ESCLUDE dal routing (is_retired consultato da pick/chain)
    ma NON tocca il CSV: nessuna cancellazione automatica, MAI.
  - sblocco manuale: POST /admin/deployments/unretire {"unique":...}
  - sblocco automatico: un probe riuscito pulisce dead/retired (il successo
    e' la sola prova che conti).

[EN] WHAT: persistent per-deployment key health evidence feeding routing
exclusion and admin surfaces. Conservative thresholds; retirement excludes
from routing but NEVER deletes CSV rows; manual unretire endpoint plus
automatic clearing on successful probe.
"""
from __future__ import annotations

import logging
import os
import time

from .atomic_store import load_json, save_json

log = logging.getLogger("nx.keyhealth")

HEALTH_FILE_NAME = "key_health.json"
# Motivi di ritiro PERMANENTI (chiave morta / modello rimosso): mai usati
# nemmeno in ultima spiaggia. Un ritiro per quota/probe-cap NON e' qui.
_PERMANENT_MARKERS = ("permanent_dead", "not_found", "model_missing",
                      "upstream_401", "upstream_402", "upstream_403",
                      "http_401", "http_402", "http_403", "auth",
                      "forbidden", "unauthorized", "invalid_api_key")

STREAK_DEAD_THRESHOLD = 5          # fail_streak minimo per "dead_suspect"
SUCCESS_EMA_FLOOR = 0.1            # sotto questo tasso la chiave e' sospetta


def set_health_thresholds(*, streak_dead=None, success_ema_floor=None) -> None:
    """Applica le soglie di classificazione dalla policy (default = costanti).

    Non cambia la logica: con valori assenti restano i default storici."""
    global STREAK_DEAD_THRESHOLD, SUCCESS_EMA_FLOOR
    if streak_dead is not None:
        try:
            STREAK_DEAD_THRESHOLD = max(1, int(streak_dead))
        except (TypeError, ValueError):
            pass
    if success_ema_floor is not None:
        try:
            SUCCESS_EMA_FLOOR = max(0.0, float(success_ema_floor))
        except (TypeError, ValueError):
            pass


class KeyHealth:
    """Store su disco con update throttled (pattern adaptive_stats)."""

    def __init__(self, var_dir: str | os.PathLike):
        self.path = os.path.join(str(var_dir), HEALTH_FILE_NAME)
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        data = load_json(self.path)
        self.data = data if isinstance(data, dict) else {}

    def save(self) -> None:
        save_json(self.path, self.data, indent=1)

    # ------------------------------------------------------------ observe --
    def observe(self, unique: str, *, fail_streak: int,
                success_ema: float | None, is_cooled: bool,
                reason: str | None = None, status: int | None = None,
                now: float | None = None) -> str | None:
        """Aggiorna l'evidenza di UN deployment; ritorna lo stato calcolato.

        Stati: 'healthy' (nessun record), 'dead_suspect', 'retired'.
        Una chiamata riuscita (fail_streak==0) ripulisce tutto: il successo
        e' l'unica prova che conta.

        F30: un 429/quota NON e' "chiave rotta" ma "chiave satura" (tipico coi
        gemelli a burst): non deve far avanzare verso dead_suspect/retired, o
        una free-key con quota giornaliera bassa verrebbe ritirata per sempre
        solo perche' saturata un giorno. Solo 401/403/5xx veri contano.
        """
        now = now if now is not None else time.time()
        rec = self.data.get(unique)
        _r = (reason or "").lower()
        _quota = (status == 429 or "429" in _r or "quota" in _r
                  or "rate_limit" in _r)
        if _quota and fail_streak > 0:
            # evidenza NON valida: stato invariato, nessun avanzamento
            return (rec or {}).get("state") if rec else None
        if fail_streak == 0 or (
                success_ema is not None and success_ema > SUCCESS_EMA_FLOOR):
            if rec:
                self.data.pop(unique, None)
                log.info("[keyhealth] %s torna healthy", unique)
            return "healthy"
        dead_now = (fail_streak >= STREAK_DEAD_THRESHOLD and is_cooled
                    and (success_ema is None or success_ema
                         < SUCCESS_EMA_FLOOR))
        if not dead_now:
            # non abbastanza morto adesso: mantieni l'anagrafica ma non peggiorare
            return (rec or {}).get("state") if rec else None
        if rec is None:
            rec = {"first_dead_ts": int(now),
                   "last_reason": None, "streak_max": 0,
                   "state": "dead_suspect"}   # prima constatazione di morte
            self.data[unique] = rec
        elif not rec.get("state"):
            rec["state"] = "dead_suspect"
        rec["streak_max"] = max(rec.get("streak_max") or 0, fail_streak)
        return rec.get("state")

    def set_state(self, unique: str, state: str,
                  reason: str | None = None) -> None:
        rec = self.data.setdefault(unique, {"first_dead_ts": int(time.time()),
                                            "last_reason": None,
                                            "streak_max": 0})
        rec["state"] = state
        if reason:
            rec["last_reason"] = reason

    def apply_retirement(self, retire_after_days: int) -> list[str]:
        """Promuove i dead_suspect vecchi a retired. Ritorna i nuovi retirati."""
        out = []
        cutoff = time.time() - max(1, int(retire_after_days)) * 86400
        for u, rec in list(self.data.items()):
            if rec.get("state") == "dead_suspect" \
                    and (rec.get("first_dead_ts") or 0) < cutoff:
                rec["state"] = "retired"
                out.append(u)
                log.warning("[keyhealth] %s RETIRED (morto da >%d giorni)",
                            u, retire_after_days)
            elif not rec.get("state"):
                rec["state"] = "dead_suspect"
        return out

    # ------------------------------------------------------------- query --
    def is_retired(self, unique: str) -> bool:
        rec = self.data.get(unique)
        return bool(rec and rec.get("state") == "retired")

    def retire_reason(self, unique: str) -> str:
        """Motivo dell'ultimo ritiro ('' se ignoto)."""
        rec = self.data.get(unique) or {}
        return str(rec.get("last_reason") or "")

    def is_permanently_retired(self, unique: str) -> bool:
        """Ritirato per un motivo PERMANENTE (chiave morta / modello rimosso):
        NON va riusato nemmeno come ultima spiaggia. I ritiri per quota o per
        probe-cap NON sono permanenti: la chiave e' viva, va solo lasciata
        stare finche' non serve davvero."""
        if not self.is_retired(unique):
            return False
        r = self.retire_reason(unique).lower()
        if not r:
            return False
        return any(m in r for m in _PERMANENT_MARKERS)

    def clear(self, unique: str) -> None:
        if self.data.pop(unique, None):
            self.save()
