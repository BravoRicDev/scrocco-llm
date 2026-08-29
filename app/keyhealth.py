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

import json
import logging
import os
import time

log = logging.getLogger("nx.keyhealth")

HEALTH_FILE_NAME = "key_health.json"
STREAK_DEAD_THRESHOLD = 5          # fail_streak minimo per "dead_suspect"
SUCCESS_EMA_FLOOR = 0.1            # sotto questo tasso la chiave e' sospetta


class KeyHealth:
    """Store su disco con update throttled (pattern adaptive_stats)."""

    def __init__(self, var_dir: str | os.PathLike):
        self.path = os.path.join(str(var_dir), HEALTH_FILE_NAME)
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                self.data = json.loads(
                    open(self.path, encoding="utf-8").read() or "{}")
        except Exception:                    # noqa: BLE001 - corrotto: riparti
            log.warning("[keyhealth] file illeggibile, riparto pulito",
                        exc_info=True)
            self.data = {}

    def save(self) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            log.debug("[keyhealth] save error", exc_info=True)

    # ------------------------------------------------------------ observe --
    def observe(self, unique: str, *, fail_streak: int,
                success_ema: float | None, is_cooled: bool,
                now: float | None = None) -> str | None:
        """Aggiorna l'evidenza di UN deployment; ritorna lo stato calcolato.

        Stati: 'healthy' (nessun record), 'dead_suspect', 'retired'.
        Una chiamata riuscita (fail_streak==0) ripulisce tutto: il successo
        e' l'unica prova che conta.
        """
        now = now if now is not None else time.time()
        rec = self.data.get(unique)
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

    def clear(self, unique: str) -> None:
        if self.data.pop(unique, None):
            self.save()
