"""Autoprobe dei cooldown, triggerato da una chiamata (solo gruppi -dim testo).

Non entra MAI nel percorso di risposta: parte fire-and-forget e si limita a
"sondare" i deployment dormienti piu' pronti. Se un probe riesce il deployment
viene risvegliato (cooldown azzerato) e dalla chiamata successiva sara' tra i
vivi; se fallisce si allunga il cooldown di poco (`grow`) cosi' i bersagli
ruotano tra una chiamata e l'altra e non si insiste sempre sugli stessi.

Non chiama `note_result`/`mark_failed`/`note_start`: il probe e' puramente
ricognitivo e non deve avvelenare la rotazione adattiva.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

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


def _select_targets(router, profile: str, per_dim: int, min_age: float,
                    min_gap: float, max_total: int) -> list[tuple[str, str]]:
    now = time.time()
    pfx = f"{getattr(router.config, 'proxy_prefix', 'scrocco-llm-')}{profile}-"
    by_group: dict[str, list[tuple[float, str]]] = {}
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
        _en, per_dim, min_age, grow, min_gap, max_total, timeout = _cfg(router.policy)
        if per_dim <= 0 or max_total <= 0:
            return
        targets = _select_targets(router, profile, per_dim, min_age, min_gap,
                                  max_total)
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
            ok = await _probe_one(forwarder, dep, timeout)
            if ok:
                router.clear_cooldown(unique)
                log.info("[autoprobe] %s: probe OK -> risvegliato", unique)
            else:
                base = max(router._cooldown.get(unique, 0.0), time.time())
                router._cooldown[unique] = base + grow
                rem = max(0.0, router._cooldown[unique] - time.time())
                log.info("[autoprobe] %s: probe KO -> cooldown +%.0fs "
                         "(residuo %.0fs)", unique, grow, rem)
            await asyncio.sleep(0.2)
    except Exception:  # noqa: BLE001
        log.debug("[autoprobe] pass terminato con errore", exc_info=True)
    finally:
        _running = False


async def _probe_one(forwarder, dep: dict, timeout: float) -> bool:
    url = f"{str(dep.get('api_base', '')).rstrip('/')}/chat/completions"
    body = {"model": dep.get("model", ""), "max_tokens": 1,
            "messages": [{"role": "user", "content": _PROBE_PROMPT}]}
    headers = {"Authorization": f"Bearer {dep.get('api_key', '')}"}
    try:
        cli = forwarder._client_for(url)
        resp = await cli.post(url, json=body, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return False
        data = resp.json()
        return isinstance(data, dict) and "choices" in data
    except Exception:  # noqa: BLE001
        return False
