"""Warm pool / prestiti warm (estratto verbatim da router.py).

[IT] Mixin con la logica del pool "caldo": TTL, eleggibilita', prestiti tra
sessioni, chiavi warm. Il codice e' spostato senza modifiche.
[EN] Warm-pool mixin: TTL, eligibility, cross-session borrows, warm keys.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter

from ..opencode_gate import (dep_usable as _dep_usable,
                             is_native_session, is_opencode_zen_dep,
                             opencode_cautious_request)
from ..session_ctx import current_session

log = logging.getLogger("nx.router")


class WarmMixin:
    def note_warm_owner(self, session_id: str | None, unique: str) -> None:
        """Registra la PROVA DI FUNZIONAMENTO di un deployment per la sessione
        (solo ownership warm, NIENTE holder/reputazione/latency).

        Usata per i canary che hanno PRODOTTO contenuto ma hanno perso la gara:
        il deploy e' buono (l'abbiamo visto rispondere), quindi entra subito
        nella lista warm del profilo. Cosi' il prossimo giro la warm sa gia'
        dove andare e non serve andare a caccia. Non diventa holder: il primo
        posto resta di chi ha servito davvero la risposta."""
        if not session_id or not unique:
            return
        try:
            self._note_dep_session(session_id, unique)
        except Exception:                      # mai rompere la risposta
            log.debug("[warm] note_warm_owner %s fallito", unique,
                      exc_info=True)

    def _warm_ttl(self) -> float:
        """Finestra di validita' del pool caldi (0 = session_dep_guard_sec)."""
        try:
            v = float(getattr(self.policy, "warm_pool_ttl_sec", 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        return v if v > 0 else self._guard_sec()

    def _warm_allowed(self, pname: str, group_name: str | None) -> set[str]:
        """Univoci ammessi nel pool caldi per `pname` IMPONENDO la dim minima
        del gruppo di partenza: mai dim < dim(group_name), anche se la
        sessione le ha gia' servite con successo (es. richiesta esplicita
        `...-200k` -> il caldo `-64k` della stessa sessione NON va pescato).

        I bucket -go/-fallback sono inclusi ma il pool li ignora comunque
        (l'ownership caldi traccia solo i free-dims)."""
        floor = self._group_min_dim(group_name)
        return set(self._text_ladder(pname, start_dim=floor))

    def _warm_pool(self, session_id: str | None, allowed: set[str] | None,
                   need: frozenset[str] | None = None,
                   ctx: int | None = None,
                   tried: set[str] | None = None,
                   failed_unique: str | None = None,
                   include_borrowed: bool = False,
                   out_tokens: int | None = None) -> list[dict]:
        """Tier "caldi": free-dims che QUESTA sessione ha gia' servito con
        SUCCESSO entro la finestra warm, ancora vivi (no cooldown/retired/
        draining) e compatibili con `need` + `max_input`/contesto (`_cap_fits`).

        Con `out_tokens` (budget di output della richiesta) si richiede anche
        `dep_deliverable(..., out_tokens)`: un caldo che sta nel contesto ma
        non ha spazio per produrre l'output richiesto NON e' un caldo "buono"
        (vale anche per i prestiti). Default None = solo `_cap_fits`.

        Con `include_borrowed=True` (prestito dei warm) entrano anche i warm
        di ALTRE sessioni "in disuso" (vedi `_lendable_set`). Propri e prestati
        formano UN UNICO blocco ordinato (non piu' propri-accodati-poi-
        prestati); al primo successo su un prestato la proprieta' si trasferisce
        da sola (note_session_success).

        Lista ORDINATA in TRE FASCE, decise SOLO dal flag del TIMER lento
        (>45s, `_slow_timer_flagged`):
          1. propri NON lenti  — holder "eletto" per primo, poi il piu' veloce
             per EMA di latenza (se `warm_pick_fastest`);
          2. prestati NON lenti — piu' veloce prima;
          3. lenti comuni (propri + prestati) — piu' veloce prima.
        I lenti RESTANO nel pool (nessuna esclusione), solo in fondo. Tie-break
        MRU (`last_used`), `order`, `max_input` crescente. Vuota se disabilitato,
        senza sessione, o senza candidati. `allowed` limita al MONDO richiesto
        (catena del profilo); None = nessun filtro di mondo."""
        # Cautela opencode: le richieste spoofate (client non-opencode) NON
        # usano il warm, ne' proprio ne' in prestito. L'ownership resta
        # registrato (note_warm_owner) cosi' una sessione reale lo trova caldo.
        if opencode_cautious_request():
            return []
        if not getattr(self.policy, "warm_pool_enabled", True):
            return []
        sid = session_id or current_session()
        if not sid:
            return []
        owned = self._sess_deps().get(sid)
        # NB: con `include_borrowed` NON si esce se la sessione non possiede
        # nulla. E' il caso del cronjob che riparte "a freddo": senza questo
        # il pool tornava vuoto PRIMA di guardare i prestabili, quindi una
        # sessione nuova non poteva ereditare il parco pronto (e sparava
        # canary inutili). La priorita' resta comunque: propri >> prestati.
        if not owned and not include_borrowed:
            return []
        d = self._dep_sess()
        ttl = self._warm_ttl()
        now = time.time()
        skip = tried or set()
        holder = self.session_holder(sid)
        _fast = bool(getattr(self.policy, "warm_pick_fastest", True))

        _own_set = set(owned or ())

        # Client opencode NATIVO: il pool e' a DUE BLOCCHI, prima gli zen e
        # poi tutto il resto. Dentro ogni blocco valgono le 3 fasce solite
        # (propri non-lenti > prestati non-lenti > lenti, EMA).
        _zen_first = bool(getattr(self, "_zen_first_active",
                                  lambda: False)())

        def _wkey(dep: dict):
            # Ordine a TRE FASCE, con il SOLO flag del TIMER lento (>45s) a
            # decidere la fascia: (1) propri non lenti (holder "eletto" primo,
            # poi il piu' veloce per EMA); (2) prestati non lenti (piu' veloce);
            # (3) lenti comuni propri+prestati (piu' veloce). I lenti restano
            # nel pool: cambia solo la posizione.
            u = dep["unique"]
            _slow = self._slow_timer_flagged(u, sid)
            _own = u in _own_set
            _blk = 0 if (_own and not _slow) else (1 if not _slow else 2)
            if _fast:
                try:
                    _l = self.bucket_latency_ms(u, ctx)
                    _l = float(_l) if _l and float(_l) > 0 else None
                except Exception:                   # noqa: BLE001
                    _l = None
                _lat = (0, _l) if _l is not None else (1, 0.0)
            else:
                _lat = (0, 0.0)
            _zr = (0 if is_opencode_zen_dep(dep) else 1) if _zen_first else 0
            return (_zr,
                    _blk,
                    0 if (_blk == 0 and holder and u == holder) else 1,
                    _lat[0], _lat[1],
                    -(self.stats_for(u).last_used or 0.0),
                    self._eff_order(dep),
                    int(dep.get("max_input_tokens") or 0))

        out: list[dict] = []
        for u in (owned or ()):
            if u in skip or u == failed_unique:
                continue
            if allowed is not None and u not in allowed:
                continue
            ent = d.get(u)
            if not ent or ent[0] != sid:
                continue
            if now - ent[1] > ttl:
                continue
            dep = self.config.deployment_by_unique(u)
            if dep is None or self.is_retired(u) or self.is_draining(u):
                continue
            if self._endpoint_quarantined(dep):
                continue
            if self.is_cooled_down(u) or self._gemini_blocked(dep):
                continue
            if self._is_demoted_dep(u, sid, ctx,
                                    allow_slow=self._warm_allow_slow()):
                log.debug("[warm] skip (chiave satura o demote): %s", u)
                continue
            if not _dep_usable(dep):
                continue
            if need and not self._dep_supports(dep, need):
                continue
            if not self._cap_fits(dep, ctx):
                continue
            if (out_tokens is not None and out_tokens > 0
                    and not self.dep_deliverable(dep, need, ctx, out_tokens)):
                continue
            out.append(dep)
        if not out and not include_borrowed:
            return []
        if include_borrowed:
            _own = {d["unique"] for d in out}
            borr: list[dict] = []
            for u in self._lendable_set(now):
                if u in _own or u in skip or u == failed_unique:
                    continue
                if allowed is not None and u not in allowed:
                    continue
                ent = d.get(u)
                if ent and ent[0] == sid:            # e' gia' un nostro warm
                    continue
                dep = self.config.deployment_by_unique(u)
                if dep is None or self.is_retired(u) or self.is_draining(u):
                    continue
                if self._endpoint_quarantined(dep):
                    continue
                if self.is_cooled_down(u) or self._gemini_blocked(dep):
                    continue
                if self._is_demoted_dep(u, sid, ctx,
                                        allow_slow=self._warm_allow_slow()):
                    continue
                if not _dep_usable(dep):
                    continue
                if need and not self._dep_supports(dep, need):
                    continue
                if not self._cap_fits(dep, ctx):
                    continue
                if (out_tokens is not None and out_tokens > 0
                        and not self.dep_deliverable(dep, need, ctx,
                                                     out_tokens)):
                    continue
                borr.append(dep)
            if borr:
                log.info("[warm] prestito: %d dep da altre sessioni (fermi "
                         "da >=%.0fs) nel blocco prestati di %s",
                         len(borr),
                         float(getattr(self.policy, "warm_borrow_idle_sec",
                                       240.0) or 0.0), sid)
                out = out + borr
        if out:
            out.sort(key=_wkey)     # blocco unico: propri > prestati > lenti
        if not out:
            return []
        max_n = max(0, int(getattr(self.policy, "warm_pool_max_attempts", 0) or 0))
        if max_n > 0:
            out = out[:max_n]
        log.debug("[warm] pool=%d sid=%s: %s", len(out), sid,
                  ",".join(d["unique"] for d in out[:6]))
        _n_own = sum(1 for d in out if d["unique"] in _own_set)
        # "propri" e' ownership (ts rinfrescato finche' la sessione e' viva),
        # NON l'ultimo uso effettivo: un proprio puo' essere gia' PRESTABILE
        # ad altre sessioni se il DEPLOYMENT e' fermo da >= borrow_idle_sec.
        # Lo contiamo a parte per chiarezza (stessa definizione di _lendable_set).
        try:
            _lend = self._lendable_set(now)
        except Exception:                              # noqa: BLE001
            _lend = set()
        _n_idle = sum(1 for d in out
                      if d["unique"] in _own_set and d["unique"] in _lend)
        log.info("[warm] pool %s: %s (propri %d, di cui prestabili %d "
                 "[fermi >=%.0fs], prestiti %d)",
                 sid, self._provider_mix(out), _n_own, _n_idle,
                 float(getattr(self.policy, "warm_borrow_idle_sec", 240.0)
                       or 0.0),
                 len(out) - _n_own)
        return out

    @staticmethod
    def _provider_mix(deps: list[dict]) -> str:
        """Composizione per provider di un pool, tipo 'openrouter 12,
        bynara 3, opencode-zen 1' (ordine decrescente)."""
        c: Counter = Counter()
        for d in deps or ():
            p = str((d or {}).get("provider") or "").strip() or "?"
            c[p] += 1
        return ", ".join(f"{p} {n}" for p, n in c.most_common())

    def _borrow_selectable(self) -> bool:
        """I prestati sono anche SELEZIONABILI (nel blocco prestati, dopo i
        propri non lenti) o solo contati per i 3 ready?
        `warm_borrow_selectable=False` = solo conteggio."""
        return (bool(getattr(self.policy, "warm_borrow_enabled", True))
                and bool(getattr(self.policy, "warm_borrow_selectable", True)))

    def _lendable_set(self, now: float | None = None) -> set[str]:
        """Warm "prestabili" a livello GLOBALE (memo ~5s): il dep ha un owner
        vivo, e' fermo da `warm_borrow_idle_sec` (idle del DEPLOYMENT, non
        della sessione) e non ha richieste in volo. Non dipende dalla
        sessione corrente: chi lo consuma scarta i propri (priorita' propri)."""
        if not getattr(self.policy, "warm_borrow_enabled", True):
            return set()
        now = time.time() if now is None else now
        cache = getattr(self, "_lendable_cache", None)
        if cache is not None and (now - cache[0]) < 5.0:
            return cache[1]
        out: set[str] = set()
        idle = float(getattr(self.policy, "warm_borrow_idle_sec", 240.0) or 0.0)
        try:
            guard = self._guard_sec()
            for u, ent in list(self._dep_sess().items()):
                if not ent or not ent[0]:
                    continue
                try:
                    if (now - float(ent[1] or 0.0)) >= guard:
                        continue                       # owner decaduto
                    if int(getattr(self.stats_for(u), "inflight", 0) or 0) > 0:
                        continue                       # in volo: non e' fermo
                except Exception:                      # noqa: BLE001
                    continue
                if self._dep_idle_age(u, now) >= idle:
                    out.add(u)
        except Exception:                              # noqa: BLE001
            return set()
        self._lendable_cache = (now, out)
        return out

    def _borrowable(self, unique: str, now: float | None = None) -> bool:
        """True se `unique` e' un warm PRESTABILE: owner di un'ALTRA sessione
        ancora vivo, il DEPLOYMENT fermo da almeno `warm_borrow_idle_sec` e
        nessuna richiesta in volo su di lui (una generazione lunga non e'
        "fermo"). Nessun requisito sul numero di warm del proprietario: anche
        lui conta propri+prestati e decidera' da solo se gli serve un canary."""
        if not getattr(self.policy, "warm_borrow_enabled", True):
            return False
        ent = self._dep_sess().get(unique)
        if not ent:
            return False
        now = time.time() if now is None else now
        try:
            owner, ts = ent[0], float(ent[1] or 0.0)
        except Exception:                              # noqa: BLE001
            return False
        sid = current_session()
        if not owner or owner == sid:
            return False
        if (now - ts) >= self._guard_sec():            # owner decaduto
            return False
        try:
            if int(getattr(self.stats_for(unique), "inflight", 0) or 0) > 0:
                return False
        except Exception:                              # noqa: BLE001
            return False
        idle = float(getattr(self.policy, "warm_borrow_idle_sec", 240.0) or 0.0)
        return self._dep_idle_age(unique, now) >= idle

    def warm_ready_effective(self, session_id: str | None,
                             policy=None) -> int:
        """Soglia `warm_ready_min` effettiva: sale con la media rpm della
        sessione, tappata a `warm_ready_min_max`:
            ready = min(ready_min + ceil((rpm-base)/step), min_max)
        con ceil solo se rpm > base. `adaptive=False` -> soglia fissa."""
        pol = policy or self.policy
        rmin = max(0, int(getattr(pol, "warm_ready_min", 3) or 0))
        if not session_id or not bool(
                getattr(pol, "warm_ready_rpm_adaptive", True)):
            return rmin
        ready = rmin
        cap = max(rmin, int(getattr(pol, "warm_ready_min_max", rmin) or rmin))
        rpm = self.session_rpm(session_id, getattr(
            pol, "warm_ready_rpm_window_sec", 180))
        b = float(getattr(pol, "warm_ready_rpm_base", 5.0) or 0.0)
        st = float(getattr(pol, "warm_ready_rpm_step", 5.0) or 0.0)
        if st > 0 and rpm > b:
            ready += int(math.ceil((rpm - b) / st))
        return max(rmin, min(ready, cap))

    def warm_valid_for(self, session_id: str | None, profile: str | None,
                       group_name: str | None,
                       need: frozenset[str] | None, ctx: int | None,
                       out_tokens: int | None,
                       tried: set[str] | None = None,
                       failed_unique: str | None = None,
                       include_borrowed: bool = False) -> list[dict]:
        """I caldi della sessione che possono EFFETTIVAMENTE servire questa
        richiesta (need + ctx + output assicurato): e' COSI' che si contano i
        "3 pronti-caldi" del refill, non il numero grezzo del pool.

        Con `include_borrowed=True` contano anche i warm PRESTABILI di altre
        sessioni (fermi da `warm_borrow_idle_sec`): e' cosi' che si evita di
        sprecare un canary quando le carte utili ci sono gia'."""
        if not profile:
            return []
        allowed = self._warm_allowed(profile, group_name)
        pool = self._warm_pool(session_id, allowed, need, ctx, tried,
                               failed_unique, include_borrowed=include_borrowed,
                               out_tokens=out_tokens)
        return [d for d in pool
                if self.dep_deliverable(d, need, ctx, out_tokens)]

    def warm_api_keys(self, session_id: str | None, pname: str | None,
                      group_name: str | None,
                      include_borrowed: bool | None = None) -> set[str]:
        """Chiavi api gia' rappresentate (stessa api_key) dai deployment nel
        warm della
        sessione: un probe di refill NON deve testare una chiave che abbiamo
        gia' nel parco dei caldi. Con i prestiti attivi si considerano anche
        le chiavi dei prestabili (sono comunque a disposizione)."""
        if not pname:
            return set()
        if include_borrowed is None:
            include_borrowed = bool(getattr(self.policy,
                                            "warm_borrow_selectable", True))
        try:
            pool = self._warm_pool(session_id,
                                   self._warm_allowed(pname, group_name),
                                   include_borrowed=include_borrowed)
        except Exception:                          # noqa: BLE001
            return set()
        return {str(d.get("api_key") or "") for d in pool
                if d.get("api_key")}
