"""Connection draining: deployment tolti dal CSV con richieste ancora in volo.

[IT] Il deployment resta in config marcato draining (ignorato dal pick per
le nuove richieste) finche' le sue richieste finiscono (`note_end`) o scade
il TTL; il drain voluto dall'operatore (/admin/hosts/drain) lascia il
deployment in config all'undrain. Mixin di Router, estratto da
app/router.py senza modifiche di logica.

[EN] Connection draining of removed deployments (Router mixin).
"""
from __future__ import annotations

import logging
import time

from ..policy import policy_float
from .lazy import lazy_dict

log = logging.getLogger("nx.router")


class DrainMixin:
    # ------------------------------------------- connection draining (hot-reload)
    def start_draining(self, unique: str, dep: dict, inflight: int, *, operator: bool = False) -> None:
        """Archivia un deployment rimosso dal CSV ma con richieste in volo.

        Il dep viene RI-AGGIUNTO alla config (se assente) marcato draining:
        riferimenti/retry/record_* dello stesso ciclo continuano a risolverlo,
        mentre pick_deployment lo ignora per le nuove richieste.

        `operator=True` (drain via /admin): alla fine del drain (inflight=0 o
        TTL) il dep NON viene rimosso dalla config: e' un drain VOLUTO, non un
        hot-reload, quindi resta attivo e lo si toglie solo con undrain.
        """
        n = max(0, int(inflight))
        self._drain()[unique] = {
            "ts": time.time(),
            "inflight": n,
            "dep": dep,
            "operator": bool(operator),
        }
        grp = (dep or {}).get("group")
        if grp and self.config is not None:
            lst = self.config.groups.setdefault(grp, [])
            if not any(x.get("unique") == unique for x in lst):
                lst.append(dep)

    def drain_by_operator(self, unique: str, dep: dict, inflight: int) -> None:
        """Drain voluto dall'operatore (POST /admin/hosts/drain)."""
        self.start_draining(unique, dep, inflight, operator=True)

    def undrain_by_operator(self, unique: str) -> bool:
        """Annulla un drain (POST /admin/hosts/undrain): il dep resta in config."""
        return self.stop_draining(unique, purge_config=False)

    def _drain(self) -> dict:
        """Accessor lazy di `_draining` (pattern `_esc`): protegge i Router
        costruiti senza __init__ (`Router.__new__(Router)` nei test)."""
        return lazy_dict(self, "_draining")

    def is_draining(self, unique: str) -> bool:
        return unique in self._drain()

    def purge_draining(self) -> int:
        """Rimuove i draining oltre il TTL (anche con inflight residua)."""
        now = time.time()
        ttl = max(1.0, policy_float(self.policy, "hotreload_drain_ttl_sec", 120.0))
        dead = [u for u, d in list(self._drain().items()) if now - d.get("ts", 0) > ttl]
        for u in dead:
            log.info(
                "[drain] %s: TTL %ds scaduto (%d inflight) -> rimosso definitivamente",
                u,
                int(ttl),
                self._drain()[u].get("inflight", 0),
            )
            self._finish_drain(u)
        return len(dead)

    def stop_draining(self, unique: str, *, purge_config: bool = True) -> bool:
        """Annulla lo stato draining. purge_config=True (default, TTL/note_end):
        il dep viene rimosso dalla config. purge_config=False (undrain
        operatore): il dep resta attivo e torna eleggibile al pick.

        Ritorna True se c'era uno stato draining da annullare, False se non
        c'era nulla (idempotenza: undrain di un dep non in draining)."""
        d = self._drain().pop(unique, None)
        if d is None:
            return False
        if purge_config:
            dep = d.get("dep") or {}
            grp = dep.get("group")
            if grp and self.config is not None and grp in self.config.groups:
                self.config.groups[grp] = [x for x in self.config.groups[grp] if x.get("unique") != unique]
            log.info("[drain] %s: draining completata -> rimosso dalla config", unique)
        else:
            log.warning("[drain] %s: draining ANNULLATA (undrain operatore)", unique)
        return True

    def _finish_drain(self, unique: str) -> None:
        d = self._drain().get(unique)
        # Il flag va letto PRIMA del pop (stop_draining rimuove l'entry): un
        # drain voluto dall'operatore non deve sparire dalla config su
        # note_end/TTL, altrimenti undrain non troverebbe piu' l'entry.
        operator = bool((d or {}).get("operator"))
        self.stop_draining(unique, purge_config=not operator)
