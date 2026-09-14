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
   360s... I deployment il cui cooldown residuo supera
   `cooldown_autoprobe_skip_over_sec` (2h) sono ESCLUSI del tutto dai probe:
   li rivedra' il tempo o la ULTIMA SPIAGGIA della scala.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque

from .forwarder import _MODEL_MISSING_RE

log = logging.getLogger("nx.autoprobe")

# Solo i gruppi dim testo (es. `...-200k`): escludono -go/-fallback/capability.
_DIM_RE = re.compile(r"-(\d+)k$")
_PROBE_PROMPT = "Reply with the single letter A"
_running = False
_last_probe: dict[str, float] = {}
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
    )


def maybe_spawn(router, forwarder, profile: str) -> None:
    """Avvia (se non gia' in corso e se abilitato) un pass di probe sui dim cooled."""
    global _running
    if not _cfg(router.policy)[0]:
        return
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
                          min_gap: float, max_total: int) -> list[tuple[str, str]]:
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
            s = router._stats.get(unique)
            last_act = 0.0 if s is None else max(
                s.last_used, s.last_success_ts, s.last_fail_ts)
            by_group.setdefault(grp, []).append((last_act, unique))
    targets: list[tuple[str, str]] = []
    for grp, items in by_group.items():
        items.sort(key=lambda x: (_probe_count_24h(x[1], now), x[0]))
        for _la, unique in items[:per_dim]:
            targets.append((grp, unique))
    return targets[:max_total]


def _select_targets(router, profile: str, per_dim: int, min_age: float,
                    min_gap: float, max_total: int,
                    crisis: tuple[bool, float, float] | None = None,
                    skip_over: float = 0.0) -> list[tuple[str, str]]:
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
        by_group.setdefault(grp, []).append((resid, unique))
    targets: list[tuple[str, str]] = []
    for grp, items in by_group.items():
        items.sort(key=lambda x: (_probe_count_24h(x[1], now), x[0]))
        for _rem, unique in items[:per_dim]:
            targets.append((grp, unique))
    return targets[:max_total]


async def _probe_pass(router, forwarder, profile: str) -> None:
    global _running
    try:
        (_en, per_dim, min_age, grow, min_gap, max_total, timeout,
         crisis_en, crisis_ratio, crisis_mult, fresh_age,
         transient_sec, skip_over, multiply) = _cfg(router.policy)
        if per_dim <= 0 or max_total <= 0:
            return
        _streak_cap = max(0, int(getattr(
            router.policy, "probe_retire_after", 5) or 0))
        # --- MODO FRESH: sonda i MAI USATI (24h) con probe "normale" -----
        fresh = _select_fresh_targets(
            router, profile, per_dim, fresh_age, min_gap, max_total)
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
                _last_probe[unique] = time.time()
                _probe_times.setdefault(unique, deque()).append(_last_probe[unique])
                ok, lat, code, _body = await _probe_one(forwarder, dep, timeout)
                if ok:
                    router.note_result(unique, lat)
                    log.info("[autoprobe] %s: probe OK -> promosso (%.0fms)",
                             unique, lat)
                else:
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
            skip_over=skip_over)
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
            _last_probe[unique] = time.time()
            _probe_times.setdefault(unique, deque()).append(_last_probe[unique])
            ok, _lat, code, _body = await _probe_one(forwarder, dep, timeout)
            if ok:
                router.clear_cooldown(unique)
                log.info("[autoprobe] %s: probe OK -> risvegliato", unique)
            else:
                _cd = _probe_ko_cooldown(code, _body, grow, transient_sec)
                _esc = _bump_probe_streak(router, unique, _streak_cap) > 0
                if _esc:
                    _cd = max(_cd, grow)
                _cd = _scale_probe_cd(unique, _cd, time.time(), multiply)
                _n = _probe_count_24h(unique, time.time())
                log.info("[autoprobe] %s: probe KO (%s) -> cooldown "
                         "+%.0fs (x%d/24h%s)", unique, code or "timeout",
                         _cd, max(1, _n), " escalation" if _esc else "")
                now2 = time.time()
                base = max(router._cooldown.get(unique, 0.0), now2)
                new_exp = base + _cd
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
        log.debug("[autoprobe] pass terminato con errore", exc_info=True)
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
        for unique in uniques:
            try:
                dep = router.config.deployment_by_unique(unique)
            except Exception:  # noqa: BLE001
                dep = None
            if not dep:
                continue
            ok, lat, _code, _body = await _probe_one(forwarder, dep, timeout)
            if ok:
                router.note_result(unique, lat)
                log.info("[hotreload] %s: probe OK -> deployment caldo "
                         "(%.0fms)", unique, lat)
            else:
                router.mark_failed(unique, seconds=_cd, reason="hotreload_probe",
                                   additive=True)
                log.warning("[hotreload] %s: probe KO -> cooldown %.0fs",
                            unique, _cd)
            await asyncio.sleep(0.2)
    except Exception:  # noqa: BLE001
        log.debug("[hotreload] pass terminato con errore", exc_info=True)


async def _probe_one(forwarder, dep: dict,
                     timeout: float) -> tuple[bool, float, int, str]:
    """Sonda un deployment. Ritorna (ok, latency_ms, code, body_snippet).
    code = status HTTP; 0 = timeout/rete/eccezione (transitorio)."""
    url = f"{str(dep.get('api_base', '')).rstrip('/')}/chat/completions"
    body = {"model": dep.get("model", ""), "max_tokens": 1,
            "messages": [{"role": "user", "content": _PROBE_PROMPT}]}
    headers = {"Authorization": f"Bearer {dep.get('api_key', '')}"}
    t0 = time.monotonic()
    try:
        cli = forwarder._client_for(url)
        resp = await cli.post(url, json=body, headers=headers, timeout=timeout)
        lat = (time.monotonic() - t0) * 1000.0
        if resp.status_code != 200:
            return False, lat, resp.status_code, (resp.text or "")[:300]
        data = resp.json()
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
