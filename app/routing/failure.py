"""Cluster failure (estratto verbatim da router.py, refactor R3).

[IT] Mixin con la gestione del FALLIMENTO e del COOLDOWN di router.py:
`mark_failed` (mette in cooldown un deployment, classi d'errore, tetto
operatore, jitter, soft-skip per-chiave su 429, circuit breaker),
l'escalation dolce del cooldown per fallimenti ricorrenti
(`escalate_cooldown`), il raddoppio del residuo per i deployment
dormienti che falliscono ancora (`mark_failed_double_residual`), la rimozione
del cooldown e azzeramento dei contatori su successo (`clear_cooldown`), i
blocchi soft per chiave sospetta (`_key_tag`, `_key_fault_blocked`,
`note_rate_limit`) e il ritiro permanente di un deployment rotto
(`_retire_permanent`). Codice spostato senza modifiche.

[EN] Failure/cooldown management mixin: `mark_failed` (cooldown, error-class
tiers, operator ceiling, jitter, 429 per-key soft-skip, circuit breaker),
gentle cooldown escalation for recurring failures, residual doubling for
sleeping deployments, cooldown removal on success, soft blocks for suspicious
keys, and permanent retirement of a broken deployment. Verbatim move, no
behaviour change. See docs/ROUTING.md.

Dipendenze: questo mixin NON definisce attributi d'istanza e NON inizializza
niente. Usa lo stato che `Router` inizializza in `__init__` (`_cooldown`,
`_cooldown_since`, `_stats`, `_key_soft`, `_key_hints`, `policy`, ...)
piu' i metodi di CooldownMixin (`_apply_jitter`, `_decay_streak`,
`_cooldown_full_map`, `_maybe_retire_on_probe_fail`) e i metodi interni di
Router (`_esc`, `_cooldown_prov`, `_update_circuit_breaker_on_failure`,
`_update_circuit_breaker_on_success`, `_punish_concurrency`, `_error_class`,
`_is_key_level_failure`, `_note_model_failure`, `_dep_of`,
`_infer_provenance`, `_is_renewal_bucket`, `record_failure`, `stats_for`).
Gli import di `main` restano lazy dentro i metodi per evitare cicli d'import.
"""

from __future__ import annotations

import hashlib
import logging
import time

from .estimate import ErrorKind, _is_quota_evidence
from ..policy import policy_float, policy_int

log = logging.getLogger("nx.router")


class FailureMixin:
    def mark_failed(
        self,
        unique: str,
        seconds: float | None = None,
        reason: str | None = None,
        status: int | None = None,
        kind: str | None = None,
        *,
        additive: bool = False,
        provenance: str | None = None,
    ) -> float:
        """Marca il deployment fallito con cooldown.

        - `seconds` esplicito vince SEMPRE (es. Retry-After su 429)
        - `additive=True`: ESTENDE il cooldown esistente di `seconds` invece di
          sovrascriverlo (autoprobe: un KO allunga, non resetta).
        - default: dipende da cooldown_mode:
          * "linear": BASE + MULT*(fail_count_24h - 1) minuti
          * "exponential": cooldown_sec * 2^(streak-1) (legacy)
        - `reason`: classificazione dell'errore (http_429, no_credits,
          not_found...). Un 429 alimenta il BUDGET GUARD.
        - `status`: codice HTTP upstream per la classificazione key/provider.
        Ritorna i secondi applicati.
        """
        # [Blocco 1] Aggiorna reputation scoring
        self.record_failure(unique, reason, status)

        pol = self.policy
        s = self.stats_for(unique)
        # ESC-PIN: se questo deployment era il winner di qualche bucket, lo
        # sblocchiamo subito (un winner che fallisce NON deve essere riattaccato
        # alla richiesta dopo: si riapprende alla prossima salita buona).
        for _g, (_u, _ts) in list(self._esc().items()):
            if _u == unique:
                self._esc().pop(_g, None)
        if reason:
            s.last_reason = reason
        # --- fail_count_24h: contatore giornaliero (mai azzerato da successi)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if s.fail_day_key != today:
            s.fail_count_24h = 0
            s.fail_day_key = today
        s.fail_count_24h += 1
        # contatore cumulativo + timestamp ultimo fallimento (persistiti)
        s.fail_count += 1
        s.fail_streak = self._decay_streak(s.fail_streak, s.last_fail_ts)
        s.last_fail_ts = time.time()
        if kind == ErrorKind.PERMANENT_DEAD:
            # Chiave morta / modello rimosso: il cooldown e' inutile (non
            # tornera'), si ritira il deployment (CSV intatto) e si smette di
            # sprecare tentativi, log e metriche.
            self._update_circuit_breaker_on_failure(unique, key_level=True)
            self._retire_permanent(unique, reason or "permanent_dead")
            log.warning(
                "[cooldown] %s errore PERMANENTE (%s): niente cooldown, deployment retired", unique, reason or "-"
            )
            return 0.0
        # --- budget guard: apprendimento del limite dal 429 --------------
        bg_cfg = pol.budget_guard or {}
        if reason in ("http_429", "quota_exhausted") and bg_cfg.get("enabled"):
            floor_min = max(1.0, float(bg_cfg.get("min_per_min", 10)))
            floor_day = max(1.0, float(bg_cfg.get("min_per_day", 200)))
            learned_min = max(floor_min, s.minute_calls * 1.2)
            learned_day = max(floor_day, s.day_calls * 1.2)
            s.min_cap_learned = max(s.min_cap_learned, learned_min)
            s.day_cap_learned = max(s.day_cap_learned, learned_day)
            log.info(
                "[budget] %s: cap appresi da 429 -> ~%.0f/min, ~%.0f/giorno",
                unique,
                s.min_cap_learned,
                s.day_cap_learned,
            )
        # --- dynamic concurrency limit: 429/503 = concorrenza oltre il
        # limite -> dimezza il limite appreso (min 1) -----------------------
        if status in (429, 503):
            self._punish_concurrency(unique)
        s.fail_streak += 1
        prev = 1.0 if s.success_ema is None else s.success_ema
        s.success_ema = max(0.0, 0.8 * prev)  # EMA verso lo 0 (α=0.2)
        _explicit_seconds = seconds is not None  # F18: solo per i DERIVATI
        # PROVENIENZA del cooldown (P0): da cosa NASCE. Solo gli 'heuristic'
        # (nostra stima) possono essere testati in anticipo dalla sveglia;
        # 'authoritative' (Retry-After/quota dichiarati dal provider), 'credit'
        # (402) e 'tier' (403) NON si toccano: il provider ha detto quando
        # torna e ritentare prima e' solo rumore (e rischio ban).
        _prov = provenance or self._infer_provenance(reason, status, _explicit_seconds)
        s.last_provenance = _prov
        self._cooldown_prov()[unique] = _prov
        if seconds is None:
            mode = getattr(pol, "cooldown_mode", "linear") or "linear"
            if mode == "linear":
                base_m = max(1, int(getattr(pol, "cooldown_base_min", 30) or 30))
                mult_m = max(0, int(getattr(pol, "cooldown_linear_mult_min", 30) or 30))
                seconds = (base_m + mult_m * (s.fail_count_24h - 1)) * 60.0
                seconds = min(seconds, float(pol.max_cooldown_sec))
            elif pol.cooldown_escalation and s.fail_streak > 1:
                expo = min(s.fail_streak - 1, 24)
                seconds = min(float(pol.max_cooldown_sec), float(pol.cooldown_sec) * (2**expo))
            else:
                seconds = float(pol.cooldown_sec)
        seconds = max(1.0, float(seconds))
        # TETTO operatore sui cooldown STIMATI ('heuristic'): un retry
        # dichiarato dal provider (Retry-After/quota => 'authoritative') o un
        # credit/tier non si tocca MAI. 0 = nessun tetto.
        _is_quota = str(reason or "").startswith("quota_exhausted")
        if _prov == "heuristic" and not _is_quota:
            _ceil = max(0, int(getattr(pol, "cooldown_estimate_ceiling_sec", 0) or 0))
            if _ceil > 0:
                seconds = min(seconds, float(_ceil))
        # ── CLASSI DI ERRORE (F18) ────────────────────────────────────────
        # 429 = quota: chiave satura -> soft per-chiave (sotto) + durata
        #   dettata dal Retry-After; 503/529 = dep sovraccarico -> cooldown
        #   breve per-unique; 500/timeout = transitorio -> breve dedicato.
        # Evita che un 503 isolato tenga la chiave fuori per 60s e mangi
        # _key_scores per ore (halflife) e che un timeout esploda a minuti.
        _cls = self._error_class(status, reason)
        if getattr(pol, "error_class_cooldowns", True):
            if _cls == "transient" and not _explicit_seconds:
                # Solo i cooldown DERIVATI vengono accorciati: un `seconds`
                # esplicito (Retry-After, soft anti-black-hole) e' un segnale
                # concreto del chiamante e vince.
                if reason == "timeout":
                    _sc = int(getattr(pol, "cooldown_timeout_sec", 60) or 60)
                else:
                    _sc = int(getattr(pol, "cooldown_transient_sec", 15) or 15)
                seconds = min(seconds, max(1.0, float(_sc)))
                log.info("[cooldown-class] %s classe=transient(%s) -> %.0fs", unique, reason or "5xx", seconds)
            elif _cls == "quota":
                log.info("[cooldown-class] %s classe=quota -> %.0fs (retry-after)", unique, seconds)
        # TIMEOUT: solo in modalita' storica (classi disattivate) vale il
        # moltiplicatore `timeout_cooldown_mult`; con le classi attive il
        # timeout ha gia' il suo cooldown breve dedicato (anti black-hole:
        # resta >0, ma non esplode a minuti).
        if reason == "timeout" and not getattr(pol, "error_class_cooldowns", True):
            _tm = max(1, int(getattr(pol, "timeout_cooldown_mult", 10) or 10))
            seconds = min(seconds * _tm, float(pol.max_cooldown_sec))
        # Leva B: un deployment CRONICO (fail_24h >= soglia) che fallisce
        # di nuovo va in pausa MINIMA longa (chronic_fail_cooldown_sec, 2h):
        # dopo essere stato "svegliato" dal paracadute e aver fallito, non
        # deve essere ritentato a breve. clear_cooldown su successo lo azzera.
        thr = max(1, int(getattr(pol, "cooldown_retry_max_fail_24h", 10) or 10))
        if s.fail_count_24h >= thr and kind != ErrorKind.QUOTA_RESET and not _is_quota:
            floor_cd = max(1.0, float(getattr(pol, "chronic_fail_cooldown_sec", 7200) or 7200))
            seconds = min(max(seconds, floor_cd), float(pol.max_cooldown_sec))
        if kind != ErrorKind.QUOTA_RESET:
            seconds = self._apply_jitter(seconds, unique)
        _now = time.time()
        if additive:
            # ESTENSIONE ADDITIVA: non sovrascrivere il residuo esistente ma
            # sommargli `seconds` (usato dall'autoprobe: un KO deve ALLUNGARE il
            # cooldown di grow/transient, non resettarlo a un valore secco).
            base = max(self._cooldown.get(unique, 0.0), _now)
            since = self._cooldown_since.get(unique)
            if since is None or since > _now:
                since = _now
            new_exp = base + seconds
            self._cooldown[unique] = new_exp
            self._cooldown_since[unique] = since
            self._cooldown_full_map()[unique] = float(new_exp - since)
        else:
            self._cooldown[unique] = _now + seconds
            self._cooldown_since[unique] = _now
            self._cooldown_full_map()[unique] = float(seconds)
        esc = ""
        if seconds is not None and s.fail_streak > 1 and getattr(pol, "cooldown_mode", "linear") == "exponential":
            esc = " escalation"
        _tag = "esteso di" if additive else "inattivo per"
        log.warning(
            "[cooldown] %s %s %ds%s (streak=%d, fail_24h=%d)",
            unique,
            _tag,
            int(seconds),
            esc,
            s.fail_streak,
            s.fail_count_24h,
        )

        # F7: il 429 e' quasi sempre un fatto di CHIAVE/account, non del
        # singolo deployment: bloccare soft (skip a pick, zero strike/zero
        # cooldown) TUTTE le twin sulla stessa api_key per il Retry-After.
        # F18: vale anche per 'quota_exhausted' (limite giornaliero/mensile
        # della chiave: stesso effetto, stessa gestione).
        if reason in ("http_429", "quota_exhausted") and getattr(pol, "key_soft_429_enabled", True):
            _d429 = self.config.deployment_by_unique(unique)
            _k429 = (_d429 or {}).get("api_key")
            if isinstance(_k429, str) and _k429:
                _tag429 = hashlib.sha256(_k429.encode("utf-8", errors="replace")).hexdigest()[:12]
                _cap = max(10.0, float(getattr(pol, "key_soft_max_sec", 900) or 900))
                # jitter DETERMINISTICO (F19): le twin non devono ripartire
                # tutte nel medesimo millisecondo al termine del Retry-After.
                _until = _now + min(float(seconds), _cap) + self._jitter_spread(_tag429)
                _sd = getattr(self, "_key_soft", None)
                if _sd is None:
                    _sd = {}
                    self._key_soft = _sd
                if float(_sd.get(_tag429, 0.0)) < _until:
                    _sd[_tag429] = _until
                    log.info(
                        "[key-soft] chiave %s*: skip soft %ds (429 su %s)",
                        _tag429[:6],
                        int(min(float(seconds), _cap)),
                        unique,
                    )

        # --- Circuit Breaker (hybrid: dep sempre, key solo errori di chiave) ---
        self._update_circuit_breaker_on_failure(unique, key_level=self._is_key_level_failure(status, reason))
        # F25: 5xx sistematici su chiavi diverse -> breaker di MODELLO
        self._note_model_failure(self._dep_of(unique), status)

        return seconds

    # ------------------------------- soft skip PER-CHIAVE (F6/F7, zero colpa)
    def _key_tag(self, unique: str) -> str | None:
        """Tag stabile (hash, mai la chiave in chiaro) della api_key del
        deployment; None se ignota."""
        dep = self.config.deployment_by_unique(unique)
        if not dep:
            return None
        k = dep.get("api_key")
        if not isinstance(k, str) or not k:
            return None
        return hashlib.sha256(k.encode("utf-8", errors="replace")).hexdigest()[:12]

    def _key_fault_blocked(self, unique: str) -> bool:
        """Vero se la CHIAVE del deployment e' momentaneamente sazia:
        - F7: 429 appena preso -> blocco soft per tutto il Retry-After
          (tetto key_soft_max_sec) sull'INTERA chiave: le twin sulla stessa
          account rifarebbero solo un altro 429;
        - F6: header X-RateLimit-Requests-remaining fresco (ttl) e basso.
        Nessuna punizione: niente cooldown reale, niente strike, niente
        reputazione. Vale SOLO sul free-world (gruppi dims/cap): i bucket
        pagati -go/-fallback sono la RISERVA a cui si chiede comunque il
        possibile. Altra sessione, stessa chiave: bloccata uguale (e' verita'
        del provider, non della sessione)."""
        if not getattr(self.policy, "rate_hint_skip_enabled", True):
            return False
        dep = self.config.deployment_by_unique(unique)
        if not dep:
            return False
        g = dep.get("group", "")
        try:
            if self.config.group_caps.get(g) is not None or self._is_renewal_bucket(g):
                return False
        except Exception:  # noqa: BLE001
            return False
        k = dep.get("api_key")
        if not isinstance(k, str) or not k:
            return False
        tag = hashlib.sha256(k.encode("utf-8", errors="replace")).hexdigest()[:12]
        now = time.time()
        if not getattr(self.policy, "key_soft_429_enabled", True):
            soft = {}
        else:
            soft = getattr(self, "_key_soft", None) or {}
        if float(soft.get(tag, 0.0)) > now:
            return True
        if not getattr(self.policy, "rate_hint_skip_enabled", True):
            return False
        rec = (getattr(self, "_key_hints", None) or {}).get(tag)
        if not rec:
            return False
        ts, rem = rec
        ttl = max(1.0, policy_float(self.policy, "rate_hint_ttl_sec", 20.0))
        if now - ts > ttl:
            return False
        cap = policy_int(self.policy, "rate_hint_remaining_max", 5)
        return rem <= cap

    def note_rate_limit(self, unique: str, rl: dict) -> None:
        """Hint quota dagli header X-RateLimit-*: se le richieste rimanenti
        sono poche (<= soglia), abbassa il cap appreso PRIMA che scatti il 429.
        Il budget guard dosera' lo scoring (deprioritizzazione) senza bisogno
        di un cooldown. Solo-riduzione: il cap puo' solo scendere con gli hint,
        poi si ri-apprende dai 429 reali."""
        # F6: snapshot SEMPRE (indipendente dal budget guard): alimenta il
        # soft skip per-chiave di durata ttl, senza alcuna punizione.
        if getattr(self.policy, "rate_hint_skip_enabled", True):
            try:
                _rem = float((rl or {}).get("requests_remaining"))
            except (TypeError, ValueError):
                _rem = -1.0
            if _rem >= 0:
                tag = self._key_tag(unique)
                if tag:
                    d = getattr(self, "_key_hints", None)
                    if d is None:
                        d = {}
                        self._key_hints = d
                    d[tag] = (time.time(), _rem)
                    if len(d) > 8192:
                        _now = time.time()
                        for k in [k for k, (t, _r) in d.items() if _now - t > 3600]:
                            d.pop(k, None)
        bg = self.policy.budget_guard or {}
        if not bg.get("enabled"):
            return
        rl = rl or {}
        try:
            remaining = float(rl.get("requests_remaining") or -1)
        except (TypeError, ValueError):
            return
        if remaining < 0:
            return
        thr = max(1, int(bg.get("rate_hint_threshold", 3) or 3))
        if remaining > thr:
            return
        s = self.stats_for(unique)
        new_cap = max(1.0, remaining + 1.0)
        if s.min_cap_learned <= 0 or new_cap < s.min_cap_learned:
            s.min_cap_learned = new_cap
            log.info(
                "[budget] %s: hint rate-limit (remaining=%.0f<=%d) -> cap ~%.0f/min", unique, remaining, thr, new_cap
            )

    def _retire_permanent(self, unique: str, reason: str) -> None:
        """Ritira un deployment permanentemente rotto (chiave morta, modello
        rimosso). NON tocca il CSV: usa il lifecycle keyhealth (retired)."""
        try:
            from .. import main as _gw_mod  # lazy: evita cicli import

            kh = getattr(_gw_mod, "KEYHEALTH", None)
            if kh is None:
                return
            kh.set_state(unique, "retired", reason=reason)
            kh.save()
            log.warning("[lifecycle] %s RETIRED (permanent: %s)", unique, reason)
        except Exception:  # noqa: BLE001
            log.warning("[lifecycle] retire %s fallito", unique, exc_info=True)

    def escalate_cooldown(self, base_seconds: float, fail_count_24h: int) -> float:
        """Escalation DOLCE del cooldown per fallimenti ricorrenti (24h).

        - primo fallimento (~1): ritorna `base_seconds` invariato;
        - fallimenti successivi: `base + 10%` del cooldown "potente" lineare
          (cooldown_base_min + cooldown_linear_mult_min*(fail_24h-1) minuti,
          cap max_cooldown_sec), così una chiave che fallisce in continuazione
          (es. 18 volte/24h) viene esclusa per minuti/ore invece di essere
          riesumata ogni 2 minuti.

        Usata dai fallimenti SOFT/transitori (stream vuoto, 429, body vuoto):
        non per gli errori duri (auth/modello assente) che hanno già cooldown
        propri. `clear_cooldown` su successo resetta fail_count_24h, quindi
        un deployment "svegliato" dalla catena e che risponde riparte dal
        base appena riabilitato.
        """
        n = max(0, int(fail_count_24h or 0))
        if n <= 1:
            return float(base_seconds)
        base_m = max(1, policy_int(self.policy, "cooldown_base_min", 30))
        mult_m = max(0, policy_int(self.policy, "cooldown_linear_mult_min", 30))
        potent = (base_m + mult_m * max(0, n - 1)) * 60.0
        potent = min(potent, policy_float(self.policy, "max_cooldown_sec", 18000))
        return min(float(base_seconds) + 0.1 * potent, policy_float(self.policy, "max_cooldown_sec", 18000))

    def mark_failed_double_residual(self, unique: str, reason: str | None = None, status: int | None = None) -> float:
        """Raddoppia il cooldown residuo quando un deployment dormiente fallisce
        di nuovo. Usato al posto di mark_failed per i retry di deployment
        in cooldown (stale/ultima spiaggia)."""
        # [Blocco 1] Aggiorna reputation scoring
        self.record_failure(unique, reason, status)

        now = time.time()
        remaining = max(1.0, self._cooldown.get(unique, now) - now)
        new_cd = remaining * 2.0
        new_cd = min(new_cd, float(self.policy.max_cooldown_sec))
        if reason == "timeout":
            _tm = max(1, policy_int(self.policy, "timeout_cooldown_mult", 10))
            new_cd = min(new_cd * _tm, float(self.policy.max_cooldown_sec))
        s = self.stats_for(unique)
        # ESC-PIN: idem mark_failed (il winner fallito si sblocca).
        for _g, (_u, _ts) in list(self._esc().items()):
            if _u == unique:
                self._esc().pop(_g, None)
        if reason:
            s.last_reason = reason
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if s.fail_day_key != today:
            s.fail_count_24h = 0
            s.fail_day_key = today
        s.fail_count_24h += 1
        s.fail_count += 1
        s.fail_streak = self._decay_streak(s.fail_streak, s.last_fail_ts, now)
        s.last_fail_ts = now
        s.fail_streak += 1
        # Un 429/quota NON e' "chiave rotta" ma "chiave satura": non deve
        # contare verso il ritiro automatico, altrimenti una free-key con
        # quota giornaliera bassa verrebbe parcheggiata per sempre solo
        # perche' saturata oggi (stessa regola di KeyHealth.observe, F30).
        if not _is_quota_evidence(reason, status):
            s.probe_fail_streak += 1
        prev = 1.0 if s.success_ema is None else s.success_ema
        s.success_ema = max(0.0, 0.8 * prev)
        # Leva B: stessa pausa minima longa (2h) se il cronico fallisce
        # di nuovo anche da dormiente (mark_failed_double_residual usa già
        # il raddoppio del residuo; qui garantiamo almeno il floor).
        thr = max(1, policy_int(self.policy, "cooldown_retry_max_fail_24h", 10))
        if s.fail_count_24h >= thr:
            floor_cd = max(1.0, policy_float(self.policy, "chronic_fail_cooldown_sec", 7200))
            new_cd = min(max(new_cd, floor_cd), float(self.policy.max_cooldown_sec))
        new_cd = self._apply_jitter(new_cd, unique)
        self._cooldown[unique] = now + new_cd
        self._cooldown_since[unique] = now
        self._cooldown_full_map()[unique] = float(new_cd)
        log.warning(
            "[cooldown] %s dormiente ri-fallito -> cooldown raddoppiato a %ds (residuo era %ds, fail_24h=%d)",
            unique,
            int(new_cd),
            int(remaining),
            s.fail_count_24h,
        )
        self._maybe_retire_on_probe_fail(unique, s)
        return new_cd

    def clear_cooldown(self, unique: str) -> None:
        """Rimuove cooldown e azzera contatori fail per un deployment che ha
        risposto con successo dopo essere stato in cooldown."""
        self._cooldown.pop(unique, None)
        self._cooldown_since.pop(unique, None)
        self._cooldown_full_map().pop(unique, None)
        self._cooldown_prov().pop(unique, None)
        s = self.stats_for(unique)
        s.fail_streak = 0
        s.fail_count_24h = 0
        s.fail_day_key = ""
        s.probe_fail_streak = 0
        log.info("[cooldown] %s riabilitato (successo da dormiente)", unique)

        # --- Circuit Breaker: success updates ---
        self._update_circuit_breaker_on_success(unique)
