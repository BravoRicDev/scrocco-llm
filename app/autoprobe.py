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
   di poco (`grow`). In questa modalita' NON chiama
   note_result/mark_failed/note_start: il probe e' puramente ricognitivo e
   non deve avvelenare la rotazione adattiva.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from .forwarder import _MODEL_MISSING_RE

log = logging.getLogger("nx.autoprobe")

# Solo i gruppi dim testo (es. `...-200k`): escludono -go/-fallback/capability.
_DIM_RE = re.compile(r"-(\d+)k$")
_PROBE_PROMPT = "Reply with the single letter A"
_running = False
_last_probe: dict[str, float] = {}


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
        items.sort(key=lambda x: x[0])      # meno recenti (vergini) prima
        for _la, unique in items[:per_dim]:
            targets.append((grp, unique))
    return targets[:max_total]


def _select_targets(router, profile: str, per_dim: int, min_age: float,
                    min_gap: float, max_total: int,
                    crisis: tuple[bool, float, float] | None = None) -> list[tuple[str, str]]:
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
    for unique, exp in list(router._cooldown.items()):
        if exp <= now:
            continue
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
        by_group.setdefault(grp, []).append((exp - now, unique))
    targets: list[tuple[str, str]] = []
    for grp, items in by_group.items():
        items.sort(key=lambda x: x[0])      # i piu' "pronti" (residuo minore) prima
        for _rem, unique in items[:per_dim]:
            targets.append((grp, unique))
    return targets[:max_total]


async def _probe_pass(router, forwarder, profile: str) -> None:
    global _running
    try:
        (_en, per_dim, min_age, grow, min_gap, max_total, timeout,
         crisis_en, crisis_ratio, crisis_mult, fresh_age) = _cfg(router.policy)
        if per_dim <= 0 or max_total <= 0:
            return
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
                ok, lat, code, _body = await _probe_one(forwarder, dep, timeout)
                if ok:
                    router.note_result(unique, lat)
                    log.info("[autoprobe] %s: probe OK -> promosso (%.0fms)",
                             unique, lat)
                elif _probe_ko_is_cooldown(code, _body):
                    router.mark_failed(unique, seconds=grow,
                                       reason="autoprobe_fresh")
                    log.info("[autoprobe] %s: probe KO (%s) -> cooldown %.0fs",
                             unique, code or "timeout", grow)
                else:
                    log.info("[autoprobe] %s: probe KO (%s) -> skip, nessun "
                             "cooldown (transitorio)",
                             unique, code or "timeout")
                await asyncio.sleep(0.2)
            return
        # --- MODO COOLED (fallback): risveglio dormienti, come da sempre ----
        targets = _select_targets(
            router, profile, per_dim, min_age, min_gap, max_total,
            crisis=(crisis_en, crisis_ratio, crisis_mult))
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
            ok, _lat, code, _body = await _probe_one(forwarder, dep, timeout)
            if ok:
                router.clear_cooldown(unique)
                log.info("[autoprobe] %s: probe OK -> risvegliato", unique)
            elif _probe_ko_is_cooldown(code, _body):
                base = max(router._cooldown.get(unique, 0.0), time.time())
                router._cooldown[unique] = base + grow
                rem = max(0.0, router._cooldown[unique] - time.time())
                log.info("[autoprobe] %s: probe KO (%s) -> cooldown +%.0fs "
                         "(residuo %.0fs)", unique, code or "timeout", grow, rem)
            else:
                log.info("[autoprobe] %s: probe KO (%s) -> skip, residuo "
                         "invariato (transitorio)", unique, code or "timeout")
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
                router.mark_failed(unique, seconds=_cd, reason="hotreload_probe")
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


def _probe_ko_is_cooldown(code: int, body: str) -> bool:
    """True se il KO del probe merita un cooldown:
    - 429 rate-limit: SI' SEMPRE (120s, come richiesto: non si rimuove);
    - 401/402/403 (auth/permission definitiva): SI';
    - 400 con "no such model"/model inesistente/ritirato: SI' (definitivo);
    - transitori (5xx, timeout/rete code=0, altri 4xx): NO -> skip, il pass
      lo ritenta al giro successivo senza bruciare cooldown."""
    if code == 429:
        return True
    if code in (401, 402, 403):
        return True
    if code == 400 and _MODEL_MISSING_RE.search(body or ""):
        return True
    return False
