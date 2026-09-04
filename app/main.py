"""scrocco-llm — gateway LLM OpenAI-compatible multi-provider (FastAPI :4001).

[IT] COSA: espone /v1/chat/completions (+ images/tts/stt/videos, models,
metrics) e instrada ogni richiesta al deployment migliore tra decine di
account/provider. HOW: auth -> canonicalize alias -> stima contesto ->
gruppo dims/capacita -> pick adattivo -> forwarder con fallback a catena
-> QC/watchdog -> risposta (stream o JSON). WHY le decisioni chiave:
  - routing per CONTESTO STIMATO (-32k..-1000k): nei free-tier le finestre
    sono piccole; serve il modello minimo che CI STA, non il migliore
    assoluto (che rifiuterebbe o taglierebbe).
  - gruppi capacita strutturali (-vision/-tts/...): un fallback di scopo
    non deve mai atterrare su un modello senza la capac richiesta.
  - sticky session SOLO dal routing automatico: gli espliciti sono legge.
  - watchdog passivo sullo stream (tier1 vuoto/error, tier2 no-[DONE]):
    non tocca i byte, rileva solo upstream mezzi morti.
  - [summary] per-richiesta: osservabilita senza grep sparsi.
Doc vivente: docs/AGENT.md (day-2), docs/BOOTSTRAP.md (setup),
GET /admin/guide e GET /bootstrap (serviti dal gateway stesso).

[EN] WHAT: OpenAI-compatible gateway fanning requests across many provider
accounts. HOW: auth -> alias -> ctx estimate -> capability group ->
adaptive pick -> chained fallback -> QC/watchdog. WHY: minimum-context
routing beats best-model routing on free tiers; purpose-aware fallback;
passive stream watchdog; per-request summary logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (JSONResponse, PlainTextResponse, Response,
                               StreamingResponse)

from .admin import admin_api
from .bootstrap import bootstrap_api
from .auth import AuthManager, AuthResult
from . import journal, metrics
from .config import GatewayConfig, csv_mtime_ns, maybe_reload
from .forwarder import (Forwarder, MODEL_MISSING_COOLDOWN_S,
                        PROVIDER_TRANSIENT_COOLDOWN_S, UpstreamError,
                        _MODEL_MISSING_RE, _PAYLOAD_SCHEMA_RE,
                        _PROVIDER_TRANSIENT_RE,
                        _THOUGHT_SIG_RE, is_provider_error_body,
                        media_reject_signature)
from .health import health_loop
from .policy import Policy
from .qc import annotate_reasoning
from .router import Router, inject_identity, estimate_tokens
from .capabilities import required_caps, count_image_parts

# Logging strutturato: [auth] [route] [vigile] [identity] [fallback] [cooldown]
# basicConfig è no-op se root ha già handler (es. sotto pytest/caplog).
# Formato con colori per terminali (ANSI escape codes)
from app.terminal_logging import (
    ColoredFormatter,
    setup_colored_logging,
    colorize_tag,
    colorize_level,
    is_terminal_stream,
)

console_handler = setup_colored_logging()

logging.basicConfig(level=logging.INFO,
                    handlers=[console_handler],
                    force=True)  # Override any existing basicConfig
log = logging.getLogger("nx.main")

BASE_DIR = Path(__file__).resolve().parent.parent
# I DATI (credenziali + policy) vivono in var/: directory bind-montata nel
# container Docker, così l'admin API scrive i file VERO dell'host.
VAR_DIR = BASE_DIR / "var"
CSV_PATH = Path(os.environ.get("GATEWAY_CSV", VAR_DIR / "keys_rotation.csv"))
POLICY_PATH = Path(os.environ.get("GATEWAY_POLICY",
                                  VAR_DIR / "gateway.yaml"))
PORT = int(os.environ.get("GATEWAY_PORT", "4001"))
# 127.0.0.1 di default (loopback-only); nel container vale 0.0.0.0
HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
WATCH_SECONDS = float(os.environ.get("GATEWAY_WATCH_SECONDS", "5"))


def _install_file_logging() -> None:
    """Handler su FILE oltre allo stdout (docker logs resta invariato).

    - var/gateway.log  : tutto il log INFO (bind-montato -> sopravvive al
      redeploy del container, dove lo stdout viene perso).
    - var/error-audit.log : SOLO i body upstream con "error" (logger
      nx.erroraudit, alimentato da forwarder.UpstreamError + le righe
      PASS-THROUGH). File LOCALE, gitignored (var/*), da rivedere ogni tanto.
    Fail-safe: se un path non e' scrivibile si prosegue col solo stdout.
    Saltato sotto pytest (PYTEST_CURRENT_TEST) per non sporcare il repo.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    from logging.handlers import RotatingFileHandler
    # Same format as console for consistency
    fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
    mb = int(os.environ.get("GATEWAY_LOG_MAX_MB", "20"))
    bk = int(os.environ.get("GATEWAY_LOG_BACKUPS", "5"))
    main_path = os.environ.get("GATEWAY_LOG_FILE", str(VAR_DIR / "gateway.log"))
    audit_path = os.environ.get("GATEWAY_ERROR_LOG_FILE",
                                str(VAR_DIR / "error-audit.log"))
    try:
        h = RotatingFileHandler(main_path, maxBytes=mb * 1024 * 1024,
                                backupCount=bk, encoding="utf-8")
        h.setFormatter(fmt)
        h.setLevel(logging.INFO)
        logging.getLogger().addHandler(h)
    except OSError as exc:                       # noqa: BLE001
        log.warning("[log] file %s non scrivibile (%s): solo stdout",
                    main_path, exc)
    try:
        ah = RotatingFileHandler(audit_path, maxBytes=mb * 1024 * 1024,
                                 backupCount=bk, encoding="utf-8")
        ah.setFormatter(fmt)
        ah.setLevel(logging.INFO)
        eaudit = logging.getLogger("nx.erroraudit")
        eaudit.addHandler(ah)
        eaudit.propagate = True                  # va anche in gateway.log/stdout
    except OSError as exc:                       # noqa: BLE001
        log.warning("[log] file %s non scrivibile (%s)", audit_path, exc)


_install_file_logging()

# La POLITICA (gateway.yaml) è separata dalle CREDENZIALI (keys_rotation.csv):
# file assente/corrotto -> default, il servizio parte comunque.
# I nomi pubblici usano policy.proxy_prefix: nessun nome è hardcodato qui.
policy = Policy.load_or_default(POLICY_PATH)
config = GatewayConfig(CSV_PATH, proxy_prefix=policy.proxy_prefix,
                       go_suffix=policy.go_suffix,
                       fallback_suffix=policy.fallback_suffix,
                       extra_prefixes=policy.legacy_prefixes)
router = Router(config, policy)
# provider callable: l'hot-reload della policy aggiorna anche le chiavi client
authn = AuthManager(config, client_keys_provider=lambda: policy.client_keys)
forwarder = Forwarder()

_watch_task: asyncio.Task | None = None
_health_task: asyncio.Task | None = None
_stats_file = VAR_DIR / "adaptive_stats.json"
_last_stats_save = 0.0
# job video asincroni (OR-style): job_id -> snapshot deployment per poll/content.
# MAPPING IN MEMORIA con TTL: al restart i job in corso si perdono -> 404 con hint.
_videos_jobs: dict[str, dict] = {}
VIDEO_JOB_TTL_SEC = 24 * 3600
# i TEST settano GATEWAY_PERSIST_STATS=0: nessuna contaminazione col live
PERSIST_STATS = os.environ.get("GATEWAY_PERSIST_STATS", "1") != "0"

# Ledger usage/costi (Feature: /admin/insights). Stessa env dei test per non
# sporcare var/ reale durante la suite.
from .ledger import Ledger as _Ledger
LEDGER = _Ledger(VAR_DIR)

# Evidenza persistente salute chiavi (lifecycle dead/retired, no-delete).
from .keyhealth import KeyHealth as _KeyHealth
KEYHEALTH = _KeyHealth(VAR_DIR)


def _load_adaptive_stats() -> None:
    """All'avvio: ripristina EMA/last_used/cooldown dal file (F4)."""
    if not PERSIST_STATS:
        return
    try:
        if _stats_file.exists():
            import json
            router.load_stats(json.loads(
                _stats_file.read_text(encoding="utf-8")))
            log.info("[stats] ripristinate da %s (%d deployment tracciati)",
                     _stats_file.name, len(router._stats))
    except Exception as exc:                 # mai bloccare lo startup
        log.warning("[stats] load fallito (%s): riparto pulito", exc)


def _maybe_save_adaptive_stats(force: bool = False) -> None:
    """Salvataggio atomico throttled (max ogni 60s) delle stats adattive."""
    global _last_stats_save
    if not PERSIST_STATS:
        return
    now = time.time()
    if not force and now - _last_stats_save < 60:
        return
    _last_stats_save = now
    try:
        import json
        import tempfile
        _stats_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(VAR_DIR), suffix=".tmp.json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(router.dump_stats(), f)
        os.replace(tmp_name, _stats_file)     # atomico
    except Exception as exc:                 # best-effort, mai fatali
        log.debug("[stats] save fallito (%s)", exc)


async def _watcher(interval: float) -> None:
    """Ogni `interval` secondi controlla mtime di CSV (credenziali) e
    gateway.yaml (policy) e ricarica ciò che è cambiato.

    Un file corrotto/mancante NON abbatta il servizio: lo stato vecchio resta vivo.
    """
    last_csv: int | None = None
    last_yaml: int | None = None
    while True:
        try:
            router.purge_expired()      # igiene: sticky/cooldown scaduti
            _maybe_save_adaptive_stats()
            LEDGER.flush()              # ledger usage: buffer -> jsonl
            # keyhealth: osserva TUTTI i deployment con stats e aggiorna
            # l'evidenza su disco (throttled dal tick stesso)
            try:
                now = time.time()
                for u, s in list(router._stats.items()):
                    cooled = router._cooldown.get(u, 0) > now
                    KEYHEALTH.observe(u, fail_streak=s.fail_streak,
                                      success_ema=s.success_ema,
                                      is_cooled=cooled, now=now)
                new_retired = KEYHEALTH.apply_retirement(
                    policy.retire_after_days)
                if new_retired:
                    log.warning("[keyhealth] %d chiavi passate RETIRED: %s",
                                len(new_retired), ", ".join(new_retired[:5]))
                KEYHEALTH.save()
            except Exception:           # analytics non deve mai mordere
                log.debug("[keyhealth] tick error", exc_info=True)
            # purge job video scaduti (mapping in memoria, TTL 24h)
            now = time.time()
            expired = [j for j, m in _videos_jobs.items()
                       if now - m.get("created", 0) > VIDEO_JOB_TTL_SEC]
            for j in expired:
                _videos_jobs.pop(j, None)

            new = maybe_reload(config, last_csv)
            if last_csv is not None and new != last_csv:
                log.info("[config] CSV ricaricato: profili=%s deployment=%d",
                         ",".join(config.profiles),
                         sum(len(v) for v in config.groups.values()))
            last_csv = new

            ym = csv_mtime_ns(POLICY_PATH)
            if ym is not None and last_yaml is not None and ym != last_yaml:
                try:
                    fresh = Policy.load(POLICY_PATH)
                except Exception as exc:
                    log.warning("[policy] reload FALLITO (%s): resta la "
                                "precedente", exc)
                else:
                    router.policy = fresh          # swap atomico dei riferimenti
                    globals()["policy"] = fresh
                    log.info("[policy] ricaricata: step_up=%s%% aliases=%d "
                             "per-profilo=%s", fresh.step_up_pct,
                             len(fresh.aliases),
                             fresh.profile_step_up_pct or "-")
            if ym is not None and last_yaml is None:
                log.info("[policy] watcher: baseline %s", POLICY_PATH.name)
            last_yaml = ym
        except Exception as exc:            # mai far morire il watcher
            log.warning("[config] watcher error: %s", exc)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _watch_task
    _load_adaptive_stats()                  # F4: ripristino EMA/cooldown
    _maybe_save_adaptive_stats(force=True)  # baseline subito
    _watch_task = asyncio.create_task(_watcher(WATCH_SECONDS))
    _health_task = asyncio.create_task(
        health_loop(router, policy.health_interval_sec))
    log.info("[start] %s su %s:%d · profili=%s · deployment=%d",
             policy.service_name, HOST, PORT, ",".join(config.profiles),
             sum(len(v) for v in config.groups.values()))
    try:
        yield
    finally:
        for task in (_watch_task, _health_task):
            if task:
                task.cancel()
        await forwarder.aclose()
        _maybe_save_adaptive_stats(force=True)   # F4: salva allo shutdown
        LEDGER.flush()                           # ledger: nessuna riga persa


app = FastAPI(title=policy.service_name, version="0.2.0", lifespan=lifespan)
app.include_router(admin_api)
app.include_router(bootstrap_api)


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"error": {"message": detail,
                           "type": "auth_error",
                           "param": None, "code": "401"}})


def _forbidden(model: str, profile: str | None) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"error": {"message":
                           f"Model '{model}' not allowed for this key"
                           + (f" (profile '{profile}')" if profile else ""),
                           "type": "permission_error",
                           "param": None, "code": "403"}})


# ------------------------------------------------------------------- health
@app.get("/health/liveliness")
async def liveliness() -> PlainTextResponse:
    return PlainTextResponse("I'm alive!")


@app.get("/healthz")
async def healthz() -> dict:
    # Conti delle capacità tra tutti i deployment
    cap_counts = {cap: 0 for cap in frozenset({"text", "vision", "video", "audio", "image_gen", "tools"})}
    for deps in config.groups.values():
        for dep in deps:
            model = dep.get("model", "")
            caps = policy.caps_for(model)
            for c in caps:
                cap_counts[c] = cap_counts.get(c, 0) + 1

    return {
        "status": "ok",
        "profiles": config.profiles,
        "groups": len(config.groups),
        "deployments": sum(len(v) for v in config.groups.values()),
        "cooldowns": len(router._cooldown),
        "sticky_sessions": len(router._sticky),
        "port": PORT,
        "policy": {
            "file": POLICY_PATH.name,
            "step_up_pct": policy.step_up_pct,
            "step_up_per_profile": {k: f"{v}%" for k, v
                                     in policy.profile_step_up_pct.items()},
            "speed_hotwords": len(policy.speed_hotwords),
            "speed_min_dim_k": policy.speed_min_dim_k,
            "aliases": len(policy.aliases),
            "capability_routing_enabled": policy.routing_active(),
            "capabilities_configured": len(policy.model_capabilities),
        },
        "capabilities_summary": {
            "total_deployments": sum(cap_counts.values()),
            "per_capability": {k: cap_counts[k] for k in sorted(cap_counts)},
        },
    }


@app.get("/metrics")
async def metrics_endpoint():
    """Formato testo Prometheus. Loopback-only come tutto il servizio."""
    metrics.set_gauge("nx_cooldown_active", len(router._cooldown))
    metrics.set_gauge("nx_sticky_active", len(router._sticky))
    return PlainTextResponse(metrics.render(),
                             media_type="text/plain; version=0.0.4")


@app.post("/admin/reload")
async def admin_reload(request: Request):
    auth = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok or auth.mode != "master":
        return _unauthorized(auth.error or "admin only")
    config.reload()
    fresh = Policy.load_or_default(POLICY_PATH)
    router.policy = fresh
    globals()["policy"] = fresh
    return {"reloaded": True, "profiles": config.profiles,
            "deployments": sum(len(v) for v in config.groups.values()),
            "policy": {"step_up_pct": fresh.step_up_pct,
                       "aliases": len(fresh.aliases)}}


# ------------------------------------------------------------------- models
@app.get("/v1/models")
async def list_models(request: Request):
    auth = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)

    if auth.mode == "master":
        allowed: set[str] | None = None          # tutto
        names: list[str] = []
        for g, deps in config.groups.items():
            names.extend(d["unique"] for d in deps)
    else:
        wl = config.whitelist_for(auth.profile or "")
        allowed = set(wl)
        names = list(wl)

    # gli ALIAS configurati sono nomi pubblici a tutti gli effetti:
    # visibili solo se il loro target è usabile dalla chiave
    usable_aliases = sorted(
        a for a, t in policy.aliases.items()
        if allowed is None or t in allowed)

    def _model_entry(name: str, is_alias: bool = False) -> dict:
        """Costruisce l'entry modello con capacità risolte.

        Fonte di verità: MEMBERSHIP (dep["caps"]) quando presente;
        altrimenti la mappa advisory capability_routing.model_capabilities."""
        entry = {"id": name, "object": "model",
                 "created": int(time.time()), "owned_by": policy.service_name}
        target = policy.aliases.get(name, name) if is_alias else name

        def _caps_of_dep(d: dict) -> frozenset:
            member = d.get("caps") or frozenset()
            return member if member else policy.caps_for(d["model"])

        caps: set[str] = set()
        dep = config.deployment_by_unique(target)
        if dep:
            caps |= _caps_of_dep(dep)
        elif target in config.groups:
            for d in config.groups[target]:
                caps |= _caps_of_dep(d)
        else:
            for g, deps in config.groups.items():
                if g.startswith(target + "-"):
                    for d in deps:
                        caps |= _caps_of_dep(d)
        if caps:
            entry["capabilities"] = sorted(caps)
        return entry

    all_names = sorted(set(names))
    data = [_model_entry(n, False) for n in all_names]
    data += [_model_entry(a, True) for a in usable_aliases]
    return {"object": "list", "data": data}


# ------------------------------------------------ compat endpoints (404 fixes)
def _visible_model_names(request: Request):
    """(names, auth). Stessa logica di visibilità di /v1/models ma solo i nomi."""
    auth = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return None, auth
    if auth.mode == "master":
        # superficie completa richiamabile: nome base + gruppi + univoci di
        # OGNI profilo (non solo gli univoci, cosi' `scrocco-llm-<profilo>` e i
        # gruppi -Nk/-go/... sono riconosciuti da /v1/models/{id} e /api/show)
        names = []
        for p in config.profiles:
            names.extend(config.whitelist_for(p))
        allowed = None
    else:
        names = list(config.whitelist_for(auth.profile or ""))
        allowed = set(names)
    for a, t in policy.aliases.items():
        if allowed is None or t in allowed:
            names.append(a)
    # de-dup preservando l'ordine
    seen = set(); out = []
    for n in names:
        if n not in seen:
            seen.add(n); out.append(n)
    return out, auth


def _model_obj(name: str) -> dict:
    return {"id": name, "object": "model", "created": int(time.time()),
            "owned_by": policy.service_name}


def _ollama_entry(name: str) -> dict:
    return {"name": name, "model": name,
            "modified_at": "1970-01-01T00:00:00Z", "size": 0, "digest": "",
            "details": {"parent_model": "", "format": "gguf", "family": "",
                        "families": None, "parameter_size": "",
                        "quantization_level": ""}}


@app.get("/v1/models/{model_id:path}")
async def retrieve_model(model_id: str, request: Request):
    """OpenAI 'retrieve model'. 200 se il modello è gestito (nome diretto,
    alias, o canonicalizzabile a un gruppo/base noto), altrimenti 404."""
    names, auth = _visible_model_names(request)
    if names is None:
        return _unauthorized(auth.error)
    canon = policy.canonicalize(model_id)
    known = (model_id in names or canon in names
             or canon in config.groups
             or config.deployment_by_unique(canon) is not None
             or any(canon == a or canon == policy.aliases.get(a)
                    for a in policy.aliases))
    if not known:
        return JSONResponse(status_code=404, content={"error": {
            "message": f"model '{model_id}' not found",
            "type": "invalid_request_error",
            "code": "model_not_found"}})
    return _model_obj(model_id)


@app.get("/api/tags")
async def ollama_tags(request: Request):
    """Ollama: lista modelli."""
    names, auth = _visible_model_names(request)
    if names is None:
        return _unauthorized(auth.error)
    return {"models": [_ollama_entry(n) for n in names]}


@app.get("/api/v1/models")
async def api_v1_models(request: Request):
    """Alias non-standard di /v1/models usato da alcuni client."""
    names, auth = _visible_model_names(request)
    if names is None:
        return _unauthorized(auth.error)
    return {"object": "list", "data": [_model_obj(n) for n in names]}


@app.post("/api/show")
async def ollama_show(request: Request):
    """Ollama: dettagli modello. Stub statico + capabilities minime."""
    names, auth = _visible_model_names(request)
    if names is None:
        return _unauthorized(auth.error)
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = (body.get("name") or body.get("model") or "") if isinstance(body, dict) else ""
    canon = policy.canonicalize(name) if name else ""
    known = bool(name) and (name in names or canon in names or canon in config.groups
                            or config.deployment_by_unique(canon) is not None)
    if name and not known:
        return JSONResponse(status_code=404, content={"error": {
            "message": f"model '{name}' not found",
            "type": "invalid_request_error"}})
    return {"license": "", "modelfile": "", "parameters": "", "template": "",
            "details": _ollama_entry(name or policy.service_name)["details"],
            "model_info": {}, "capabilities": ["completion", "chat"]}


@app.get("/api/version")
async def ollama_version():
    return {"version": app.version}


@app.get("/version")
async def llamacpp_version():
    return {"version": app.version}


@app.get("/props")
async def llamacpp_props():
    """llama.cpp server props: stub minimo ma valido."""
    return {"default_generation_settings": {"n_ctx": 0},
            "total_slots": 1, "chat_template": "",
            "model_path": policy.service_name, "build_info": f"nx {app.version}"}


@app.get("/v1/props")
async def llamacpp_props_v1():
    """llama.cpp server props (prefisso /v1): stesso stub di /props."""
    return {"default_generation_settings": {"n_ctx": 0},
            "total_slots": 1, "chat_template": "",
            "model_path": policy.service_name, "build_info": f"nx {app.version}"}


# ---------------------------------------------------------- chat completions
def _session_id(request: Request, payload: dict) -> str | None:
    sid = request.headers.get("x-session-id")
    if sid:
        return sid
    md = payload.get("metadata") or {}
    sid = payload.get("user") or md.get("session_id")
    return str(sid) if sid else None


def _client_ip(request: Request) -> str:
    """IP del client (rispetta X-Forwarded-For se dietro proxy)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else ""


def _opencode_session(request: Request) -> str | None:
    """Header x-opencode-session in arrivo dal client (passthrough):
    se presente, questo valore PREVALE su ogni hash calcolato al volo."""
    v = (request.headers.get("x-opencode-session") or "").strip()
    return v or None


def _emit_summary(**f) -> None:
    """Riga [summary] JSON a fine richiesta (osservabilità per-request).

    Campi tipici: ses, req, grp, dep, tries, fb, dur_ms, stream, qc, wd,
    ttfb_ms, usage{prompt_tokens,completion_tokens,total_tokens,cost}.

    STESSA riga alimenta il LEDGER persistente (var/usage_ledger.jsonl):
    una sola fonte di verita' per log e analytics (/admin/insights).
    Il profilo e' deducibile dal gruppo (<prefix><profilo>-...); se manca
    (gruppi cap senza dims) si prova il campo esplicito "profile".
    """
    try:
        if "via" not in f and f.get("dep"):
            d = router.config.deployment_by_unique(f["dep"])
            if d:
                base = d.get("api_base", "")
                if "://" in base:
                    base = base.split("://", 1)[1].split("/", 1)[0]
                f["via"] = base
        log.info("[summary] %s",
                 json.dumps(f, ensure_ascii=False, separators=(",", ":"),
                            default=str))
    except Exception:                        # mai bloccare la risposta per un log
        pass
    try:
        grp = str(f.get("grp") or "")
        prof = f.get("profile")
        if not prof:
            for p in config.profiles:        # match esatto sul segmento
                if grp.startswith(config.proxy_prefix + p + "-"):
                    prof = p
                    break
        dep_unique = str(f.get("dep") or "")
        model = ""
        d = config.deployment_by_unique(dep_unique)
        if d:
            model = d["model"]
        LEDGER.record({
            "ses": f.get("ses"), "profile": prof, "req": f.get("req"),
            "grp": grp or None, "dep": dep_unique or None, "model": model,
            "tries": f.get("tries"), "fb": f.get("fb"),
            "dur_ms": f.get("dur_ms"), "stream": f.get("stream", False),
            "qc": f.get("qc", False), "wd": f.get("wd"),
            "ttfb_ms": f.get("ttfb_ms"), "kind": f.get("kind", "chat"),
            "status": f.get("status"),
            "usage": f.get("usage") if isinstance(f.get("usage"), dict)
            else None,
        }, pricing=policy.pricing, upstream_model=model)
    except Exception:                        # analytics non deve mai mordere
        pass


def _usage_of(data) -> dict | None:
    """Estrae usage/costo da una risposta upstream OpenAI-style (se presente)."""
    if not isinstance(data, dict):
        return None
    u = data.get("usage")
    if not isinstance(u, dict):
        return None
    out = {k: u.get(k) for k in ("prompt_tokens", "completion_tokens",
                                 "total_tokens") if u.get(k) is not None}
    cost = u.get("cost")
    if isinstance(cost, dict):
        out["cost"] = cost.get("total_cost")
    elif cost is not None:
        out["cost"] = cost
    return out or None


# --------------------------------------------------- auto-learn capacità
def _auto_learn_apply(model: str, cap: str, evidence: str, count: int) -> None:
    """Registra il SUGGERIMENTO (mode=suggest, default) o applica la rimozione
    della membership (mode=auto) per il (modello, capà) colpito."""
    mode = router.policy.cap_auto_learn
    if mode == "off":
        return
    if mode == "suggest":
        # righe candidate alla rimozione del token cap (membership CSV)
        try:
            from .admin import membership_removal_candidates as _mrc
            candidates = _mrc(model, cap)
        except Exception:
            candidates = []
        journal.record(VAR_DIR, "cap_learn_suggest",
                       {"model": model, "cap": cap, "count": count,
                        "evidence": evidence[:200],
                        "candidates": candidates,
                        "suggested_ops": [{"action": "update", "id": c["id"],
                                           "caps": ",".join(
                                               [t for t in c["caps"] if t != cap])}
                                          for c in candidates]})
        log.warning("[caps][suggest] %s rifiuta '%s' (%d strike): %d righe "
                    "candidate alla rimozione del token (GET /admin/history)",
                    model, cap, count, len(candidates))
        return
    try:
        from .admin import remove_cap_for_model
        remove_cap_for_model(model=model, cap=cap, evidence=evidence,
                             count=count)
    except Exception as exc:                 # noqa: BLE001
        log.error("[caps][auto-learn] applicazione fallita %s/%s: %s",
                  model, cap, exc)


def _strike_hook(explicit: bool, need=frozenset()):
    """Hook per il forwarder/loop endpoint: attribuisce i rifiuti modalità al
    modello upstream. MAI su richieste esplicite (il client le ha volute), mai
    con routing disattivato; conta solo cap dichiarate dal modello colpito."""
    def hook(model: str, detail: str) -> None:
        pol = router.policy
        if explicit or not pol.routing_active() or pol.cap_auto_learn == "off":
            return
        from .forwarder import media_reject_signature
        if not media_reject_signature(detail):
            return
        declared = pol.caps_for(model)
        active = {c for c in need if c != "text" and c != "tools"
                  and c in declared}
        if not active:
            return
        hits = router.note_cap_strike(model, active, detail)
        for cap in hits:
            cnt = next((s["count"] for s in router.cap_strikes_view()
                        if s["model"] == model and s["cap"] == cap), 0)
            _auto_learn_apply(model, cap, detail, cnt)
    return hook


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _api_log = logging.getLogger("nx.api")
    try:
        payload = await request.json()
    except Exception as exc:
        # FIX: Log error details for debugging; previously blank exception handler
        _api_log.warning("[api] invalid JSON body: %s", exc)
        return JSONResponse(status_code=400, content={
            "error": {"message": "invalid JSON body", "type": "invalid_request_error"}})

    raw_model = payload.get("model") or ""
    messages = payload.get("messages") or []
    stream = bool(payload.get("stream"))

    # --- normalizzazione del nome richiesto:
    #     1) prefisso STORICO -> prefisso corrente (compatibilità client)
    #     2) alias (gateway.yaml) -> nome canonico
    model = policy.canonicalize(raw_model)

    # --- auth ---
    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)

    # --- autorizzazione modello (whitelist tre livelli, sul nome canonico) ---
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    # --- capability detection ---
    # capacità richieste dal payload; OGNI chat produce testo -> "text" è sempre
    # implicita: i modelli solo-tts/stt/image_gen escono dal pool chat automatico
    # (gli espliciti passano comunque; kill-switch: capability_routing.enabled=false)
    if router.policy.routing_active():
        need = required_caps(payload) | {"text"}
    else:
        need = frozenset()

    # --- routing ---
    session_id = _session_id(request, payload)
    ctx_est = estimate_tokens(messages, router.policy.estimate_divisor,
                              getattr(router.policy, "image_token_estimate", 0) or 0,
                              tools=payload.get("tools"))

    group_or_explicit = router.resolve_group_for_request(model, messages,
                                                         session_id, need,
                                                         ctx_est)
    if group_or_explicit is None:
        if need:
            for c in sorted(need):
                metrics.inc("nx_caps_unroutable_total", (c,))
            return JSONResponse(status_code=400, content={
                "error": {"message": f"nessun deployment dichiara le capacità richieste: {sorted(need)}. "
                                     f"Configura capability_routing.model_capabilities in gateway.yaml",
                         "type": "invalid_request_error"}})
        return JSONResponse(status_code=404, content={
            "error": {"message": f"model '{model}' not managed by "
                                 f"{policy.service_name}",
                      "type": "invalid_request_error"}})

    explicit_req = router.is_explicit(model)
    dep = router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        # ESPLICITO: nessun filtro (la lettera della richiesta vince); il retry
        # ruota solo nel gruppo. BASE: need+ctx con catena del mondo scelta da
        # initial_pick (dims per testo, cap-chain per -C).
        dep = router.initial_pick(auth.profile, group_or_explicit,
                                  None if explicit_req else need,
                                  None if explicit_req else ctx_est)
    if dep is None:
        return JSONResponse(status_code=503, content={
            "error": {"message": "nessun deployment disponibile"
                      + (" per le capacità richieste" if not explicit_req else ""),
                      "type": "server_error"}})

    # override chiave: alias GENERICO con chiave custom (policy.alias_keys):
    # sostituisce SOLO dep["api_key"]. Vale solo per il PRIMO tentativo —
    # i fallback successivi tornano al pool normale del profilo, così una
    # chiave rotta non blocca mai il servizio.
    custom_key = router.resolve_alias_key(raw_model, model)
    if custom_key:
        dep = {**dep, "api_key": custom_key}

    profile = auth.profile or config.profile_of_base(model.split("__")[0]) \
        or config.profile_of_base(model)

    if model != raw_model:
        log.info("[route] alias %r -> %r", raw_model, model)
    metrics.inc("nx_requests_total",
                (raw_model[:60], str(bool(payload.get("stream")))))
    metrics.inc("nx_group_total", (dep["group"],))
    for c in sorted(need):
        metrics.inc("nx_caps_requests_total", (c,))
    log.info("[route] %s -> %s (ctx≈%d tok%s, need=%s, session=%s, stream=%s)",
             model, dep["group"],
             ctx_est,
             f"+{count_image_parts(messages)}img" if count_image_parts(messages) else "",
             sorted(need) if need else "-",
             session_id or "anonima", bool(payload.get("stream")))

    # sticky session SOLO dal routing automatico (nome base): le richieste
    # esplicite (-Nk/-go/-fallback/__univoco) non leggono né scrivono sticky
    if session_id and not router.is_explicit(model):
        router.sticky_set(session_id, group_or_explicit)

    # iniezione identità + modello univoco nel payload upstream
    inject_identity(payload, dep, router=router)
    t_req = time.monotonic()
    # sessione OpenCode: passthrough se il client la invia, altrimenti header
    # calcolato al volo nel forwarder (hash api_key+client_ip)
    _sess = _opencode_session(request)
    _cip = _client_ip(request)

    if stream:
        return await _stream_with_fallback(
            profile, dep, payload, need,
            hook=_strike_hook(explicit_req, need),
            scope="group" if explicit_req else "chain",
            ctx=ctx_est,
            ses=session_id, req=raw_model,
            session=_sess, client_ip=_cip)

    qc_pol = router.policy.qc_json
    attempts_box: list[str] = []
    try:
        res = await forwarder.call_with_fallback(
            router, profile, dep, payload,
            collect_qc_failures=bool(qc_pol.enabled or router.policy.qc_sanity.enabled),
            media_strike_hook=_strike_hook(explicit_req, need),
            need=need,
            scope="group" if explicit_req else "chain",
            ctx=ctx_est,
            attempts_box=attempts_box,
            session=_sess, client_ip=_cip)
    except UpstreamError as err:
        # errore azionabile -> status vero; catena esaurita / nessun output
        # utile -> 503 RETRYABLE (mai un turno finto verso il client).
        if _actionable_upstream_error(err) and err.status:
            st = abs(err.status)
            return JSONResponse(status_code=st if st >= 400 else 502,
                                content={"error": {"message": err.detail,
                                                   "type": "upstream_error"}})
        _emit_summary(ses=session_id or "-", req=raw_model,
                      grp=dep.get("group"),
                      dep=(attempts_box[-1] if attempts_box
                           else dep.get("unique")),
                      tries=max(1, len(attempts_box)),
                      fb=max(0, len(attempts_box) - 1),
                      dur_ms=int((time.monotonic() - t_req) * 1000),
                      stream=False, qc=True, wd="chain-exhausted", usage=None)
        return _exhausted(len(attempts_box), err.detail)
    data, used = res[0], res[1]
    qc_failed = res[2] if len(res) > 2 else []

    # Divulgazione del modello nel campo "model" della risposta
    # (policy.response_model, vedi app/policy.py): nx_deployment è SEMPRE
    # presente con il deployment univoco realmente usato.
    if isinstance(data, dict):
        disc = router.policy.response_model
        if disc == "upstream":
            # nome ESATTO scritto dal provider nella sua risposta
            # (es. groq ritorna "meta-llama/llama-3.3-70b-instruct");
            # fallback al nome che noi inviamo se il campo manca/vuoto
            orig = data.get("model")
            data["model"] = (orig if isinstance(orig, str) and orig.strip()
                             else used["model"])
        elif disc == "deployment":
            data["model"] = used["unique"]
        else:                                   # requested (storico)
            data["model"] = raw_model
        data["nx_deployment"] = used["unique"]
    # nota QC nel reasoning (D3): solo se ci sono stati scarti e la policy
    # lo consente — il client che ignora reasoning_content non ne è toccato
    if qc_failed and qc_pol.annotate_reasoning and isinstance(data, dict):
        data = annotate_reasoning(data, qc_failed)
    _emit_summary(ses=session_id or "-", req=raw_model,
                  grp=used.get("group"), dep=used["unique"],
                  tries=max(1, len(attempts_box)),
                  fb=max(0, len(attempts_box) - 1),
                  dur_ms=int((time.monotonic() - t_req) * 1000),
                  stream=False, qc=bool(qc_failed), wd=None,
                  usage=_usage_of(data))
    return data


# --------------------------------------------------- streaming helpers (TASK D2)
def _sse_data_objs(chunk: bytes):
    """Yield gli oggetti JSON dei righi 'data: {...}' in un chunk SSE
    (salta [DONE] e i rigi non-JSON)."""
    for line in chunk.split(b"\n"):
        s = line.strip()
        if not s.startswith(b"data:"):
            continue
        body = s[5:].strip()
        if body == b"[DONE]" or not body:
            continue
        try:
            yield json.loads(body)
        except Exception:
            continue


def _delta_has_content(obj) -> bool:
    """True se un oggetto chunk OpenAI-style porta contenuto reale
    (answer, reasoning o tool_call)."""
    if not isinstance(obj, dict):
        return False
    for ch in (obj.get("choices") or []):
        d = ch.get("delta") or ch.get("message") or {}
        if not isinstance(d, dict):
            continue
        c = d.get("content")
        if isinstance(c, str) and c.strip():
            return True
        if isinstance(c, list) and c:
            return True
        rc = d.get("reasoning_content") or d.get("reasoning")
        if isinstance(rc, str) and rc.strip():
            return True
        if d.get("tool_calls"):
            return True
    return False


def _answer_chars(obj) -> int:
    """Solo il testo di risposta (NON reasoning) per il verdetto finale C."""
    n = 0
    for ch in (obj.get("choices") or []) if isinstance(obj, dict) else []:
        d = ch.get("delta") or ch.get("message") or {}
        c = d.get("content") if isinstance(d, dict) else None
        if isinstance(c, str):
            n += len(c.strip())
        elif isinstance(c, list):
            for p in c:
                t = p.get("text") if isinstance(p, dict) else None
                if isinstance(t, str):
                    n += len(t.strip())
    return n


def _obj_is_error(obj) -> bool:
    return isinstance(obj, dict) and bool(obj.get("error"))


def _delta_has_answer(obj) -> bool:
    """Contenuto di RISPOSTA (testo answer o tool_calls), NON reasoning.
    E' questo che impegna lo stream verso il client."""
    if not isinstance(obj, dict):
        return False
    for ch in (obj.get("choices") or []):
        d = ch.get("delta") or ch.get("message") or {}
        if not isinstance(d, dict):
            continue
        c = d.get("content")
        if isinstance(c, str) and c.strip():
            return True
        if isinstance(c, list) and c:
            return True
        if d.get("tool_calls"):
            return True
    return False


def _chunk_finish_reason(obj):
    for ch in (obj.get("choices") or []) if isinstance(obj, dict) else []:
        fr = ch.get("finish_reason") if isinstance(ch, dict) else None
        if fr:
            return fr
    return None


async def _peek_stream(gen, first_content_ms: int, include_reasoning: bool,
                       min_chars: int = 40):
    """Consuma `gen` finche' arriva CONTENUTO DI RISPOSTA sufficiente, oppure
    error / EOF / deadline.

    Impegna lo stream (verdict 'content') solo quando:
      - i caratteri di risposta accumulati raggiungono `min_chars`, OPPURE
      - arriva un finish_reason / [DONE] con almeno 1 char di risposta
        (risposta breve ma COMPLETA, es. "OK"), OPPURE
      - tool_calls (risposta valida senza testo), OPPURE
      - reasoning (solo se include_reasoning).
    Un finish_reason / [DONE] con 0 char -> 'empty_eof'. Un solo token seguito
    dalla morte dello stream NON impegna: -> 'timeout' -> rotazione.

    Ritorna (verdict, buffered, pending, meta) con verdict in
    {'content','error','empty_eof','timeout'}; `pending` = task `__anext__` in
    volo (SOLO se 'timeout'): NON cancellato qui, chi ruota chiama
    _discard_stream(). meta = {'finish_reason': str|None}.
    """
    buffered: list[bytes] = []
    meta = {"finish_reason": None, "saw_reasoning": False}
    answer_chars = 0
    saw_tool_calls = False
    saw_reasoning = False
    buffered_bytes = 0
    # FIX: cap difensivo anti-memoria. In condizioni normali si esce dopo
    # min_chars (40 char): il cap scatta SOLO su upstream patologici che
    # floodano stream reasoning-only senza contenuto di risposta.
    # Valore predefinito generoso: 10MB, sovrapponibile da policy o chiamante.
    MAX_PEEK_BUFFER_BYTES = 10 * 1024 * 1024  # 10MB prima di ruotare

    def _eof():
        # fine stream senza risposta.
        if answer_chars or saw_tool_calls or (include_reasoning and saw_reasoning):
            return "content", buffered, None, meta
        meta["saw_reasoning"] = saw_reasoning
        # no_rotate = il modello ha COMPLETATO (c'e' un finish_reason) senza
        # rispondere: non e' rotto, ruotare nel gruppo non aiuta -> notice.
        # reasoning TRONCATO senza finish_reason = troncamento upstream -> un
        # altro deployment puo' farcela -> ruota (+ mark_failed).
        meta["no_rotate"] = bool(meta.get("finish_reason"))
        return "empty_eof", buffered, None, meta

    deadline = time.monotonic() + max(0.0, first_content_ms) / 1000.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout", buffered, None, meta
        task = asyncio.ensure_future(gen.__anext__())
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if not done:
            return "timeout", buffered, task, meta
        try:
            chunk = task.result()
        except StopAsyncIteration:
            return _eof()
        except Exception:
            return "empty_eof", buffered, None, meta
        buffered.append(chunk)
        buffered_bytes += len(chunk)
        # FIX: upstream patologico (flood reasoning-only / contenuto enorme):
        # esci prima della deadline per non accumulare memoria illimitata.
        if buffered_bytes > MAX_PEEK_BUFFER_BYTES:
            log.warning("[peek] buffer %d byte senza contenuto sufficiente: "
                        "rotazione (answer_chars=%d)", buffered_bytes,
                        answer_chars)
            if answer_chars or saw_tool_calls:
                return "content", buffered, None, meta
            return "timeout", buffered, None, meta
        for obj in _sse_data_objs(chunk):
            if _obj_is_error(obj):
                return "error", buffered, None, meta
            # alcuni provider infilano il PROPRIO envelope d'errore
            # ({"type":"error",...} / {"error":...}) DENTRO delta.content come
            # se fosse testo: non e' una risposta reale -> ruota.
            for ch in (obj.get("choices") or []):
                dd = (ch.get("delta") or ch.get("message") or {}) \
                    if isinstance(ch, dict) else {}
                cc = dd.get("content") if isinstance(dd, dict) else None
                if isinstance(cc, str) and is_provider_error_body(cc):
                    return "error", buffered, None, meta
            answer_chars += _answer_chars(obj)
            for ch in (obj.get("choices") or []):
                d = ch.get("delta") or ch.get("message") or {} \
                    if isinstance(ch, dict) else {}
                if isinstance(d, dict):
                    if d.get("tool_calls"):
                        saw_tool_calls = True
                    rc = d.get("reasoning_content") or d.get("reasoning")
                    if isinstance(rc, str) and rc.strip():
                        saw_reasoning = True
            fr = _chunk_finish_reason(obj)
            if fr:
                meta["finish_reason"] = fr
            if saw_tool_calls or answer_chars >= max(1, min_chars):
                return "content", buffered, None, meta
            if fr:
                return ("content", buffered, None, meta) if answer_chars > 0 \
                    else _eof()
            if include_reasoning and saw_reasoning:
                return "content", buffered, None, meta
        if b"[DONE]" in chunk:
            return ("content", buffered, None, meta) if answer_chars > 0 \
                else _eof()


def _actionable_upstream_error(err) -> bool:
    """True se l'errore e' AZIONABILE dall'agente/utente (auth, credito,
    permessi, modello inesistente, thought_signature) e va consegnato col suo
    status vero. Il resto (rete, 5xx, 404 transitori, body d'errore provider)
    -> risposta 'notice' non vuota, cosi' il loop dell'agente non si pianta."""
    detail = getattr(err, "detail", "") or ""
    if (_THOUGHT_SIG_RE.search(detail) or _MODEL_MISSING_RE.search(detail)
            or _PAYLOAD_SCHEMA_RE.search(detail)):
        return True
    st = getattr(err, "status", None)
    return st in (-401, -402, -403)


def _soft_cd() -> int:
    """Secondi di cooldown per un fallimento SOFT dello streaming (vuoto/
    troncato/zero-answer): fisso e breve, mai l'escalation."""
    return int(getattr(router.policy.qc_json, "watchdog_cooldown_sec", 90)
               or 90)


async def _discard_stream(gen, pending=None) -> None:
    """Chiude in sicurezza uno stream upstream abbandonato (rotazione pre-byte):
    la lettura in volo NON viene cancellata a meta' frame ma consumata, poi si
    chiude la connessione httpx sottostante."""
    if pending is not None:
        pending.cancel()
        try:
            await pending
        except BaseException:
            pass
    try:
        aclose = getattr(gen, "aclose", None)
        if aclose is not None:
            await aclose()
    except Exception:
        pass


def _exhausted(n_tries: int, detail: str | None = None):
    """Risposta di errore RETRYABLE quando nessun deployment ha prodotto un
    output utile: HTTP 503 + Retry-After. MAI un turno finto verso il client —
    l'agente ritenta (e col routing resiliente/transient il retry di solito
    trova una chiave viva)."""
    msg = ("nessun deployment upstream ha prodotto una risposta dopo %d "
           "tentativi" % max(1, int(n_tries or 1)))
    if detail:
        msg += " (ultimo: %s)" % str(detail)[:160]
    return JSONResponse(status_code=503, headers={"Retry-After": "2"},
                        content={"error": {
                            "message": msg,
                            "type": "upstream_unavailable",
                            "code": "no_healthy_deployment"}})


def _payload_text_empty(payload: dict) -> bool:
    """True se il payload non porta alcun testo di input utile."""
    try:
        for m in payload.get("messages") or []:
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return False
            if isinstance(c, list):
                for p in c:
                    if (isinstance(p, dict) and isinstance(p.get("text"), str)
                            and p["text"].strip()):
                        return False
        for k in ("input", "prompt"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return False
        return True
    except Exception:
        return False


def _parachute_verdict(verdict: str, qcp, dep: dict, policy) -> str:
    """Sulla catena PARACADUTE (-go/-fallback, ultimo scaglione del ladder) il
    timeout sul primo contenuto non deve produrre rotazione/503: li' non c'e'
    piu' nessun deployment dietro, quindi si trasmette comunque quello che
    arriva. Attivo con qc_json.stream_parachute_no_timeout (default True);
    False ripristina il comportamento legacy (timeout -> rotazione/503)."""
    if (verdict == "timeout"
            and getattr(qcp, "stream_parachute_no_timeout", True)):
        grp = dep.get("group") or ""
        if grp.endswith(policy.go_suffix) \
                or grp.endswith(policy.fallback_suffix):
            return "content"
    return verdict


async def _stream_with_fallback(profile: str | None, first_dep: dict,
                                payload: dict, need: frozenset[str] = frozenset(),
                                hook=None, scope: str = "chain",
                                ctx: int | None = None,
                                ses: str | None = None,
                                req: str | None = None,
                                session: str | None = None,
                                client_ip: str = ""):
    """Streaming SSE con fallback PRIMA del primo byte inviato al client."""
    dep = first_dep
    tried = 0
    tried_set: set[str] = set()
    _max_tries = int(getattr(router.policy, "max_fallback_tries",
                            os.environ.get("GATEWAY_MAX_FALLBACK_TRIES", "128"))
                     or 128)
    t_req = time.monotonic()
    attempts: list[str] = []
    ttfb_ms: int | None = None          # letta da sse()/_summary via closure
    while True:
        tried += 1
        attempts.append(dep["unique"])
        tried_set.add(dep["unique"])
        _was_dormant = router.is_cooled_down(dep["unique"])
        def _fail(u, *, seconds=None, reason=None):
            if _was_dormant:
                return router.mark_failed_double_residual(u, reason=reason)
            return router.mark_failed(u, seconds=seconds, reason=reason)
        router.note_start(dep["unique"])
        try:
            t_att = time.monotonic()
            gen = await forwarder.stream_response(dep, payload,
                                                  client_ip=client_ip,
                                                  session=session)
            # la TTFB vera e' il tempo fino agli HEADER upstream
            # (send(stream=True) ritorna gia' col primo chunk bufferizzato:
            # misurarla sul primo yield darebbe sempre ~0ms e avvelenerebbe
            # l'EMA della rotazione adattiva con latenze nulle).
            ttfb_ms = int((time.monotonic() - t_att) * 1000)
            router.note_result(dep["unique"], (time.monotonic() - t_att) * 1000)
            if _was_dormant:
                router.clear_cooldown(dep["unique"])
            # ANTI-STALLO (1): lo stream verso il client NON parte finche' non
            # arriva CONTENUTO DI RISPOSTA reale. Entro stream_first_content_ms
            # un upstream vuoto/errore/lento viene ruotato in modo TRASPARENTE
            # (nessun byte inviato). Esaurita la catena -> risposta "notice".
            qcp = router.policy.qc_json
            fc_ms = max(2000, int(getattr(qcp, "stream_first_content_ms",
                                          20000) or 20000))
            incl_reason = bool(getattr(qcp, "stream_commit_include_reasoning",
                                       False))
            min_ch = int(getattr(qcp, "stream_commit_min_chars", 40) or 0)
            verdict, prebuf, pending, meta = await _peek_stream(
                gen, fc_ms, incl_reason, min_ch)
            # FIX paracadute: sulla catena -go/-fallback (ULTIMO scaglione del
            # ladder) il timeout sul primo contenuto NON deve produrre un 503:
            # li' non c'e' piu' nessuno dietro a cui ruotare, quindi si
            # trasmette comunque quello che arriva (parametro opzionale
            # stream_parachute_no_timeout, default True).
            verdict = _parachute_verdict(verdict, qcp, dep, router.policy)
            if verdict == "content":
                break                   # risposta reale in arrivo: si parte
            # --- nessun contenuto: rotazione PRE-BYTE ---
            await _discard_stream(gen, pending)
            router.note_end(dep["unique"])
            fr = meta.get("finish_reason")
            rot_len = getattr(router.policy.qc_sanity,
                              "rotate_on_length_empty", False)
            # NON ruotare (e non punire) se il modello HA prodotto reasoning o
            # ha esaurito max_tokens: non e' rotto, ruotare non cambia nulla
            # (tutto il gruppo si comporterebbe uguale) -> 503 retryable diretto.
            no_rotate = (verdict == "empty_eof" and meta.get("no_rotate")
                         and not rot_len)
            if not no_rotate:
                # fallimento SOFT (stream vuoto/troncato pre-contenuto): il
                # modello ha risposto male, non e' morto -> cooldown CORTO
                # fisso, non l'escalation che spegnerebbe il pool.
                _fail(dep["unique"], seconds=_soft_cd())
            over_deadline = ((time.monotonic() - t_req) * 1000 >
                             int(getattr(qcp, "stream_total_deadline_ms",
                                         90000) or 90000))
            nxt = None if (no_rotate or over_deadline) else (
                router.fallback_next(profile, dep, need, scope, ctx=ctx,
                                     tried=tried_set)
                if profile else None)
            log.warning("[fallback] stream %s pre-contenuto verdict=%s fr=%s "
                        "no_rotate=%s -> %s", dep["unique"], verdict, fr,
                        bool(no_rotate),
                        nxt["unique"] if nxt else "503")
            if nxt is None or tried > _max_tries:
                # nessun byte inviato al client -> errore RETRYABLE pulito
                _emit_summary(ses=ses or "-", req=req or "-",
                              grp=dep.get("group"), dep=dep.get("unique"),
                              tries=len(attempts),
                              fb=max(0, len(attempts) - 1),
                              dur_ms=int((time.monotonic() - t_req) * 1000),
                              stream=True, qc=True, wd="chain-exhausted",
                              ttfb_ms=ttfb_ms, usage=None)
                return _exhausted(len(attempts),
                                  "%s (%s)" % (verdict, fr) if fr else verdict)
            dep = nxt
            inject_identity(payload, dep, router=router)
            continue                    # ri-entra nel while col nuovo dep
        except UpstreamError as err:
            router.note_end(dep["unique"])   # tentativo chiuso senza stream
            detail = err.detail or ""
            # D5 anche in STREAMING: 4xx deployment-side (firma provider-side,
            # modello inesistente oppure 404) -> fallback pre-byte invece di
            # pass-through. Gli altri 4xx restano errori del client.
            thought_sig = bool(_THOUGHT_SIG_RE.search(detail))
            # CF Workers AI & co.: rifiuto di SCHEMA (content array vs string,
            # messaggio senza content) -> stesso trattamento del
            # thought_signature: ruota SENZA cooldown, mai pass-through finche'
            # c'e' un'alternativa (un provider OpenAI-compatibile lo accetta).
            schema_sig = bool(_PAYLOAD_SCHEMA_RE.search(detail))
            if schema_sig:
                thought_sig = True         # riusa tutta la logica no-cooldown
            prov_err = is_provider_error_body(detail)   # body {"error":...} & co.
            transient = bool(_PROVIDER_TRANSIENT_RE.search(detail))
            openai_sig = ("bad_response_status_code" in detail
                          or "openai_error" in detail)
            # 4xx con body d'errore ASSENTE/illeggibile (stream appeso ->
            # _safe_aread scaduto): non c'e' alcun messaggio azionabile per il
            # client -> NON e' un errore del client, e' infrastruttura ->
            # ruota + cooldown corto, mai pass-through (503 se catena esaurita).
            empty_body = (not detail.strip()
                          or "body non leggibile" in detail.lower()
                          or len(detail.strip()) < 12)
            # motivo della classificazione deployment-side (per il log)
            if schema_sig:
                reason = "payload_schema"
            elif thought_sig:
                reason = "thought_signature"
            elif prov_err:
                reason = "provider_error_body"
            elif transient:
                reason = "provider_transient"
            elif _MODEL_MISSING_RE.search(detail):
                reason = "model_missing"
            elif router.policy.qc_json.retry_provider_4xx and openai_sig:
                reason = "openai_error"
            elif err.status == -402:
                reason = "http_402"
            elif empty_body:
                reason = "empty_error_body"
            elif err.status is not None and err.status < 0:
                reason = "other_4xx"
            else:
                reason = ("http_%s" % err.status if err.status
                          else "network")
            if err.status is not None and err.status < 0:
                provider_side = (
                    (router.policy.qc_json.retry_provider_4xx and openai_sig)
                    or _MODEL_MISSING_RE.search(detail)
                    or thought_sig
                    or prov_err
                    or transient
                    or empty_body
                    or err.status == -402)
                # né thought_signature né il body d'errore provider sono
                # rifiuti di modalita': non alimentano l'auto-learn (hook).
                if provider_side and hook and not thought_sig and not prov_err:
                    try:
                        hook(dep["model"], detail)
                    except Exception:
                        pass
                if -err.status != 404 and not provider_side:
                    log.warning("[fallback] stream %s %d PASS-THROUGH al "
                                "client (non deployment-side) :: %.120s",
                                dep["unique"], -err.status, detail)
                    return JSONResponse(status_code=abs(err.status),
                                        content={"error": {
                                            "message": err.detail}})
            # Gemini 3 tool replay: ruota SENZA cooldown (vedi _THOUGHT_SIG_RE
            # nel forwarder) — la key Gemini resta sana per il traffico non-tool.
            # model_missing (inesistente/non servito/giu'): 24h fissi.
            if not thought_sig:
                if reason in ("provider_transient", "empty_error_body"):
                    _cd = PROVIDER_TRANSIENT_COOLDOWN_S
                elif reason == "model_missing":
                    _cd = MODEL_MISSING_COOLDOWN_S
                else:
                    _cd = err.retry_after
                _fail(dep["unique"], seconds=_cd)
            nxt = router.fallback_next(profile, dep, need, scope, ctx=ctx,
                                       tried=tried_set) \
                if profile else None
            if nxt is not None and thought_sig and nxt["unique"] in attempts:
                nxt = None                 # gruppo/catena tutto Gemini 3
            log.warning("[fallback] stream %s %s motivo=%s -> %s :: %.120s",
                        dep["unique"], err.status or "conn", reason,
                        nxt["unique"] if nxt else "nessun alternativo", detail)
            if nxt is None or tried > _max_tries:
                # errori AZIONABILI (auth/credito/permessi/modello assente/
                # thought_signature) -> status vero. Il resto -> 503 RETRYABLE.
                if _actionable_upstream_error(err) and err.status:
                    return JSONResponse(status_code=abs(err.status), content={
                        "error": {"message": err.detail,
                                  "type": "upstream_error"}})
                _emit_summary(ses=ses or "-", req=req or "-",
                              grp=dep.get("group"), dep=dep.get("unique"),
                              tries=len(attempts),
                              fb=max(0, len(attempts) - 1),
                              dur_ms=int((time.monotonic() - t_req) * 1000),
                              stream=True, qc=True, wd="chain-exhausted",
                              ttfb_ms=ttfb_ms, usage=None)
                return _exhausted(len(attempts), err.detail)
            dep = nxt
            inject_identity(payload, dep, router=router)
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as exc:
            # qualsiasi errore IMPREVISTO nell'ottenere lo stream da questo
            # deployment (es. httpx che cade leggendo il body d'errore) -> NON
            # deve 500-are la richiesta: cooldown corto + rotazione, 503 solo
            # se non resta nulla.
            try:
                router.note_end(dep["unique"])
            except Exception:
                pass
            _fail(dep["unique"], seconds=_soft_cd())
            nxt = router.fallback_next(profile, dep, need, scope, ctx=ctx,
                                       tried=tried_set) \
                if profile else None
            log.warning("[fallback] stream %s errore imprevisto %r -> %s",
                        dep["unique"], exc,
                        nxt["unique"] if nxt else "503")
            if nxt is None or tried > _max_tries:
                _emit_summary(ses=ses or "-", req=req or "-",
                              grp=dep.get("group"), dep=dep.get("unique"),
                              tries=len(attempts), fb=max(0, len(attempts) - 1),
                              dur_ms=int((time.monotonic() - t_req) * 1000),
                              stream=True, qc=True, wd="chain-exhausted",
                              ttfb_ms=ttfb_ms, usage=None)
                return _exhausted(len(attempts), repr(exc)[:160])
            dep = nxt
            inject_identity(payload, dep, router=router)

    async def sse():
        # Watchdog PASSIVO (D4): conta chunk, rileva [DONE] ed eventi error.
        # NON modifica mai i byte verso il client. Due livelli:
        #   tier1 stream vuoto / evento "error" esplicito -> cooldown subito
        #   tier2 chiuso senza [DONE] -> solo log, cooldown se policy lo vuole
        # + (D2) ri-emissione del prebuffer e coda d'errore SSE finale (verdict C).
        sent_first = False
        chunks = 0
        seen_done = False
        seen_error = False
        finished = False
        wd: str | None = None
        usage_final: dict | None = None
        sum_sent = False
        answer_total = 0                  # solo testo risposta (D2/C)
        req_has_input = not _payload_text_empty(payload)   # D2/C
        finish_len = False                 # finish_reason == "length" (D2/C)
        saw_finish_reason = False          # QUALSIASI finish_reason non nullo
        had_tool_calls = False             # tool_calls visti (D2/C)

        def _summary(dur_ms: int) -> None:
            nonlocal sum_sent
            if sum_sent:
                return
            sum_sent = True
            _emit_summary(ses=ses or "-", req=req or "-", grp=dep["group"],
                          dep=dep["unique"], tries=len(attempts),
                          fb=max(0, len(attempts) - 1), dur_ms=dur_ms,
                          stream=True, qc=False, wd=wd, ttfb_ms=ttfb_ms,
                          usage=usage_final)

        # corpo del loop fattorizzato: aggiorna lo stato watchdog ed emette
        # il chunk invariato. Condiviso da prebuffer e dal flusso residuo.
        def _ingest(chunk: bytes) -> bytes:
            nonlocal chunks, seen_done, seen_error, usage_final
            nonlocal answer_total, finish_len, had_tool_calls, sent_first
            nonlocal saw_finish_reason
            chunks += 1
            if b"[DONE]" in chunk:
                seen_done = True
            if b'data: {"error"' in chunk:
                seen_error = True
            if usage_final is None and b'"usage"' in chunk and \
                    chunks > 1:      # parse best-effort del chunk usage
                try:
                    line = next((ln for ln in chunk.split(b"\n")
                                 if ln.startswith(b"data:") and
                                 b'"usage"' in ln), None)
                    if line:
                        obj = json.loads(line[5:].strip())
                        u = obj.get("usage")
                        if isinstance(u, dict):
                            usage_final = {
                                k: u[k] for k in
                                ("prompt_tokens", "completion_tokens",
                                 "total_tokens") if u.get(k) is not None}
                            c = u.get("cost")
                            if isinstance(c, dict):
                                usage_final["cost"] = c.get("total_cost")
                            elif c is not None:
                                usage_final["cost"] = c
                except Exception:
                    pass
            for o in _sse_data_objs(chunk):
                answer_total += _answer_chars(o)
                for ch in (o.get("choices") or []):
                    if not isinstance(ch, dict):
                        continue
                    fr = ch.get("finish_reason")
                    if fr:
                        saw_finish_reason = True
                        if fr == "length":
                            finish_len = True
                    d = ch.get("delta") or ch.get("message") or {}
                    if isinstance(d, dict) and d.get("tool_calls"):
                        had_tool_calls = True
            if not sent_first:
                sent_first = True      # TTFB gia' presa agli header upstream
            return chunk

        gen_broken = False
        try:
            # (D2/B) ordine: prima il prebuffer gia' letto da _peek_stream, poi
            # l'eventuale lettura rimasta in volo (`pending`), poi il resto.
            for chunk in prebuf:
                yield _ingest(chunk)
            if pending is not None:
                try:
                    yield _ingest(await pending)
                except StopAsyncIteration:
                    finished = True
                except Exception:
                    gen_broken = True     # upstream rotto a meta' frame
            if not finished and not gen_broken:
                async for chunk in gen:
                    yield _ingest(chunk)
                finished = True             # StopAsyncIteration: stream chiuso
        except (GeneratorExit, asyncio.CancelledError):
            raise                          # disconnessione client: non punire
        except Exception:
            gen_broken = True
        finally:
            dur_ms = int((time.monotonic() - t_req) * 1000)
            router.note_end(dep["unique"])
            # NB (fix): il watchdog NON inietta mai nulla nello stream verso il
            # client (un `data:` non-conforme viene renderizzato come testo da
            # opencode & simili). L'unica reazione automatica e' il cooldown del
            # deployment, cosi' i retry del client / le richieste successive
            # evitano la chiave che ha scazzato.
            if finished or gen_broken:
                if chunks == 0:
                    wd = "tier1-empty"
                    metrics.inc("nx_qc_watchdog_total", (dep["unique"], "empty"))
                    log.warning("[watchdog] tier1 stream VUOTO da %s "
                                "(chunks=0): cooldown", dep["unique"])
                    _fail(dep["unique"], seconds=_soft_cd())
                elif seen_error:
                    wd = "tier1-error"
                    metrics.inc("nx_qc_watchdog_total", (dep["unique"], "error"))
                    log.warning("[watchdog] tier1 evento error esplicito "
                                "da %s (chunks=%d)", dep["unique"], chunks)
                    _fail(dep["unique"], seconds=_soft_cd())
                elif gen_broken or (not seen_done and not saw_finish_reason):
                    # troncamento GENUINO: stream rotto a meta' oppure niente
                    # [DONE] E niente finish_reason -> il modello ha scazzato.
                    wd = "tier2-truncated"
                    metrics.inc("nx_qc_watchdog_total",
                                (dep["unique"], "truncated"))
                    log.warning("[watchdog] tier2 stream TRONCATO da %s "
                                "(chunk=%d, finish_reason=%s): cooldown",
                                dep["unique"], chunks, saw_finish_reason)
                    _fail(dep["unique"], seconds=_soft_cd())
                elif not seen_done:
                    # c'e' un finish_reason ma manca [DONE]: risposta di fatto
                    # completa, il provider omette solo il sentinel. Solo log.
                    wd = "tier2-no-done"
                    log.info("[watchdog] tier2 %s: finish_reason presente, "
                             "nessun [DONE] (provider senza sentinel)",
                             dep["unique"])
                elif (answer_total == 0 and req_has_input and not had_tool_calls
                      and not (finish_len and not
                               router.policy.qc_sanity.rotate_on_length_empty)):
                    # stream "completo" ma 0 testo di risposta con input reale:
                    # fallimento silenzioso -> cooldown (nessun artefatto verso
                    # il client: i byte, per quanto vuoti, sono gia' partiti).
                    wd = "zero-answer"
                    metrics.inc("nx_qc_watchdog_total",
                                (dep["unique"], "zero_answer"))
                    log.warning("[watchdog] stream 0-answer da %s (input non "
                                "vuoto, finish_len=%s): cooldown",
                                dep["unique"], finish_len)
                    _fail(dep["unique"], seconds=_soft_cd())
            _summary(dur_ms)

    return StreamingResponse(sse(), media_type="text/event-stream")


# ---------------------------------------------------------- image generations
@app.post("/v1/images/generations")
async def images_generations(request: Request):
    """Endpoint OpenAI-compatibile per la generazione immagini.

    Instrada SOLO verso deployment con capacità image_gen (model_capabilities).
    Su 404/provider-4xx dall'endpoint /images/generations e con
    capability_routing.images_chat_fallback=true, ritenta via chat/completions
    (modelli immagine esposti come chat, es. gemini-image).
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "error": {"message": "invalid JSON body", "type": "invalid_request_error"}})

    raw_model = payload.get("model") or ""
    if not str(payload.get("prompt") or "").strip():
        return JSONResponse(status_code=400, content={
            "error": {"message": "'prompt' è obbligatorio",
                      "type": "invalid_request_error"}})

    model = policy.canonicalize(raw_model)

    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"image_gen"}) if router.policy.routing_active() else frozenset()
    session_id = _session_id(request, payload)
    scope = "group" if router.is_explicit(model) else "chain"

    group_or_explicit = router.resolve_group_for_request(model, [], session_id, need)
    if group_or_explicit is None:
        return JSONResponse(status_code=400 if need else 404, content={
            "error": {"message":
                      ("nessun deployment dichiara image_gen: configura "
                       "capability_routing.model_capabilities in gateway.yaml"
                       if need else
                       f"model '{model}' not managed by {policy.service_name}"),
                      "type": "invalid_request_error"}})

    dep = router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        dep = router.pick_deployment(group_or_explicit, need)
    if dep is None and auth.profile:
        dep = router.fallback_after(auth.profile, None, need)
    if dep is None:
        return JSONResponse(status_code=503, content={
            "error": {"message": "nessun deployment disponibile per image_gen",
                      "type": "server_error"}})

    custom_key = router.resolve_alias_key(raw_model, model)
    if custom_key:
        dep = {**dep, "api_key": custom_key}

    _sess = _opencode_session(request)
    _cip = _client_ip(request)

    profile = auth.profile or config.profile_of_base(model.split("__")[0]) \
        or config.profile_of_base(model)

    metrics.inc("nx_images_total", (dep["group"], "attempt"))
    log.info("[images] %s -> %s (prompt=%d chars)", model, dep["unique"],
             len(str(payload.get("prompt") or "")))

    tried: set[str] = set()
    attempts: list[str] = []
    t_req = time.monotonic()
    last_err: UpstreamError | None = None
    while dep is not None and len(tried) < 32:
        cur = dep["unique"]
        _was_dormant = router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        router.note_start(cur)
        t0 = time.monotonic()
        try:
            data = await forwarder.call_images(dep, payload,
                                           client_ip=_cip, session=_sess)
            router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                router.clear_cooldown(cur)
            metrics.inc("nx_images_total", (dep["group"], "ok"))
            if isinstance(data, dict):
                data["nx_deployment"] = cur
            _emit_summary(ses=session_id or "-", req=raw_model,
                          grp=dep["group"], dep=cur,
                          tries=len(attempts), fb=len(attempts) - 1,
                          dur_ms=int((time.monotonic() - t_req) * 1000),
                          stream=False, qc=False, wd=None, usage=None,
                          kind="images")
            return data
        except UpstreamError as err:
            router.note_end(cur)
            last_err = err
            detail = err.detail or ""
            status = err.status if err.status is not None else 0
            deployment_side = (
                status > 0                       # retryable (429/5xx/timeout)
                or -status == 404                # endpoint/modello assente lì
                or -status == 402                # chiave senza crediti (deployment)
                or _MODEL_MISSING_RE.search(detail)   # "No such model" stile CF
                or (router.policy.images_chat_fallback
                    and (-status == 400 or -status == 403)
                    and ("openai_error" in detail
                         or "bad_response_status_code" in detail)))
            if not deployment_side:
                metrics.inc("nx_images_total", (dep["group"], "client_error"))
                st = abs(status) if status else 502
                return JSONResponse(status_code=st if st >= 400 else 502,
                                    content={"error": {"message": err.detail,
                                                       "type": "upstream_error"}})
            # images.chat_fallback: prima di cambiare deployment prova via chat
            if router.policy.images_chat_fallback and f"{cur}::chat" not in tried:
                tried.add(f"{cur}::chat")
                log.info("[images] %s: /images/generations non disponibile "
                         "(status=%s): ritenta via chat", cur, -status or "?")
                chat_payload = {
                    "model": raw_model,
                    "messages": [{"role": "user", "content":
                                  str(payload.get("prompt"))}],
                    "modalities": ["image"],
                }
                try:
                    data = await forwarder.call(dep, chat_payload)
                    router.note_result(cur, (time.monotonic() - t0) * 1000)
                    metrics.inc("nx_images_total", (dep["group"], "ok_chat"))
                    # normalizza: estrae l'immagine dal messaggio se presente
                    out = data
                    if isinstance(data, dict):
                        out = dict(data)
                        out["nx_deployment"] = cur
                        out.setdefault("via", "chat")
                        msg = ((data.get("choices") or [{}])[0].get("message")
                               or {})
                        img = msg.get("images") or msg.get("content")
                        if img:
                            out["data"] = (img if isinstance(img, list)
                                           else [{"b64_json_or_url_fallback": img}])
                    _emit_summary(ses=session_id or "-", req=raw_model,
                                  grp=dep["group"], dep=cur,
                                  tries=len(attempts), fb=len(attempts) - 1,
                                  dur_ms=int((time.monotonic() - t_req) * 1000),
                                  stream=False, qc=False, wd=None, usage=None,
                                  kind="images", via="chat")
                    return out
                except UpstreamError as chat_err:
                    log.warning("[images] fallback chat su %s fallito: %s",
                                cur, chat_err.detail[:120])
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    pass
            if _was_dormant:
                router.mark_failed_double_residual(cur, reason=str(err.detail or "")[:80])
            else:
                router.mark_failed(cur, seconds=err.retry_after)
            metrics.inc("nx_images_total", (dep["group"], "retry"))
            nxt = router.fallback_next(profile, dep, need, scope, tried=tried) \
                if profile else None
            if nxt is None:
                break
            dep = nxt
        finally:
            if cur in tried:
                router.note_end(cur)

    status = abs(last_err.status) if last_err and last_err.status else 502
    return JSONResponse(status_code=status if status >= 400 else 502,
                        content={"error": {
                            "message": (last_err.detail if last_err
                                        else "nessun deployment image_gen disponibile"),
                            "type": "upstream_error"}})


# ----------------------------------------------------------------- audio TTS
def _audio_route(profile: str | None, model: str, raw_model: str,
                 session_id: str | None, need: frozenset[str]):
    """Routing condiviso degli endpoint audio: risolve il primo deployment
    capace (o explicit pass-through) oppure ritorna una JSONResponse d'errore.
    Ritorna (dep, profile, error_response)."""
    group_or_explicit = router.resolve_group_for_request(model, [], session_id,
                                                         need)
    if group_or_explicit is None:
        capname = sorted(need)[0] if need else model
        for c in sorted(need):
            metrics.inc("nx_caps_unroutable_total", (c,))
        return None, profile, JSONResponse(
            status_code=400 if need else 404,
            content={"error": {"message":
                               (f"nessun deployment dichiara la capacità "
                                f"'{capname}': configura "
                                f"capability_routing.model_capabilities in "
                                f"gateway.yaml" if need else
                                f"model '{model}' not managed by "
                                f"{policy.service_name}"),
                               "type": "invalid_request_error"}})

    dep = router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        dep = router.pick_deployment(group_or_explicit, need)
    if dep is None and profile:
        dep = router.fallback_after(profile, None, need)
    if dep is None:
        return None, profile, JSONResponse(status_code=503, content={
            "error": {"message": f"nessun deployment disponibile per {sorted(need) or model}",
                      "type": "server_error"}})

    custom_key = router.resolve_alias_key(raw_model, model)
    if custom_key:
        dep = {**dep, "api_key": custom_key}
    return dep, profile, None


@app.post("/v1/audio/speech")
async def audio_speech(request: Request):
    """TTS OpenAI-compatibile: instrada SOLO su deployment con capacità tts."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "error": {"message": "invalid JSON body", "type": "invalid_request_error"}})
    raw_model = payload.get("model") or ""
    if not str(payload.get("input") or "").strip():
        return JSONResponse(status_code=400, content={
            "error": {"message": "'input' è obbligatorio",
                      "type": "invalid_request_error"}})
    model = policy.canonicalize(raw_model)
    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"tts"}) if router.policy.routing_active() else frozenset()
    scope = "group" if router.is_explicit(model) else "chain"
    dep, profile, err = _audio_route(auth.profile, model, raw_model,
                                     _session_id(request, payload), need)
    if err:
        return err

    metrics.inc("nx_tts_total", (dep["group"], "attempt"))
    log.info("[tts] %s -> %s (input=%d chars)", model, dep["unique"],
             len(str(payload.get("input") or "")))

    tried: set[str] = set()
    attempts: list[str] = []
    session_id = _session_id(request, payload)
    _sess = _opencode_session(request)
    _cip = _client_ip(request)
    t_req = time.monotonic()
    last_err: UpstreamError | None = None
    while dep is not None and len(tried) < 32:
        cur = dep["unique"]
        _was_dormant = router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        router.note_start(cur)
        t0 = time.monotonic()
        try:
            content, ctype = await forwarder.call_speech(
                dep, payload, client_ip=_cip, session=_sess)
            router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                router.clear_cooldown(cur)
            metrics.inc("nx_tts_total", (dep["group"], "ok"))
            _emit_summary(ses=session_id or "-", req=raw_model,
                          grp=dep["group"], dep=cur,
                          tries=len(attempts), fb=len(attempts) - 1,
                          dur_ms=int((time.monotonic() - t_req) * 1000),
                          stream=False, qc=False, wd=None, usage=None,
                          kind="tts", bytes=len(content), ctype=ctype)
            return Response(content=content, media_type=ctype,
                            headers={"x-nx-deployment": cur})
        except UpstreamError as err:
            router.note_end(cur)
            last_err = err
            detail = err.detail or ""
            status = err.status if err.status is not None else 0
            deployment_side = (
                status > 0 or -status == 404 or -status == 402
                or _MODEL_MISSING_RE.search(detail)   # "No such model" stile CF
                or (-status in (400, 403)
                    and ("openai_error" in detail
                         or "bad_response_status_code" in detail)))
            if not deployment_side:
                metrics.inc("nx_tts_total", (dep["group"], "client_error"))
                st = abs(status) if status else 502
                return JSONResponse(status_code=st if st >= 400 else 502,
                                    content={"error": {"message": err.detail,
                                                       "type": "upstream_error"}})
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    pass
            if _was_dormant:
                router.mark_failed_double_residual(cur, reason=str(err.detail or "")[:80])
            else:
                router.mark_failed(cur, seconds=err.retry_after)
            metrics.inc("nx_tts_total", (dep["group"], "retry"))
            nxt = router.fallback_next(profile, dep, need, scope, tried=tried) \
                if profile else None
            if nxt is None:
                break
            dep = nxt

    status = abs(last_err.status) if last_err and last_err.status else 502
    return JSONResponse(status_code=status if status >= 400 else 502,
                        content={"error": {
                            "message": (last_err.detail if last_err
                                        else "nessun deployment tts disponibile"),
                            "type": "upstream_error"}})


# ----------------------------------------------------------------- audio STT
async def _audio_transcribe(request: Request, path: str):
    """Handler condiviso transcriptions/translations (multipart form).

    Form OpenAI: file (binario), model, language?, prompt?, response_format?
    (json|text|srt|verbose_json|vtt), temperature?. Instrada SOLO su
    deployment con capacità stt.
    """
    try:
        form = await request.form()
    except Exception:
        return JSONResponse(status_code=400, content={
            "error": {"message": "multipart/form-data non valido",
                      "type": "invalid_request_error"}})

    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        return JSONResponse(status_code=400, content={
            "error": {"message": "'file' (audio) è obbligatorio",
                      "type": "invalid_request_error"}})
    filename = getattr(upload, "filename", "") or "audio"
    fcontent = getattr(upload, "content_type", "") or "application/octet-stream"
    try:
        file_bytes = await upload.read()
    except Exception:
        return JSONResponse(status_code=400, content={
            "error": {"message": "lettura del file fallita",
                      "type": "invalid_request_error"}})
    if not file_bytes:
        return JSONResponse(status_code=400, content={
            "error": {"message": "file audio vuoto",
                      "type": "invalid_request_error"}})

    data_fields = {k: v for k in ("language", "prompt", "response_format",
                                  "temperature")
                   if (v := form.get(k)) is not None}
    raw_model = str(form.get("model") or "")
    response_format = str(data_fields.get("response_format") or "json").lower()

    model = policy.canonicalize(raw_model)
    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"stt"}) if router.policy.routing_active() else frozenset()
    scope = "group" if router.is_explicit(model) else "chain"
    dep, profile, err = _audio_route(auth.profile, model, raw_model,
                                     _session_id(request, {}), need)
    if err:
        return err

    metrics.inc("nx_stt_total", (dep["group"], "attempt"))
    log.info("[stt] %s -> %s (%s, %d bytes, via /%s)", model, dep["unique"],
             filename, len(file_bytes), path)

    tried: set[str] = set()
    attempts: list[str] = []
    _sess = _opencode_session(request)
    _cip = _client_ip(request)
    t_req = time.monotonic()
    last_err: UpstreamError | None = None
    while dep is not None and len(tried) < 32:
        cur = dep["unique"]
        _was_dormant = router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        router.note_start(cur)
        t0 = time.monotonic()
        try:
            result = await forwarder.transcribe(dep, data_fields, file_bytes,
                                                filename, fcontent, path=path,
                                                client_ip=_cip, session=_sess)
            router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                router.clear_cooldown(cur)
            metrics.inc("nx_stt_total", (dep["group"], "ok"))
            _emit_summary(ses=_session_id(request, {}) or "-", req=raw_model,
                          grp=dep["group"], dep=cur,
                          tries=len(attempts), fb=len(attempts) - 1,
                          dur_ms=int((time.monotonic() - t_req) * 1000),
                          stream=False, qc=False, wd=None, usage=None,
                          kind="stt", path=path)
            if isinstance(result, dict):
                result.setdefault("nx_deployment", cur)
                return JSONResponse(result)
            # formati text/srt/vtt: passthrough testo + header di disclosure
            return PlainTextResponse(result,
                                     headers={"x-nx-deployment": cur})
        except UpstreamError as err:
            router.note_end(cur)
            last_err = err
            detail = err.detail or ""
            status = err.status if err.status is not None else 0
            deployment_side = (
                status > 0 or -status == 404 or -status == 402
                or _MODEL_MISSING_RE.search(detail)   # "No such model" stile CF
                or (-status in (400, 403)
                    and ("openai_error" in detail
                         or "bad_response_status_code" in detail)))
            if not deployment_side:
                metrics.inc("nx_stt_total", (dep["group"], "client_error"))
                st = abs(status) if status else 502
                return JSONResponse(status_code=st if st >= 400 else 502,
                                    content={"error": {"message": err.detail,
                                                       "type": "upstream_error"}})
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    pass
            if _was_dormant:
                router.mark_failed_double_residual(cur, reason=str(err.detail or "")[:80])
            else:
                router.mark_failed(cur, seconds=err.retry_after)
            metrics.inc("nx_stt_total", (dep["group"], "retry"))
            nxt = router.fallback_next(profile, dep, need, scope, tried=tried) \
                if profile else None
            if nxt is None:
                break
            dep = nxt

    status = abs(last_err.status) if last_err and last_err.status else 502
    return JSONResponse(status_code=status if status >= 400 else 502,
                        content={"error": {
                            "message": (last_err.detail if last_err
                                        else "nessun deployment stt disponibile"),
                            "type": "upstream_error"}})


@app.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request):
    """STT OpenAI-compatibile: audio -> testo nella lingua originale."""
    return await _audio_transcribe(request, "transcriptions")


@app.post("/v1/audio/translations")
async def audio_translations(request: Request):
    """STT OpenAI-compatibile: audio -> testo tradotto in inglese."""
    return await _audio_transcribe(request, "translations")


# ------------------------------------------------------------- video async
@app.post("/v1/videos/generations")
async def videos_generations(request: Request):
    """Generazione video ASINCRONA (stile OR): submit -> {id,status,polling_url}.

    Instrada SOLO su deployment con capacità video_gen. Il client polla
    GET /v1/videos/generations/{job_id} e scarica via .../{job_id}/content.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "error": {"message": "invalid JSON body", "type": "invalid_request_error"}})
    raw_model = payload.get("model") or ""
    if not str(payload.get("prompt") or "").strip():
        return JSONResponse(status_code=400, content={
            "error": {"message": "'prompt' è obbligatorio",
                      "type": "invalid_request_error"}})

    model = policy.canonicalize(raw_model)
    auth: AuthResult = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    if not authn.authorize_model(auth, model):
        return _forbidden(model, auth.profile)

    need = frozenset({"video_gen"}) if router.policy.routing_active() else frozenset()
    # i2v / reference-to-video: le immagini d'input esigono il token vision
    # nell'inner-filter (solo modelli che accettano frame/reference)
    if payload.get("frame_images") or payload.get("input_references"):
        need = need | {"vision"}
    scope = "group" if router.is_explicit(model) else "chain"
    session_id = _session_id(request, payload)

    group_or_explicit = router.resolve_group_for_request(
        model, [], session_id,
        need | {"vision"} if (need and (payload.get("frame_images")
                                        or payload.get("input_references")))
        else need)
    if group_or_explicit is None:
        for c in sorted(need or {"video_gen"}):
            metrics.inc("nx_caps_unroutable_total", (c,))
        return JSONResponse(status_code=400 if need else 404, content={
            "error": {"message":
                      ("nessun deployment dichiara 'video_gen': configura "
                       "capability_routing.model_capabilities / colonna caps"
                       if need else f"model '{model}' non gestito"),
                      "type": "invalid_request_error"}})

    explicit_req = router.is_explicit(model)
    dep = router.config.deployment_by_unique(group_or_explicit)
    if dep is None:
        dep = router.initial_pick(auth.profile, group_or_explicit,
                                  None if explicit_req else need)
    if dep is None and auth.profile and not explicit_req:
        dep = router.fallback_after(auth.profile, None, need)
    if dep is None:
        return JSONResponse(status_code=503, content={
            "error": {"message": "nessun deployment video_gen disponibile",
                      "type": "server_error"}})

    metrics.inc("nx_videos_total", (dep["group"], "attempt"))
    log.info("[videos] %s -> %s (prompt=%d chars)", model, dep["unique"],
             len(str(payload.get("prompt") or "")))

    tried: set[str] = set()
    attempts: list[str] = []
    _sess = _opencode_session(request)
    _cip = _client_ip(request)
    t_req = time.monotonic()
    last_err: UpstreamError | None = None
    while dep is not None and len(tried) < 32:
        cur = dep["unique"]
        _was_dormant = router.is_cooled_down(cur)
        tried.add(cur)
        attempts.append(cur)
        router.note_start(cur)
        t0 = time.monotonic()
        try:
            envelope = await forwarder.submit_video(
                dep, payload, client_ip=_cip, session=_sess)
            router.note_result(cur, (time.monotonic() - t0) * 1000)
            if _was_dormant:
                router.clear_cooldown(cur)
            metrics.inc("nx_videos_total", (dep["group"], "ok"))
            job_id = str(envelope.get("id") or "")
            _videos_jobs[job_id] = {
                "api_base": dep["api_base"], "api_key": dep["api_key"],
                "group": dep["group"], "created": time.time(), "_sess": _sess,
                "_cip": _cip}
            qm = f"?model={urllib.parse.quote(raw_model)}"
            out = dict(envelope)
            out["nx_deployment"] = cur
            out["eta_seconds"] = 90
            # ENVELOPE AUTO-DESCRIBENTE per agenti: le URL portano il model
            # embedded -> poll/content STATELESS, sopravvivono ai restart
            out["poll"] = {"url": f"/v1/videos/generations/{job_id}{qm}",
                           "interval_s": 15}
            out["content"] = {"url":
                              f"/v1/videos/generations/{job_id}/content{qm}"}
            out["polling_url"] = out["poll"]["url"]
            out["content_url"] = out["content"]["url"]

            # --- wait mode (per tool-agent semplici): il gateway polla ---
            wait_s = int(payload.get("wait_seconds") or 0)
            if wait_s > 0:
                wait_s = min(wait_s, 240)
                deadline = time.time() + wait_s
                while time.time() < deadline:
                    await asyncio.sleep(5)
                    try:
                        st = await forwarder.poll_video(
                            dep, job_id,
                            client_ip=_cip or _videos_jobs.get(job_id, {}).get("_cip", ""),
                            session=_sess or _videos_jobs.get(job_id, {}).get("_sess"))
                        log.debug("[video-wait] job=%s t=%ds status=%s",
                                  job_id, wait_s - int(deadline - time.time()),
                                  st.get("status"))
                    except UpstreamError:
                        continue          # blip upstream: riprova fino a deadline
                    if st.get("status") == "completed":
                        out.update({"status": "completed",
                                    "usage": st.get("usage"),
                                    "unsigned_urls": st.get("unsigned_urls")})
                        break
                    if st.get("status") in ("failed", "cancelled", "expired"):
                        out.update({"status": st.get("status"),
                                    "error": st.get("error")})
                        break
                else:
                    out["note"] = ("wait_seconds esaurito col job ancora in "
                                   "corso: prosegui con poll.url")
            _emit_summary(ses=_session_id(request, {}) or "-",
                          req=raw_model, grp=dep["group"], dep=cur,
                          tries=len(attempts), fb=len(attempts) - 1,
                          dur_ms=int((time.monotonic() - t_req) * 1000),
                          stream=False, qc=False, wd=None,
                          usage=_usage_of(out), kind="videos",
                          job=job_id, status=out.get("status"))
            return JSONResponse(out)
        except UpstreamError as err:
            router.note_end(cur)
            last_err = err
            detail = err.detail or ""
            status = err.status if err.status is not None else 0
            deployment_side = (
                status > 0 or -status == 404 or -status == 402
                or _MODEL_MISSING_RE.search(detail)   # "No such model" stile CF
                or (-status in (400, 403)
                    and ("openai_error" in detail
                         or "bad_response_status_code" in detail)))
            if not deployment_side:
                metrics.inc("nx_videos_total", (dep["group"], "client_error"))
                st = abs(status) if status else 502
                return JSONResponse(status_code=st if st >= 400 else 502,
                                    content={"error": {"message": err.detail,
                                                       "type": "upstream_error"}})
            if -status in (400, 403) and media_reject_signature(detail):
                try:
                    _strike_hook(False, need)(dep["model"], detail)
                except Exception:
                    pass
            if _was_dormant:
                router.mark_failed_double_residual(cur, reason=str(err.detail or "")[:80])
            else:
                router.mark_failed(cur, seconds=err.retry_after)
            metrics.inc("nx_videos_total", (dep["group"], "retry"))
            nxt = router.fallback_next(profile, dep, need, scope, tried=tried) \
                if (profile := auth.profile or
                    config.profile_of_base(model.split("__")[0])
                    or config.profile_of_base(model)) else None
            if nxt is None:
                break
            dep = nxt

    status = abs(last_err.status) if last_err and last_err.status else 502
    return JSONResponse(status_code=status if status >= 400 else 502,
                        content={"error": {
                            "message": (last_err.detail if last_err
                                        else "nessun deployment video_gen disponibile"),
                            "type": "upstream_error"}})


def _job_deps(job_id: str, model: str | None):
    """Risolve i deployment per poll/content di un job video.

    STATELESS-FIRST: con ?model= (nome/alias/gruppo) si ricostruisce la
    lista dei candidati dal CSV/policy — sopravvive ai restart del gateway
    e funziona da qualsiasi replica. Senza model, si usa il mapping in
    memoria della submit (fast-path, perso su restart).
    Ritorna (deps_list | None, error_response | None)."""
    if model:
        canonical = policy.canonicalize(model)
        deps = router.video_gen_candidates(canonical)
        if deps:
            return deps, None
        return None, JSONResponse(status_code=404, content={
            "error": {"message":
                      f"nessun gruppo video_gen per '{model}' "
                      "(colonna caps / capability_groups)",
                      "type": "invalid_request_error"}})
    snap = _videos_jobs.get(job_id)
    if snap is None:
        return None, JSONResponse(status_code=404, content={
            "error": {"message":
                      f"job '{job_id}' sconosciuto o scaduto (TTL 24h / "
                      f"restart): ripassa ?model=<modello> per il lookup "
                      f"stateless, oppure rifai la submit",
                      "type": "invalid_request_error"}})
    return [{"api_base": snap["api_base"], "api_key": snap["api_key"],
             "model": "", "unique": job_id}], None


@app.get("/v1/videos/generations/{job_id}")
async def videos_status(job_id: str, request: Request,
                        model: str | None = None):
    auth = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    deps, err = _job_deps(job_id, model or request.query_params.get("model"))
    if err:
        return err
    try:
        status = await forwarder.poll_video_any(deps, job_id,
                                                client_ip=_client_ip(request),
                                                session=_opencode_session(request))
    except UpstreamError as e:
        st = abs(e.status) if e.status and e.status > 0 else 502
        return JSONResponse(status_code=st if st >= 400 else 502,
                            content={"error": {"message": e.detail}})
    st_val = status.get("status") if isinstance(status, dict) else "?"
    if st_val in ("completed", "failed", "cancelled", "expired"):
        log.info("[video-poll] job=%s -> %s", job_id, st_val)
    else:
        log.debug("[video-poll] job=%s status=%s", job_id, st_val)
    qm = request.query_params.get("model")
    if isinstance(status, dict):
        status.setdefault("polling_url",
                          f"/v1/videos/generations/{job_id}"
                          + (f"?model={qm}" if qm else ""))
        if status.get("unsigned_urls"):
            status["content_url"] = (f"/v1/videos/generations/{job_id}/content"
                                     + (f"?model={qm}" if qm else ""))
    return JSONResponse(status)


@app.get("/v1/videos/generations/{job_id}/content")
async def videos_content(job_id: str, request: Request,
                         model: str | None = None):
    auth = authn.authenticate(request.headers.get("authorization"))
    if not auth.ok:
        return _unauthorized(auth.error)
    deps, err = _job_deps(job_id, model or request.query_params.get("model"))
    if err:
        return err
    try:
        t_dl = time.monotonic()
        content, ctype = await forwarder.download_video_any(
            deps, job_id,
            client_ip=_client_ip(request),
            session=_opencode_session(request))
        log.info("[video-content] job=%s bytes=%d ctype=%s dur=%.1fs",
                 job_id, len(content), ctype, time.monotonic() - t_dl)
    except UpstreamError as e:
        st = abs(e.status) if e.status and e.status > 0 else 502
        return JSONResponse(status_code=st if st >= 400 else 502,
                            content={"error": {"message": e.detail}})
    return Response(content=content, media_type=ctype)


def main() -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
