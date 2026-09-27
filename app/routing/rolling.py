"""Finestra temporale mobile con somma incrementale.

[IT] Le finestre d'uso del router (tentativi pesati a 24h, token di output a
5h) venivano risommate per intero a OGNI lettura, e la lettura avviene per
ogni candidato a ogni pick. Qui la somma e' tenuta aggiornata a ogni
inserimento/scadenza: la lettura costa O(1) (piu' la potatura dei campioni
scaduti, che resta ammortizzata). Le quantita' sono INTERE, quindi la somma
e' esatta e non accumula deriva nel tempo.

[EN] Rolling time window of (ts, int amount) samples with an exact running
total.
"""
from __future__ import annotations

from collections import deque


class RollingWindow(deque):
    """deque di campioni `(ts, quantita')` in ordine di tempo, con `total`
    sempre uguale alla somma delle quantita' presenti. Va modificata SOLO
    tramite `add` e `prune` (che mantengono `total`)."""

    def __init__(self) -> None:
        super().__init__()
        self.total = 0

    def add(self, ts: float, amount: int) -> None:
        self.append((ts, amount))
        self.total += amount

    def prune(self, cut: float) -> None:
        """Scarta i campioni con ts < cut."""
        while self and self[0][0] < cut:
            self.total -= self.popleft()[1]
