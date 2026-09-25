"""Sessions mixin (estratto verbatim da router.py, refactor Phase 4).

[IT] Sessioni sticky, holder di deployment/gruppo, helper cache-aware e note di
attivita'/richiesta per sessione. Codice spostato senza modifiche.
[EN] Sticky sessions, deployment/group holders, cache-aware helpers and
per-session activity/request notes. Verbatim move. See docs/ROUTING.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time

from ..opencode_gate import (dep_usable as _dep_usable,
                             opencode_cautious_request)
from ..session_ctx import current_session

log = logging.getLogger("nx.router")


class SessionMixin:
    def _is_go_group(self, g: str | None) -> bool:
        """True se `g` e' il bucket -go."""
        suf = getattr(self.config, "go_suffix", None) or "-go"
        return bool(g) and str(g).endswith(suf)

    def _last_go_map(self) -> dict:
        d = getattr(self, "_last_go", None)
        if d is None:
            d = {}
            self._last_go = d
        return d

    def last_go(self, session_id: str | None = None,
                allow_cooled: bool = False) -> str | None:
        """Ultimo dep del bucket -go che ha servito con successo la sessione.
        Da riusare quando la scala ARRIVA a -go (dopo aver giocato tutte le
        carte free/zen): il provider ha potenzialmente ancora cache integra.
        `allow_cooled=True` per lo step dei -go "stantii" (in cooldown)."""
        sid = session_id or current_session()
        if not sid:
            return None
        d = self._last_go_map()
        ent = d.get(sid)
        if not ent:
            return None
        unique, ts = ent
        ttl = float(getattr(self.policy, "go_stick_ttl_sec", 600) or 600)
        if time.time() - ts > ttl:
            d.pop(sid, None)
            return None
        dep = self.config.deployment_by_unique(unique)
        if dep is None or not self._is_go_group(dep.get("group")):
            d.pop(sid, None)
            return None
        if self.is_retired(unique):
            return None
        if not allow_cooled and self.is_cooled_down(unique):
            return None
        return unique

    # ------------------------------------------------- rimborso latenza (-go)
    def _sess_turns_map(self) -> dict:
        d = getattr(self, "_session_turns", None)
        if d is None:
            d = {}
            self._session_turns = d
        return d

    def note_session_turn(self, session_id: str | None) -> bool:
        """Conta un turno della sessione e dice se va servito in -go.

        Ritorna True se il turno corrente e' coperto dal "rimborso latenza"
        (n < go_until): va instradato al bucket -go come se il client l'avesse
        chiesto. Il conteggio avviene QUI, all'atterraggio (una volta per
        richiesta)."""
        sid = session_id or current_session()
        if not sid:
            return False
        d = self._sess_turns_map()
        ent = d.get(sid)
        if ent is None:
            ent = {"n": 0, "go_until": 0}
            d[sid] = ent
        n = int(ent.get("n") or 0)
        gu = int(ent.get("go_until") or 0)
        go = n < gu
        ent["n"] = n + 1
        ent["ts"] = time.time()
        if go:
            log.info("🎁 [go-refund] %s: turno %d servito in -go "
                     "(rimborso: n < go_until=%d, restano %d)",
                     sid, n + 1, gu, gu - (n + 1))
        if len(d) > 4096:
            _ttl = self._warm_ttl() * 4
            _now = time.time()
            for s, e in list(d.items()):
                if _now - float(e.get("ts") or 0) > _ttl:
                    d.pop(s, None)
        return go

    def _add_go_turns(self, sid: str, refund: int, extra: str = "") -> int:
        """Accredita `refund` turni -go alla sessione (`go_until = max(prev,
        n + refund)`, mai ridotto). Ritorna i turni accreditati (0 se <= 0)."""
        if refund <= 0:
            return 0
        d = self._sess_turns_map()
        ent = d.get(sid)
        if ent is None:
            ent = {"n": 0, "go_until": 0}
            d[sid] = ent
        n = int(ent.get("n") or 0)
        target = n + refund
        prev = int(ent.get("go_until") or 0)
        if target > prev:
            ent["go_until"] = target
            ent["ts"] = time.time()
            log.info("🎁 [go-refund] %s: +%d turni -go (%sgo_until=%d)",
                     sid, refund, extra, target)
        return refund

    def grant_go_refund(self, session_id: str | None) -> int:
        """Concede il rimborso latenza: `go_until = max(go_until, n + refund)`
        con `refund = clamp(round(pct% * n), min, max)`. Ritorna i turni
        regalati (0 se disabilitato o sessione assente)."""
        if not getattr(self.policy, "go_refund_enabled", True):
            return 0
        sid = session_id or current_session()
        if not sid:
            return 0
        d = self._sess_turns_map()
        n = int((d.get(sid) or {}).get("n") or 0)
        pct = float(getattr(self.policy, "go_refund_pct", 20) or 0)
        lo = int(getattr(self.policy, "go_refund_min_turns", 5) or 0)
        hi = int(getattr(self.policy, "go_refund_max_turns", 20) or 0)
        refund = max(lo, min(hi, int(round(pct / 100.0 * n))))
        return self._add_go_turns(sid, refund, "n=%d, pct=%.0f%%, " % (n, pct))

    def grant_go_refund_fb(self, session_id: str | None, fb: int) -> int:
        """Regala turni -go per i fallback attraversati dalla richiesta:
        `turns = clamp(floor(fb_per_fallback * fb), fb_min_turns,
        fb_max_turns)`. Ritorna i turni accreditati (0 se disabilitato,
        `fb <= 0`, `per <= 0` o sessione assente)."""
        if not getattr(self.policy, "go_refund_enabled", True):
            return 0
        if not getattr(self.policy, "go_refund_fb_enabled", True):
            return 0
        sid = session_id or current_session()
        if not sid:
            return 0
        try:
            n_fb = int(fb)
        except (TypeError, ValueError):
            return 0
        if n_fb <= 0:
            return 0
        per = float(getattr(self.policy, "go_refund_fb_per_fallback", 0.5) or 0)
        if per <= 0:
            return 0
        lo = int(getattr(self.policy, "go_refund_fb_min_turns", 1) or 0)
        hi = int(getattr(self.policy, "go_refund_fb_max_turns", 3) or 0)
        refund = max(lo, min(hi, int(n_fb * per)))
        return self._add_go_turns(sid, refund, "fb=%d, " % n_fb)

    def go_refund_status(self, session_id: str | None = None) -> dict:
        sid = session_id or current_session()
        ent = (self._sess_turns_map().get(sid) if sid else None) or {}
        n = int(ent.get("n") or 0)
        gu = int(ent.get("go_until") or 0)
        return {"session": sid, "turns": n, "go_until": gu,
                "active": n < gu, "refund_left": max(0, gu - n)}

    def sticky_get(self, session_id: str) -> str | None:
        entry = self._sticky.get(session_id)
        if not entry:
            return None
        target, ts = entry
        if time.time() - ts > self.policy.sticky_ttl_sec:
            log.debug("[sticky] %s group_sticky TTL scaduto (%.0fs > %ds), rilasciato", session_id, time.time() - ts, self.policy.sticky_ttl_sec)
            self._sticky.pop(session_id, None)
            return None
        log.debug("[sticky] %s group_sticky valido: %s", session_id, target)
        return target

    def sticky_set(self, session_id: str, target: str) -> None:
        log.debug("[sticky] %s group_sticky impostato: %s", session_id, target)
        self._sticky[session_id] = (target, time.time())

    def sticky_release(self, session_id: str) -> None:
        self._sticky.pop(session_id, None)

    def dep_sticky_get(self, session_id: str) -> str | None:
        """Ritorna l'unique del deployment sticky per questa sessione."""
        entry = self._sticky_dep.get(session_id)
        if not entry:
            return None
        unique, ts = entry
        if time.time() - ts > self.policy.sticky_ttl_sec:
            log.debug("[sticky] %s dep_sticky TTL scaduto (%.0fs > %ds), rilasciato", session_id, time.time() - ts, self.policy.sticky_ttl_sec)
            self._sticky_dep.pop(session_id, None)
            return None
        dep = self.config.deployment_by_unique(unique)
        if opencode_cautious_request() and not self._is_go_group(
                (dep or {}).get("group")):
            # Spoofato in cautela: unico aggancio ammesso = stesso dep -go
            # (cache). Nessun'altra casistica.
            return None
        log.debug("[sticky] %s dep_sticky valido: %s", session_id, unique)
        return unique

    def dep_sticky_set(self, session_id: str, unique: str) -> None:
        log.debug("[sticky] %s dep_sticky impostato: %s", session_id, unique)
        self._sticky_dep[session_id] = (unique, time.time())

    def dep_sticky_release(self, session_id: str) -> None:
        self._sticky_dep.pop(session_id, None)

    def sticky_handoff(self, session_id: str | None,
                       nxt: dict | None) -> bool:
        """Warm handoff dello sticky quando un failover atterra su `nxt`.

        Se la sessione era sticky su un deployment della STESSA famiglia di
        `nxt`, sposta lo sticky su `nxt`: la richiesta successiva riparte gia'
        warm (stessa cache key/prefix del provider) invece di ripartire da un
        deployment freddo scelto a caso. Ritorna True se lo sticky e' stato
        spostato."""
        if not session_id or not nxt:
            return False
        if not getattr(self.policy, "sticky_handoff_same_family", True):
            return False
        target = nxt.get("unique")
        if not target:
            return False
        try:
            old = self.dep_sticky_get(session_id)
        except Exception:                       # mai bloccare il failover
            return False
        if not old or old == target:
            return False
        try:
            old_dep = self.config.deployment_by_unique(old)
        except Exception:
            old_dep = None
        fam = nxt.get("family")
        if old_dep and fam and old_dep.get("family") == fam:
            self.dep_sticky_set(session_id, target)
            log.info("[sticky-handoff] %s -> %s (stessa famiglia %s)",
                     old, target, fam)
            return True
        return False

    def _cap_sticky_key(self, session_id: str, need: frozenset[str] | None) -> str:
        """Chiave per lo sticky per-capability: session_id + sorted caps."""
        cap_str = ",".join(sorted(need)) if need else "_text"
        return f"{session_id}|{cap_str}"

    def dep_cap_sticky_get(self, session_id: str, need: frozenset[str] | None) -> str | None:
        """Ritorna l'unique sticky per la specifica capability richiesta."""
        if not getattr(self.policy, "deployment_sticky_per_capability", False):
            return None
        key = self._cap_sticky_key(session_id, need)
        entry = self._sticky_dep.get(key)
        if not entry:
            return None
        unique, ts = entry
        if time.time() - ts > self.policy.sticky_ttl_sec:
            self._sticky_dep.pop(key, None)
            return None
        dep = self.config.deployment_by_unique(unique)
        if opencode_cautious_request() and not self._is_go_group(
                (dep or {}).get("group")):
            return None
        log.debug("[cap-sticky] %s key=%s riuso %s", session_id, key, unique)
        return unique

    def dep_cap_sticky_set(self, session_id: str, need: frozenset[str] | None, unique: str) -> None:
        if not getattr(self.policy, "deployment_sticky_per_capability", False):
            return
        key = self._cap_sticky_key(session_id, need)
        log.debug("[cap-sticky] %s key=%s -> %s", session_id, key, unique)
        self._sticky_dep[key] = (unique, time.time())

    def dep_cap_sticky_release(self, session_id: str, need: frozenset[str] | None) -> None:
        if not getattr(self.policy, "deployment_sticky_per_capability", False):
            return
        key = self._cap_sticky_key(session_id, need)
        self._sticky_dep.pop(key, None)

    def _dep_sess(self) -> dict:
        """Accessor lazy della mappa inversa unique -> (session, ts) usata
        dalla SESSION-DEP GUARD (protegge i Router 'nudi' dei test)."""
        d = getattr(self, "_dep_last_session", None)
        if d is None:
            d = {}
            self._dep_last_session = d
        return d

    def _sess_deps(self) -> dict:
        """Accessor lazy dell'indice inverso session -> set(unique) posseduti
        (protetto per i Router 'nudi' dei test)."""
        d = getattr(self, "_session_deps", None)
        if d is None:
            d = {}
            self._session_deps = d
        return d

    def _sess_slow(self) -> dict:
        """Accessor lazy dell'indice session -> {unique: ts} dei free-dims
        'lenti per la stessa sessione' (protetto per i Router 'nudi')."""
        d = getattr(self, "_session_slow", None)
        if d is None:
            d = {}
            self._session_slow = d
        return d

    def _sess_slow_timer(self) -> dict:
        """Marchi 'lento' emessi dal TIMER della gara lenta (non dalla
        euristica F1). Si ripuliscono solo con un successo ASSOLUTAMENTE
        rapido (< soglia gara lenta): un dep che a 107s e' "normale per la
        sua baseline" non deve riprendere la prima posizione della warm."""
        d = getattr(self, "_session_slow_timer", None)
        if d is None:
            d = {}
            self._session_slow_timer = d
        return d

    def _slow_timer_flagged(self, unique: str,
                            session_id: str | None = None) -> bool:
        """True se `unique` porta il flag del TIMER della gara lenta (>45s)
        per questa sessione (non scaduto). E' il SOLO flag che decide i
        blocchi del warm pool (propri/prestati/lenti); hard/soft di
        `_sess_slow` restano fuori da quella partizione."""
        if not unique:
            return False
        sid = session_id or current_session()
        if not sid:
            return False
        tm = self._sess_slow_timer().get(sid)
        if not tm or unique not in tm:
            return False
        if time.time() - float(tm[unique]) > self._warm_ttl():
            tm.pop(unique, None)
            return False
        return True

    def ctx_boundary_floor(self, session_id: str | None) -> int:
        """Frontiera MASSIMA gia' applicata (con stub/dedup reali) a questa
        sessione: compact_tool_outputs non deve mai retrocedere sotto questo
        valore, cosi' i byte di prefisso gia' compressi restano stabili
        anche ruotando su un deployment con finestra piu' piccola.
        TTL = guard di sessione; cap 4096 sessioni."""
        if not session_id:
            return 0
        d = getattr(self, "_ctx_frontier", None)
        if not d:
            return 0
        rec = d.get(session_id)
        if rec is None:
            return 0
        b, ts = rec
        if time.time() - ts > self._guard_sec():
            d.pop(session_id, None)
            return 0
        return int(b)

    def note_compact_boundary(self, session_id: str | None,
                              boundary: int | None) -> None:
        """Registra la frontiera APPLICATA (solo quando il report e'
        changed=True): monotona in avanti per la sessione."""
        if not session_id or not boundary:
            return
        try:
            b = int(boundary)
        except (TypeError, ValueError):
            return
        d = getattr(self, "_ctx_frontier", None)
        if d is None:
            d = {}
            self._ctx_frontier = d
        cur = d.get(session_id)
        if cur is not None and cur[0] >= b:
            d[session_id] = (cur[0], time.time())   # solo refresh TTL
            return
        d[session_id] = (b, time.time())
        if len(d) > 4096:
            _now = time.time()
            _ttl = max(1.0, float(self._guard_sec()))
            for sid, (bb, ts) in list(d.items()):
                if _now - ts > _ttl:
                    d.pop(sid, None)
            while len(d) > 4096:
                _oldest = min(d, key=lambda k: d[k][1])
                d.pop(_oldest, None)

    def audit_prefix(self, session_id: str | None, messages,
                     boundary: int | None) -> str:
        """Impronta SHA-256 (16 hex) del PREFISSO canonico [1:boundary] e del
        system message [0], confrontata con la richiesta precedente della
        stessa sessione. Motivi: 'new' (prima vista), 'ok' (prefisso identico:
        la cache a monte E' riusabile), 'identity' (cambiato SOLO il system:
        siamo noi, rotazione/inject_identity), 'prefix' (cambiato il corpo:
        ctxcompact/histnorm o riscrittura del client), 'skip' (non calcolabile).
        Funzione pura dei byte: nessun effetto su routing/pick."""
        if not session_id or not isinstance(messages, list) \
                or boundary is None or boundary < 2 or len(messages) < 2:
            return "skip"
        try:
            body = json.dumps(messages[1:boundary], sort_keys=True,
                              separators=(",", ":"), default=str)
            sysm = messages[0] if isinstance(messages[0], dict) else None
            sysb = json.dumps(sysm, sort_keys=True, separators=(",", ":"),
                              default=str)
        except Exception:                       # noqa: BLE001
            return "skip"
        h_body = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:16]
        h_sys = hashlib.sha256(sysb.encode("utf-8", errors="replace")).hexdigest()[:16]
        now = time.time()
        reg = getattr(self, "_prefix_fp", None)
        if reg is None:
            reg = {}
            self._prefix_fp = reg
        prev = reg.get(session_id)
        if prev is not None and now - prev[2] > self._guard_sec():
            prev = None
        if prev is None:
            reason = "new"
        elif prev[0] == h_body and prev[1] == h_sys:
            reason = "ok"
        elif prev[0] == h_body:
            reason = "identity"
        else:
            reason = "prefix"
        reg[session_id] = (h_body, h_sys, now)
        if len(reg) > 4096:
            for s in [s for s, (_b, _y, t) in reg.items()
                      if now - t > self._guard_sec()]:
                reg.pop(s, None)
            while len(reg) > 4096:
                _oldest = min(reg, key=lambda k: reg[k][2])
                reg.pop(_oldest, None)
        return reason

    def _refresh_session(self, session_id: str, now: float | None = None) -> None:
        """Rinnova l'ownership di TUTTI i deployment ancora posseduti dalla
        sessione: finché la sessione e' viva i suoi dep recenti non decadono.
        Un dep gia' scaduto (o preso da un'altra sessione) NON viene
        resuscitato: esce dal set (dopo 15 min di silenzio tutto torna libero)."""
        if not getattr(self.policy, "session_dep_guard_enabled", True):
            return
        if not session_id:
            return
        now = time.time() if now is None else now
        guard = self._guard_sec()
        owned = self._sess_deps().get(session_id)
        if not owned:
            return
        d = self._dep_sess()
        for u in list(owned):
            ent = d.get(u)
            if not ent or ent[0] != session_id:
                owned.discard(u)          # preso da altri/rimosso
                continue
            if now - ent[1] > guard:
                owned.discard(u)          # già decaduto: non resuscitare
                continue
            d[u] = (session_id, now)
        if not owned:
            self._sess_deps().pop(session_id, None)

    def note_session_activity(self, session_id: str | None) -> None:
        """Segnala che la sessione ha USATO il servizio (richiesta in arrivo):
        rinfresca l'ownership di tutti i suoi dep, cosi' una sessione lunga
        che ruota/va in cooldown/si risveglia se li tiene 'tutti in tasca'
        invece di lasciarli decadere dopo il singolo uso."""
        if not session_id:
            return
        self._refresh_session(session_id)

    def _note_dep_session(self, session_id: str, unique: str) -> None:
        """Registra l'ultima sessione che ha servito con SUCCESSO `unique`.
        SOLO bucket free-dims (mai -go/-fallback ne' gruppi capacita'): la
        guardia serve a smorzare i rate-limit per-chiave delle key free."""
        if not getattr(self.policy, "session_dep_guard_enabled", True):
            return
        dep = self.config.deployment_by_unique(unique)
        if dep is None:
            return
        g = dep.get("group", "")
        if self.config.group_caps.get(g) is not None \
                or self._is_renewal_bucket(g):
            return
        now = time.time()
        d = self._dep_sess()
        d[unique] = (session_id, now)
        self._sess_deps().setdefault(session_id, set()).add(unique)
        # rinnova anche gli altri dep posseduti: la sessione e' viva.
        self._refresh_session(session_id, now)
        if len(d) > 8192:
            _ttl = self._guard_sec() * 4
            for k, (_s, ts) in list(d.items()):
                if now - ts > _ttl:
                    d.pop(k, None)

    def other_session_recent(self, unique: str) -> bool:
        """True se `unique` e' stato servito con successo da un'ALTRA
        sessione meno di session_dep_guard_sec fa. False se la guardia e'
        spenta, se non c'e' una sessione corrente, o se l'ultima che l'ha
        usato e' la sessione stessa."""
        if not getattr(self.policy, "session_dep_guard_enabled", True):
            return False
        sid = current_session()
        if not sid:
            return False
        ent = self._dep_sess().get(unique)
        if not ent:
            return False
        other, ts = ent
        if not other or other == sid:
            return False
        return (time.time() - ts) < self._guard_sec()

    def note_session_success(self, session_id: str | None,
                             unique: str | None,
                             latency_ms: float | None = None,
                             ctx_est: int | None = None,
                             kind: str = "total") -> None:
        """Ricorda l'ultimo deployment che ha servito con SUCCESSO la
        sessione (detentore cache) + l'inverso per la SESSION-DEP GUARD.
        Con `latency_ms` sopra soglia marca il dep come 'lento per la
        sessione' (hard); con latenza intermedia e `ctx_est` pesante marca
        soft (demote solo per richieste pesanti). `kind` dice COSA vale
        quella latenza ('total' o 'ttft') per il confronto relativo col
        bucket. In-memory."""
        if not session_id or not unique:
            return
        self._note_dep_session(session_id, unique)
        self._note_session_slow(session_id, unique, latency_ms, ctx_est, kind)
        _lg = self.config.deployment_by_unique(unique)
        if _lg is not None and self._is_go_group(_lg.get("group")):
            # Ultimo -go usato: verra' riusato quando la scala torna a -go.
            self._last_go_map()[session_id] = (unique, time.time())
        if not getattr(self.policy, "cache_aware_enabled", True):
            return
        d = self._cache_ok()
        d[session_id] = (unique, time.time())
        if len(d) > 4096:
            _ttl = float(getattr(self.policy, "cache_holder_ttl_sec",
                                 3600) or 3600)
            _now = time.time()
            for k, (_u, ts) in list(d.items()):
                if _now - ts > _ttl:
                    d.pop(k, None)

    def session_holder(self, session_id: str | None = None,
                       ttl: float | None = None) -> str | None:
        sid = session_id or current_session()
        if not sid:
            return None
        d = self._cache_ok()
        ent = d.get(sid)
        if not ent:
            return None
        unique, ts = ent
        ttl = float(ttl if ttl is not None else
                    (getattr(self.policy, "cache_holder_ttl_sec", 3600)
                     or 3600))
        if time.time() - ts > ttl:
            d.pop(sid, None)
            return None
        dep = self.config.deployment_by_unique(unique)
        if opencode_cautious_request() and not self._is_go_group(
                (dep or {}).get("group")):
            # Spoofato in cautela: detentore cache solo per -go.
            return None
        return unique

    def cache_holder(self, session_id: str | None = None,
                     need: frozenset[str] | None = None,
                     ctx: int | None = None,
                     ttl: float | None = None) -> dict | None:
        """Detentore cache per la sessione, se ancora valido. `ttl` opzionale
        per accorciare la validita' (nel bucket -go si usa `go_stick_ttl_sec`)."""
        if not getattr(self.policy, "cache_aware_enabled", True):
            return None
        unique = self.session_holder(session_id, ttl=ttl)
        if not unique:
            return None
        dep = self.config.deployment_by_unique(unique)
        if dep is None:
            return None
        if self.is_cooled_down(unique) or self.is_retired(unique):
            return None
        if self._endpoint_quarantined(dep):
            return None
        if self._is_demoted_dep(unique, session_id, ctx,
                                allow_slow=self._warm_allow_slow()):
            return None
        if not self._cap_fits(dep, ctx):
            return None
        if not _dep_usable(dep):
            return None
        if need and not self._dep_supports(dep, need):
            return None
        return dep

    def is_session_compact(self, session_id: str | None = None) -> bool:
        sid = session_id or current_session()
        if not sid:
            return False
        d = getattr(self, "_session_compact", None)
        if d is None:
            return False
        ts = d.get(sid)
        if ts is None:
            return False
        ttl = float(getattr(self.policy, "cache_holder_ttl_sec", 3600) or 3600)
        if time.time() - ts > ttl:
            d.pop(sid, None)
            return False
        return True

    def mark_session_compact(self, session_id: str | None = None) -> None:
        sid = session_id or current_session()
        if not sid:
            return
        d = getattr(self, "_session_compact", None)
        if d is None:
            d = {}
            self._session_compact = d
        d[sid] = time.time()

    # ------------------------------------------------ VISTE STATO (read-only)
    def slow_timer_view(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        ttl = self._warm_ttl()
        src = getattr(self, "_session_slow_timer", None)
        out: list[dict] = []
        for sid, marks in (src if isinstance(src, dict) else {}).items():
            if not isinstance(marks, dict):
                continue
            for u, ts in list(marks.items()):
                try:
                    age = now - float(ts)
                except (TypeError, ValueError):
                    continue
                if age > ttl:
                    continue
                out.append({"session_id": sid, "unique": u,
                            "age_sec": round(age, 1),
                            "ttl_left_sec": round(max(0.0, ttl - age), 1)})
        return out

    def go_refund_view(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        out: list[dict] = []
        for sid, e in self._sess_turns_map().items():
            n = int((e or {}).get("n") or 0)
            gu = int((e or {}).get("go_until") or 0)
            out.append({"session_id": sid, "turns": n, "go_until": gu,
                        "active": n < gu, "refund_left": max(0, gu - n),
                        "age_sec": round(
                            now - float((e or {}).get("ts") or 0.0), 1)})
        return out

    def last_go_view(self, now: float | None = None) -> list[dict]:
        """Vista READ-ONLY dell'ultimo -go per sessione. NON chiama
        `last_go()` (che muta la mappa espellendo gli scaduti): la validita' e'
        ricalcolata qui, in modo difensivo, dal solo TTL di policy."""
        now = time.time() if now is None else now
        ttl = float(getattr(self.policy, "go_stick_ttl_sec", 600) or 600)
        out: list[dict] = []
        for sid, ent in (getattr(self, "_last_go", None) or {}).items():
            try:
                unique, ts = ent[0], float(ent[1])
            except (TypeError, IndexError, ValueError):
                continue
            age = now - ts
            out.append({"session_id": sid, "unique": unique,
                        "age_sec": round(age, 1), "ttl_sec": ttl,
                        "valid": age <= ttl})
        return out

    def ctx_frontier_view(self, session_id: str | None = None,
                          now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        src = getattr(self, "_ctx_frontier", None)
        out: list[dict] = []
        for sid, rec in (src if isinstance(src, dict) else {}).items():
            if session_id and sid != session_id:
                continue
            try:
                b, ts = int(rec[0]), float(rec[1])
            except (TypeError, IndexError, ValueError):
                continue
            out.append({"session_id": sid, "boundary": b,
                        "age_sec": round(now - ts, 1)})
        return out

    def prefix_fp_view(self, session_id: str | None = None) -> list[dict]:
        now = time.time()
        src = getattr(self, "_prefix_fp", None)
        out: list[dict] = []
        for sid, rec in (src if isinstance(src, dict) else {}).items():
            if session_id and sid != session_id:
                continue
            try:
                h_body, h_sys, ts = rec[0], rec[1], float(rec[2])
            except (TypeError, IndexError, ValueError):
                continue
            out.append({"session_id": sid, "body_fp": h_body,
                        "sys_fp": h_sys, "age_sec": round(now - ts, 1)})
        return out

    def session_dep_guard_view(self, now: float | None = None) -> list[dict]:
        now = time.time() if now is None else now
        guard = self._guard_sec()
        out: list[dict] = []
        for u, ent in (self._dep_sess() or {}).items():
            try:
                sid, ts = str(ent[0] or ""), float(ent[1] or 0.0)
            except (TypeError, IndexError, ValueError):
                continue
            out.append({"unique": u, "session_id": sid,
                        "age_sec": round(now - ts, 1),
                        "guard_sec": guard,
                        "expired": (now - ts) >= guard})
        out.sort(key=lambda r: -r["age_sec"])
        return out
