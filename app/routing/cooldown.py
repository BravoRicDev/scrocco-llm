"""Cluster cooldown (estratto verbatim da router.py, refactor R2 Round 3).

[IT] Mixin con il ciclo di VITA del cooldown di router.py: la preferenza
modello che scala la durata (`_pref_for` / `_pref_cooldown_factor` /
`_effective_cooldown_full`), i query di stato (`cooldown_residual`,
`is_cooled_down`, `cooldown_age`, `cooldown_progress`, `probe_ready`), il
decadimento della penalita' (`_cooldown_decay`, `_decay_streak`), l'anti-herd
(jitter deterministico per-unique + componente random), il lifecycle
keyhealth (ritiro automatico e consultazione) e la potatura periodica
(`_prune_wake_times`, `_prune_hunt_state`, `purge_expired`, piu' la mappa
`_SESSION_STATE_MAPS` delle 15 mappe per-sessione). Codice spostato senza
modifiche.

[EN] Cooldown-lifecycle mixin: model-preference scaling of the cooldown
duration, the state queries (residual / cooled-down / age / progress / probe
readiness), penalty decay, anti-herd jitter (deterministic per-unique spread
plus a random component), the keyhealth retirement lifecycle, and the periodic
pruning (`purge_expired` plus the 15 per-session state maps). Verbatim move,
no behaviour change. See docs/ROUTING.md.

Dipendenze: questo mixin NON definisce attributi d'istanza e NON inizializza
niente. Usa lo stato che `Router` inizializza in `__init__` (`_cooldown`,
`_cooldown_since`, `_cooldown_full`, `_stats`, `_sticky`, `policy`, ...)
piu' i metodi di SessionMixin (`_sess_est`, `_sess_floor_map`, `_dep_sess`,
`_sess_deps`, `_guard_sec`) e UsageMixin (`_go_balance_window`). Le costanti di
classe (`_SESSION_STATE_MAPS`) viaggiano con i metodi che le usano; `Router` le
eredita normalmente. Gli import di `main` restano lazy dentro i metodi per
evitare cicli d'import.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time

from ..caution import background_cautious_enabled
from .estimate import _is_quota_evidence

log = logging.getLogger("nx.router")


class CooldownMixin:
    def _pref_for(self, unique: str) -> int:
        """model_preference del deployment (0 se non disponibile)."""
        try:
            dep = self.config.deployment_by_unique(unique)
        except Exception:  # noqa: BLE001
            dep = None
        if not dep:
            return 0
        try:
            return int(dep.get("model_preference") or 0)
        except (TypeError, ValueError):
            return 0

    def _pref_cooldown_factor(self, raw_full: float, pref: int) -> float:
        """Fattore >0 da applicare alla durata GREZZA del cooldown: >1 allunga
        (preferenza negativa = riposa di piu'), <1 accorcia (positiva = ritenta
        prima). Sotto i 60s (dato grezzo) nessuna modifica. Ampiezza max
        +/-50%, pari a preferenza/2."""
        if raw_full <= 60.0:
            return 1.0
        pct = max(-50.0, min(50.0, float(pref) / 2.0))
        return 1.0 - pct / 100.0

    def _effective_cooldown_full(self, unique: str, full: float) -> float:
        """Durata efficace = durata grezza scalata dalla preferenza del
        deployment, clampata a [1, max_cooldown_sec]. ECCEZIONE: i cooldown di
        QUOTA (giornaliera/mensile) seguono il RESET dichiarato o stimato
        (mezzanotte UTC / "Resets in 9 days") e non vanno tagliati dal ceiling
        operatore, altrimenti la chiave torna prima del reset e si riprova a
        vuoto (osservato: quota CF giornaliera tagliata a 5h)."""
        factor = self._pref_cooldown_factor(full, self._pref_for(unique))
        _pol = getattr(self, "policy", None)
        _mx = float(getattr(_pol, "max_cooldown_sec", 18000) or 18000)
        _s = self._stats.get(unique)
        _rs = str(getattr(_s, "last_reason", "") or "")
        if _rs.startswith("quota_exhausted"):
            _mx = max(_mx, 7 * 86400.0)  # QUOTA_MAX_COOLDOWN_S (7 giorni)
        return max(1.0, min(_mx, full * factor))

    def cooldown_residual(self, unique: str) -> float:
        """Residuo (secondi) del cooldown RESIDUO, scalato dalla preferenza del
        deployment. Lo storage resta GREZZO: qui si applica solo il fattore."""
        exp = self._cooldown.get(unique)
        if exp is None:
            return 0.0
        now = time.time()
        since = self._cooldown_since.get(unique)
        full = self._cooldown_full_map().get(unique) or 0.0
        if full > 0 and since is not None:
            return max(0.0, since + self._effective_cooldown_full(unique, full) - now)
        return max(0.0, exp - now)

    def is_cooled_down(self, unique: str) -> bool:
        exp = self._cooldown.get(unique)
        if exp is None:
            return False
        if self.cooldown_residual(unique) > 0.0:
            return True
        # Residuo efficace esaurito: pota l'entry solo se anche il grezzo e'
        # passato; se il cooldown e' stato accorciato dalla preferenza lascio
        # il dato grezzo fino alla sua scadenza.
        if time.time() > exp:
            self._cooldown.pop(unique, None)
            self._cooldown_since.pop(unique, None)
            self._cooldown_full_map().pop(unique, None)
        return False

    def cooldown_age(self, unique: str) -> float | None:
        """Da quanti secondi e' in cooldown questo deployment (None se non lo e'
        o se non lo sappiamo)."""
        since = self._cooldown_since.get(unique)
        return (time.time() - since) if since is not None else None

    def cooldown_progress(self, unique: str) -> float | None:
        """Frazione di cooldown TRASCORSA (0..1); None se non in cooldown o se
        la durata totale non e' nota."""
        if self._cooldown.get(unique) is None:
            return None
        full = self._cooldown_full_map().get(unique) or 0.0
        if full <= 0:
            return None
        resid = self.cooldown_residual(unique)
        if resid <= 0.0:
            return 1.0
        eff = self._effective_cooldown_full(unique, full)
        return max(0.0, min(1.0, 1.0 - resid / max(1e-6, eff)))

    def _cooldown_full_map(self) -> dict:
        """Mappa unique -> durata totale del cooldown (lazy: alcuni test
        costruiscono il Router via __new__ senza inizializzarla)."""
        m = getattr(self, "_cooldown_full", None)
        if m is None:
            m = {}
            self._cooldown_full = m
        return m

    def probe_ready(self, unique: str) -> bool:
        """True se un deployment dormiente e' maturo per un probe passivo
        (>= cooldown_probe_after_ratio del cooldown trascorso)."""
        if background_cautious_enabled():  # cautela generica: niente re-probe
            return False
        if not getattr(self.policy, "cooldown_probe_enabled", True):
            return False
        ratio = float(getattr(self.policy, "cooldown_probe_after_ratio", 0.5) or 0.0)
        pr = self.cooldown_progress(unique)
        return pr is not None and pr >= ratio

    def _cooldown_decay(self, unique: str) -> float:
        """Fattore di DECADIMENTO della penalita' durante il cooldown:
        1.0 = penalita' piena (appena messo), 0.0 = neutra (cooldown finito).
        Con `cooldown_probe_decay` off ritorna sempre 1.0."""
        if not getattr(self.policy, "cooldown_probe_decay", True):
            return 1.0
        pr = self.cooldown_progress(unique)
        if pr is None:
            return 1.0
        return max(0.0, 1.0 - pr)

    def _decay_streak(self, streak: int, last_fail_ts: float, now: float | None = None) -> int:
        """Decadimento del fail_streak per inattivita' (halflife).

        Dopo `cooldown_streak_halflife_sec` senza fallimenti lo streak si
        dimezza, cosi' una chiave riattivata dopo ore non viene riesiliata
        per un singolo errore isolato. 0 = nessun decadimento."""
        if streak <= 0:
            return 0
        hl = float(getattr(self.policy, "cooldown_streak_halflife_sec", 0) or 0)
        if hl <= 0 or not last_fail_ts:
            return streak
        now = time.time() if now is None else now
        elapsed = max(0.0, now - float(last_fail_ts))
        if elapsed <= 0:
            return streak
        decayed = int(round(streak * (0.5 ** (elapsed / hl))))
        decayed = max(0, min(streak, decayed))
        if decayed < streak:
            log.info("[streak] %d -> %d dopo %.0f min di inattivita'", streak, decayed, elapsed / 60.0)
        return decayed

    def _jitter_spread(self, unique: str) -> float:
        """Spread DETERMINISTICO per-unique (0..cooldown_jitter_sec_max s).

        Anti-herd: i gemelli che prendono 429 nello stesso secondo scadono
        spalmati su ~2s invece che al medesimo millisecondo (altrimenti al
        secondo N ripartono tutti insieme -> nuova raffica di 429). E' una
        funzione pura di `unique`: stabile tra restart, niente random."""
        cap = float(getattr(self.policy, "cooldown_jitter_sec_max", 2.0) or 0.0)
        if cap <= 0:
            return 0.0
        h = hashlib.sha256(str(unique).encode("utf-8")).digest()
        return (int.from_bytes(h[:4], "big") % 2000) / 1000.0 * (cap / 2.0)

    def _apply_jitter(self, seconds: float, unique: str | None = None) -> float:
        """Jitter sul cooldown: componente ADDITIVA deterministica per-unique
        (0..cooldown_jitter_sec_max s) + eventuale componente random
        moltiplicativa (`cooldown_jitter_ratio`, default 0 = disattivata).

        Lo spread additivo spalma la scadenza dei gemelli che incassano 429
        nello stesso secondo: senza, ripartono tutti al medesimo ms e
        rifanno raffica."""
        sec = max(1.0, float(seconds))
        ratio = float(getattr(self.policy, "cooldown_jitter_ratio", 0.0) or 0.0)
        if ratio > 0:
            sec = max(1.0, sec * random.uniform(1.0 - ratio, 1.0 + ratio))
        if unique:
            sec += self._jitter_spread(unique)
        return max(1.0, sec)

    def _maybe_retire_on_probe_fail(self, unique: str, s) -> bool:
        """Auto-retirement dopo N probe passivi consecutivi falliti: se il
        problema non e' temporaneo (credenziali/modello morti) smettiamo di
        sprecare probe. Il CSV non viene toccato (unretire manuale o probe ok).

        Un'evidenza di QUOTA (429/satura) non basta a ritirare: la chiave e'
        viva, ha solo finito il budget del momento. Il ritiro scatta solo su
        fallimenti sostanziali (401/403/modello morto/5xx permanenti)."""
        cap = int(getattr(self.policy, "probe_retire_after", 0) or 0)
        if cap <= 0 or s.probe_fail_streak < cap:
            return False
        if _is_quota_evidence(getattr(s, "last_reason", None)):
            log.debug("[probe] %s non ritirato: ultima evidenza di quota (%s)", unique, s.last_reason)
            return False
        try:
            from .. import main as _gw_mod  # lazy: evita cicli d'import

            kh = getattr(_gw_mod, "KEYHEALTH", None)
            if kh is not None and not kh.is_retired(unique):
                kh.set_state(unique, "retired", reason="probe_escalation_cap")
                kh.save()
                log.warning(
                    "[probe] %s RETIRED: %d probe consecutivi falliti (problema permanente, non temporaneo)",
                    unique,
                    s.probe_fail_streak,
                )
                return True
        except Exception:  # mai bloccare il routing
            log.warning("[probe] auto-retirement di %s fallito", unique, exc_info=True)
        return False

    def is_retired(self, unique: str) -> bool:
        """Chiave RETIRED (lifecycle keyhealth): esclusa dal routing.

        NON e' una cancellazione: il CSV resta intatto; si sblocca con
        POST /admin/deployments/unretire o con un probe riuscito.
        """
        try:
            from .. import main as _gw_mod  # lazy: evita cicli d'import

            kh = getattr(_gw_mod, "KEYHEALTH", None)
            return bool(kh and kh.is_retired(unique))
        except Exception:  # mai bloccare il routing
            return False

    def _retired_permanent(self, unique: str) -> bool:
        """Ritirato per motivo PERMANENTE: mai riusabile, nemmeno in ultima
        spiaggia (spam di errori inutili su una chiave/modello morti)."""
        try:
            from .. import main as _gw_mod  # lazy: evita cicli d'import

            kh = getattr(_gw_mod, "KEYHEALTH", None)
            return bool(kh and kh.is_permanently_retired(unique))
        except Exception:  # mai bloccare il routing
            return False

    def _retired_usable(self, unique: str) -> bool:
        """Ritirato NON permanente (quota/probe-cap): fuori dai tier normali,
        eleggibile SOLO come ultima spiaggia. Un successo lo ripulisce dal
        lifecycle (successo = prova di vita), senza spendere probe."""
        return self.is_retired(unique) and not self._retired_permanent(unique)

    def _prune_wake_times(self, now: float | None = None) -> None:
        """Pota i timestamp di wakeup oltre la finestra (memoria)."""
        now = now if now is not None else time.time()
        _win = max(1.0, float(getattr(self.policy, "ladder_cooldown_wakeup_window_sec", 3600) or 3600))
        for u, dq in list((getattr(self, "_wake_times", None) or {}).items()):
            while dq and now - dq[0] > _win:
                dq.popleft()
            if not dq:
                getattr(self, "_wake_times", {}).pop(u, None)

    def _prune_hunt_state(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        win = float(getattr(self.policy, "hunt_window_sec", 3600) or 3600)
        for k, st in list((getattr(self, "_hunt_state", None) or {}).items()):
            dq = st.get("races")
            while dq and now - dq[0] > win:
                dq.popleft()
            if not dq and float(st.get("backoff_until") or 0.0) <= now:
                getattr(self, "_hunt_state", {}).pop(k, None)
        if len(self._fleet_cache) > 4096:
            self._fleet_cache.clear()

    def purge_expired(self) -> tuple[int, int]:
        """Rimuove sticky scadute e cooldown espirati (chiamato dal watcher).

        Senza purge, con tanti session_id unici, i dict crescerebbero senza
        limite sugli uptime lunghi. Ritorna (sticky_rimosse, cooldown_rimossi).

        [Blocco 1] Ora include anche la pulizia di _stats (memory leak fix):
        rimuove entry stale (>48h) per prevenire crescita infinita della memoria.
        """
        self._prune_hunt_state()
        self._prune_wake_times()
        now = time.time()
        self._decay_scores(now)  # time-decay reputazione (halflife policy)
        dead_sessions = [s for s, (_t, ts) in self._sticky.items() if now - ts > self.policy.sticky_ttl_sec]
        for s in dead_sessions:
            self._sticky.pop(s, None)
        dead_sg = [s for s, (_g, ts) in self._session_group.items() if now - ts > self.policy.sticky_ttl_sec]
        for s in dead_sg:
            self._session_group.pop(s, None)
        # PURGE deployment-sticky: stessa TTL dello sticky di gruppo
        dead_dep = [s for s, (_u, ts) in self._sticky_dep.items() if now - ts > self.policy.sticky_ttl_sec]
        for s in dead_dep:
            self._sticky_dep.pop(s, None)
        # STIMA per-sessione: TTL di policy + cap 4096 (eviction sul piu' vecchio)
        _sr = self._sess_est()
        _sttl = int(getattr(self.policy, "session_estimate_ttl_sec", 3600) or 0)
        if _sttl > 0:
            for s in [s for s, r in _sr.items() if now - float((r or {}).get("ts") or 0.0) > _sttl]:
                _sr.pop(s, None)
        if len(_sr) > 4096:
            for s in sorted(_sr, key=lambda k: float((_sr[k] or {}).get("ts") or 0.0))[: len(_sr) - 4096]:
                _sr.pop(s, None)
        # FLOOR per-sessione (overflow context): stessa TTL + cap 4096.
        _sf = self._sess_floor_map()
        if _sttl > 0:
            for s in [s for s, (_v, t) in _sf.items() if now - t > _sttl]:
                _sf.pop(s, None)
        if len(_sf) > 4096:
            for s in sorted(_sf, key=lambda k: _sf[k][1])[: len(_sf) - 4096]:
                _sf.pop(s, None)
        # TURNI/RIMBORSO -go per-sessione: TTL sticky + cap 4096.
        _tn = getattr(self, "_session_turns", None)
        if isinstance(_tn, dict):
            for s in [
                s for s, e in _tn.items() if now - float((e or {}).get("ts") or 0.0) > self.policy.sticky_ttl_sec
            ]:
                _tn.pop(s, None)
            if len(_tn) > 4096:
                for s in sorted(_tn, key=lambda k: float((_tn[k] or {}).get("ts") or 0.0))[: len(_tn) - 4096]:
                    _tn.pop(s, None)
        # BILANCIAMENTO -go: token di output — pota la finestra e limita le voci.
        _ot = getattr(self, "_out_tokens", None)
        if isinstance(_ot, dict):
            _owin = self._go_balance_window()
            for u in [u for u, dq in _ot.items() if not dq or now - dq[-1][0] > _owin]:
                _ot.pop(u, None)
            if len(_ot) > 4096:
                for u in sorted(_ot, key=lambda k: _ot[k][-1][0] if _ot[k] else 0.0)[: len(_ot) - 4096]:
                    _ot.pop(u, None)
        # SESSION-DEP GUARD: entry piu' vecchi della finestra (x2) non servono.
        _gttl = self._guard_sec() * 2
        _ds = self._dep_sess()
        for u in [u for u, (_s, ts) in _ds.items() if now - ts > _gttl]:
            _ds.pop(u, None)
        # Indice session -> dep posseduti: tieni solo i dep ancora tracciati.
        _sd = self._sess_deps()
        for s in list(_sd):
            live = {u for u in _sd[s] if u in _ds}
            if live:
                _sd[s] = live
            else:
                _sd.pop(s, None)
        # AUDIT prefisso: le impronte vecchie come la guard non servono.
        _pf = getattr(self, "_prefix_fp", None)
        if isinstance(_pf, dict):
            for s in [s for s, (_b, _y, t) in _pf.items() if now - t > _gttl]:
                _pf.pop(s, None)
        # SOFT-PER-CHIAVE: hint scaduti (ttl x2) e blocchi oltre la scadenza.
        _kh = getattr(self, "_key_hints", None)
        if isinstance(_kh, dict):
            _kttl = max(120.0, float(getattr(self.policy, "rate_hint_ttl_sec", 20.0) or 20.0) * 2)
            for tag in [t for t, (ts, _r) in _kh.items() if now - ts > _kttl]:
                _kh.pop(tag, None)
        _ks = getattr(self, "_key_soft", None)
        if isinstance(_ks, dict):
            for tag in [t for t, until in _ks.items() if until <= now]:
                _ks.pop(tag, None)
        # F25: breaker di modello — dimentica le aperture molto scadute.
        _mcb = getattr(self, "_model_cb", None)
        if isinstance(_mcb, dict):
            _mttl = max(300.0, float(getattr(self.policy, "model_circuit_open_sec", 60) or 60) * 4)
            for k in [k for k, e in _mcb.items() if now - max(e.get("opened") or 0.0, e.get("ts") or 0.0) > _mttl]:
                _mcb.pop(k, None)
        dead_cd = [u for u, exp in self._cooldown.items() if now > exp]
        for u in dead_cd:
            self._cooldown.pop(u, None)
            self._cooldown_since.pop(u, None)
            self._cooldown_full_map().pop(u, None)
        # PURGE escalation-winner: TTL a finestra scorrevole; gli entries
        # vecchi di escalation_pin_ttl_sec vengono droppati.
        _epp = max(1, int(getattr(self.policy, "escalation_pin_ttl_sec", 300) or 300))
        _ewd = self._esc()
        dead_ew = [g for g, (_u, ts) in _ewd.items() if now - ts > _epp]
        for g in dead_ew:
            _ewd.pop(g, None)
        # [Blocco 1] Cleanup _stats: rimuovi entry vecchie di 48h (fix memoria)
        stale_stats = [u for u, s in self._stats.items() if now - s.last_used > 172800]  # 48h
        for u in stale_stats:
            del self._stats[u]
        # [Blocco 1] Cleanup _cap_strikes: rimuovi strike vecchi di 7gg
        stale_strikes = [k for k, st in self._cap_strikes.items() if now - st.get("last", 0) > 604800]  # 7gg
        for k in stale_strikes:
            del self._cap_strikes[k]
        # [Blocco 1] Cleanup scoring: rimuovi punteggi dei deployment non più attivi.
        # Difensivo: alcuni test usano Router.__new__ senza config né attributi
        # di scoring inizializzati: in quel caso si salta la pulizia.
        try:
            self._init_scoring_if_needed()
            active_uniques = set(self.config.all_uniques()) if hasattr(self.config, "all_uniques") else set()
            if not active_uniques:
                # Fallback: raccogli tutti gli unique dai gruppi
                for deps in self.config.groups.values():
                    for d in deps:
                        active_uniques.add(d["unique"])
            stale_base = [u for u in self._base_scores if u not in active_uniques]
            for u in stale_base:
                self._base_scores.pop(u, None)
                self._avg_latencies.pop(u, None)
                getattr(self, "_lat_buckets", {}).pop(u, None)
                getattr(self, "_ttft_buckets", {}).pop(u, None)
                getattr(self, "_prefill_rate", {}).pop(u, None)
                getattr(self, "_est_div", {}).pop(u, None)
            # Cleanup provider/key scores vecchi: mantieni solo chiavi attive
            active_providers = set()
            active_keys = set()
            for deps in self.config.groups.values():
                for d in deps:
                    active_providers.add(self._provider_key(d))
                    active_keys.add(self._api_key_str(d))
            stale_prov = [k for k in self._provider_scores if k not in active_providers]
            for k in stale_prov:
                self._provider_scores.pop(k, None)
            stale_keys = [k for k in self._key_scores if k not in active_keys]
            for k in stale_keys:
                self._key_scores.pop(k, None)
        except Exception as exc:  # noqa: BLE001
            log.debug("[purge] scoring cleanup saltato (%s)", exc)
        if dead_sessions or dead_cd or dead_sg or dead_dep:
            log.debug(
                "[purge] sticky=%d cooldown=%d sessioni=%d dep_sticky=%d",
                len(dead_sessions),
                len(dead_cd),
                len(dead_sg),
                len(dead_dep),
            )
        if stale_stats or stale_strikes:
            log.debug("[purge] stats=%d strikes=%d cleaned", len(stale_stats), len(stale_strikes))
        return len(dead_sessions), len(dead_cd)

    _SESSION_STATE_MAPS = (
        "_sticky",
        "_sticky_dep",
        "_session_group",
        "_session_last_ok",
        "_session_deps",
        "_session_slow",
        "_session_slow_timer",
        "_ctx_frontier",
        "_prefix_fp",
        "_session_compact",
        "_sess_ratio",
        "_sess_floor",
        "_session_rate",
        "_session_turns",
        "_probes_flight",
    )
