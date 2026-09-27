"""Cluster contatori usage (estratto verbatim da router.py, refactor Phase 4).

[IT] Mixin con i due cluster di contatori "usage" di router.py: la finestra
ROLLING 24h dei TENTATIVI per-deployment che alimenta lo COLD SPREAD (pesi
prefill ctx_est/8000, F28) e la finestra rolling dei TOKEN DI OUTPUT consegnati
che alimenta il BILANCIAMENTO -go, piu' l'helper `_attached_unique` (dep
"attaccato" alla sessione corrente, protetto dallo spread). Codice spostato
senza modifiche.

[EN] Usage-counters mixin: the rolling 24h ATTEMPT window feeding the cold
spread (prefill-weighted, F28) and the rolling DELIVERED OUTPUT-token window
feeding the -go balance, plus the `_attached_unique` helper. Verbatim move,
no behaviour change. See docs/ROUTING.md.

Dipendenze: questo mixin NON definisce attributi d'istanza e NON inizializza
niente. Usa lo stato che `Router` inizializza in `__init__` (`_usage_times`,
`_out_tokens`, `policy`) piu' i metodi di SessionMixin (`_dep_sess`,
`_guard_sec`). Le costanti di classe `_USAGE_WINDOW` e
`_GO_BALANCE_DEFAULT_WINDOW` viaggiano con i metodi che le usano; `Router` le
eredita normalmente.
"""

from __future__ import annotations

import logging
import time

from ..session_ctx import current_session
from ..policy import policy_float
from .lazy import lazy_dict
from .rolling import RollingWindow

log = logging.getLogger("nx.router")


class UsageMixin:
    # --------------------------------------------- cold usage spread
    _USAGE_WINDOW = 86400.0
    # Peso 1.0 = 8000 token di prefill: i pesi si accumulano in UNITA' intere
    # (somma esatta), la lettura riconverte in peso.
    _USAGE_UNIT = 8000

    def _usage(self) -> dict:
        return lazy_dict(self, "_usage_times")

    def note_usage(self, unique: str, ts: float | None = None, ctx_est: int | None = None) -> None:
        """Registra un TENTATIVO (ok o fail, NON un probe) nella finestra
        rolling 24h usata dallo spread a freddo. F28: il peso e' il PREFILL
        reale (ctx_est/8000), non 1: 10 chiamate da 80k pesano come 100 da 2k,
        che e' quello che vede il provider sul rate-limit a token/minuto.
        Senza ctx (es. ricostruzione dai log) si pesa 1.0."""
        if not unique:
            return
        d = self._usage()
        dq = d.get(unique)
        if dq is None:
            dq = RollingWindow()
            d[unique] = dq
        now = time.time() if ts is None else ts
        try:
            units = max(self._USAGE_UNIT, int(ctx_est)) if ctx_est else self._USAGE_UNIT
        except (TypeError, ValueError):
            units = self._USAGE_UNIT
        dq.add(now, units)
        dq.prune(now - self._USAGE_WINDOW)

    def usage_weight_24h(self, unique: str, now: float | None = None) -> float:
        """Somma dei pesi (token/8000) dei tentativi nelle ultime 24h."""
        dq = self._usage().get(unique)
        if not dq:
            return 0.0
        now = time.time() if now is None else now
        dq.prune(now - self._USAGE_WINDOW)
        return dq.total / float(self._USAGE_UNIT)

    def usage_count_24h(self, unique: str, now: float | None = None) -> int:
        dq = self._usage().get(unique)
        if not dq:
            return 0
        now = time.time() if now is None else now
        dq.prune(now - self._USAGE_WINDOW)
        return len(dq)

    def usage_count_window(self, unique: str, sec: float, now: float | None = None) -> int:
        """Tentativi (start) nella finestra rolling `sec`, usato dal fair-share
        delle chiavi nei gruppi capacità primary. NON pota il deque: la finestra
        a 24h del cold-spread deve restare intatta (si limita a contare, dal
        fondo, le entry piu' recenti di `cut`)."""
        dq = self._usage().get(unique)
        if not dq:
            return 0
        now = time.time() if now is None else now
        cut = now - max(0.001, float(sec))
        n = 0
        for ts, _units in reversed(dq):
            if ts < cut:
                break
            n += 1
        return n

    # ------------------------------------------------- bilanciamento -go
    _GO_BALANCE_DEFAULT_WINDOW = 18000.0  # 5h

    def _out_toks(self) -> dict:
        return lazy_dict(self, "_out_tokens")

    def _go_balance_window(self) -> float:
        try:
            v = float(
                getattr(self.policy, "go_balance_window_sec", self._GO_BALANCE_DEFAULT_WINDOW)
                or self._GO_BALANCE_DEFAULT_WINDOW
            )
        except (TypeError, ValueError):
            v = self._GO_BALANCE_DEFAULT_WINDOW
        return v if v > 0 else self._GO_BALANCE_DEFAULT_WINDOW

    def _go_balance_enabled(self) -> bool:
        return bool(getattr(self.policy, "go_balance_enabled", True))

    def _go_balance_flat_pool(self) -> bool:
        return bool(getattr(self.policy, "go_balance_flat_pool", True))

    def _go_stick_ttl_sec(self) -> float:
        try:
            v = policy_float(self.policy, "go_stick_ttl_sec", 600)
        except (TypeError, ValueError):
            v = 600.0
        return v if v > 0 else 600.0

    def note_output_tokens(self, unique: str, completion_tokens, ts: float | None = None) -> None:
        """Registra i TOKEN DI OUTPUT di una risposta CONSEGNATA nella finestra
        rolling del bilanciamento -go. Solo successi con token reali: un
        fallimento (o una risposta a vuoto) non consuma quota di output."""
        if not unique:
            return
        try:
            n = int(completion_tokens or 0)
        except (TypeError, ValueError):
            return
        if n <= 0:
            return
        d = self._out_toks()
        dq = d.get(unique)
        if dq is None:
            dq = RollingWindow()
            d[unique] = dq
        now = time.time() if ts is None else ts
        dq.add(now, n)
        dq.prune(now - self._go_balance_window())

    def output_tokens_window(self, unique: str, sec: float | None = None, now: float | None = None) -> int:
        """Token di output consumati da `unique` nella finestra rolling (default
        5h, policy `go_balance.window_sec`). 0 se non ci sono campioni."""
        dq = self._out_toks().get(unique)
        if not dq:
            return 0
        now = time.time() if now is None else now
        win = self._go_balance_window() if sec is None else float(sec)
        dq.prune(now - max(0.001, win))
        return int(dq.total)

    def _attached_unique(self, unique: str) -> bool:
        """True se `unique` e' 'attaccato' alla sessione CORRENTE (successo
        recente entro session_dep_guard_sec): resta prioritario e non viene
        mai nascosto dallo spread (rispetto della cache)."""
        sid = current_session()
        if not sid:
            return False
        ent = self._dep_sess().get(unique)
        if not ent or ent[0] != sid:
            return False
        return (time.time() - ent[1]) < self._guard_sec()
