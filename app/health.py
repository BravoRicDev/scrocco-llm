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

log = logging.getLogger("nx.health")


async def run_health_cycle(router, http) -> tuple[int, int]:
    """Un giro di verifica. Ritorna (marcati, account_controllati).

    `http` è un httpx.AsyncClient già configurato (iniettabile nei test).
    Aggiorna anche router.last_health per la visibilità in /admin/state e TUI.
    """
    cfg = router.config
    accounts: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for gname, deps in cfg.groups.items():
        for d in deps:
            if not router.is_cooled_down(d["unique"]):
                accounts[(d["api_base"], d["api_key"])].append(d)

    marked = 0
    checked = 0
    for (base, key), deps in sorted(accounts.items()):
        try:
            r = await http.get(f"{base.rstrip('/')}/models",
                               headers={"Authorization": f"Bearer {key}"})
            checked += 1
            if r.status_code != 200:
                log.debug("[health] %s /models -> %s: giro saltato",
                          base, r.status_code)
                continue
            ids = {m.get("id", "") for m in (r.json().get("data") or [])}
        except Exception as exc:             # noqa: BLE001 — rete: riprova dopo
            log.debug("[health] %s errore %s", base, exc)
            continue

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
        await asyncio.sleep(0.3)             # gentilezza tra account

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
