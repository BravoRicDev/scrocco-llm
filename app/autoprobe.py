"""Autoprobe dei cooldown, triggerato da una chiamata (solo gruppi -dim testo).

Non entra MAI nel percorso di risposta: parte fire-and-forget.

Due modalita':
1) FRESH: se nel pool dim ci sono deployment MAI USATI nelle ultime
   `cooldown_autoprobe_fresh_age_sec` (24h) li sonda PRIMA con un probe
   "normale" (note_result/mark_failed): se buoni salgono in cima alla
   classifica, se falliscono finiscono in cooldown (non si insiste). Un
   probe riuscito/fallito li toglie dal set "fresco", quindi non vengono
   ri-sondati in continuazione.
2) COOLED (fallback, come da sempre): se non ci sono freschi sonda i
   deployment dormienti piu' "pronti". Se il probe riesce il deployment
   viene risvegliato (cooldown azzerato); se fallisce si allunga il cooldown
   di `cooldown_autoprobe_grow_sec` (definitivi/429) o del MODESTO
   `cooldown_autoprobe_transient_sec` (transitori): si ruota comunque via
   dal dep flaky, con una "grazia" breve. In questa modalita' NON chiama
   note_result/mark_failed/note_start (il probe e' puramente ricognitivo).
   Dopo `probe_retire_after` KO consecutivi il cooldown sale al livello
   `grow` (escalation): rotazione piu' lunga, MAI retire dall'autoprobe.
   L'incremento e' MOLTIPLICATO per il numero di probe fatti su quel
   deployment nelle ultime 24h (cooldown_autoprobe_multiply_24h): 120s, 240s,
   360s... Inoltre il residuo ALMENO RADDOPPIA a ogni KO (backoff: niente
   richieste ravvicinate inutili).
   I deployment il cui cooldown residuo supera
   `cooldown_autoprobe_skip_over_sec` (2h) sono ESCLUSI del tutto dai probe:
   li rivedra' il tempo, il risveglio della scala (fra -dim e -go) o la
   ULTIMA SPIAGGIA.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque

from .forwarder import _MODEL_MISSING_RE, maybe_quarantine_ban
from . import protocols as proto
from .caution import background_cautious_enabled
from .opencode_gate import opencode_cautious_enabled, is_opencode_zen_dep

log = logging.getLogger("nx.autoprobe")

# Solo i gruppi dim testo (es. `...-200k`): escludono -go/-fallback/capability.
_DIM_RE = re.compile(r"-(\d+)k$")
_PROBE_PROMPT = "Reply with the single letter A"


def _zen_rank(dep: dict) -> int:
    """Rank di ordinamento probe: 0 = normale, 1 = zen (in cautela opencode).

    In cautela opencode gli zen NON vengono esclusi dai probe: restano
    sondabili ma per ULTIMI (priorita' minima, in coda ai target)."""
    return 1 if (opencode_cautious_enabled()
                 and is_opencode_zen_dep(dep)) else 0
_running = False
_last_probe: dict[str, float] = {}
# F32: ultimo probe per CHIAVE (non per deployment). Lo stesso conto non deve
# essere martellato dall'autoprobe anche se il probe gira su un altro
# deployment della stessa chiave: quota giornaliera / rate-limit sono per
# chiave, non per unique.
_key_last_probe: dict[str, float] = {}
# BUDGET GIORNALIERO PER CHIAVE: molti free-tier (es. openrouter) contano
# RICHIESTE/giorno, non token. Con N modelli sulla stessa chiave, un probe per
# deployment satura il conto "solo per vedere se e' viva". Qui si contano i
# probe realmente fatti per chiave nelle ultime 24h e si smette al cap.
_key_probe_day: dict[str, deque[float]] = {}
_MAX_KEY_PROBE_BY_PROVIDER = {
    "openrouter": 1, "llm7": 1, "tokenrouter": 1, "unorouter": 1,
    "bynara": 1, "api.airforce": 1, "airforce": 1, "cloudflare": 2,
    "google": 1, "requesty": 1,
}


_key_quota_day: dict[str, float] = {}


def _quota_code(code) -> bool:
    """True se il KO del probe e' di QUOTA/saturazione (429)."""
    return str(code or "").strip() in ("429", "http_429") or "429" in str(code or "")


def _key_saturated(router, dep: dict, now: float) -> bool:
    """True se la CHIAVE e' satura ORA: blocco giornaliero messo da un probe
    429, oppure soft-429 visto dal traffico reale (F7 `_key_soft`).

    Su una chiave satura non si provano altri modelli: la quota e' per
    chiave/provider, non per modello -> sarebbe spreco puro.
    """
    key = _key_of(dep)
    if not key:
        return False
    if _key_quota_day.get(key, 0.0) > now:
        return True
    try:
        import hashlib
        tag = hashlib.sha256(key.encode("utf-8", errors="replace")) \
            .hexdigest()[:12]
        return float(router._key_soft.get(tag, 0.0) or 0.0) > now
    except Exception:  # noqa: BLE001
        return False


def _block_key_for_day(dep: dict, now: float) -> None:
    """Dopo un 429 di probe: nessun altro probe su questa chiave per 24h."""
    key = _key_of(dep)
    if key:
        _key_quota_day[key] = now + 86400.0


def _provider_of(dep: dict) -> str:
    try:
        return str(dep.get("provider") or dep.get("tier") or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _key_day_max(dep: dict, base: int) -> int:
    """Cap giornaliero di probe per QUESTA chiave (provider-aware)."""
    if base <= 0:
        return 0
    prov = _provider_of(dep)
    return min(base, _MAX_KEY_PROBE_BY_PROVIDER.get(prov, base))


def _key_day_ok(dep: dict, now: float, base: int) -> bool:
    """False se la chiave ha gia' esaurito il budget di probe giornaliero."""
    cap = _key_day_max(dep, base)
    if cap <= 0:
        return True       # 0 = budget disabilitato (nessun cap giornaliero)
    key = _key_of(dep)
    if not key:
        return True
    dq = _key_probe_day.setdefault(key, deque())
    while dq and now - dq[0] > 86400.0:
        dq.popleft()
    return len(dq) < cap


def _note_key_probe_day(dep: dict, now: float) -> None:
    key = _key_of(dep)
    if key:
        _key_probe_day.setdefault(key, deque()).append(now)


def _key_ok_map(router) -> dict[str, float]:
    """api_key -> ultimo successo REALE (traffico, non probe) su quella chiave.

    Se una chiave ha servito una risposta vera di recente e' viva: sondarla
    e' puro spreco di quota. Non crea statistiche nuove (usa solo le esistenti).
    """
    out: dict[str, float] = {}
    try:
        for grp in router.config.groups.values():
            for d in grp:
                k = str(d.get("api_key") or "")
                if not k:
                    continue
                s = router._stats.get(d.get("unique"))
                ts = float(getattr(s, "last_success_ts", 0.0) or 0.0) if s else 0.0
                if ts > out.get(k, 0.0):
                    out[k] = ts
    except Exception:  # noqa: BLE001
        return {}
    return out


def _key_ok_fresh(okmap: dict, dep: dict, now: float, fresh_sec: float) -> bool:
    """True se la chiave ha dato prova di vita (successo reale) da poco."""
    if fresh_sec <= 0 or not okmap:
        return False
    ts = okmap.get(_key_of(dep), 0.0)
    return bool(ts and now - ts <= fresh_sec)
# Giro giornaliero sui RITIRATI (un probe riuscito li riabilita: nessun
# ritiro e' definitivo). Uno solo in volo, con partenza al primo tick dopo
# mezzanotte locale e ritmo lento per non martellare i conti.
_retired_task = None
_retired_day = ""


def _keyhealth():
    """Istanza KeyHealth del gateway (None se non disponibile)."""
    try:
        from . import main as _gw_mod
        return getattr(_gw_mod, "KEYHEALTH", None)
    except Exception:  # noqa: BLE001
        return None


def _key_of(dep: dict) -> str:
    """Tag della chiave API del deployment ('' se assente)."""
    try:
        return str(dep.get("api_key") or "")
    except Exception:  # noqa: BLE001
        return ""


def _key_gap_ok(dep: dict, now: float, key_gap: float) -> bool:
    """False se la CHIAVE di `dep` e' stata sondata meno di `key_gap` fa."""
    if key_gap <= 0:
        return True
    k = _key_of(dep)
    if not k:
        return True
    last = _key_last_probe.get(k)
    if last is None:
        return True
    return (now - last) >= key_gap


def _note_key_probe(dep: dict, ts: float) -> None:
    k = _key_of(dep)
    if k:
        _key_last_probe[k] = ts
# Storico dei timestamp di ogni probe (fino a 24h), per ordinare i target
# dal MENO tentato: si spalma il carico di probing su tutto il pool e si
# evita che due chiavi vicine siano martellate in continuazione.
_probe_times: dict[str, deque[float]] = {}


def _probe_count_24h(unique: str, now: float) -> int:
    """Numero di probe eseguiti sul deployment nelle ultime 24h (con potatura
    degli ingressi piu' vecchi per non far crescere la memoria all'infinito)."""
    dq = _probe_times.get(unique)
    if not dq:
        return 0
    cutoff = now - 86400.0
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq)


def note_probe_time(unique: str, ts: float) -> None:
    """Registra un probe avvenuto a `ts` (usato dal bootstrap dai log per
    ricostruire `_probe_times` al riavvio). Potatura >24h relativa a `ts`."""
    if not unique:
        return
    dq = _probe_times.setdefault(unique, deque())
    dq.append(ts)
    cutoff = ts - 86400.0
    while dq and dq[0] < cutoff:
        dq.popleft()


def _scale_probe_cd(unique: str, base_cd: float, now: float,
                    multiply: bool) -> float:
    """MOLTIPLICA l'incremento di cooldown di un KO del probe per il numero di
    probe fatti su quel deployment nelle ultime 24h (minimo 1). Piu' lo si
    riprova senza successo, piu' dorme: 120s -> 240s -> 360s ... Il valore e'
    sempre ADDITIVO al residuo esistente (mark_failed additive / add manuale)."""
    if not multiply:
        return base_cd
    return base_cd * max(1, _probe_count_24h(unique, now))


def _cfg(policy):
    return (
        bool(getattr(policy, "cooldown_autoprobe_enabled", True)),
        max(0, int(getattr(policy, "cooldown_autoprobe_per_dim", 2) or 0)),
        float(getattr(policy, "cooldown_autoprobe_min_age_sec", 300.0) or 0.0),
        float(getattr(policy, "cooldown_autoprobe_grow_sec", 120.0) or 0.0),
        float(getattr(policy, "cooldown_autoprobe_min_gap_sec", 60.0) or 0.0),
        max(0, int(getattr(policy, "cooldown_autoprobe_max_total", 6) or 0)),
        float(getattr(policy, "cooldown_autoprobe_timeout_sec", 20.0) or 20.0),
        bool(getattr(policy, "cooldown_autoprobe_crisis_enabled", True)),
        float(getattr(policy, "cooldown_autoprobe_crisis_ratio", 0.30) or 0.0),
        float(getattr(policy, "cooldown_autoprobe_crisis_mult", 2.0) or 0.0),
        float(getattr(policy, "cooldown_autoprobe_fresh_age_sec",
                       86400.0) or 86400.0),
        float(getattr(policy, "cooldown_autoprobe_transient_sec",
                       30.0) or 0.0),
        float(getattr(policy, "cooldown_autoprobe_skip_over_sec",
                       7200.0) or 0.0),
        bool(getattr(policy, "cooldown_autoprobe_multiply_24h", True)),
        float(getattr(policy, "cooldown_autoprobe_key_gap_sec", 300.0) or 0.0),
    )


async def _retired_pass(router, forwarder) -> None:
    """Giro giornaliero sui deployment RITIRATI, con calma.

    Parte al primo tick dopo mezzanotte locale e sonda TUTTI i ritirati in
    sequenza, uno ogni `cooldown_autoprobe_retired_gap_sec` secondi, saltando
    le chiavi sondate di recente (`cooldown_autoprobe_key_gap_sec`): lo stesso
    conto non viene martellato anche se il probe gira su deployment diversi.
    Un probe riuscito riabilita (clear keyhealth + cooldown azzerato); un KO
    non fa danni: restano ritirati fino al giro successivo.
    """
    try:
        kh = _keyhealth()
        if kh is None:
            return
        timeout = float(getattr(router.policy,
                                "cooldown_autoprobe_timeout_sec", 20.0) or 20.0)
        key_gap = float(getattr(router.policy,
                                "cooldown_autoprobe_key_gap_sec", 300.0) or 0.0)
        day_max = max(0, int(getattr(
            router.policy, "cooldown_autoprobe_key_day_max", 4) or 0))
        spacing = float(getattr(router.policy,
                                "cooldown_autoprobe_retired_gap_sec",
                                20.0) or 0.0)
        retired = sorted(
            (u for u, rec in list((kh.data or {}).items())
             if (rec or {}).get("state") == "retired"),
            key=lambda u: (_zen_rank(
                router.config.deployment_by_unique(u) or {}), u))
        if not retired:
            return
        log.info("[autoprobe] giro giornaliero RITIRATI: %d deployment "
                 "(gap %.0fs, chiave min %.0fs)", len(retired), spacing,
                 key_gap)
        riab = 0
        for unique in retired:
            try:
                dep = router.config.deployment_by_unique(unique)
            except Exception:  # noqa: BLE001
                dep = None
            if not dep or dep.get("enabled") is False:
                continue
            if _keyhealth() is None:          # gateway in shutdown
                return
            if not _key_gap_ok(dep, time.time(), key_gap):
                continue      # F32: stessa chiave sondata da poco
            if not _key_day_ok(dep, time.time(), day_max):
                continue      # stessa quota giornaliera degli altri pass
            if _key_saturated(router, dep, time.time()):
                continue      # chiave satura: nessun altro modello oggi
            name = str(dep.get("api_key") or "")[:8]
            _note_key_probe(dep, time.time())
            _note_key_probe_day(dep, time.time())
            ok, lat, code, _body = await _probe_one(forwarder, dep, timeout)
            if ok:
                kh.clear(unique)
                router.clear_cooldown(unique)
                router.stats_for(unique).probe_fail_streak = 0
                riab += 1
                log.warning("[autoprobe] %s RITIRATO ma risponde (%.0fms, "
                            "chiave %s*) -> RIABILITATO", unique, lat, name)
            else:
                maybe_quarantine_ban(router, dep, code, _body)
                if _quota_code(code):
                    _block_key_for_day(dep, time.time())
                log.info("[autoprobe] %s ritirato: probe KO (%s) -> resta "
                         "fuori (chiave %s*)", unique, code or "timeout",
                         name)
            if spacing > 0:
                await asyncio.sleep(spacing)
        log.info("[autoprobe] giro RITIRATI terminato: %d riabilitati su %d",
                 riab, len(retired))
    except Exception:  # noqa: BLE001
        log.warning("[autoprobe] giro ritirati terminato con errore",
                    exc_info=True)


def maybe_spawn_retired(router, forwarder) -> None:
    """Avvia il giro giornaliero sui ritirati (primo tick dopo mezzanotte)."""
    global _retired_task, _retired_day
    if background_cautious_enabled():        # cautela generica: niente probe
        return
    if not bool(getattr(router.policy,
                        "cooldown_autoprobe_retired_enabled", True)):
        return
    if not _cfg(router.policy)[0]:
        return
    if not _keyhealth():
        return
    day = time.strftime("%Y-%m-%d")          # confine di mezzanotte LOCALE
    if _retired_day == day:
        return
    if _retired_task is not None and not _retired_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _retired_day = day
    _retired_task = loop.create_task(_retired_pass(router, forwarder))


def _schedule(policy) -> str:
    return str(getattr(policy, "cooldown_autoprobe_schedule",
                       "nightly") or "nightly").strip().lower()


def maybe_spawn(router, forwarder, profile: str) -> None:
    global _running
    if background_cautious_enabled():        # cautela generica: niente probe
        return
    if not _cfg(router.policy)[0]:
        return
    if _schedule(router.policy) != "request":
        return          # NOTTE-SOLO: i probe partono SOLO dal giro di
                        # mezzanotte (main._nightly_scheduler), mai a ogni
                        # richiesta (regola utente post-ban llm7.io)
    if _running:
        return
    _running = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _running = False
        return
    loop.create_task(_probe_pass(router, forwarder, profile))


def spawn_hotreload_probe(router, forwarder, uniques) -> None:
    """Probe fire-and-forget dei deployment APPENA AGGIUNTI via hot-reload
    CSV: scopre lo stato di salute PRIMA che ricevano traffico reale.
    OK -> note_result (entra caldo con un successo registrato); KO -> cooldown
    breve. Non entra mai nel percorso di risposta."""
    if background_cautious_enabled():        # cautela generica: niente probe
        return
    _u = [u for u in (uniques or []) if u]
    if not _u:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_hotreload_pass(router, forwarder, _u))


def _is_fresh(router, unique: str, now: float, fresh_age: float) -> bool:
    """True se il deployment non ha AVUTO ATTIVITA' (uso/successo/fallimento)
    nelle ultime `fresh_age` secondi (o non ha mai avuto attivita')."""
    s = router._stats.get(unique)
    if s is None:
        return True
    last_act = max(s.last_used, s.last_success_ts, s.last_fail_ts)
    if last_act <= 0:
        return True
    return (now - last_act) > fresh_age


def _select_fresh_targets(router, profile: str, per_dim: int, fresh_age: float,
                          min_gap: float, max_total: int,
                          key_gap: float = 0.0) -> list[tuple[str, str]]:
    """Bersagli FRESCHI: deployment dim mai usati nelle ultime 24h (o mai
    usati affatto), NON in cooldown (niente insistenza sui falliti), ordinati
    per attivita' piu' vecchia prima (i piu' vergini vengono sondati per
    primi)."""
    now = time.time()
    pfx = f"{getattr(router.config, 'proxy_prefix', 'scrocco-llm-')}{profile}-"
    by_group: dict[str, list[tuple[float, str]]] = {}
    for grp, deps in (getattr(router.config, "groups", {}) or {}).items():
        if not _DIM_RE.search(grp):
            continue           # solo dim testo (-<N>k), niente -go/-fallback/cap
        if profile and not grp.startswith(pfx):
            continue
        for d in deps:
            unique = d.get("unique") or ""
            if router.is_cooled_down(unique):
                continue       # appena fallito: non insistere
            if router.is_retired(unique):
                continue
            if not _is_fresh(router, unique, now, fresh_age):
                continue
            if now - _last_probe.get(unique, 0.0) < min_gap:
                continue
            if not _key_gap_ok(d, now, key_gap):
                continue       # F32: stessa chiave sondata troppo di recente
            s = router._stats.get(unique)
            last_act = 0.0 if s is None else max(
                s.last_used, s.last_success_ts, s.last_fail_ts)
            by_group.setdefault(grp, []).append(
                (_zen_rank(d), last_act, unique))
    targets: list[tuple[str, str]] = []
    for grp, items in by_group.items():
        items.sort(key=lambda x: (x[0], _probe_count_24h(x[2], now), x[1]))
        for _zr, _la, unique in items[:per_dim]:
            targets.append((grp, unique))
    return targets[:max_total]


def _select_targets(router, profile: str, per_dim: int, min_age: float,
                    min_gap: float, max_total: int,
                    crisis: tuple[bool, float, float] | None = None,
                    skip_over: float = 0.0,
                    key_gap: float = 0.0) -> list[tuple[str, str]]:
    now = time.time()
    pfx = f"{getattr(router.config, 'proxy_prefix', 'scrocco-llm-')}{profile}-"
    by_group: dict[str, list[tuple[float, str]]] = {}
    # --- CRISIS MODE: frazione di deployment dim in cooldown nel pool ---
    # Se supera la soglia, il pass raddoppia per_dim e dimezza min_gap:
    # sotto pressione (quota esaurita/outage) i dormienti tornano disponibili
    # piu' in fretta, riducendo la finestra col pool dimezzato.
    _crisis_en, _ratio, _mult = crisis or (False, 0.0, 1.0)
    if _crisis_en and _ratio > 0:
        total_dim = 0
        cooled_dim = 0
        for grp, deps in (getattr(router.config, "groups", {}) or {}).items():
            if not _DIM_RE.search(grp):
                continue
            if profile and not grp.startswith(pfx):
                continue
            for d in deps:
                total_dim += 1
                if router.is_cooled_down(d["unique"]):
                    cooled_dim += 1
        if total_dim > 0 and (cooled_dim / total_dim) > _ratio:
            per_dim = max(1, int(per_dim * _mult))
            min_gap = max(0.0, min_gap / _mult)
            log.info("[autoprobe] CRISIS: cooled %.0f%% (%d/%d) -> per_dim "
                     "x%.1f, min_gap /%.1f", (cooled_dim / total_dim) * 100,
                     cooled_dim, total_dim, _mult, _mult)
    for unique in list(router._cooldown.keys()):
        resid = router.cooldown_residual(unique)
        if resid <= 0:
            continue
        if skip_over > 0 and resid > skip_over:
            continue           # cooldown > soglia (2h): troppo rotto, no probe
        try:
            dep = router.config.deployment_by_unique(unique)
        except Exception:  # noqa: BLE001
            dep = None
        if not dep:
            continue
        grp = dep.get("group") or ""
        if not _DIM_RE.search(grp):
            continue           # solo dim testo (-<N>k), niente -go/-fallback/cap
        if profile and not grp.startswith(pfx):
            continue
        if not router.is_cooled_down(unique):
            continue
        age = router.cooldown_age(unique)
        if age is None or age < min_age:
            continue           # appena messo in cooldown: non insistere
        if now - _last_probe.get(unique, 0.0) < min_gap:
            continue
        if not _key_gap_ok(dep, now, key_gap):
            continue           # F32: stessa chiave sondata troppo di recente
        by_group.setdefault(grp, []).append((_zen_rank(dep), resid, unique))
    targets: list[tuple[str, str]] = []
    for grp, items in by_group.items():
        items.sort(key=lambda x: (x[0], _probe_count_24h(x[2], now), x[1]))
        for _zr, _rem, unique in items[:per_dim]:
            targets.append((grp, unique))
    return targets[:max_total]


async def _probe_pass(router, forwarder, profile: str) -> None:
    global _running
    try:
        (_en, per_dim, min_age, grow, min_gap, max_total, timeout,
         crisis_en, crisis_ratio, crisis_mult, fresh_age,
         transient_sec, skip_over, multiply, key_gap) = _cfg(router.policy)
        if per_dim <= 0 or max_total <= 0:
            return
        _day_max = max(0, int(getattr(
            router.policy, "cooldown_autoprobe_key_day_max", 4) or 0))
        _ok_fresh = float(getattr(
            router.policy, "cooldown_autoprobe_key_ok_fresh_sec",
            43200.0) or 0.0)
        _okmap = _key_ok_map(router)
        _streak_cap = max(0, int(getattr(
            router.policy, "probe_retire_after", 5) or 0))
        # --- MODO FRESH: sonda i MAI USATI (24h) con probe "normale" -----
        fresh = _select_fresh_targets(
            router, profile, per_dim, fresh_age, min_gap, max_total,
            key_gap=key_gap)
        if fresh:
            log.info("[autoprobe] fresh: %d deployment mai usati in %.0fh -> "
                     "probe normale", len(fresh), fresh_age / 3600.0)
            for _grp, unique in fresh:
                try:
                    dep = router.config.deployment_by_unique(unique)
                except Exception:  # noqa: BLE001
                    continue
                if not dep:
                    continue
                _now = time.time()
                if _key_ok_fresh(_okmap, dep, _now, _ok_fresh):
                    log.info("[autoprobe] %s: chiave con successo reale "
                             "recente -> probe inutile, salto", unique)
                    continue
                if _key_saturated(router, dep, _now):
                    log.info("[autoprobe] %s: chiave satura (429/quota) -> "
                             "nessun altro modello, salto", unique)
                    continue
                if not _key_day_ok(dep, _now, _day_max):
                    log.info("[autoprobe] %s: budget probe/24h della chiave "
                             "esaurito -> salto", unique)
                    continue
                _last_probe[unique] = time.time()
                _note_key_probe_day(dep, _last_probe[unique])
                _probe_times.setdefault(unique, deque()).append(_last_probe[unique])
                _note_key_probe(dep, _last_probe[unique])
                ok, lat, code, _body = await _probe_one(forwarder, dep, timeout)
                if ok:
                    router.note_result(unique, lat)
                    log.info("[autoprobe] %s: probe OK -> promosso (%.0fms)",
                             unique, lat)
                else:
                    maybe_quarantine_ban(router, dep, code, _body)
                    if _quota_code(code):
                        _block_key_for_day(dep, time.time())
                    _cd = _probe_ko_cooldown(code, _body, grow, transient_sec)
                    _esc = _bump_probe_streak(router, unique, _streak_cap) > 0
                    if _esc:
                        _cd = max(_cd, grow)
                    _cd = _scale_probe_cd(unique, _cd, time.time(), multiply)
                    _n = _probe_count_24h(unique, time.time())
                    log.info("[autoprobe] %s: probe KO (%s) -> cooldown "
                             "+%.0fs (x%d/24h%s)", unique, code or "timeout",
                             _cd, max(1, _n), " escalation" if _esc else "")
                    router.mark_failed(unique, seconds=_cd,
                                       reason="autoprobe_fresh",
                                       additive=True)
                await asyncio.sleep(0.2)
            return
        # --- MODO COOLED (fallback): risveglio dormienti, come da sempre ----
        targets = _select_targets(
            router, profile, per_dim, min_age, min_gap, max_total,
            crisis=(crisis_en, crisis_ratio, crisis_mult),
            skip_over=skip_over, key_gap=key_gap)
        if not targets:
            return
        log.info("[autoprobe] pass: %d deployment cooled da sondare", len(targets))
        for _grp, unique in targets:
            try:
                dep = router.config.deployment_by_unique(unique)
            except Exception:  # noqa: BLE001
                continue
            if not dep or not router.is_cooled_down(unique):
                continue
            _now = time.time()
            if _key_ok_fresh(_okmap, dep, _now, _ok_fresh):
                log.info("[autoprobe] %s: chiave con successo reale recente "
                         "-> non la risveglio a vuoto", unique)
                continue
            if _key_saturated(router, dep, _now):
                log.info("[autoprobe] %s: chiave satura (429/quota) -> "
                         "nessun altro modello, salto", unique)
                continue
            if not _key_day_ok(dep, _now, _day_max):
                log.info("[autoprobe] %s: budget probe/24h della chiave "
                         "esaurito -> salto", unique)
                continue
            _last_probe[unique] = time.time()
            _note_key_probe_day(dep, _last_probe[unique])
            _probe_times.setdefault(unique, deque()).append(_last_probe[unique])
            _note_key_probe(dep, _last_probe[unique])
            ok, _lat, code, _body = await _probe_one(forwarder, dep, timeout)
            if ok:
                router.clear_cooldown(unique)
                log.info("[autoprobe] %s: probe OK -> risvegliato", unique)
            else:
                maybe_quarantine_ban(router, dep, code, _body)
                if _quota_code(code):
                    _block_key_for_day(dep, time.time())
                _cd = _probe_ko_cooldown(code, _body, grow, transient_sec)
                _esc = _bump_probe_streak(router, unique, _streak_cap) > 0
                if _esc:
                    _cd = max(_cd, grow)
                _cd = _scale_probe_cd(unique, _cd, time.time(), multiply)
                now2 = time.time()
                base = max(router._cooldown.get(unique, 0.0), now2)
                # BACKOFF: il residuo almeno RADDOPPIA a ogni KO cooled (cosi'
                # non si fanno richieste ravvicinate inutili), con un minimo di
                # +_cd. Quel residuo lo riproveranno la scala (fra -dim e -go)
                # o l'ultima spiaggia, non l'autoprobe.
                _add = max(_cd, base - now2)
                new_exp = base + _add
                _n = _probe_count_24h(unique, now2)
                log.info("[autoprobe] %s: probe KO (%s) -> cooldown "
                         "+%.0fs (x%d/24h%s)", unique, code or "timeout",
                         _add, max(1, _n), " escalation" if _esc else "")
                since = router._cooldown_since.get(unique)
                if since is None:
                    since = now2
                    router._cooldown_since[unique] = since
                router._cooldown[unique] = new_exp
                router._cooldown_full_map()[unique] = float(new_exp - since)
                rem = router.cooldown_residual(unique)
                log.info("[autoprobe] %s: residuo %.0fs",
                         unique, rem)
            await asyncio.sleep(0.2)
    except Exception:  # noqa: BLE001
        log.warning("[autoprobe] pass terminato con errore", exc_info=True)
    finally:
        _running = False


async def _hotreload_pass(router, forwarder, uniques) -> None:
    try:
        if not getattr(router.policy, "hotreload_probe_enabled", True):
            return
        timeout = float(getattr(router.policy, "hotreload_probe_timeout_sec",
                                15.0) or 15.0)
        _cd = float(getattr(router.policy, "hotreload_probe_cooldown_sec",
                            300.0) or 300.0)
        _key_gap = float(getattr(router.policy,
                                 "cooldown_autoprobe_key_gap_sec", 300.0) or 0.0)
        _day_max = max(0, int(getattr(
            router.policy, "cooldown_autoprobe_key_day_max", 1) or 0))
        for unique in sorted(uniques, key=lambda u: (
                _zen_rank(router.config.deployment_by_unique(u) or {}), u)):
            try:
                dep = router.config.deployment_by_unique(unique)
            except Exception:  # noqa: BLE001
                dep = None
            if not dep:
                continue
            if not _key_gap_ok(dep, time.time(), _key_gap):
                continue       # F32: chiave gia' sondata poco fa
            if not _key_day_ok(dep, time.time(), _day_max):
                continue       # budget 1/giorno per CHIAVE anche qui
            _note_key_probe_day(dep, time.time())
            _note_key_probe(dep, time.time())
            ok, lat, _code, _body = await _probe_one(forwarder, dep, timeout)
            if ok:
                router.note_result(unique, lat)
                log.info("[hotreload] %s: probe OK -> deployment caldo "
                         "(%.0fms)", unique, lat)
            else:
                maybe_quarantine_ban(router, dep, _code, _body)
                router.mark_failed(unique, seconds=_cd, reason="hotreload_probe",
                                   additive=True)
                log.warning("[hotreload] %s: probe KO -> cooldown %.0fs",
                            unique, _cd)
            await asyncio.sleep(0.2)
    except Exception:  # noqa: BLE001
        log.warning("[hotreload] pass terminato con errore", exc_info=True)


async def _probe_one(forwarder, dep: dict,
                     timeout: float) -> tuple[bool, float, int, str]:
    """Sonda un deployment. Ritorna (ok, latency_ms, code, body_snippet).
    code = status HTTP; 0 = timeout/rete/eccezione (transitorio)."""
    style = proto.style_of(dep)
    url = proto.build_url(dep, stream=False)
    chat_body = {"model": dep.get("model", ""), "max_tokens": 1,
                 "messages": [{"role": "user", "content": _PROBE_PROMPT}]}
    body = (chat_body if style == proto.CHAT
            else proto.translate_request(style, chat_body, dep))
    headers = proto.apply_auth(dep, {
        "Authorization": f"Bearer {dep.get('api_key', '')}"})
    t0 = time.monotonic()
    try:
        cli = forwarder._client_for(url)
        resp = await cli.post(url, json=body, headers=headers, timeout=timeout)
        lat = (time.monotonic() - t0) * 1000.0
        if resp.status_code != 200:
            return False, lat, resp.status_code, (resp.text or "")[:300]
        data = resp.json()
        if style != proto.CHAT:
            try:
                chat = proto.translate_response(style, data, dep)
                ok = isinstance(chat, dict) and bool(chat.get("choices"))
            except Exception:  # noqa: BLE001
                ok = False
            return ok, lat, resp.status_code, ""
        return (isinstance(data, dict) and "choices" in data), lat, resp.status_code, ""
    except Exception:  # noqa: BLE001
        return False, (time.monotonic() - t0) * 1000.0, 0, ""


def _probe_ko_cooldown(code: int, body: str, grow: float,
                       transient: float) -> float:
    """Cooldown da applicare a un KO del probe:
    - 429 rate-limit: SEMPRE `grow` (120s, come richiesto);
    - 401/402/403 (auth/permission definitiva): `grow`;
    - 400 con "no such model"/model inesistente/ritirato: `grow`;
    - transitori (5xx, timeout/rete code=0, altri 4xx): `transient` MODESTO
      (30s): si ruota comunque via dal dep flaky, senza bruciare il grow."""
    if code == 429:
        return max(1.0, float(grow))
    if code in (401, 402, 403):
        return max(1.0, float(grow))
    if code == 400 and _MODEL_MISSING_RE.search(body or ""):
        return max(1.0, float(grow))
    return max(1.0, float(transient))


def _bump_probe_streak(router, unique: str, streak_cap: int) -> float:
    """Incrementa probe_fail_streak del deployment e ritorna un'eventuale
    escalation di cooldown: dopo `streak_cap` KO consecutivi il transitorio
    sale al livello `grow` (rotazione piu' lunga, MAI retire dall'autoprobe).
    Il reset dello streak avviene da note_result/clear_cooldown (successo)."""
    s = router.stats_for(unique)
    s.probe_fail_streak += 1
    if streak_cap > 0 and s.probe_fail_streak >= streak_cap:
        return 1.0     # multiplicatore: escalate il transitorio a grow
    return 0.0


async def nightly_pass(router, forwarder, profiles=None) -> None:
    """GIRO NOTTURNO (regola utente post-ban llm7.io): UNA sola volta al
    giorno, poco dopo mezzanotte locale, un giro completo per ogni profile
    testo: PRIMA i FRESH (mai usati), POI i COOLED (risvegli dormienti) —
    due chiamate di _probe_pass perche' il ramo fresh, se trova bersagli,
    esce prima di toccare i cooled. Valgono sempre i limiti conservativi:
    per_dim, max_total, gap per-chiave e budget 1/giorno per CHIAVE (un
    giro puo' quindi trovare poco o nulla da sondare, ed e' giusto)."""
    global _running
    if background_cautious_enabled():        # cautela generica: niente probe
        return
    if not _cfg(router.policy)[0]:
        return
    if _running:
        return
    _running = True
    try:
        profiles = list(profiles or
                        (getattr(router.config, "profile_dims", {}) or {}))
        log.info("[autoprobe] nightly: giro notturno su %d profile", len(profiles))
        for pname in profiles:
            for _round in range(2):          # 1=FRESH, 2=COOLED
                try:
                    await _probe_pass(router, forwarder, pname)
                except Exception:            # noqa: BLE001
                    log.warning("[autoprobe] nightly %s: errore nel pass",
                                pname, exc_info=True)
                _running = True              # _probe_pass lo azzera nel finally
    finally:
        _running = False
