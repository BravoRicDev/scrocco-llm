"""Lease di concorrenza per chiave (P2): quante richieste in volo per api_key.

[IT] Opt-in (`key_concurrency_enabled`): con il cap raggiunto la chiave viene
solo DEPRIORITIZZATA finche' esistono alternative (il cap non diventa mai un
503). Le lease piu' vecchie di `key_concurrency_lease_max_age_sec` si
scartano (una richiesta interrotta non satura la chiave per sempre).
`_lease_put`/`_lease_drop` sono i due punti che mutano lo stato: la modalita'
multi-worker li replica (app/cluster.py). Mixin di Router, estratto da
app/router.py senza modifiche di logica.

[EN] Per-key concurrency leases (Router mixin).
"""
from __future__ import annotations

import logging
import time

from ..policy import policy_float, policy_int

log = logging.getLogger("nx.router")


class KeyLeaseMixin:
    # ------------------------------------------------- LEASE PER CHIAVE (P2)
    def _key_leases(self) -> dict:
        d = getattr(self, "_key_leases_map", None)
        if d is None:
            d = self._key_leases_map = {}
        return d

    def _lease_max_age(self) -> float:
        return max(1.0, policy_float(self.policy, "key_concurrency_lease_max_age_sec", 120))

    def _prune_key_leases(self, now: float | None = None) -> None:
        """Scarta le lease piu' vecchie del tetto (una richiesta interrotta
        non deve saturare la chiave per sempre)."""
        now = time.time() if now is None else now
        age = self._lease_max_age()
        m = self._key_leases()
        for k in list(m):
            fresh = [e for e in m[k] if now - e[1] <= age]
            if fresh:
                m[k] = fresh
            else:
                m.pop(k, None)

    def key_inflight(self, dep: dict | None) -> int:
        if not dep:
            return 0
        return len(self._key_leases().get(dep.get("api_key") or "", ()))

    def key_lease_acquire(self, dep: dict | None) -> tuple | None:
        """Riserva una "lease" (richiesta in volo) per la api_key del dep.

        Opt-in (`key_concurrency_enabled`): con il cap raggiunto ritorna None
        — il chiamante NON deve bloccare la richiesta (cap SOFT: la chiave
        viene solo deprioritizzata finche' esistono alternative), quindi il
        None serve solo a non incrementare il contatore."""
        if not dep or not bool(getattr(self.policy, "key_concurrency_enabled", False)):
            return None
        now = time.time()
        self._prune_key_leases(now)
        key = dep.get("api_key") or ""
        cap = max(0, policy_int(self.policy, "key_concurrency_max", 2, falsy=0))
        ent = self._key_leases().setdefault(key, [])
        if cap and len(ent) >= cap:
            return None
        tok = "%s|%s|%d" % (key[:12], dep.get("unique"), now)
        self._lease_put(dep, tok, now)
        return (key, tok)

    def key_lease_release(self, lease: tuple | None) -> None:
        if not lease:
            return
        key, tok = lease
        if not self._key_leases().get(key):
            return
        self._lease_drop(tok)

    def _lease_put(self, dep: dict, tok: str, now: float) -> None:
        self._key_leases().setdefault(dep.get("api_key") or "", []).append((tok, now, dep.get("unique")))

    def _lease_drop(self, tok: str) -> None:
        """Toglie la lease `tok` (il token contiene gia' chiave e unique:
        identifica una sola chiave)."""
        m = self._key_leases()
        for key, ent in list(m.items()):
            kept = [e for e in ent if e[0] != tok]
            if len(kept) == len(ent):
                continue
            if kept:
                m[key] = kept
            else:
                m.pop(key, None)

    def _lease_filter(self, deps: list[dict]) -> list[dict]:
        """Depriorizza (non elimina) i dep la cui api_key e' al cap di
        concorrenza: se TUTTI sono al cap ritorna la lista intera — il cap
        non deve mai trasformarsi in un 503."""
        if not bool(getattr(self.policy, "key_concurrency_enabled", False)):
            return deps
        cap = max(0, policy_int(self.policy, "key_concurrency_max", 2, falsy=0))
        if not cap:
            return deps
        self._prune_key_leases()
        m = self._key_leases()
        if not m:
            return deps
        kept = [d for d in deps if len(m.get(d.get("api_key") or "", ())) < cap]
        return kept or deps

    def key_leases_view(self) -> dict:
        self._prune_key_leases()
        m = self._key_leases()
        return {k: len(v) for k, v in sorted(m.items())}

    def clear_key_leases(self, unique: str | None = None) -> dict:
        """Azzera le lease di concorrenza per chiave. Senza `unique` svuota
        tutto; con `unique` rimuove solo la lease della chiave di quel dep.
        Nessuna api_key in chiaro nell'output (solo conteggi)."""
        m = self._key_leases()
        if not unique:
            keys = sum(1 for v in m.values() if v)
            leases = sum(len(v or ()) for v in m.values())
            m.clear()
            return {"ok": True, "keys": keys, "leases": leases}
        dep = self.config.deployment_by_unique(unique) or {}
        key = str(dep.get("api_key") or "")
        ent = m.pop(key, None) if key else None
        return {"ok": True, "keys": 1 if ent else 0, "leases": len(ent or ())}
