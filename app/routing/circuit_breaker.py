"""Circuit breaker ibrido (per-deployment + per-API-key) e breaker proattivo
F25 per provider|modello (estratto verbatim da router.py).

[IT] Mixin con i due cluster "circuit breaker" di router.py: lo scope IBRIDO
(dep sempre, chiave solo sui fallimenti di chiave) e il breaker proattivo F25
che si apre quando piu' chiavi diverse prendono 5xx sullo stesso
provider|modello. Il codice e' spostato senza modifiche.

[EN] Circuit-breaker mixin: the hybrid dep/API-key breaker plus the F25
proactive per provider|model breaker. Moved verbatim, behaviour unchanged.

Dipendenze: questo mixin NON definisce nulla e non inizializza attributi. Usa
solo stato che `Router` inizializza in `__init__` (`policy`, `config`,
`_circuit_breakers`, `_dep_circuit_breakers`, `_model_cb`) piu' i metodi degli
altri mixin via `self`. NOTA: `_api_key_str` resta definito in `Router` (non e'
in questo mixin ne' in un altro) e viene chiamato via `self`: funziona perche'
il mixin viene composto nella stessa classe, quindi `self` e' la stessa
istanza. `_dep_cb_store` e `_note_model_failure` creano il proprio store con
`getattr(..., None)` se assente.
"""

from __future__ import annotations

import hashlib
import logging
import time

from .lazy import lazy_dict

log = logging.getLogger("nx.router")


class CircuitBreakerMixin:
    # --- Circuit Breaker methods (hybrid: per-deployment + per-API-key) ---
    def _cb_scope(self) -> str:
        s = str(getattr(self.policy, "circuit_breaker_scope", "hybrid")
                or "hybrid").lower()
        return s if s in ("hybrid", "dep", "key") else "hybrid"

    def _dep_cb_store(self) -> dict:
        return lazy_dict(self, "_dep_circuit_breakers")

    def _get_cb_entry(self, store: dict, key: str) -> dict:
        cb = store.get(key)
        if cb is None:
            cb = {"failures": 0, "last_failure": 0.0, "state": "closed",
                  "opened_at": 0.0, "half_open_successes": 0}
            store[key] = cb
        return cb

    def _dep_of(self, unique: str) -> dict | None:
        if not hasattr(self, 'config') or self.config is None:
            return None
        # Handle Router.__new__ test pattern where config may lack this method
        if not hasattr(self.config, 'deployment_by_unique'):
            return None
        return self.config.deployment_by_unique(unique)

    def _get_circuit_breaker(self, unique: str) -> dict | None:
        """Legacy: breaker per CHIAVE API (store per-key), usato da admin/test."""
        dep = self._dep_of(unique)
        if not dep:
            return None
        ak = self._api_key_str(dep)
        if not ak:
            return None
        return self._get_cb_entry(self._circuit_breakers, ak)

    def _get_dep_circuit_breaker(self, unique: str) -> dict:
        """Breaker per-DEPLOYMENT (unique)."""
        return self._get_cb_entry(self._dep_cb_store(), unique)

    def _cb_register_failure(self, store: dict, key: str, label: str,
                             name: str) -> None:
        cb = self._get_cb_entry(store, key)
        cb["failures"] += 1
        cb["last_failure"] = time.time()
        threshold = getattr(self.policy, "circuit_breaker_threshold", 5)
        if cb["state"] == "closed" and cb["failures"] >= threshold:
            cb["state"] = "open"
            cb["opened_at"] = time.time()
            log.warning("[circuit-breaker] %s %s OPEN (failures=%d)",
                        label, name, cb["failures"])
        elif cb["state"] == "half_open":
            cb["state"] = "open"
            cb["opened_at"] = time.time()
            cb["half_open_successes"] = 0
            log.warning("[circuit-breaker] %s %s RE-OPEN after half-open failure",
                        label, name)

    def _cb_register_success(self, store: dict, key: str, label: str,
                             name: str) -> None:
        cb = store.get(key)
        if cb is None:
            return
        if cb["state"] == "half_open":
            cb["half_open_successes"] += 1
            half_open_reqs = getattr(self.policy,
                                     "circuit_breaker_half_open_requests", 3)
            if cb["half_open_successes"] >= half_open_reqs:
                cb["state"] = "closed"
                cb["failures"] = 0
                log.info("[circuit-breaker] %s %s CLOSED after %d half-open "
                         "successes", label, name, cb["half_open_successes"])
        elif cb["state"] == "closed":
            cb["failures"] = 0

    def _cb_block_or_transition(self, store: dict, key: str, label: str,
                                name: str) -> bool:
        cb = store.get(key)
        if cb is None or cb["state"] == "closed":
            return False
        if cb["state"] == "open":
            timeout = getattr(self.policy, "circuit_breaker_timeout", 60.0)
            if time.time() - cb["opened_at"] >= timeout:
                cb["state"] = "half_open"
                cb["half_open_successes"] = 0
                log.info("[circuit-breaker] %s %s HALF-OPEN (timeout %ds)",
                         label, name, int(timeout))
                return False  # Allow one request through
            return True
        return False  # half-open: allow through

    @staticmethod
    def _is_key_level_failure(status, reason) -> bool:
        """True se il fallimento riguarda la CHIAVE (auth/quota), non il modello."""
        try:
            if status is not None and abs(int(status)) in (401, 402, 403, 429):
                return True
        except (TypeError, ValueError):
            pass
        r = (reason or "").lower()
        for tok in ("401", "402", "403", "429", "auth", "forbidden",
                    "unauthorized", "quota", "rate_limit", "rate-limit"):
            if tok in r:
                return True
        return False

    def _update_circuit_breaker_on_failure(self, unique: str,
                                           key_level: bool = True) -> None:
        """Registra un fallimento. Hybrid: dep sempre; key solo errori di chiave."""
        scope = self._cb_scope()
        dep = self._dep_of(unique)
        if dep is None:
            return
        if scope in ("hybrid", "dep"):
            self._cb_register_failure(self._dep_cb_store(), unique, "dep", unique)
        if scope == "dep":
            return
        if scope == "key" or key_level:
            ak = self._api_key_str(dep)
            if ak:
                self._cb_register_failure(self._circuit_breakers, ak, "key", ak)

    def _update_circuit_breaker_on_success(self, unique: str) -> None:
        """Registra un successo (hybrid: dep + key)."""
        scope = self._cb_scope()
        dep = self._dep_of(unique)
        if dep is None:
            return
        if scope in ("hybrid", "dep"):
            self._cb_register_success(self._dep_cb_store(), unique, "dep", unique)
        if scope in ("hybrid", "key"):
            ak = self._api_key_str(dep)
            if ak:
                self._cb_register_success(self._circuit_breakers, ak, "key", ak)

    def _is_circuit_open(self, unique: str) -> bool:
        """True se il breaker (dep o key, secondo scope) blocca il deployment."""
        scope = self._cb_scope()
        dep = self._dep_of(unique)
        if dep is None:
            return False
        ak = self._api_key_str(dep)
        if scope == "dep":
            return self._cb_block_or_transition(
                self._dep_cb_store(), unique, "dep", unique)
        if scope == "key":
            return bool(ak) and self._cb_block_or_transition(
                self._circuit_breakers, ak, "key", ak)
        # hybrid: prima la chiave (retro-compat), poi il deployment
        blocked_key = bool(ak) and self._cb_block_or_transition(
            self._circuit_breakers, ak, "key", ak)
        blocked_dep = self._cb_block_or_transition(
            self._dep_cb_store(), unique, "dep", unique)
        return blocked_key or blocked_dep

    # --- F25: circuit breaker proattivo per provider|modello ---
    @staticmethod
    def _model_cb_key(dep: dict) -> str:
        return f"{dep.get('provider', '')}|{dep.get('model', '')}"

    def _key_tag_of(self, dep: dict) -> str:
        ak = self._api_key_str(dep)
        if not ak:
            return ""
        return hashlib.sha256(ak.encode()).hexdigest()[:12]

    def _note_model_failure(self, dep: dict | None, status=None) -> None:
        """F25: accumula i 5xx per provider|modello. Quando arrivano da
        almeno `model_circuit_keys` CHIAVI diverse nella finestra si apre il
        breaker di modello (skip nel pick, zero penale reputazionale)."""
        if not dep:
            return
        if not getattr(self.policy, "model_circuit_enabled", True):
            return
        try:
            st = abs(int(status)) if status else 0
        except (TypeError, ValueError):
            st = 0
        if st < 500:
            return
        if not dep.get("model"):
            return
        now = time.time()
        win = float(getattr(self.policy, "model_circuit_window_sec", 60) or 60)
        need = int(getattr(self.policy, "model_circuit_keys", 3) or 3)
        store = getattr(self, "_model_cb", None)
        if store is None:
            store = {}
            self._model_cb = store
        mkey = self._model_cb_key(dep)
        ent = store.get(mkey)
        if ent is None or now - ent.get("ts", 0.0) > win:
            ent = {"tags": set(), "ts": now, "opened": 0.0}
            store[mkey] = ent
        tag = self._key_tag_of(dep)
        if tag:
            ent["tags"].add(tag)
        if len(ent["tags"]) >= need and not ent.get("opened"):
            ent["opened"] = now
            log.warning("[model-cb] %s APERTO: %d chiavi distinte in 5xx in "
                        "%.0fs", mkey, len(ent["tags"]), win)

    def _model_blocked(self, dep: dict | None) -> bool:
        """F25: True se il modello del dep e' in breaker aperto (soft skip)."""
        if not dep or not getattr(self.policy, "model_circuit_enabled", True):
            return False
        if not dep.get("model"):
            return False
        ent = getattr(self, "_model_cb", {}).get(self._model_cb_key(dep))
        if not ent or not ent.get("opened"):
            return False
        open_sec = float(getattr(self.policy, "model_circuit_open_sec", 60) or 60)
        if time.time() - ent["opened"] > open_sec:
            return False     # finestra chiusa: si riprova (half-open implicito)
        return True
