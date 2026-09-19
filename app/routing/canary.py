"""Canary/hedge mixin (estratto verbatim da router.py, refactor Phase 4).

[IT] Warm-fill/warm-wake canary e hedge canaries: probe speculative FREE-only
usate per riscaldare o risvegliare candidati e coprire upstream lenti. Codice
spostato senza modifiche di comportamento.
[EN] Canary/hedge mixin: free-only speculative probes (warm fill/wake, hedges).
Verbatim move, no behaviour change. See docs/ROUTING.md.
"""

from __future__ import annotations

import logging
import math
import time

from ..config import ORDER_LAST
from ..opencode_gate import (dep_usable as _dep_usable,
                             is_opencode_zen_dep,
                             opencode_cautious_request)
from ..session_ctx import current_session

log = logging.getLogger("nx.router")


class CanaryMixin:
    def _warm_allow_slow(self) -> bool:
        return bool(getattr(self.policy, "warm_pool_allow_slow", True))

    def hedge_delay_ms(self, unique: str, ctx_est=None) -> int:
        """Ritardo del canary HEDGE calibrato sul bucket di contesto.

        Su contesti grandi il TTFT fisiologico e' di secondi: lanciare il
        canary a un valore fisso (1500ms) e' rumore — si pagherebbe un
        tentativo in piu' quasi su ogni heavy. Formula:
            clamp(TTFT_bucket * frac, min_ms, max_ms)
        Senza stima TTFT vale il valore fisso `stream_hedge_delay_ms`
        (0 = hedge spento del tutto)."""
        qcp = getattr(self.policy, "qc_json", None)
        base = int(getattr(qcp, "stream_hedge_delay_ms", 0) or 0)
        if base <= 0:
            return 0
        try:
            frac = float(getattr(qcp, "stream_hedge_ttft_frac", 0.6) or 0.6)
            lo = int(getattr(qcp, "stream_hedge_min_ms", 800) or 800)
            hi = int(getattr(qcp, "stream_hedge_max_ms", 2500) or 2500)
        except (TypeError, ValueError):
            frac, lo, hi = 0.6, 800, 2500
        if hi < lo:
            hi = lo
        ttft = float(self.bucket_latency_ms(unique, ctx_est,
                                           kind="ttft") or 0.0)
        if ttft <= 0:
            return base
        return max(lo, min(hi, int(ttft * frac)))

    def hedge_canaries(self, profile: str | None, dep: dict,
                       need: frozenset[str] | None, ctx: int | None,
                       tried: set[str] | None, requested_group: str | None,
                       k: int = 2, exclude: set[str] | None = None,
                       fresh_only: bool = False,
                       out_tokens: int | None = None) -> list[dict]:
        """Fino a `k` candidati NUOVI per la gara sul primo contenuto.

        Regole: mai A ne' i gia' provati; mai bucket pagati (-go/-fallback);
        mai ritirati permanenti; mai sotto il floor di dim richiesto dal
        client; mai dep noti non-streaming. Tier CRESCENTI: prima i tier
        diversi da quello di A, poi (se serve) quello di A. Con
        `fresh_only=True` (eletto warm lento) si esclude TUTTO il warm della
        sessione (vogliamo candidati nuovi) e si preferiscono i MENO USATI
        nelle 24h. Con `out_tokens` si richiede anche la capacita' REALE di
        consegnare l'output (altrimenti un canary puo' nascere su un dep che
        sta nel contesto ma non ha spazio per rispondere)."""
        k = max(1, int(k))
        a_u = dep.get("unique")
        ex = set(tried or ())
        if a_u:
            ex.add(a_u)
        for u in (exclude or ()):
            if u:
                ex.add(u)
        if fresh_only:
            try:
                ex |= set(self._sess_deps().get(current_session(), ()))
            except Exception:                  # noqa: BLE001
                pass
        floor = 0
        if requested_group:
            try:
                floor = int(self._group_min_dim(requested_group) or 0)
            except Exception:                  # noqa: BLE001
                floor = 0
        a_group = dep.get("group")
        ladder = self.config.chains.get(profile or "", []) or []
        tiers: list[tuple[int, str, list[str]]] = []
        cur_g = None
        cur: list[str] = []
        cur_mxi = 0
        for u in ladder:
            d = self.config.deployment_by_unique(u)
            if not d:
                continue
            if not _dep_usable(d):
                continue                       # upstream non usabile dal client
            g = str(d.get("group") or "")
            if not self._free_group(g):
                continue                       # solo dim gratis
            if self._endpoint_quarantined(d):
                continue                       # ban/ToS: host in quarantena
            if self.is_retired(u):
                if not self._retired_usable(u):
                    continue
                continue                       # i ritirati non sono canary
            mxi = int(d.get("max_input_tokens") or 0)
            if floor and mxi and mxi < floor * 1000:
                continue                       # mai sotto la dim richiesta
            if self._nonstream_blocked(u):
                continue
            if g != cur_g:
                if cur:
                    tiers.append((cur_mxi, cur_g, cur))
                cur_g, cur, cur_mxi = g, [], mxi
            cur.append(u)
        if cur:
            tiers.append((cur_mxi, cur_g, cur))
        tiers.sort(key=lambda t: (t[1] == a_group, t[0]))
        # ORDINE ANTI-RAFFICA: tutti i candidati eleggibili vengono ordinati
        # col round-robin provider x slot-chiave (`_canary_sweep`) e POI
        # validati uno a uno con `_walk_chain` (che mantiene i filtri reali).
        _all: list[str] = []
        for _mxi, _g, us in tiers:
            _all.extend(us)
        _cands: list[dict] = []
        _seen: set[str] = set()
        for u in _all:
            if u in _seen:
                continue
            _seen.add(u)
            d = self.config.deployment_by_unique(u)
            if d is not None:
                _cands.append(d)
        out: list[dict] = []
        taken: set[str] = set()
        for d in self._canary_sweep(_cands, self._tiers_of(ex)):
            if len(out) >= k:
                break
            got = self._walk_chain([d["unique"]], None, need, ctx,
                                   tried=ex, out_tokens=out_tokens)
            if got is not None and got["unique"] not in taken:
                taken.add(got["unique"])
                ex.add(got["unique"])
                out.append(got)
        return out[:k]

    def _group_dim_order_key(self, group: str) -> tuple[int, int]:
        """Chiave di ordinamento DIM-MAJOR (regola utente): prima TUTTE le
        -dim, dalla piu' bassa (quella richiesta) in su, poi i gruppi non-dim.
        Serve a far SCAVARE al canary/sveglia la -dim richiesta fino
        all'esaurimento prima di salire a quella superiore."""
        m = self.DIM_SUFFIX_RE.search(str(group or ""))
        if not m:
            return (1, 0)
        return (0, int(m.group(1)))

    def _canary_cold_pick(self, cands: list[dict], ctx: int | None,
                          sampled_tiers: set[int] | None = None
                          ) -> dict | None:
        """Scelta del candidato canary/sveglia "come una chiamata a freddo"
        (regola utente), con ROUND-ROBIN SUI TIER `order`:

        - si parte sempre dal tier `order` MINIMO NON ancora sondato in questo
          giro (1 probe per tier, in ordine crescente);
        - quando tutti i tier sono stati sondati si RICICLA dal piu' basso
          (le chiavi gia' provate restano escluse a monte, quindi si prende
          una chiave diversa);
        - dentro il tier si applica il cold spread (nasconde il 20% piu'
          usato) e il reputation scoring adattivo (fallback legacy: priority
          + model_preference).

        Cosi' la cascata scopre in pochi probe QUALE tier e' vivo invece di
        bruciare tutti i tentativi su un tier morto."""
        if not cands:
            return None
        _kept = self._spread_hide(cands)
        if _kept:
            cands = _kept
        _by_tier: dict[int, list[dict]] = {}
        for d in cands:
            try:
                _t = self._eff_order(d)
            except Exception:                          # noqa: BLE001
                _t = ORDER_LAST
            _by_tier.setdefault(_t, []).append(d)
        _tiers = sorted(_by_tier)
        _sampled = {int(t) for t in (sampled_tiers or ())}
        _fresh = [t for t in _tiers if t not in _sampled]
        _t = _fresh[0] if _fresh else _tiers[0]
        _tier = _by_tier.get(_t) or cands
        try:
            if getattr(self.policy, "adaptive_pick", True):
                return min(_tier, key=lambda d: (
                    self._reputation_score(d["unique"], d, ctx),
                    self.usage_weight_24h(d["unique"])))
            return min(_tier, key=lambda d: (
                int(d.get("priority", 0) or 0),
                -int(d.get("model_preference", 0) or 0),
                self.usage_weight_24h(d["unique"])))
        except Exception:                              # noqa: BLE001
            return _tier[0]

    def _sweep_provider_key(self, cands: list[dict],
                            sampled_tiers: set[int] | None = None
                            ) -> list[dict]:
        """Round-robin PROVIDER x CHIAVE (regola utente): in ogni giro si
        prende il candidato MIGLIORE di OGNI provider (in ordine stabile per
        dim-fit) usando una chiave del provider NON ancora usata nel giro, poi
        si passa al giro successivo (prov1/chiave2, prov2/chiave2, ...).
        Dentro il provider l'ordine e' dim-fit > tier `order` > reputation >
        uso 24h, cosi' si SCAVA la -dim richiesta prima di salire e si alternano
        le chiavi (mai martellare la stessa). INDIPENDENTE dal modello."""
        if not cands:
            return []
        by_prov: dict[str, list[dict]] = {}
        p_fit: dict[str, tuple] = {}
        for d in cands:
            p = str(d.get("provider") or "")
            by_prov.setdefault(p, []).append(d)
            try:
                _fit = self._group_dim_order_key(str(d.get("group") or ""))
            except Exception:                          # noqa: BLE001
                _fit = (1, 0)
            _cur = p_fit.get(p)
            if _cur is None or _fit < _cur:
                p_fit[p] = _fit
        provs = sorted(by_prov, key=lambda p: (p_fit.get(p, (1, 0)), p))

        _sam = {int(t) for t in (sampled_tiers or ())}

        def _key(d: dict):
            try:
                return (self._prov_avoid_key(d),
                        self._group_dim_order_key(str(d.get("group") or "")),
                        1 if self._eff_order(d) in _sam else 0,
                        self._eff_order(d),
                        self._reputation_score(d["unique"], d, None),
                        self.usage_weight_24h(d["unique"]))
            except Exception:                          # noqa: BLE001
                return ((0, 0, 0), (1, 0), 1, ORDER_LAST, 0.0, 0.0)

        queues = {p: sorted(by_prov[p], key=_key) for p in provs}
        used_keys: dict[str, set] = {p: set() for p in provs}
        remaining = sum(len(q) for q in queues.values())
        out: list[dict] = []
        while remaining > 0:
            progressed = False
            for p in provs:
                q = queues[p]
                if not q:
                    continue
                pick = None
                for d in q:                            # prima chiave libera
                    k = str(d.get("api_key") or "")
                    if k not in used_keys[p]:
                        pick = d
                        break
                if pick is None:                       # tutte le chiavi usate
                    pick = q[0]
                used_keys[p].add(str(pick.get("api_key") or ""))
                q.remove(pick)
                out.append(pick)
                remaining -= 1
                progressed = True
            if not progressed:                         # noqa: SIM103
                break
        return out

    def _canary_sweep(self, cands: list[dict],
                      sampled_tiers: set[int] | None = None) -> list[dict]:
        """Ordine ANTI-RAFFICA per la RICERCA dei canary (imbuto):
          1) esclude i provider GIA' IN USO (warm/probe/inflight, ogni sessione);
          2) esclude le singole api_key GIA' IN USO e i dep gia' in warm di
             QUALSIASI sessione;
          3) round-robin provider x slot-chiave su cio' che avanza;
          4) FASCIA 2: se la fascia pulita e' vuota, riammette i provider in
             uso ma SOLO con chiavi diverse da quelle in uso (mai la stessa).
        I candidati arrivano gia' filtrati (FREE/cap/ctx/need/deliverable)."""
        if not cands:
            return []
        if not getattr(self.policy, "canary_provider_sweep_enabled", True):
            return list(cands)
        # `canary_warm_last=False` = legacy: nessuna separazione per provider
        # (resta solo l'esclusione delle chiavi in uso / dep in warm).
        if getattr(self.policy, "canary_warm_last", True):
            p_used = self._warm_providers()
        else:
            p_used = set()
        k_used = self._in_use_keys()
        clean: list[dict] = []
        tail: list[dict] = []
        for d in cands:
            u = d.get("unique")
            if not u or self._owned_by_any_session(u):
                continue                               # gia' in warm: mai
            k = str(d.get("api_key") or "")
            if k and k in k_used:
                continue                               # chiave in uso: mai
            p = str(d.get("provider") or "")
            (tail if p in p_used else clean).append(d)
        return (self._sweep_provider_key(clean, sampled_tiers)
                + self._sweep_provider_key(tail, sampled_tiers))

    def warm_fill_canary(self, profile: str | None, cur_dep: dict,
                         need: frozenset[str] | None, ctx: int | None,
                         out_tokens: int | None,
                         tried: set[str] | None = None,
                         requested_group: str | None = None,
                         exclude_keys: set[str] | None = None,
                         exclude_uniq: set[str] | None = None,
                         only_zen: bool = False,
                         sampled_tiers: set[int] | None = None) -> dict | None:
        """UN candidato probe per il warm-refill: percorre il ladder -dim
        ASCENDENTE partendo dal gruppo corrente (se nel -dim corrente non c'e'
        niente di buono si sale al -dim superiore) fermandosi ai FREE: i bucket
        -go/-fallback NON sono mai candidati. Esclusioni: api_key di dep gia'
        in warm, dep assegnati a UNA qualsivoglia sessione (owner vivo), dep
        gia' tentati/sondati in QUESTA richiesta, dep che non possono
        effettivamente consegnare (need + ctx + output assicurato).
        Nel -dim corrente la scelta e' "come a freddo" (_canary_cold_pick:
        cold-spread dei piu' usati + tier order + reputation): si SCAVA il
        -dim (gli tentativi successivi escludono i gia' provati) e solo a
        esaurimento si sale al -dim superiore.
        Ritorna il miglior candidato del primo -dim utile; None = esauriti."""
        if opencode_cautious_request():
            return None
        cur = cur_dep.get("unique")
        ex: set[str] = set(tried or ())
        if cur:
            ex.add(cur)
        for u in (exclude_uniq or ()):
            if u:
                ex.add(u)
        keys = {str(k) for k in (exclude_keys or ()) if k}
        floor = 0
        if requested_group:
            try:
                floor = int(self._group_min_dim(requested_group) or 0)
            except Exception:                          # noqa: BLE001
                floor = 0
        go_suf = self.config.go_suffix or "-go"
        fb_suf = self.config.fallback_suffix or "-fallback"
        # "SCAVA IL -DIM" (regola utente): si raccolgono TUTTI i candidati
        # liberi del -dim corrente, si sceglie il migliore "come a freddo"
        # e solo quando quel -dim e' esaurito si sale al successivo. Il ladder
        # resta ASCENDENTE dal gruppo corrente; i bucket -go/-fallback non
        # sono mai candidati (FREE only).
        _by: dict[str, list[dict]] = {}
        _by_tail: dict[str, list[dict]] = {}
        _order: list[str] = []
        # CODA PROVIDER (regola utente): le chiavi gia' in warm restano
        # ESCLUSE (filtro `keys`, sempre). Un candidato pero' il cui PROVIDER
        # e' gia' IN USO (warm, probe in volo o chiamata in corso, di UNA
        # QUALSIASI sessione) non va scartato: va in CODA, dopo TUTTI i
        # provider non ancora in uso, cosi' si sfruttano tutti i provider
        # senza martellare gli stessi (e senza cache-hit "sospette" su una
        # chiave nuova di un provider che ha gia' visto lo stesso contenuto).
        _last = bool(getattr(self.policy, "canary_warm_last", True))
        _wprov = self._warm_providers() if _last else set()
        # FISSATO (regola utente "scava il -dim ESPPLICITO"): il ladder parte
        # dalla dim RICHIESTA, non dal gruppo della holder (che puo' essere
        # piu' alta: una sessione ancorata a -1000k non vedrebbe mai le free
        # -200k). Se la dim richiesta non da' ladder, ripiega su quello corrente.
        _lad = self._ladder_for_group(requested_group
                                      or cur_dep.get("group") or "")
        if not _lad:
            _lad = self._ladder_for_group(cur_dep.get("group") or "")
        for u in _lad:
            if u in ex:
                continue
            d = self.config.deployment_by_unique(u)
            if not d:
                continue
            g = str(d.get("group") or "")
            if g.endswith(go_suf) or g.endswith(fb_suf):
                continue                               # FREE only: qui si ferma
            if self.is_retired(u) or self.is_draining(u):
                continue
            if self._endpoint_quarantined(d):
                continue
            if self.is_cooled_down(u) or self._gemini_blocked(d):
                continue
            if self._nonstream_blocked(u):
                continue
            if self._owned_by_any_session(u):
                continue                               # occupato da qualcuno
            if not _dep_usable(d):
                continue                               # upstream non usabile
            if only_zen and not is_opencode_zen_dep(d):
                continue                 # caccia zen-only (nativo opencode)
            mxi = int(d.get("max_input_tokens") or 0)
            if floor and mxi and mxi < floor * 1000:
                continue
            if not self.dep_deliverable(d, need, ctx, out_tokens):
                continue
            k = str(d.get("api_key") or "")
            if k and k in keys:
                continue                               # chiave gia' in warm
            _tail = _last and str(d.get("provider") or "") in _wprov
            _tgt = _by_tail if _tail else _by
            if g not in _by and g not in _by_tail:
                _order.append(g)
            _tgt.setdefault(g, []).append(d)
        _sam0 = {int(t) for t in (sampled_tiers or ())}
        # DIM-MAJOR (regola utente): si SCAVA la -dim richiesta fino
        # all'esaurimento e solo dopo si sale alla successiva; dentro la -dim
        # la scelta resta "come a freddo" (tier round-robin + spread dei piu'
        # usati). L'ordine del ladder (tier-major) non decide piu' il salto di
        # dimensione: senza questo, una -dim piu' profonda con `order` basso
        # veniva sondata prima di finire quella richiesta.
        _order.sort(key=self._group_dim_order_key)
        # ORDINE ANTI-RAFFICA: tutti i candidati eleggibili (provider nuovi +
        # coda provider-in-uso) vengono ordinati col round-robin provider x
        # slot-chiave (`_canary_sweep`) e si prende il PRIMO.
        _cands: list[dict] = []
        for g in _order:
            _cands.extend(_by.get(g) or [])
        for g in _order:
            _cands.extend(_by_tail.get(g) or [])
        if not _cands:
            return None
        _ord = self._canary_sweep(_cands, _sam0 | self._tiers_of(ex))
        return _ord[0] if _ord else None

    def session_api_keys(self) -> set[str]:
        """api_key dei dep posseduti da UNA QUALSIASI sessione viva.
        Regola utente: la SVEglia deve provare chiavi DIVERSE da tutte le
        sessioni (mai rubare/riprovare la chiave di qualcun altro)."""
        out: set[str] = set()
        now = time.time()
        try:
            guard = self._guard_sec()
        except Exception:                              # noqa: BLE001
            guard = 900.0
        for u, ent in list(self._dep_sess().items()):
            if not ent:
                continue
            try:
                if (now - float(ent[1])) >= guard:
                    continue
            except Exception:                          # noqa: BLE001
                continue
            d = self.config.deployment_by_unique(u)
            if d and d.get("api_key"):
                out.add(str(d["api_key"]))
        return out

    def _warm_providers(self) -> set[str]:
        """Provider (colonna `provider`) attualmente IN USO da UNA QUALSIASI
        sessione (warm, probe in volo, chiamata in corso). Serve all'anti-
        raffica: un provider gia' in uso non va martellato con un'altra chiave."""
        out: set[str] = set()
        for u in self._in_use_deps():
            d = self.config.deployment_by_unique(u)
            if d and d.get("provider"):
                out.add(str(d["provider"]))
        return out

    def _in_use_keys(self) -> set[str]:
        """api_key "IN USO" da UNA QUALSIASI sessione (warm vivo, probe in
        volo, chiamata in corso): il canary non ricicla una chiave gia'
        impegnata, nemmeno quando riammette in coda un provider in uso."""
        out: set[str] = set()
        for u in self._in_use_deps():
            d = self.config.deployment_by_unique(u)
            if d and d.get("api_key"):
                out.add(str(d["api_key"]))
        return out

    def warm_wake_canary(self, profile: str | None, cur_dep: dict,
                         need: frozenset[str] | None, ctx: int | None,
                         out_tokens: int | None,
                         tried: set[str] | None = None,
                         requested_group: str | None = None,
                         exclude_keys: set[str] | None = None,
                         exclude_uniq: set[str] | None = None,
                         only_zen: bool = False,
                         min_age_sec: float = 3600.0,
                         sampled_tiers: set[int] | None = None) -> dict | None:
        """TERZO canario del warm-refill: non cerca un dep fresco ma un
        DORMIENTE da almeno `min_age_sec` (default 1h) messo in cooldown da
        un 429/quota — un vero e proprio SVEglia. Se risponde, il probe lo
        riporta caldo (clear_cooldown + warm owner): cosi' la capacita' che
        era stata messa in pausa torna utile senza aspettare l'autoprobe.
        Percorre lo stesso ladder -dim del refill (free only) con le stesse
        esclusioni, scegliendo "come a freddo" nello stesso modo; in piu' NON
        tocca i dep che non hanno un cooldown 429 maturo."""
        if opencode_cautious_request():
            return None
        now = time.time()
        cur = cur_dep.get("unique")
        ex: set[str] = set(tried or ())
        if cur:
            ex.add(cur)
        for u in (exclude_uniq or ()):
            if u:
                ex.add(u)
        keys = {str(k) for k in (exclude_keys or ()) if k}
        # Chiave DIVERSA da TUTTE LE SESSIONI (regola utente): la Sveglia non
        # tocca mai una api_key gia' impegnata da sessione alcuna.
        keys |= self.session_api_keys()
        floor = 0
        if requested_group:
            try:
                floor = int(self._group_min_dim(requested_group) or 0)
            except Exception:                          # noqa: BLE001
                floor = 0
        go_suf = self.config.go_suffix or "-go"
        fb_suf = self.config.fallback_suffix or "-fallback"
        _by: dict[str, list[dict]] = {}
        _by_tail: dict[str, list[dict]] = {}
        _order: list[str] = []
        # CODA PROVIDER (regola utente): le chiavi gia' in warm restano
        # ESCLUSE (filtro `keys`, sempre, incluse quelle di TUTTE le sessioni).
        # Un dormiente pero' il cui PROVIDER e' gia' IN USO (warm, probe in
        # volo o chiamata in corso, di UNA QUALSIASI sessione) va in CODA,
        # dopo TUTTI i provider non ancora in uso.
        _last = bool(getattr(self.policy, "canary_warm_last", True))
        _wprov = self._warm_providers() if _last else set()
        # FISSATO: anche la Sveglia scava dalla dim RICHIESTA (vedi refill).
        _lad = self._ladder_for_group(requested_group
                                      or cur_dep.get("group") or "")
        if not _lad:
            _lad = self._ladder_for_group(cur_dep.get("group") or "")
        for u in _lad:
            if u in ex:
                continue
            d = self.config.deployment_by_unique(u)
            if not d:
                continue
            g = str(d.get("group") or "")
            if g.endswith(go_suf) or g.endswith(fb_suf):
                continue                               # FREE only
            if not self.is_cooled_down(u):
                continue                               # cerchiamo dormienti
            since = float(self._cooldown_since.get(u) or 0.0)
            if since and (now - since) < max(0.0, float(min_age_sec)):
                continue                # troppo fresco: non e' un 429 maturo
            try:
                _reason = str(getattr(self.stats_for(u), "last_reason", "")
                              or "")
            except Exception:                          # noqa: BLE001
                _reason = ""
            if _reason not in ("http_429", "quota_exhausted",
                               "quota_exhausted_account"):
                continue             # solo 429/quota: mai svegliare un 403/ban
            # PROVENIENZA (P0): si sveglia SOLO un cooldown 'heuristic' (nostra
            # stima). Se il provider ha DICHIARATO quando torna (Retry-After /
            # reset quota = 'authoritative'), o e' credito/tier, ritentare
            # prima scadenza e' solo rumore e rischio ban.
            if not self.cooldown_probeable(u):
                continue
            if self.is_retired(u) or self.is_draining(u):
                continue
            if self._endpoint_quarantined(d) or self._gemini_blocked(d):
                continue
            if self._nonstream_blocked(u):
                continue
            if self._owned_by_any_session(u):
                continue
            if not _dep_usable(d):
                continue                               # upstream non usabile
            if only_zen and not is_opencode_zen_dep(d):
                continue                 # caccia zen-only (nativo opencode)
            mxi = int(d.get("max_input_tokens") or 0)
            if floor and mxi and mxi < floor * 1000:
                continue
            if not self.dep_deliverable(d, need, ctx, out_tokens):
                continue
            k = str(d.get("api_key") or "")
            if k and k in keys:
                continue
            _tail = _last and str(d.get("provider") or "") in _wprov
            _tgt = _by_tail if _tail else _by
            if g not in _by and g not in _by_tail:
                _order.append(g)
            _tgt.setdefault(g, []).append(d)
        _sam0 = {int(t) for t in (sampled_tiers or ())}
        # DIM-MAJOR (regola utente): si SCAVA la -dim richiesta fino
        # all'esaurimento e solo dopo si sale alla successiva; dentro la -dim
        # la scelta resta "come a freddo" (tier round-robin + spread dei piu'
        # usati). L'ordine del ladder (tier-major) non decide piu' il salto di
        # dimensione: senza questo, una -dim piu' profonda con `order` basso
        # veniva sondata prima di finire quella richiesta.
        _order.sort(key=self._group_dim_order_key)
        # ORDINE ANTI-RAFFICA (come il refill): sweep provider x slot-chiave.
        _cands: list[dict] = []
        for g in _order:
            _cands.extend(_by.get(g) or [])
        for g in _order:
            _cands.extend(_by_tail.get(g) or [])
        if not _cands:
            return None
        _ord = self._canary_sweep(_cands, _sam0 | self._tiers_of(ex))
        return _ord[0] if _ord else None
