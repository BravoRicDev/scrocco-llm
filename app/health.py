"""Health proattivo (F6): verifica periodica che i modelli configurati
esistano davvero nei provider, SENZA consumare token (GET /models).
[EN] WHAT: proactive model-existence checks via provider /models lists
(token-free), so dead rows surface before traffic hits them.

I deployment con modello assente vengono messi in cooldown PRIMA che una
richiesta reale li incontri. Rispetta i rate-limit: UNA chiamata per
combinazione endpoint+chiave per ciclo, skip dei cooled-down.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import defaultdict

from .provider_models import DEFAULT_TTL_SEC, fetch_provider_models

log = logging.getLogger("nx.health")


async def run_health_cycle(router, http) -> tuple[int, int]:
    """Un giro di verifica. Ritorna (marcati, account_controllati).

    `http` è un httpx.AsyncClient già configurato (iniettabile nei test).
    Aggiorna anche router.last_health per la visibilità in /admin/state e TUI.
    """
    cfg = router.config
    # Raggruppa per ENDPOINT (non per endpoint+chiave): la lista /models e'
    # identica per tutte le chiavi dello stesso provider. Una GET con la prima
    # chiave valida (fallback sulle successive); le chiavi gemelle sono saltate.
    deps_by_endpoint: dict[str, list[dict]] = defaultdict(list)
    keys_by_endpoint: dict[str, list[str]] = defaultdict(list)
    _seen_keys: dict[str, set[str]] = defaultdict(set)
    for deps in cfg.groups.values():
        for d in deps:
            if router.is_cooled_down(d["unique"]):
                continue
            base = (d["api_base"] or "").rstrip("/")
            deps_by_endpoint[base].append(d)
            k = d["api_key"]
            if k and k not in _seen_keys[base]:
                _seen_keys[base].add(k)
                keys_by_endpoint[base].append(k)

    ttl = int(getattr(router.policy, "provider_models_ttl_sec",
                      DEFAULT_TTL_SEC) or 0)
    marked = 0
    checked = 0
    for base, deps in sorted(deps_by_endpoint.items()):
        res = await fetch_provider_models(http, base, keys_by_endpoint[base],
                                          ttl_sec=ttl)
        if not res.ok:
            log.debug("[health] %s /models -> %s: giro saltato",
                      base, res.error)
            continue
        checked += 1
        ids = res.ids
        for d in deps:
            if router.is_cooled_down(d["unique"]):
                continue
            eff = d["model"]                 # già il nome EFFETTIVO inviato
            if eff not in ids and f"models/{eff}" not in ids:
                log.warning("[health] %s: modello '%s' ASSENTE dal provider "
                            "(%d disponibili): cooldown preventivo",
                            d["unique"], eff, len(ids))
                router.mark_failed(d["unique"])
                marked += 1
        await asyncio.sleep(0.15)            # gentilezza tra endpoint

    router.last_health = {"last_cycle_at": int(_time.time()),
                          "marked": marked, "accounts": checked,
                          "enabled": bool(getattr(router.policy,
                                                  "proactive_health", False))}
    return marked, checked


async def health_loop(router, interval_sec: float) -> None:
    """Task di lungo periodo: gira SOLO se la policy lo abilita (a caldo)."""
    import time as _time
    import httpx
    async with httpx.AsyncClient(timeout=20.0) as http:
        while True:
            try:
                if router.policy.proactive_health:
                    n, _acc = await run_health_cycle(router, http)
                    if n:
                        log.info("[health] ciclo completato: %d deployment "
                                 "marcati", n)
            except Exception as exc:         # mai far morire il task
                log.warning("[health] ciclo fallito: %s", exc)
            await asyncio.sleep(max(60.0, float(interval_sec)))
