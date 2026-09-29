"""Il bias provider/chiave non deve costare O(N) PER OGNI deployment valutato.

Contesto reale (cubotto, 2026-09-29): `_reputation_score` veniva chiamata per
OGNI deployment della catena (vedi `rep_scores = [...]` in `app/router.py`) e al
suo interno ricalcolava i conteggi provider/chiave con due `sum(...)` che
scorrevano TUTTI i deployment. Con ~9800 deployment il costo diventava O(N^2)
eseguito sull'event loop: il gateway restava fermo 31 secondi e il supervisore
interpretava il worker come morto (stack di tutti i thread in `var/stall-*.txt`,
thread fermo dentro questa funzione).

Questi test bloccano il ritorno di quella complessita': contano le chiamate e
verificano che i conteggi restino corretti (la normalizzazione e' log).
"""

from __future__ import annotations

import os
import tempfile

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

HEADER = ("commento,modello,provider,endpoint,data,context,max_input,"
          "priority,scrocco-llm-test,caps\n")


def _mk_router(n_modelli: int = 200, per_modello: int = 1) -> tuple[Router, GatewayConfig]:
    """Router con N modelli distinti (provider/chiave dedicati) su una sola -dim."""
    righe = []
    for i in range(n_modelli):
        for k in range(per_modello):
            righe.append(
                f"r{i}-{k},m{i},groq,https://p{i}.test/v1,free,128,8000,5,K{i}-{k},text\n"
            )
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(HEADER + "".join(righe))
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict({})
    router = Router(cfg, pol)
    os.unlink(path)
    return router, cfg


def _deployments(cfg: GatewayConfig) -> list[dict]:
    """Tutti i deployment delle catene, senza duplicati."""
    visti: dict[str, dict] = {}
    for lst in cfg.groups.values():
        for d in lst:
            visti.setdefault(d["unique"], d)
    return list(visti.values())


def test_conteggi_provider_chiave_coerenti():
    """La normalizzazione resta corretta: conteggio per provider e per chiave."""
    router, cfg = _mk_router(n_modelli=20, per_modello=3)
    prov, keys = router._provider_key_counts("https://p7.test/v1|m7", "K7-0")
    assert prov == 3, "3 deployment condividono lo stesso provider/modello"
    assert keys == 1, "ogni chiave compare una volta sola"
    # chiave inesistente -> 0 (nessuna divisione per zero a valle)
    prov0, keys0 = router._provider_key_counts("mai-visto", "mai-vista")
    assert prov0 == 0 and keys0 == 0


def test_cache_agganciata_al_config_si_invalida_col_reload():
    """La cache vive sul Config: un reload (nuovo oggetto) la azzera."""
    router, cfg = _mk_router(n_modelli=10)
    router._provider_key_counts("x", "y")
    assert hasattr(cfg, "_provider_key_counts_cache")
    cfg2 = GatewayConfig.__new__(GatewayConfig)  # nuovo oggetto, stessa classe
    cfg2.groups = cfg.groups
    router.config = cfg2
    router._provider_key_counts("x", "y")
    assert hasattr(cfg2, "_provider_key_counts_cache"), "ristagnata sul nuovo config"


def test_nessuna_scansione_globale_per_ogni_deployment():
    """N valutazioni di `_reputation_score` non devono costare O(N^2) chiamate.

    Senza cache ogni valutazione ripercorreva tutti i deployment: con 300
    deployment valutati sarebbero ~90.000 chiamate a `_provider_key`. Con la
    cache i conteggi si fanno una volta sola e ogni valutazione ne fa una.
    """
    router, _cfg = _mk_router(n_modelli=300)
    deps = _deployments(router.config)
    assert len(deps) == 300, f"attesi 300 deployment, trovati {len(deps)}"

    chiamate = {"n": 0}
    originale = router._provider_key

    def _conta(dep: dict) -> str:
        chiamate["n"] += 1
        return originale(dep)

    router._provider_key = _conta  # type: ignore[method-assign]
    for d in deps:
        router._reputation_score(d["unique"], d, None)

    limite = 4 * len(deps)
    assert chiamate["n"] <= limite, (
        f"_provider_key chiamata {chiamate['n']} volte per {len(deps)} deployment "
        f"(limite {limite}): i conteggi vengono ricalcolati a ogni valutazione (O(N^2))"
    )
