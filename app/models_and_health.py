"""Endpoint di health, metrics e /v1/models.

Estratti da `app/main.py` (C2, Round 4 Clean Code). Gli oggetti condivisi
(`config`, `policy`, `router`, `authn`: STATO runtime) sono raggiunti
DENTRO il corpo delle funzioni tramite `import app.main as M`: a livello di
modulo si creerebbe un ciclo di import (main include questo router a fine
file, dopo aver definito tutto).
"""

import time

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from starlette.requests import Request

from . import capmeta
from . import metrics
from .http_responses import unauthorized as _unauthorized
from .observability import render_prometheus

router = APIRouter()


@router.get("/health/liveliness")
async def liveliness() -> PlainTextResponse:
    return PlainTextResponse("I'm alive!")


@router.get("/healthz")
async def healthz() -> dict:
    import app.main as M
    # Conti delle capacità tra tutti i deployment
    cap_counts = {cap: 0 for cap in frozenset({"text", "vision", "video", "audio", "image_gen", "tools"})}
    for deps in M.config.groups.values():
        for dep in deps:
            model = dep.get("model", "")
            caps = M.policy.caps_for(model)
            for c in caps:
                cap_counts[c] = cap_counts.get(c, 0) + 1

    return {
        "status": "ok",
        "profiles": M.config.profiles,
        "groups": len(M.config.groups),
        "deployments": sum(len(v) for v in M.config.groups.values()),
        "cooldowns": len(M.router._cooldown),
        "sticky_sessions": len(M.router._sticky),
        "port": M.PORT,
        "policy": {
            "file": M.POLICY_PATH.name,
            "step_up_pct": M.policy.step_up_pct,
            "step_up_per_profile": {k: f"{v}%" for k, v in M.policy.profile_step_up_pct.items()},
            "speed_hotwords": len(M.policy.speed_hotwords),
            "speed_min_dim_k": M.policy.speed_min_dim_k,
            "aliases": len(M.policy.aliases),
            "capability_routing_enabled": M.policy.routing_active(),
            "capabilities_configured": len(M.policy.model_capabilities),
        },
        "capabilities_summary": {
            "total_deployments": sum(cap_counts.values()),
            "per_capability": {k: cap_counts[k] for k in sorted(cap_counts)},
        },
    }


@router.get("/metrics")
async def metrics_endpoint():
    """Formato testo Prometheus. Unica route /metrics. Loopback-only.

    Espone SIA le metriche HTTP di observability SIA tutti gli nx_* di
    app.metrics. I gauge di stato router sono aggiornati ad ogni scrape (prima
    erano settati in una route shadowed -> mai emessi).
    """
    import app.main as M
    metrics.set_gauge("nx_cooldown_active", len(M.router._cooldown))
    metrics.set_gauge("nx_sticky_active", len(M.router._sticky))
    body = metrics.render() + render_prometheus()
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")


# ------------------------------------------------------------------- models
# I nomi esposti dipendono DA CHI CHIAMA, perche' i due insiemi hanno
# stabilita' diversa:
#   * `uniques`  = `scrocco-llm-<prof>-<grp>__<modello>__<idx>`: il deployment
#     singolo. CAMBIANO a ogni ricaricamento del CSV (l'indice si riallinea se
#     una riga viene aggiunta/tolta), quindi non sono "nomi veri": si mostrano
#     solo al master, che li usa per ispezionare il parco. Restano comunque
#     CHIAMABILI (routing e /v1/models/{id} li accettano) per non rompere
#     qualcuno che li ha pinnati.
#   * `stable`   = nome base, gruppi (`-Nk`, `-go`, `-fallback`, per-capacita')
#     e alias: nomi che restano validi domani. Sono questi che vede una chiave
#     di profilo, che non deve mai puntare a un deployment instabile.
VIEWS = ("uniques", "stable")


def _stable_names(cfg, pname: str) -> list[str]:
    """Nome base + gruppi (`-Nk`, `-go`, `-fallback`, per-capacita').

    `whitelist_for` aggiunge anche gli uniques: qui non vanno, perche' non
    sono nomi stabili (cambiano a ogni ricaricamento del CSV) ed e' esattamente
    cio' che una chiave di profilo non deve vedere.
    """
    base = cfg.proxy_prefix + pname
    groups = sorted(g for g in cfg.groups if g.startswith(base + "-"))
    return [base] + groups


def _names_for_auth(auth, view: str) -> list[str]:
    """Nomi visibili per `auth` nella vista `view` (de-dup, ordine stabile)."""
    import app.main as M
    cfg = M.config
    master = auth.mode == "master"
    profs = list(cfg.profiles) if master else [auth.profile or ""]
    out: list[str] = []
    if view == "uniques":
        for p in profs:
            if not p:
                continue
            base = cfg.proxy_prefix + p
            for g, deps in cfg.groups.items():
                if g == base or g.startswith(base + "-"):
                    out.extend(d["unique"] for d in deps)
    else:
        for p in profs:
            if p:
                out.extend(_stable_names(cfg, p))
        # alias di policy: pubblici, ma solo se il target è utilizzabile
        allowed = None if master else set(out)
        for a, t in M.policy.aliases.items():
            if allowed is None or t in allowed:
                out.append(a)
        # alias della colonna CSV `alias` (definiti nei dati)
        for p in profs:
            if p:
                out.extend(cfg.alias_names_for(p))
    seen: set[str] = set()
    res: list[str] = []
    for n in out:
        if n not in seen:
            seen.add(n)
            res.append(n)
    return res


def _view_for(request: Request, auth) -> str:
    """Vista richiesta. Una chiave di profilo vede SEMPRE `stable`: il
    parametro non puo' farla uscire dal proprio profilo."""
    if auth.mode != "master":
        return "stable"
    view = request.query_params.get("view", "").strip().lower()
    return view if view in VIEWS else "uniques"


def _deps_for_name(name: str) -> list[dict]:
    """Deployment che compongono un nome: unique, gruppo, alias o nome base."""
    import app.main as M
    cfg = M.config
    dep = cfg.deployment_by_unique(name)
    if dep is not None:
        return [dep]
    if name in cfg.groups:
        return list(cfg.groups[name])
    if name in cfg.alias_groups:
        out: list[dict] = []
        for g in sorted(cfg.alias_groups[name]):
            out.extend(cfg.groups.get(g, ()))
        return _dedup_deps(out)
    out = []
    for g, deps in cfg.groups.items():
        if g.startswith(name + "-"):
            out.extend(deps)
    return _dedup_deps(out)


def _dedup_deps(deps) -> list[dict]:
    seen: set[str] = set()
    res: list[dict] = []
    for d in deps:
        u = d.get("unique")
        if u not in seen:
            seen.add(u)
            res.append(d)
    return res


def _caps_and_deps(name: str) -> tuple[set[str], list[dict]]:
    """(capability, deployment) di un nome.

    Fonte di verità: MEMBERSHIP (`dep["caps"]`) quando presente; altrimenti la
    mappa advisory `capability_routing.model_capabilities`."""
    import app.main as M
    target = M.policy.aliases.get(name, name)
    deps = _deps_for_name(target)
    caps: set[str] = set()
    for d in deps:
        member = d.get("caps") or frozenset()
        caps |= member if member else M.policy.caps_for(d["model"])
    return caps, deps


def _model_entry(name: str, *, rich: bool = True) -> dict:
    """Entry modello standard OpenAI + capability nei formati che i client
    leggono (vedi `capmeta`).

    `rich=False` (vista `uniques`, cioè elenco di oltre 1600 deployment) tiene
    solo `capabilities` + `architecture`: i campi più grandi sono ridondanti
    per un deployment singolo e gonfierebbero la risposta di diverse volte.
    """
    import app.main as M
    entry = {
        "id": name,
        "object": "model",
        "created": int(time.time()),
        "owned_by": M.policy.service_name,
        "reasoning_effort": ["default", "low", "medium", "high"],
        "reasoning_effort_default": "default",
    }
    caps, deps = _caps_and_deps(name)
    if not caps:
        return entry
    entry["capabilities"] = capmeta.ollama_capabilities(caps)
    entry["architecture"] = capmeta.modalities(caps)
    if not rich:
        return entry
    entry["capabilities_sx"] = capmeta.structured_style(caps)
    entry["supported_parameters"] = capmeta.supported_parameters(caps)
    entry["modalities"] = capmeta.llama_cpp_modalities(caps)
    ctx_max, ctx_min = capmeta.context_lengths(deps)
    if ctx_max:
        entry["context_length"] = ctx_max
        entry["context_length_min"] = ctx_min
        entry["top_provider"] = {"max_completion_tokens": None}
    entry.update(capmeta.litellm_style(caps))
    if ctx_max:
        entry["max_input_tokens"] = ctx_max
    return entry


@router.get("/v1/models")
async def list_models(request: Request):
    import app.main as M
    auth = M.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    view = _view_for(request, auth)
    rich = view == "stable"
    names = sorted(set(_names_for_auth(auth, view)))
    return {"object": "list", "data": [_model_entry(n, rich=rich) for n in names]}


# ------------------------------------------------ compat endpoints (404 fixes)
def _visible_model_names(request: Request):
    """(names, auth). Stessa logica di visibilità di /v1/models ma solo i nomi."""
    import app.main as M
    auth = M.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return None, auth
    if auth.mode == "master":
        # superficie completa richiamabile: nome base + gruppi + univoci di
        # OGNI profilo (non solo gli univoci, cosi' `scrocco-llm-<profilo>` e i
        # gruppi -Nk/-go/... sono riconosciuti da /v1/models/{id} e /api/show)
        names = []
        for p in M.config.profiles:
            names.extend(M.config.whitelist_for(p))
        allowed = None
    else:
        names = list(M.config.whitelist_for(auth.profile or ""))
        allowed = set(names)
    for a, t in M.policy.aliases.items():
        if allowed is None or t in allowed:
            names.append(a)
    # Alias della colonna `alias` (nomi richiamabili per modello/gruppo):
    # visibili come i `policy.aliases`, ma definiti nei dati (CSV) e
    # limitati al profilo dell'autenticazione (master: tutti).
    if auth.mode == "master":
        for p in M.config.profiles:
            names.extend(M.config.alias_names_for(p))
    else:
        names.extend(M.config.alias_names_for(auth.profile or ""))
    # de-dup preservando l'ordine
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out, auth
