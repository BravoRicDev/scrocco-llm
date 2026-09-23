"""Admin API (/admin/*): gestione completa senza toccare file ne restart.

[IT] COSA: CRUD deployment (singolo + bulk ATOMICO), PATCH policy, state
aggregato, history journal, audit capacita, probe chiavi, guide. WHY:
  - bulk atomico: N operazioni in 1 chiamata ALL-OR-NOTHING (un agente
    non lascia mai meta config applicata).
  - backup_csv pre-scrittura + operations.jsonl: ogni modifica e
    annullabile/ricostruibile.
  - master-key-only: la superficie admin non esiste per i client
    sk-<profilo>.
  - PROBE one-shot cachato qui (vedi forwarder.py per il perche).

[EN] WHAT: full management API. WHY: atomic bulk ops prevent half-applied
config; every write is backed up and journaled; admin surface is invisible
to client keys.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

import re
import yaml
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from . import csv_store, journal, logview, metrics
from . import protocols as proto
from .config import MODEL_HEADER, PROVIDER_HEADER, DATA_HEADER, _classify
from .capabilities import canonical_family
from .forwarder import UpstreamError, _client_attribution
from .provider_models import DEFAULT_TTL_SEC, fetch_provider_models
from .router import estimate_tokens, estimate_shadow_stats
from .policy import Policy

log = logging.getLogger("nx.admin")

admin_api = APIRouter(prefix="/admin", tags=["admin"])


def _gw():
    """Accesso ai globali del servizio a runtime (evita import circolari)."""
    from . import main as mod
    return mod


def _tune(gw, name: str, default):
    """Legge un parametro di tuning dalla policy, con fallback al default
    (che coincide col valore storico hardcoded). Mai eccezioni."""
    try:
        return getattr(gw.policy, name, default)
    except Exception:                            # noqa: BLE001
        return default


def _client_ip_of(request: Request) -> str:
    """IP del client per l'header x-opencode-session (rispetta X-Forwarded-For)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else ""


def _session_of(request: Request) -> str | None:
    """Header di sessione in arrivo dal client (passthrough upstream).

    Priorità: x-opencode-session > x-session-affinity > x-session-id.
    opencode client (verificato via sniffing) NON invia x-opencode-session ma
    invia x-session-affinity/x-session-id con lo stesso valore del session_id
    del body: usandoli si replica il comportamento nativo invece di generare
    un hash inventato.
    """
    for name in ("x-opencode-session", "x-session-affinity", "x-session-id"):
        v = (request.headers.get(name) or "").strip()
        if v:
            return v
    return None


def _require_master(request: Request) -> JSONResponse | None:
    gw = _gw()
    auth = gw.authn.authenticate(request.headers.get("authorization"))
    if not auth.ok or auth.mode != "master":
        return JSONResponse(status_code=401, content={
            "error": {"message": "admin only: master key richiesta",
                      "type": "auth_error"}})
    return None


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message}})


async def _json_body(request: Request):
    try:
        return await request.json(), None
    except Exception:
        return None, _err(400, "invalid JSON body")


# ------------------------------------------------------------------ helpers
def _deployment_view(header: list[str], row: dict, prefix: str) -> dict:
    endpoint = csv_store.endpoint_of(header, row)
    meta = _classify(row, date.today())
    profile = ""
    for h in header:
        if h.startswith(prefix) and (row.get(h) or "").strip():
            profile = h[len(prefix):]
            break
    key = (row.get(prefix + profile) or "").strip() if profile else ""
    modello = (row.get(MODEL_HEADER) or "").strip()
    # Risolve capacità tramite policy runtime
    gw = _gw()
    caps = gw.policy.caps_for(modello) if modello else frozenset({"text"})
    # membership strutturale dalla colonna caps + gruppi derivati
    from .csv_store import CAPS_TOKENS
    row_caps = sorted({t.strip().lower() for t in
                       (row.get("caps") or "").split(",") if t.strip()}
                      & CAPS_TOKENS)
    cap_groups: list[str] = []
    if row_caps and profile:
        for c in row_caps:
            if c == "text":
                continue
            base_g = f"{prefix}{profile}-{c}"
            if meta["category"] in ("free", "priority", "zen"):
                cap_groups.append(base_g)
            elif meta["category"] == "future":
                cap_groups.append(f"{base_g}-go")
            elif meta["category"] == "fallback":
                fb_sfx = getattr(gw.policy, "fallback_suffix", "-fallback")
                cap_groups.append(f"{base_g}{fb_sfx}")
    return {
        "id": csv_store.row_id(row, endpoint),
        "profile": profile,
        "modello": modello,
        "provider": (row.get(PROVIDER_HEADER) or "").strip(),
        "endpoint": endpoint,
        "data": (row.get(DATA_HEADER) or "").strip(),
        "category": meta["category"],
        "context_k": meta["context_k"],
        "max_input": meta["max_input"],
        "priority": meta["priority"],
        "enabled": bool(meta.get("enabled", True)),
        "key_masked": csv_store.mask_key(key),
        "group": (f"{prefix}{profile}-{meta['context_k']}k"
                  if profile and meta["context_k"] else ""),
        "capabilities": sorted(caps),
        "caps": row_caps,
        "cap_groups": cap_groups,
    }


def _commit_csv(header: list[str], rows: list[dict]) -> None:
    """Scrittura atomica+validata del CSV e reload sincrono della config.
    PRIMA della riscrittura: backup rotato (undo possibile)."""
    gw = _gw()
    from .journal import backup_csv
    backup_csv(gw.CSV_PATH, gw.VAR_DIR)
    csv_store.save_table(gw.CSV_PATH, header, rows, like=gw.config)
    try:
        gw.config.reload()              # già validato dal save
        # I flag dei quirk (P2-9) vivono solo in memoria: il reload ricostruisce
        # i dep dict, quindi vanno riapplicati subito.
        try:
            gw.router.apply_quirks()
        except Exception:
            pass
        log.info("[config] CSV aggiornato via admin: profili=%s deployment=%d",
                 ",".join(gw.config.profiles),
                 sum(len(v) for v in gw.config.groups.values()))
    except Exception as exc:            # non dovrebbe accadere post-validazione
        log.error("[config] reload post-admin FALLITO: %s", exc)


def _warn_if_duplicate(header: list[str], rows: list[dict],
                       new_id: str, context: str) -> bool:
    """Logga un WARNING se esiste già una riga con lo stesso id stabile
    (righe duplicate identiche). NON blocca: solo segnalazione."""
    idx, _row = csv_store.find_row(header, rows, new_id)
    if idx is not None:
        log.warning("[admin] %s: riga DUPLICATA (id=%s): le operazioni su "
                    "questo id colpiranno sempre la prima occorrenza",
                    context, new_id)
        return True
    return False


def _required_create(payload: dict) -> None:
    missing = [f for f in ("profile", "modello", "endpoint", "data", "key")
               if not str(payload.get(f) or "").strip()]
    if missing:
        raise csv_store.CsvStoreError(
            f"campi obbligatori mancanti: {missing}")
    try:
        assert int(payload.get("context")) >= 0
    except (TypeError, ValueError, AssertionError):
        raise csv_store.CsvStoreError(
            "'context' (migliaia di token) obbligatorio e >= 0") from None


# ------------------------------------------------------------- deployments
@admin_api.get("/deployments")
async def list_deployments(request: Request, profile: str | None = None):
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    header, rows = csv_store.load_table(gw.CSV_PATH)
    out = [_deployment_view(header, r, gw.config.proxy_prefix) for r in rows]
    if profile:
        out = [d for d in out if d["profile"] == profile.strip()]
    return {"count": len(out), "deployments": out}


@admin_api.post("/deployments")
async def create_deployment(request: Request):
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    payload, bad = await _json_body(request)
    if bad:
        return bad
    try:
        _required_create(payload)
        prefix = gw.config.proxy_prefix
        # FIX bootstrap: fresh install SENZA file -> header minimale di
        # partenza (il create lo estende con profilo/caps e salva).
        try:
            header, rows = csv_store.load_table(gw.CSV_PATH)
        except FileNotFoundError:
            from .config import (CONTEXT_HEADER as _C, DATA_HEADER as _D,
                                 MAX_INPUT_HEADER as _M, MODEL_HEADER as _MO,
                                 PRIORITY_HEADER as _P,
                                 PROVIDER_HEADER as _PR)
            log.warning("[admin] CSV assente (%s): creo dal primo insert",
                        gw.CSV_PATH)
            Path(gw.CSV_PATH).parent.mkdir(parents=True, exist_ok=True)
            Path(gw.CSV_PATH).touch()
            header = ["commento", _MO, _PR, "endpoint", _D, _C, _M, _P]
            rows = []
        profile = str(payload["profile"]).strip()
        header = csv_store.ensure_profile_column(header, profile, prefix)
        if "caps" in payload:
            header = csv_store.ensure_caps_column(header)
        if "enabled" in payload:
            header = csv_store.ensure_enabled_column(header)
        row = {h: "" for h in header}
        csv_store.apply_payload(row, payload, prefix)
        csv_store.write_endpoint(row, header, payload["endpoint"])
        rows.append(row)
        new_id = csv_store.row_id(row, csv_store.endpoint_of(header, row))
        _warn_if_duplicate(header, rows, new_id, "create deployment")
        _commit_csv(header, rows)
    except csv_store.CsvStoreError as exc:
        return _err(400, str(exc))
    except Exception as exc:
        return _err(400, f"CSV non valido dopo la modifica: {exc}")
    journal.record(gw.VAR_DIR, "create", {
        "profile": profile, "modello": payload.get("modello", ""),
        "id": new_id,
        "key_rotated": bool(payload.get("key"))})
    return {"ok": True, "id": new_id, "created": profile,
            "deployments_total": sum(
                len(v) for v in gw.config.groups.values())}


@admin_api.put("/deployments/{row_hash}")
async def update_deployment(row_hash: str, request: Request):
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    payload, bad = await _json_body(request)
    if bad:
        return bad
    try:
        header, rows = csv_store.load_table(gw.CSV_PATH)
        idx, row = csv_store.find_row(header, rows, row_hash)
        if idx is None:
            return _err(404, f"deployment '{row_hash}' non esiste "
                             "(ri-leggi GET /admin/deployments)")
        prefix = gw.config.proxy_prefix
        old_profile = _deployment_view(header, row, prefix)["profile"]
        if "caps" in payload:
            header = csv_store.ensure_caps_column(header)
        if "enabled" in payload:
            header = csv_store.ensure_enabled_column(header)
        new_profile = csv_store.apply_payload(row, payload, prefix, old_profile)
        header = csv_store.ensure_profile_column(header, new_profile, prefix)
        if "endpoint" in payload:
            csv_store.write_endpoint(row, header, payload["endpoint"])
        new_id = csv_store.row_id(row, csv_store.endpoint_of(header, row))
        _warn_if_duplicate(header, rows, new_id, "update deployment")
        _commit_csv(header, rows)
    except csv_store.CsvStoreError as exc:
        return _err(400, str(exc))
    except Exception as exc:
        return _err(400, f"CSV non valido dopo la modifica: {exc}")
    journal.record(gw.VAR_DIR, "update", {
        "previous_id": row_hash, "new_id": new_id,
        "fields": sorted(k for k in payload if k != "key"),
        "key_rotated": "key" in payload})
    return {"ok": True, "id": new_id, "previous_id": row_hash}


@admin_api.delete("/deployments/{row_hash}")
async def delete_deployment(row_hash: str, request: Request):
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    try:
        header, rows = csv_store.load_table(gw.CSV_PATH)
        idx, row = csv_store.find_row(header, rows, row_hash)
        if idx is None:
            return _err(404, f"deployment '{row_hash}' non esiste")
        view = _deployment_view(header, row, gw.config.proxy_prefix)
        del rows[idx]
        _commit_csv(header, rows)
    except Exception as exc:
        return _err(400, f"CSV non valido dopo la modifica: {exc}")
    journal.record(gw.VAR_DIR, "delete", {"id": row_hash,
                                          "modello": view["modello"],
                                          "profile": view["profile"]})
    return {"ok": True, "deleted": view["modello"],
            "profile": view["profile"]}


@admin_api.post("/deployments/bulk")
async def bulk_deployments(request: Request):
    """Operazioni multiple in UNA chiamata; batch ATOMICO: se una sola op
    è invalida, NESSUNA viene applicata."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    body, bad = await _json_body(request)
    if bad:
        return bad
    ops = body.get("operations") if isinstance(body, dict) else None
    if not isinstance(ops, list) or not ops:
        return _err(400, "'operations' deve essere una lista non vuota")

    results: list[dict] = []
    try:
        header, rows = csv_store.load_table(gw.CSV_PATH)
        prefix = gw.config.proxy_prefix
        for n, op in enumerate(ops):
            action = op.get("action") if isinstance(op, dict) else None
            # i campi di controllo non entrano mai nel payload dati
            data_op = {k: v for k, v in op.items()
                       if k not in ("action", "id")}
            try:
                if "caps" in data_op:
                    header = csv_store.ensure_caps_column(header)
                if "enabled" in data_op:
                    header = csv_store.ensure_enabled_column(header)
                if action == "create":
                    _required_create(data_op)
                    prof = str(data_op["profile"]).strip()
                    header = csv_store.ensure_profile_column(
                        header, prof, prefix)
                    row = {h: "" for h in header}
                    csv_store.apply_payload(row, data_op, prefix)
                    csv_store.write_endpoint(row, header, data_op["endpoint"])
                    rows.append(row)
                    _id = csv_store.row_id(
                        row, csv_store.endpoint_of(header, row))
                    _warn_if_duplicate(header, rows, _id, "bulk create")
                    results.append({
                        "op": n, "action": "create", "ok": True, "id": _id})
                elif action == "update":
                    idx, row = csv_store.find_row(
                        header, rows, str(op.get("id", "")))
                    if idx is None:
                        raise csv_store.CsvStoreError(
                            f"id '{op.get('id')}' non trovato")
                    old_profile = _deployment_view(
                        header, row, prefix)["profile"]
                    np_ = csv_store.apply_payload(
                        row, data_op, prefix, old_profile)
                    header = csv_store.ensure_profile_column(
                        header, np_, prefix)
                    if "endpoint" in data_op:
                        csv_store.write_endpoint(
                            row, header, data_op["endpoint"])
                    results.append({"op": n, "action": "update", "ok": True})
                elif action == "delete":
                    idx, row = csv_store.find_row(
                        header, rows, str(op.get("id", "")))
                    if idx is None:
                        raise csv_store.CsvStoreError(
                            f"id '{op.get('id')}' non trovato")
                    del rows[idx]
                    results.append({"op": n, "action": "delete", "ok": True})
                else:
                    raise csv_store.CsvStoreError(
                        "action deve essere create|update|delete")
            except csv_store.CsvStoreError as exc:
                results.append({"op": n, "action": action, "ok": False,
                                "error": str(exc)})
        failed = [r for r in results if not r["ok"]]
        if failed:
            return JSONResponse(status_code=400, content={
                "error": {"message": f"{len(failed)}/{len(ops)} operazioni "
                                     "invalide: NESSUNA applicata "
                                     "(batch atomico)"},
                "results": results})
        _commit_csv(header, rows)
    except Exception as exc:
        return _err(400, f"CSV non valido dopo la modifica: {exc}")
    actions: dict[str, int] = {}
    for r_ in results:
        actions[r_.get("action", "?")] = actions.get(r_.get("action", "?"), 0) + 1
    journal.record(gw.VAR_DIR, "bulk", {"count": len(ops), "actions": actions})
    return {"ok": True, "applied": len(ops), "results": results}


@admin_api.get("/deployments/expiring")
async def deployments_expiring(request: Request, days: int = 7):
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    header, rows = csv_store.load_table(gw.CSV_PATH)
    return {"days": days,
            "expiring": csv_store.expiring(rows, header, days)}
@admin_api.get("/history")
async def admin_history(request: Request, limit: int = 50):
    """Ultime operazioni admin (più recenti prime). Nessun valore segreto."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    return journal.history(gw.VAR_DIR, limit)


@admin_api.get("/repairs")
async def admin_repairs(request: Request, limit: int = 0):
    """Conteggi delle riparazioni/salvataggi tool-call (dal ledger persistente).
    `by_family` = le 2 tipologie (repair / salvage); `by_kind` = sotto-tipi."""
    denied = _require_master(request)
    if denied:
        return denied
    from . import repairlog
    return repairlog.aggregate(limit=limit)


@admin_api.post("/profiles/purge")
async def purge_profile(request: Request):
    """Rimuove la COLONNA di un profilo dal CSV (solo se zero righe la usano).
    Serve dopo aver eliminato tutti i suoi deployment: igiene dell'header."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    body, bad = await _json_body(request)
    if bad:
        return bad
    profile = str((body or {}).get("profile") or "").strip()
    if not profile:
        return _err(400, "'profile' obbligatorio")
    try:
        header, rows = csv_store.load_table(gw.CSV_PATH)
        col = gw.config.proxy_prefix + profile
        if col not in header:
            return _err(404, f"colonna '{col}' non esiste nel CSV")
        used = sum(1 for r in rows if (r.get(col) or "").strip())
        if used:
            return _err(400, f"il profilo '{profile}' ha ancora {used} "
                             "deployment: elimina le righe prima del purge")
        new_header = [h for h in header if h != col]
        before = len(header)
        _commit_csv(new_header, rows)
    except csv_store.CsvStoreError as exc:
        return _err(400, str(exc))
    except Exception as exc:
        return _err(400, f"CSV non valido dopo la modifica: {exc}")
    journal.record(gw.VAR_DIR, "profiles_purge", {"profile": profile})
    return {"ok": True, "purged": profile,
            "columns": [before, len(new_header)]}


# ---------------------------------------------------------------- profiles
@admin_api.get("/profiles")
async def list_profiles(request: Request):
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    pol = gw.router.policy
    out = []
    for p in gw.config.profiles:
        groups = sorted(
            g for g in gw.config.groups
            if g.startswith(gw.config.proxy_prefix + p + "-"))
        deps = sum(len(gw.config.groups[g]) for g in groups)
        out.append({
            "name": p,
            "base_model": gw.config.proxy_prefix + p,
            "dims_k": gw.config.profile_dims.get(p, []),
            "groups": len(groups),
            "deployments": deps,
            "step_up_pct": pol.step_up_for(p),
            "speed_min_dim_k": pol.speed_min_for(p),
            "speed_qualify_pct": pol.speed_qualify_for(p),
        })
    return {"count": len(out), "profiles": out}


# ------------------------------------------------------------------- state
@admin_api.get("/state")
async def state(request: Request):
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    now = time.time()
    cooldowns = [{"unique": u, "remaining_sec": round(e - now),
                  "attempts": gw.router.stats_for(u).fail_streak}
                 for u, e in gw.router._cooldown.items() if e > now]
    sticky = [{"session_id": s, "group": t}
              for s, (t, _ts) in gw.router._sticky.items()]
    pol = gw.router.policy
    # conteggio deployment per capacità risolta + contatori audio/caps
    cap_counts: dict[str, int] = {}
    for deps in gw.config.groups.values():
        for d in deps:
            for c in gw.policy.caps_for(d["model"]):
                cap_counts[c] = cap_counts.get(c, 0) + 1
    from . import metrics as _mx
    counters = _mx.snapshot(("nx_caps_requests_total", "nx_caps_unroutable_total",
                             "nx_tts_total", "nx_stt_total", "nx_images_total"))
    counters_json = {n: {",".join(k): v for k, v in series.items()}
                     for n, series in counters.items()}
    health = getattr(gw.router, "last_health", None) or {
        "last_cycle_at": None, "marked": 0, "accounts": 0,
        "enabled": pol.proactive_health}
    return {
        "service": pol.service_name,
        "prefix": gw.config.proxy_prefix,
        "profiles": gw.config.profiles,
        "groups": len(gw.config.groups),
        "deployments": sum(len(v) for v in gw.config.groups.values()),
        "cooldowns_active": cooldowns,
        "sticky_sessions": sticky,
        # budget guard (Feature no-spreco): finestre + cap appresi dai 429
        "budget": {
            "enabled": bool((pol.budget_guard or {}).get("enabled")),
            "deployments": {
                u: {"minute_calls": s.minute_calls, "day_calls": s.day_calls,
                    "min_cap_learned": s.min_cap_learned or None,
                    "day_cap_learned": s.day_cap_learned or None}
                for u in gw.router._stats
                if (s := gw.router._stats[u])
                and (s.minute_calls or s.day_calls
                     or s.min_cap_learned or s.day_cap_learned)}},
        "capabilities": {
            "routing_enabled": pol.routing_active(),
            "patterns": len(pol.model_capabilities),
            "auto_learn": {"mode": pol.cap_auto_learn,
                           "threshold": pol.cap_auto_learn_threshold,
                           "strikes": gw.router.cap_strikes_view()},
            "per_capability": dict(sorted(cap_counts.items())),
            "fallback": {p: (gw.router.capability_chains(p)
                             if hasattr(gw.router, "capability_chains")
                             else {})
                         for p in gw.config.profiles},
            "groups": {p: (gw.router.capability_groups_counts(p)
                           if hasattr(gw.router,
                                      "capability_groups_counts") else {})
                       for p in gw.config.profiles},
            "multimodal_last_resort": {
                "enabled": bool(getattr(pol, "multimodal_last_resort", True)),
                "deferred": dict(getattr(gw.router, "media_deferred", {}) or {}),
            },
            "same_model_failover": {
                "enabled": bool(getattr(pol, "gen_same_model_failover", True)),
                "crossed": dict(getattr(gw.router, "gen_cross_model", {}) or {}),
                "sticky": {g: m for g, m in
                           getattr(gw.router, "_gen_last_model", {}).items()},
            },
            "counters": counters_json,
        },
        "health": health,
        "adaptive": {
            "enabled": pol.adaptive_pick,
            "tracked": len(gw.router._stats),
            "recency_halflife_sec": pol.recency_halflife_sec,
            "latency_ref_ms": pol.latency_ref_ms,
            "escalation_pin": {
                "enabled": bool(getattr(pol, "escalation_pin", True)),
                "ttl_sec": int(getattr(pol, "escalation_pin_ttl_sec", 300)),
                "probe_dims": int(getattr(pol, "escalation_pin_probe_dims", 2)),
                "probe_retry": bool(getattr(pol, "escalation_pin_probe_retry", True)),
                "probe_random": bool(getattr(pol, "escalation_pin_probe_random", True)),
                # bucket_chiesto -> {winner, eta_sec, modello, gruppo_reale}
                "pins": {g: {"winner": u,
                             "eta_sec": round(max(0.0, time.time() - ts), 1),
                             "model": (gw.config.deployment_by_unique(u) or {}).get("model"),
                             "served_group": (gw.config.deployment_by_unique(u) or {}).get("group")}
                         for g, (u, ts) in getattr(gw.router, "_esc_win", {}).items()},
            },
            "session_dep_guard": {
                "enabled": bool(getattr(pol, "session_dep_guard_enabled", True)),
                "sec": int(getattr(pol, "session_dep_guard_sec", 900)),
                "tracked": len(getattr(gw.router, "_dep_last_session", {}) or {}),
            },
            "warm_pool": {
                "enabled": bool(getattr(pol, "warm_pool_enabled", True)),
                "ttl_sec": int(getattr(pol, "warm_pool_ttl_sec", 0) or 0),
                "max_attempts": int(getattr(pol, "warm_pool_max_attempts", 0) or 0),
                "tracked": len(getattr(gw.router, "_dep_last_session", {}) or {}),
                "borrow_enabled": bool(getattr(pol, "warm_borrow_enabled", True)),
                "borrow_idle_sec": float(
                    getattr(pol, "warm_borrow_idle_sec", 240.0) or 0.0),
                "borrow_selectable": bool(
                    getattr(pol, "warm_borrow_selectable", True)),
                "borrowable_now": len(gw.router._lendable_set()),
            },
            "endpoint_quarantine": gw.router.endpoint_quarantine_view(),
            "degraded": gw.router.degraded_view(),
            "key_leases": gw.router.key_leases_view(),
            "quirks": gw.router.quirks_view(),
            "pressure": {
                "cooldowns_total":
                    gw.router.pressure_view(limit=0)["cooldowns_total"],
            },
            "cold_spread": {
                "pct": float(getattr(pol, "cold_spread_pct", 0.20) or 0.0),
                "min_pool": int(getattr(pol, "ladder_skip_after", 0) or 0),
                "tracked": len(getattr(gw.router, "_usage_times", {}) or {}),
            },
        },
        "policy": {
            "step_up_pct": pol.step_up_pct,
            "step_up_per_profile": pol.profile_step_up_pct,
            "speed_hotwords": len(pol.speed_hotwords),
            "speed_min_dim_k": pol.speed_min_dim_k,
            "aliases": pol.aliases,
            "estimate_divisor": pol.estimate_divisor,
            "sticky_ttl_sec": pol.sticky_ttl_sec,
            "cooldown_sec": pol.cooldown_sec,
        },
    }


@admin_api.post("/cooldowns/clear")
async def clear_cooldowns(request: Request):
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    body, bad = await _json_body(request)
    if bad:
        return bad
    unique = (body or {}).get("unique")
    if unique:
        removed = gw.router._cooldown.pop(unique, None) is not None
        return {"ok": True, "cleared": [unique] if removed else []}
    cleared = list(gw.router._cooldown)
    gw.router._cooldown.clear()
    return {"ok": True, "cleared": cleared}



@admin_api.post("/reload", tags=["admin"])
async def reload_gateway(request: Request):
    """Forza reload della configurazione CSV e policy."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    try:
        gw.config.reload()
        try:
            gw.router.apply_quirks()
        except Exception:
            pass
        return {"ok": True, "message": "configurazione ricaricata"}
    except Exception as exc:
        return _err(500, f"reload failed: {exc}")


@admin_api.post("/pressure/clear")
async def clear_pressure(request: Request):
    """Azzera cooldown/penalita'/finestre di fallimento (operatore).

    Body opzionale: {"unique": "<dep>"} oppure {"model": "<modello>"}; senza
    filtri azzera tutto. La pressione si ricostruisce dai risultati live."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    body, bad = await _json_body(request)
    if bad:
        return bad
    body = body or {}
    return gw.router.clear_pressure(model=body.get("model"),
                                    unique=body.get("unique"))


@admin_api.post("/pressure/inspect")
async def inspect_pressure(request: Request):
    """Vista dettagliata del perche' i deployment vengono saltati."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    body, bad = await _json_body(request)
    if bad:
        return bad
    try:
        limit = int((body or {}).get("limit", 40))
    except (TypeError, ValueError):
        limit = 40
    return gw.router.pressure_view(limit=max(0, limit))


@admin_api.post("/sessions/release")
async def release_sessions(request: Request):
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    body, bad = await _json_body(request)
    if bad:
        return bad
    sid = (body or {}).get("session_id")
    if sid:
        removed = gw.router._sticky.pop(str(sid), None) is not None
        return {"ok": True, "released": [str(sid)] if removed else []}
    released = list(gw.router._sticky)
    gw.router._sticky.clear()
    return {"ok": True, "released": released}


# ------------------------------------------------------------------ policy
def _mask_configured(raw: dict) -> dict:
    """Copia di 'configured' con alias_keys mascherate: mai chiavi in chiaro
    nella risposta admin, nemmeno nell'eco del YAML grezzo."""
    if not isinstance(raw.get("alias_keys"), dict):
        return raw
    out = dict(raw)
    out["alias_keys"] = {k: csv_store.mask_key(v)
                         for k, v in raw["alias_keys"].items()}
    return out


@admin_api.get("/policy")
async def get_policy(request: Request):
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    raw: dict = {}
    if Path(gw.POLICY_PATH).exists():
        with open(gw.POLICY_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    pol = gw.router.policy
    return {"file": str(gw.POLICY_PATH), "configured": _mask_configured(raw),
            "effective": {
                "service_name": pol.service_name,
                "proxy_prefix": pol.proxy_prefix,
                "legacy_prefixes": pol.legacy_prefixes,
                "step_up_pct": pol.step_up_pct,
                "profile_step_up_pct": pol.profile_step_up_pct,
                "aliases": pol.aliases,
                "alias_keys_masked": {k: csv_store.mask_key(v)
                                      for k, v in pol.alias_keys.items()},
                "client_keys_masked": {k: csv_store.mask_key(v)
                                       for k, v in pol.client_keys.items()},
            "estimate_divisor": pol.estimate_divisor,
            "estimate_adaptive_enabled": pol.estimate_adaptive_enabled,
            "estimate_adaptive_shadow": pol.estimate_adaptive_shadow,
            "estimate_adaptive_auto_enable": pol.estimate_adaptive_auto_enable,
            "estimate_adaptive_auto_min_n": pol.estimate_adaptive_auto_min_n,
            "estimate_adaptive_auto_max_delta_pct":
                pol.estimate_adaptive_auto_max_delta_pct,
            "estimate_adaptive_effective": bool(
                pol.estimate_adaptive_enabled
                or (estimate_shadow_stats().get("auto") or {}).get("on")),
            "estimate_shadow": estimate_shadow_stats(),
                "sticky_ttl_sec": pol.sticky_ttl_sec,
                "cooldown_sec": pol.cooldown_sec,
                "hotwords": pol.hotwords,
                "speed_hotwords": pol.speed_hotwords,
                "speed_min_dim_k": pol.speed_min_dim_k,
                "speed_qualify_pct": pol.speed_qualify_pct,
                "profile_speed_min_dim_k": pol.profile_speed_min_dim_k,
                "profile_speed_qualify_pct": pol.profile_speed_qualify_pct,
                "hotwords_window": pol.hotwords_window,
                "scoring_weights": dict(pol.scoring_weights),
                "coalesce_cache_max": pol.coalesce_cache_max,
                "video_job_ttl_sec": pol.video_job_ttl_sec,
                "keyhealth_streak_dead_threshold":
                    pol.keyhealth_streak_dead_threshold,
                "keyhealth_success_ema_floor": pol.keyhealth_success_ema_floor,
                "ctxcompact_min_protected_msgs":
                    pol.ctxcompact_min_protected_msgs,
                "toolrepair_max_unwrap_depth": pol.toolrepair_max_unwrap_depth,
                "sniff_max_b64_chars": pol.sniff_max_b64_chars,
                "sniff_max_str_chars": pol.sniff_max_str_chars,
                "sniff_max_sse_bytes": pol.sniff_max_sse_bytes,
                "upstream_connect_timeout_sec":
                    pol.upstream_connect_timeout_sec,
                "upstream_read_timeout_sec": pol.upstream_read_timeout_sec,
                "upstream_write_timeout_sec": pol.upstream_write_timeout_sec,
                "upstream_pool_timeout_sec": pol.upstream_pool_timeout_sec,
                "upstream_max_keepalive_connections":
                    pol.upstream_max_keepalive_connections,
                "upstream_max_connections": pol.upstream_max_connections,
                "upstream_keepalive_expiry_sec":
                    pol.upstream_keepalive_expiry_sec,
                "retryable_status_codes": pol.retryable_status_codes,
                "effort_incompatible_hosts": pol.effort_incompatible_hosts,
                "probe_concurrency": pol.probe_concurrency,
                "probe_timeout_sec": pol.probe_timeout_sec,
                "playground_timeout_sec": pol.playground_timeout_sec,
                "playground_max_attempts": pol.playground_max_attempts,
                "min_output_floor": pol.min_output_floor,
                "response_model": pol.response_model,
                "adaptive_pick": pol.adaptive_pick,
                "deployment_sticky": pol.deployment_sticky,
                "recency_halflife_sec": pol.recency_halflife_sec,
                "go_recency_halflife_sec": pol.go_recency_halflife_sec,
                "latency_ref_ms": pol.latency_ref_ms,
                "qc_json": asdict(pol.qc_json),
                "qc_sanity": asdict(pol.qc_sanity),
                "cooldown_escalation": pol.cooldown_escalation,
                "max_cooldown_sec": pol.max_cooldown_sec,
                "proactive_health": pol.proactive_health,
                "capability_routing": {
                    "enabled": pol.routing_active(),
                    "auto_learn": pol.cap_auto_learn,
                    "auto_learn_threshold": pol.cap_auto_learn_threshold,
                    "capabilities_default": sorted(pol.capabilities_default),
                    "image_token_estimate": pol.image_token_estimate,
                    "images_chat_fallback": pol.images_chat_fallback,
                    "model_capabilities": {k: list(v) for k, v
                                           in pol.model_capabilities.items()},
                },
                "go_refund": {
                    "enabled": pol.go_refund_enabled,
                    "pct": pol.go_refund_pct,
                    "min_turns": pol.go_refund_min_turns,
                    "max_turns": pol.go_refund_max_turns,
                },
            }}


def _apply_policy_patch(current: dict, patch: dict) -> dict:
    """Merge del patch sul yaml corrente: scalari sostituiti; 'profiles'
    unito per-profilo; 'alias_keys' unito per-alias (valore vuoto/null =
    cancella l'override, torna al pool); liste/aliases sostituiti se forniti."""
    merged = dict(current)
    for k, v in patch.items():
        if k == "profiles" and isinstance(v, dict) \
                and isinstance(merged.get("profiles"), dict):
            merged["profiles"] = {**merged["profiles"], **v}
        elif k == "alias_keys" and isinstance(v, dict):
            existing = dict(merged.get("alias_keys") or {})
            for ak, av in v.items():
                if av in (None, ""):
                    existing.pop(ak, None)
                else:
                    existing[ak] = av
            merged["alias_keys"] = existing
        elif k == "client_keys" and isinstance(v, dict):
            existing = dict(merged.get("client_keys") or {})
            for ak, av in v.items():
                if av in (None, ""):
                    existing.pop(ak, None)
                else:
                    existing[ak] = av
            merged["client_keys"] = existing
        else:
            merged[k] = v
    return merged


def _persist_policy_merged(gw, merged: dict) -> Policy | None:
    """Scrittura atomica+validata del yaml unito e swap dei riferimenti runtime.
    Ritorna la Policy fresca o None se invalida (file intatto)."""
    policy_path = Path(gw.POLICY_PATH)
    fd, tmp_name = tempfile.mkstemp(dir=str(policy_path.parent),
                                    suffix=".tmp.yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
        fresh = Policy.load(tmp_name)          # validazione preventiva
        os.replace(tmp_name, policy_path)      # atomico
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        return None
    # swap immediato dei riferimenti (stessa manovra del watcher)
    gw.router.policy = fresh
    globals()["policy"] = fresh
    gw.policy = fresh
    return fresh


# ------------------------------------------------------- auto-learn capacità
def strip_cap_from_map(map_: dict[str, list[str]], model: str,
                       cap: str, floor: tuple[str, ...] = ("text",)) -> dict[str, list[str]]:
    """PURa: mappa aggiornata con le capacità di `model` ridotte di `cap`.

    Inserisce/aggiorna una entry ESPLICITA per il modello (vince già alla
    risoluzione) preservando le glob per gli altri modelli. Se la rimozione
    svuota tutto, applica un floor minimo (default ["text"]) per non rendere
    il modello instradabile a nulla."""
    import fnmatch as _fn
    resolved: set[str] = set()
    if model in map_:
        resolved = set(map_[model])
    else:
        best_len = -1
        for pat, caps in map_.items():
            if _fn.fnmatch(model, pat) and len(pat) > best_len:
                best_len = len(pat)
                resolved = set(caps)
    if not resolved:
        resolved = {"text"}
    newcaps = sorted(resolved - {cap})
    if not newcaps:
        newcaps = list(floor)
    out = dict(map_)
    out[model] = newcaps
    return out


def remove_cap_for_model(model: str, cap: str, evidence: str = "",
                         count: int = 0) -> dict | None:
    """AUTO-LEARN (mode=auto): rimuove `cap` da `model` nella mappa con
    scrittura atomica validata + journal. Ritorna il report o None su errore."""
    gw = _gw()
    try:
        current: dict = {}
        if Path(gw.POLICY_PATH).exists():
            with open(gw.POLICY_PATH, encoding="utf-8") as f:
                current = yaml.safe_load(f) or {}
        cr = dict(current.get("capability_routing") or {})
        mc = dict(cr.get("model_capabilities") or {})
        before = sorted(gw.policy.caps_for(model))
        mc2 = strip_cap_from_map(mc, model, cap)
        cr["model_capabilities"] = mc2
        nxt = dict(current)
        nxt["capability_routing"] = cr
        fresh = _persist_policy_merged(gw, nxt)
        if fresh is None:
            log.error("[caps][auto-learn] persist fallita per %s/%s", model, cap)
            return None
        after = sorted(fresh.caps_for(model))
        report = {"model": model, "cap": cap, "count": count,
                  "evidence": (evidence or "")[:200],
                  "before": before, "after": after,
                  "pattern_edited": model}
        journal.record(gw.VAR_DIR, "cap_auto_learn", report)
        log.warning("[caps][auto-learn] rimossa '%s' da %s dopo %d strike "
                    "(%s -> %s). Revert: PATCH capability_routing."
                    "model_capabilities", cap, model, count,
                    before, after)
        return report
    except Exception as exc:                 # noqa: BLE001
        log.error("[caps][auto-learn] errore su %s/%s: %s", model, cap, exc)
        return None


@admin_api.patch("/policy")
async def patch_policy(request: Request):
    """Modifica gateway.yaml A CALDO con validazione preventiva su tmp:
    yaml invalido -> nessun cambio. Effetto immediato sui routing nuovi."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    patch, bad = await _json_body(request)
    if bad:
        return bad
    if not isinstance(patch, dict) or not patch:
        return _err(400, "body deve essere un oggetto non vuoto")

    current: dict = {}
    if Path(gw.POLICY_PATH).exists():
        with open(gw.POLICY_PATH, encoding="utf-8") as f:
            current = yaml.safe_load(f) or {}

    merged = _apply_policy_patch(current, patch)

    fresh = _persist_policy_merged(gw, merged)
    if fresh is None:
        return _err(400, "policy non valida: yaml rifiutato (file intatto)")

    log.info("[policy] aggiornata via admin: step_up=%s%% aliases=%d",
             fresh.step_up_pct, len(fresh.aliases))
    return {"ok": True, "effective": {
        "step_up_pct": fresh.step_up_pct,
        "profile_step_up_pct": fresh.profile_step_up_pct,
        "speed_hotwords": fresh.speed_hotwords,
        "speed_min_dim_k": fresh.speed_min_dim_k,
        "speed_qualify_pct": fresh.speed_qualify_pct,
        "aliases": fresh.aliases,
        "alias_keys_masked": {k: csv_store.mask_key(v)
                              for k, v in fresh.alias_keys.items()},
        "client_keys_masked": {k: csv_store.mask_key(v)
                               for k, v in fresh.client_keys.items()},
        "adaptive_pick": fresh.adaptive_pick}}


from .policy import Policy          # noqa: E402  (dopo l'uso nei type hints)


# --------------------------------------------------- csv raw (lettura/scrittura)
def _csv_backups(var_dir) -> list[dict]:
    """Ultimi 5 backup keys_rotation-*.csv da var/backups/ (mtime desc)."""
    bdir = Path(var_dir) / "backups"
    out = []
    if bdir.exists():
        for f in bdir.glob("keys_rotation-*.csv"):
            try:
                st = f.stat()
                out.append({"filename": f.name, "size": st.st_size,
                            "mtime": int(st.st_mtime)})
            except OSError:
                continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:5]


def _csv_parsed_masked(raw: str, prefix: str) -> tuple[list[str], list[dict], int]:
    """Header + righe come dict con le colonne-chiave (quelle che iniziano col
    proxy_prefix) MASCHERATE. Su CSV vuoto/rotto -> ([], [], 0)."""
    import io
    import csv as _csv
    try:
        rd = list(_csv.reader(io.StringIO(raw)))
    except Exception:
        return [], [], 0
    rd = [r for r in rd if r and any(c.strip() for c in r)]
    if not rd:
        return [], [], 0
    header = [h.strip() for h in rd[0]]
    key_cols = {i for i, h in enumerate(header) if h.startswith(prefix)}
    rows = []
    for line in rd[1:]:
        row = {}
        for i, h in enumerate(header):
            v = line[i] if i < len(line) else ""
            row[h] = csv_store.mask_key(v) if i in key_cols and v else v
        rows.append(row)
    return header, rows, len(rows)


@admin_api.get("/csv")
async def admin_csv_get(request: Request):
    """GET /admin/csv (master): {path, raw, parsed:{header,rows}, count, backups}.

    `raw` e' il testo grezzo del file (puo' contenere chiavi in chiaro:
    master-only). `parsed.rows` ha invece le colonne-chiave MASCHERATE.
    CSV assente o vuoto -> raw="", parsed vuoto, count 0.
    """
    if err := _require_master(request):
        return err
    gw = _gw()
    raw = ""
    try:
        raw = Path(gw.CSV_PATH).read_text(encoding="utf-8-sig")
    except (FileNotFoundError, OSError):
        raw = ""
    header, rows, count = _csv_parsed_masked(raw, gw.config.proxy_prefix)
    return {"path": str(gw.CSV_PATH), "raw": raw,
            "parsed": {"header": header, "rows": rows},
            "count": count, "backups": _csv_backups(gw.VAR_DIR)}


@admin_api.put("/csv")
async def admin_csv_put(request: Request):
    """PUT /admin/csv (master): sostituisce l'intero CSV.

    Body {"raw": "<csv testuale>"}. Valida su tmp (csv_store.load_table +
    save_table via _commit_csv, che fa GIA' backup+reload). 400 se il CSV e'
    invalido (file live INTATTO). Idempotente. -> {ok, backup, rows}.
    """
    if err := _require_master(request):
        return err
    body, bad = await _json_body(request)
    if bad:
        return bad
    if not isinstance(body, dict) or not isinstance(body.get("raw"), str):
        return _err(400, "'raw' (stringa) obbligatorio nel body")
    raw = body["raw"]

    tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                      encoding="utf-8")
    try:
        tmp.write(raw)
        tmp.close()
        header, rows = csv_store.load_table(tmp.name)   # parsing/base validate
    except (csv_store.CsvStoreError, OSError, ValueError) as exc:
        return _err(400, f"CSV non valido: {exc}")
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    try:
        _commit_csv(header, rows)          # backup + save_table (valida) + reload
    except (csv_store.CsvStoreError, ValueError) as exc:
        # save_table valida sul tmp PRIMA di os.replace: il CSV live resta intatto.
        return _err(400, f"CSV non valido: {exc}")

    backups = _csv_backups(_gw().VAR_DIR)
    journal.record(_gw().VAR_DIR, "csv_put", {"rows": len(rows)})
    return {"ok": True,
            "backup": backups[0]["filename"] if backups else None,
            "rows": len(rows)}


# ------------------------------------------------- policy raw (yaml full-replace)
def _policy_effective_compact(pol) -> dict:
    """Sottoinsieme di `effective` per la risposta del PUT raw (chiavi
    mascherate)."""
    return {
        "step_up_pct": pol.step_up_pct,
        "profile_step_up_pct": pol.profile_step_up_pct,
        "aliases": len(pol.aliases),
        "alias_keys_masked": {k: csv_store.mask_key(v)
                              for k, v in pol.alias_keys.items()},
        "client_keys_masked": {k: csv_store.mask_key(v)
                               for k, v in pol.client_keys.items()},
        "speed_min_dim_k": pol.speed_min_dim_k,
        "adaptive_pick": pol.adaptive_pick,
        "deployment_sticky": pol.deployment_sticky,
        "go_recency_halflife_sec": pol.go_recency_halflife_sec,
        "capability_routing_enabled": pol.routing_active(),
    }


def _persist_policy_raw(gw, raw_text: str):
    """Sostituisce l'INTERO gateway.yaml col testo fornito (nessun merge).
    Valida su tmp con Policy.load; su OK: backup best-effort del file corrente
    in var/backups/gateway.yaml-<ts>.yaml, os.replace atomico, swap dei
    riferimenti runtime. Ritorna la Policy fresca, o None se invalida
    (file live INTATTO)."""
    from .policy import Policy as _Policy
    policy_path = Path(gw.POLICY_PATH)
    fd, tmp_name = tempfile.mkstemp(dir=str(policy_path.parent),
                                    suffix=".tmp.yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw_text)
        fresh = _Policy.load(tmp_name)             # validazione preventiva
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        return None
    # backup del file corrente (best-effort, non blocca)
    try:
        if policy_path.exists():
            bdir = Path(gw.VAR_DIR) / "backups"
            bdir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            (bdir / f"gateway.yaml-{ts}.yaml").write_text(
                policy_path.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        log.warning("[policy] backup pre-raw fallito: %s", exc)
    try:
        os.replace(tmp_name, policy_path)          # atomico
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        return None
    gw.router.policy = fresh
    globals()["policy"] = fresh
    gw.policy = fresh
    return fresh


@admin_api.get("/policy/raw")
async def get_policy_raw(request: Request):
    """GET /admin/policy/raw (master): {path, raw}. `raw` senza masking
    (master-only). File assente -> raw ""."""
    if err := _require_master(request):
        return err
    gw = _gw()
    raw = ""
    try:
        raw = Path(gw.POLICY_PATH).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        raw = ""
    return {"path": str(gw.POLICY_PATH), "raw": raw}


@admin_api.put("/policy/raw")
async def put_policy_raw(request: Request):
    """PUT /admin/policy/raw (master): sostituisce l'INTERO gateway.yaml col
    testo fornito (NIENTE merge — usa PATCH /admin/policy per i patch parziali).
    Body {"raw": "<yaml testuale>"}. Valida su tmp; 400 se invalida (file
    intatto). -> {ok, validated, reloaded, effective}."""
    if err := _require_master(request):
        return err
    body, bad = await _json_body(request)
    if bad:
        return bad
    if not isinstance(body, dict) or not isinstance(body.get("raw"), str):
        return _err(400, "'raw' (stringa) obbligatorio nel body")
    gw = _gw()
    fresh = _persist_policy_raw(gw, body["raw"])
    if fresh is None:
        return _err(400, "policy non valida: yaml rifiutato (file intatto)")
    log.info("[policy] gateway.yaml sostituito via admin (raw): "
             "step_up=%s%% aliases=%d", fresh.step_up_pct, len(fresh.aliases))
    journal.record(gw.VAR_DIR, "policy_raw", {"aliases": len(fresh.aliases)})
    return {"ok": True, "validated": True, "reloaded": True,
            "effective": _policy_effective_compact(fresh)}


# --------------------------------------------------- backups (list + restore)
_BACKUP_NAME_RE = re.compile(r"^(keys_rotation-|gateway\.yaml-)[A-Za-z0-9._-]+$")


def _backups_dir(gw) -> Path:
    return Path(gw.VAR_DIR) / "backups"


def _list_backups(gw, pattern: str) -> list[dict]:
    bdir = _backups_dir(gw)
    out = []
    if bdir.exists():
        for f in bdir.glob(pattern):
            try:
                st = f.stat()
                out.append({"filename": f.name, "size": st.st_size,
                            "mtime": int(st.st_mtime)})
            except OSError:
                continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


@admin_api.get("/backups")
async def list_backups(request: Request):
    """GET /admin/backups (master): {dir, csv:[...], yaml:[...]}."""
    if err := _require_master(request):
        return err
    gw = _gw()
    return {"dir": str(_backups_dir(gw)),
            "csv": _list_backups(gw, "keys_rotation-*.csv"),
            "yaml": _list_backups(gw, "gateway.yaml-*.yaml")}


@admin_api.post("/backups/restore")
async def restore_backup(request: Request):
    """POST /admin/backups/restore (master): {filename} -> ripristina un
    backup di var/backups/ sul file live e ricarica (config per CSV, policy
    per yaml). filename validato: regex whitelist + basename + esistenza
    DENTRO var/backups/ (niente path traversal). Consiglia di ribackuppare
    l'attuale prima."""
    if err := _require_master(request):
        return err
    body, bad = await _json_body(request)
    if bad:
        return bad
    if not isinstance(body, dict):
        return _err(400, "body deve essere un oggetto")
    fname = os.path.basename(str(body.get("filename") or ""))
    if not fname or not _BACKUP_NAME_RE.match(fname):
        return _err(404, "backup non trovato")
    gw = _gw()
    src = _backups_dir(gw) / fname
    try:
        if not src.is_file() or src.resolve().parent != _backups_dir(gw).resolve():
            return _err(404, "backup non trovato")
    except OSError:
        return _err(404, "backup non trovato")

    if fname.startswith("keys_rotation-"):
        try:
            header, rows = csv_store.load_table(src)
            _commit_csv(header, rows)          # backup dell'attuale + save + reload
        except (csv_store.CsvStoreError, ValueError) as exc:
            return _err(400, f"backup CSV non valido: {exc}")
        journal.record(gw.VAR_DIR, "restore",
                       {"filename": fname, "kind": "csv", "rows": len(rows)})
        return {"ok": True, "restored": fname, "rows": len(rows),
                "note": "ripristino = sostituzione completa; l'attuale e' "
                        "stato messo in backup prima."}

    # gateway.yaml-*
    fresh = _persist_policy_raw(gw, src.read_text(encoding="utf-8"))
    if fresh is None:
        return _err(400, "backup policy non valido (file live intatto)")
    journal.record(gw.VAR_DIR, "restore", {"filename": fname, "kind": "yaml"})
    return {"ok": True, "restored": fname,
            "effective": _policy_effective_compact(fresh),
            "note": "ripristino = sostituzione completa; l'attuale e' stato "
                    "messo in backup prima."}


# --------------------------------------------------- seeder colonna caps
def _row_caps_of(row: dict) -> list[str]:
    from .csv_store import CAPS_TOKENS
    return sorted({t.strip().lower() for t in
                   (row.get("caps") or "").split(",") if t.strip()}
                  & CAPS_TOKENS)


@admin_api.post("/capabilities/seed-from-map")
async def capabilities_seed_from_map(request: Request):
    """Propone (dry_run=true) o applica (false) la colonna `caps` per ogni
    riga, derivandola dall'attuale capability_routing.model_capabilities
    (esclusi text/tools). Scrittura ATOMICA via meccanismo bulk esistente."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    body, bad = await _json_body(request)
    if bad:
        return bad
    dry_run = bool((body or {}).get("dry_run", False))

    header, rows = csv_store.load_table(gw.CSV_PATH)
    proposals: list[dict] = []
    for row in rows:
        endpoint = csv_store.endpoint_of(header, row)
        modello = (row.get(MODEL_HEADER) or "").strip()
        profile = ""
        for h in header:
            if h.startswith(gw.config.proxy_prefix) \
                    and (row.get(h) or "").strip():
                profile = h[len(gw.config.proxy_prefix):]
                break
        proposed = sorted(
            gw.policy.caps_for(modello) - {"text", "tools"})
        current = _row_caps_of(row)
        if not profile:
            continue
        proposals.append({
            "id": csv_store.row_id(row, endpoint),
            "profile": profile,
        "modello": modello,
        "family": canonical_family(modello) if modello else "",
            "current": current,
            "proposed": proposed,
        })

    to_apply = [p for p in proposals if p["proposed"] != p["current"]]
    if dry_run:
        return {"dry_run": True, "count": len(to_apply), "total": len(proposals),
                "proposals": to_apply}

    operations = [{"action": "update", "id": p["id"],
                   "caps": ",".join(p["proposed"])} for p in to_apply]
    applied = 0
    errors: list[str] = []
    if operations:
        try:
            header2, rows2 = csv_store.load_table(gw.CSV_PATH)
            header2 = csv_store.ensure_caps_column(header2)
            by_id = {}
            for i, r in enumerate(rows2):
                rid = csv_store.row_id(r, csv_store.endpoint_of(header2, r))
                by_id[rid] = i
            prefix = gw.config.proxy_prefix
            for op in operations:
                idx = by_id.get(op["id"])
                if idx is None:
                    errors.append(f"{op['id']}: non trovato")
                    continue
                row = rows2[idx]
                cur_profile = ""
                for h in header2:
                    if h.startswith(prefix) and (row.get(h) or "").strip():
                        cur_profile = h[len(prefix):]
                        break
                csv_store.apply_payload(row, {"caps": op["caps"]},
                                        prefix, cur_profile)
                applied += 1
            _commit_csv(header2, rows2)
        except Exception as exc:             # noqa: BLE001
            return _err(400, f"seed fallito: {exc}")
    journal.record(gw.VAR_DIR, "caps_seed",
                   {"applied": applied, "skipped": len(proposals) - applied})
    return {"ok": True, "applied": applied,
            "skipped": len(proposals) - applied, "errors": errors}


def membership_removal_candidates(modello: str, cap: str) -> list[dict]:
    """AUTO-LEARN suggest: righe candidate alla rimozione del token `cap`
    (modello combacia e cap presente nella colonna caps). Solo lettura."""
    gw = _gw()
    try:
        header, rows = csv_store.load_table(gw.CSV_PATH)
    except Exception:
        return []
    out: list[dict] = []
    prefix = gw.config.proxy_prefix
    for row in rows:
        if (row.get(MODEL_HEADER) or "").strip().lower() != modello.lower():
            continue
        caps_list = _row_caps_of(row)
        if cap not in caps_list:
            continue
        profile = ""
        for h in header:
            if h.startswith(prefix) and (row.get(h) or "").strip():
                profile = h[len(prefix):]
                break
        out.append({"id": csv_store.row_id(row, csv_store.endpoint_of(header, row)),
                    "profile": profile, "caps": caps_list})
    return out


# --------------------------------------------------------- capabilities audit
def _audit_slug(name: str) -> str:
    s = name.strip().lower()
    for p in ("openai/", "mistral/", "nvidia/", "cloudflare/", "meta-llama/",
              "models/"):
        if s.startswith(p):
            s = s[len(p):]
    return re.sub(r"[^a-z0-9]+", "", s)


def _free_guess(mid: str, item: dict, base: str) -> bool:
    """Euristica 'free/zen' per un modello esposto dal provider.

    True se il provider e' zen (endpoint), l'id contiene 'free'/:free, oppure
    il provider espone il prezzo (es. OpenRouter pricing.*) ed e' zero su
    prompt+completion. Solo euristica di report: nessuna scrittura."""
    low = (mid or "").lower()
    if "zen" in (base or "").lower():
        return True
    if "free" in low:
        return True
    pr = item.get("pricing") or {}
    if isinstance(pr, dict) and ("prompt" in pr or "completion" in pr):
        def _zero(v):
            try:
                return float(v) == 0.0
            except (TypeError, ValueError):
                return False
        if _zero(pr.get("prompt")) and _zero(pr.get("completion")):
            return True
    return False


@admin_api.post("/capabilities/audit")
async def capabilities_audit(request: Request):
    """Audit server-side: per ogni account (endpoint+chiave) verifica che i
    modelli configurati esistano davvero (GET /models, zero token) e, dove il
    provider espone metadati modalità (es. OpenRouter architecture.*), suggerisce
    capability_routing.model_capabilities. NESSUNA scrittura: solo report."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    # Raggruppa per ENDPOINT: la lista /models e' identica per tutte le chiavi
    # dello stesso provider. Una GET per endpoint (prima chiave valida, con
    # fallback sulle successive), cache TTL condivisa (provider_models).
    keys_by: dict[str, list[str]] = {}
    models_by: dict[str, set[str]] = {}
    _seen_keys: dict[str, set[str]] = {}
    for deps in gw.config.groups.values():
        for d in deps:
            base = (d["api_base"] or "").rstrip("/")
            models_by.setdefault(base, set()).add(d["model"])
            ks = keys_by.setdefault(base, [])
            sk = _seen_keys.setdefault(base, set())
            if d["api_key"] and d["api_key"] not in sk:
                sk.add(d["api_key"])
                ks.append(d["api_key"])

    missing: list[dict] = []
    extra: list[dict] = []
    suggestions: dict[str, list[str]] = {}
    checked = 0
    errors: list[str] = []
    ttl = int(getattr(gw.router.policy, "provider_models_ttl_sec",
                      DEFAULT_TTL_SEC) or 0)

    async def _one(base: str, keys: list[str], models: set[str]):
        nonlocal checked
        res = await fetch_provider_models(http, base, keys, ttl_sec=ttl)
        if not res.ok:
            errors.append(f"{base} ({res.key_masked or '—'}): {res.error}")
            return
        checked += 1
        masked = res.key_masked
        items = res.items
        ids = res.ids
        # modelli disponibili dal provider ma NON configurati nel CSV:
        # candidati per arricchire (free_guess marca i free/zen).
        eff_norm = {e[7:] if e.startswith("models/") else e
                    for e in models}
        for m in items:
            mid = m.get("id", "")
            nm = mid[7:] if mid.startswith("models/") else mid
            if not nm or nm in eff_norm:
                continue
            entry = {"model": mid, "endpoint": base,
                     "key_masked": masked,
                     "free_guess": _free_guess(mid, m, base)}
            ctx = m.get("context_length") or m.get("context")
            if ctx:
                entry["context_length"] = ctx
            extra.append(entry)
        # suggerimenti capacità dai metadati provider (se presenti)
        from .capabilities import CANONICAL_CAPS
        for m in items:
            mid = m.get("id", "")
            arch = m.get("architecture") or {}
            caps: set[str] = set()
            for mod in (arch.get("input_modalities") or []):
                low = str(mod).lower()
                caps |= {"vision"} if low == "image" else \
                        {"audio"} if low == "audio" else \
                        {"video"} if low == "video" else \
                        {"text"} if low == "text" else set()
            for mod in (arch.get("output_modalities") or []):
                low = str(mod).lower()
                if low == "image":
                    caps.add("image_gen")
                elif low == "audio":
                    caps.add("audio")
            if caps and mid:
                suggestions[mid] = sorted(caps & CANONICAL_CAPS)
        for eff in models:
            if eff in ids or f"models/{eff}" in ids:
                continue
            slug = _audit_slug(eff)
            cands = [i for i in sorted(ids)
                     if slug and (slug in _audit_slug(i)
                                  or _audit_slug(i) in slug)]
            missing.append({"model": eff, "endpoint": base,
                            "key_masked": masked,
                            "candidates": cands[:4]})

    import asyncio as _asyncio
    sem = _asyncio.Semaphore(4)

    async def _guarded(base, keys, models):
        async with sem:
            await _one(base, keys, models)

    async with httpx.AsyncClient(timeout=15.0) as http:
        await _asyncio.gather(*(_guarded(b, keys_by[b], models_by[b])
                                for b in sorted(keys_by)))
    extra_free = [e for e in extra if e.get("free_guess")]
    return {"checked_at": int(time.time()), "accounts": len(keys_by),
            "accounts_checked": checked, "missing_models": missing,
            "extra_models": extra, "extra_free_models": extra_free,
            "extra_models_count": len(extra),
            "extra_free_models_count": len(extra_free),
            "cap_suggestions": dict(sorted(suggestions.items())),
            "errors": errors}


# ------------------------------------------------------------------- guide
GUIDE_PATH = Path(__file__).resolve().parent.parent / "docs" / "AGENT.md"


@admin_api.get("/guide")
async def agent_guide():
    """Auto-descrizione del protocollo per gli agenti (AGENT.md)."""
    if GUIDE_PATH.exists():
        return FileResponse(GUIDE_PATH, media_type="text/markdown; charset=utf-8")
    return JSONResponse(status_code=404, content={
        "error": {"message": "AGENT.md non trovato (docs/AGENT.md)"}})


# ------------------------------------------------------------------- probe
# Validazione ONE-SHOT delle chiavi: una chiamata reale da max_tokens=1 per
# deployment, con esito CACHATO su disco. Perche' il cache e' un requisito,
# non un vezzo: alcuni free-tier contano le CHIAMATE (non i token) — ripetere
# il probe brucierebbe quota per niente. Gli endpoint /bootstrap segnalano
# solo i deployment MAI validati; nessuno invoca mai il probe in automatico.
PROBE_PATH_NAME = "probe_results.json"
_PROBE_CONCURRENCY = 5
_PROBE_TIMEOUT_S = 20.0
# Deployment con capacita' NON-chat: non espongono /chat/completions, quindi
# il probe usa GET /models (stessa chiave) invece della POST chat.
_NON_CHAT_CAPS = frozenset({"stt", "tts", "image_gen", "video_gen"})


def _probe_path() -> Path:
    from . import main as gw
    return Path(gw.VAR_DIR) / PROBE_PATH_NAME


def _load_probe_results() -> dict:
    p = _probe_path()
    if not p.exists():
        return {}
    try:
        import json as _json
        return _json.loads(p.read_text() or "{}")
    except Exception:                       # noqa: BLE001 - file corrotto: riparti
        return {}


def _save_probe_results(res: dict) -> None:
    import json as _json
    tmp = _probe_path().with_suffix(".tmp")
    tmp.write_text(_json.dumps(res, indent=1))
    tmp.replace(_probe_path())


def probe_results_view() -> dict:
    """Risultati probe (per /bootstrap/status): chiave -> esito mascherato."""
    out = {}
    for u, r in _load_probe_results().items():
        out[u] = {"ok": bool(r.get("ok")), "ts": r.get("ts"),
                  "error_class": r.get("error_class")}
    return out


async def _probe_one(http: "httpx.AsyncClient", dep: dict,
                     force: bool, *, client_ip: str = "",
                     session: str | None = None,
                     attribution: dict | None = None) -> dict:
    """UNA chiamata di verifica (max_tokens=1). Niente note_result/mark_failed:
    il probe e' informativo e non deve avvelenare la rotazione adattiva."""
    from .forwarder import _session_headers
    res_store = _load_probe_results()
    prev = res_store.get(dep["unique"])
    key_sig = csv_store.mask_key(dep["api_key"])
    if prev and prev.get("ok") and not force \
            and prev.get("key_masked") == key_sig:
        return {"unique": dep["unique"], "cached": True,
                "ok": True, "latency_ms": prev.get("latency_ms"),
                "probed_at": prev.get("ts"),
                "note": "already validated; pass force=true to re-test"}
    t0 = time.monotonic()
    # Capacita' del deployment dal gruppo: i gruppi cap (stt/tts/image_gen/
    # video_gen) sono non-chat e NON espongono /chat/completions.
    from . import main as _gwmod
    cap = _gwmod.config.group_caps.get(dep["group"])
    non_chat = cap in _NON_CHAT_CAPS
    try:
        if non_chat:
            # Non-chat: la presenza del modello si verifica su /models
            # (una GET, niente consumo quota). ok = (status 200).
            resp = await http.get(
                f"{dep['api_base'].rstrip('/')}/models",
                headers={"Authorization": f"Bearer {dep['api_key']}",
                         **_session_headers(dep, client_ip=client_ip,
                                            session=session,
                                            attribution=attribution)},
                timeout=_tune(_gwmod, "probe_timeout_sec", _PROBE_TIMEOUT_S))
            ok = resp.status_code == 200
        else:
            _style = proto.style_of(dep)
            _chat = {"model": dep["model"], "max_tokens": 1,
                     "messages": [{"role": "user",
                                   "content": "Reply with the single letter A"}]}
            _url = proto.build_url(dep, stream=False)
            _body = (_chat if _style == proto.CHAT
                     else proto.translate_request(_style, _chat, dep))
            resp = await http.post(
                _url,
                json=_body,
                headers=proto.apply_auth(dep, {
                    "Authorization": f"Bearer {dep['api_key']}",
                    **_session_headers(dep, client_ip=client_ip,
                                       session=session,
                                       attribution=attribution)}),
                timeout=_tune(_gwmod, "probe_timeout_sec", _PROBE_TIMEOUT_S))
            ok = False
            try:
                _data = resp.json() or {}
                if not isinstance(_data, dict):
                    _data = {}
                if resp.status_code == 200 and _style != proto.CHAT:
                    _ch = proto.translate_response(_style, _data, dep)
                    ok = isinstance(_ch, dict) and bool(_ch.get("choices"))
                elif resp.status_code == 200:
                    ok = "choices" in _data
            except Exception:          # body non-JSON / non interpretabile
                ok = False
        latency = int((time.monotonic() - t0) * 1000)
        entry = {"ok": ok, "latency_ms": latency, "ts": int(time.time()),
                 "status": resp.status_code, "key_masked": key_sig,
                 "probe_kind": "models" if non_chat else "chat"}
        if not ok:
            txt = (resp.text or "")[:160]
            entry["error_class"] = (
                "no_credits" if resp.status_code == 402 else
                "rate_limited" if resp.status_code == 429 else
                "not_found" if resp.status_code == 404 else
                f"http_{resp.status_code}")
            entry["detail"] = txt
    except Exception as exc:                # noqa: BLE001 - timeout/rete
        entry = {"ok": False, "latency_ms": int((time.monotonic() - t0) * 1000),
                 "ts": int(time.time()), "key_masked": key_sig,
                 "error_class": type(exc).__name__,
                 "probe_kind": "models" if non_chat else "chat"}
    # il read-modify-write del file DEVE avvenire dopo gli await, in un
    # blocco sincrono (l'event loop non è preemptive tra le istruzioni
    # sync): due probe concorrenti non si perdono più i risultati.
    # Cache SOLO dei successi: i falliti si ritentano senza force (magari
    # era un blip), ma ogni retry costa una chiamata: sta all'agente decidere.
    if entry["ok"]:
        res_store = _load_probe_results()   # rilegura FRESCA post-await
        res_store[dep["unique"]] = entry
        _save_probe_results(res_store)
        # SBLOCCO LIFECYCLE: un probe riuscito e' la prova che la chiave
        # funziona -> pulisce dead/retired e lo streak locale.
        kh = getattr(_gwmod, "KEYHEALTH", None)
        if kh:
            kh.clear(dep["unique"])
        st = _gwmod.router._stats.get(dep["unique"])
        if st is not None:
            st.fail_streak = 0
        _gwmod.router._cooldown.pop(dep["unique"], None)
    return {"unique": dep["unique"], "cached": False, **entry}


@admin_api.post("/deployments/probe")
async def deployments_probe(request: Request):
    """Valida UN deployment: body {"unique":"..."} oppure {"id":"drow_..."}.

    force=true ripete la chiamata anche se esiste gia' un risultato OK
    cachato (default: nessuna chiamata sprecata).
    """
    gw = _gw()
    if err := _require_master(request):
        return err
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "error": {"message": "invalid JSON body"}})
    dep = None
    uniq = str(payload.get("unique") or "")
    if uniq:
        dep = gw.config.deployment_by_unique(uniq)
    else:
        did = str(payload.get("id") or "")
        if did:
            try:
                header, rows = csv_store.load_table(gw.CSV_PATH)
                _idx, row = csv_store.find_row(header, rows, did)
                modello = (row.get(csv_store.MODEL_HEADER) or "").strip()
                valori = {str(v).strip() for v in row.values()}
                for deps in gw.config.groups.values():
                    for d in deps:
                        if d["model"] == modello and \
                                d["api_key"] in valori:
                            dep = d
                            break
                    if dep:
                        break
            except Exception:               # noqa: BLE001 - id sconosciuto
                dep = None
    if dep is None:
        return JSONResponse(status_code=404, content={
            "error": {"message": "deployment non trovato: passa unique "
                                 "(GET /admin/state) oppure id drow_ "
                                 "(GET /admin/deployments)"}})
    async with httpx.AsyncClient() as http:
        out = await _probe_one(http, dep, bool(payload.get("force")),
                               client_ip=_client_ip_of(request),
                               session=_session_of(request),
                               attribution=_client_attribution(request))
    journal.record(gw.VAR_DIR, "probe", {"target": out.get("unique"),
                                         "ok": out.get("ok")})
    return out


@admin_api.post("/deployments/probe/bulk")
async def deployments_probe_bulk(request: Request):
    """Valida N deployment: {"filter":"all"|"cap:<x>"|"<profilo>","force":f}.

    Concorrenza limitata (_PROBE_CONCURRENCY) per non martellare i provider.
    I risultati OK sono permanenti su disco: rilanciare il bulk NON ri-chiama
    le chiavi sane (solo force=true le ritesta).
    """
    gw = _gw()
    if err := _require_master(request):
        return err
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    filt = str(payload.get("filter") or "all")
    force = bool(payload.get("force"))
    targets: list[dict] = []
    for gname, deps in gw.config.groups.items():
        if filt.startswith("cap:") and gw.config.group_caps.get(gname) != \
                filt[4:]:
            continue
        targets.extend(deps)
    if filt not in ("all", "*") and not filt.startswith("cap:"):
        prof_base = gw.config.profile_of_base(filt) or filt
        prefix = getattr(gw.config, "proxy_prefix", "")
        # match ESATTO del profilo con trattino terminale —
        # startswith("scrocco-llm-test") matcherebbe anche il profilo "test2".
        targets = [d for d in targets
                   if d["group"].startswith(f"{prefix}{prof_base}-")
                   or d["group"] == f"{prefix}{prof_base}"]
    import asyncio as _aio
    _cip = _client_ip_of(request)
    _sess = _session_of(request)
    _attr = _client_attribution(request)

    async def _run(dep):
        async with _aio.Semaphore(_tune(gw, "probe_concurrency", _PROBE_CONCURRENCY)):
            return await _probe_one(shared_http, dep, force,
                                    client_ip=_cip, session=_sess,
                                    attribution=_attr)

    async with httpx.AsyncClient() as shared_http:
        outs = await _aio.gather(*[_run(d) for d in targets])
    journal.record(gw.VAR_DIR, "probe_bulk",
                   {"count": len(outs),
                    "ok": sum(1 for o in outs if o.get("ok"))})
    return {"filter": filt, "count": len(outs), "results": outs}


# --------------------------------------------------------------- insights
# Analytics sul LEDGER (var/usage_ledger.jsonl): burn per profilo/modello/
# giorno, costi riportati dai provider vs stimati dal catalogo pricing.
# Lettura pura: nessuna scrittura, nessun impatto sul percorso richieste.

def _insights_aggregate(rows: list[dict], group_by: str) -> dict:
    """Aggregazione generica del ledger. Chiave di raggruppamento:
    profile | model | deployment | day | kind."""
    from collections import defaultdict
    agg: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "total_tokens": 0, "cost_reported": 0.0, "cost_est": 0.0,
        "dur_ms_sum": 0, "fb_calls": 0, "qc_discards": 0,
        "wd_fail": 0, "bad_calls": 0})
    for r in rows:
        if group_by == "day":
            key = time.strftime("%Y-%m-%d", time.localtime(r.get("ts") or 0))
        elif group_by == "profile":
            key = r.get("profile") or "-"
        elif group_by == "model":
            key = r.get("model") or "-"
        elif group_by == "deployment":
            key = r.get("dep") or "-"
        elif group_by == "kind":
            key = r.get("kind") or "chat"
        else:
            key = "-"
        u = r.get("usage") or {}
        a = agg[key]
        # righe SUMMARY (aggregato storico): `count` = numero di chiamate reali
        # rappresentate; fb/qc/wd_fail sono conteggi, non flag 0/1.
        if r.get("count"):
            n_fb = int(r.get("fb") or 0)
            n_qc = int(r.get("qc") or 0)
            n_wd_fail = int(r.get("wd_fail") or 0)
            a["calls"] += int(r["count"])
            a["bad_calls"] += min(int(r["count"]), n_fb + n_qc + n_wd_fail)
        else:
            n_fb = 1 if bool((r.get("fb") or 0) and r["fb"] > 0) else 0
            n_qc = 1 if bool(r.get("qc")) else 0
            _wd = r.get("wd")
            # wd=tier2-no-done = risposta completa, il provider omette solo
            # [DONE] -> NON e' un fallimento; ogni altro wd non-vuoto lo e'.
            n_wd_fail = 1 if (bool(_wd) and _wd != "tier2-no-done") else 0
            a["calls"] += 1
            if n_fb or n_qc or n_wd_fail:
                a["bad_calls"] += 1
        a["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        a["completion_tokens"] += int(u.get("completion_tokens") or 0)
        a["total_tokens"] += int(u.get("total_tokens")
                                 or ((u.get("prompt_tokens") or 0)
                                     + (u.get("completion_tokens") or 0)))
        a["cost_reported"] += float(u.get("cost") or 0)
        a["cost_est"] += float(u.get("cost_est") or 0)
        a["dur_ms_sum"] += int(r.get("dur_ms") or 0)
        a["fb_calls"] += n_fb
        a["qc_discards"] += n_qc
        a["wd_fail"] += n_wd_fail
    out = {}
    for k in sorted(agg):
        a = agg[k]
        calls = max(1, a["calls"])
        out[k] = {
            "calls": a["calls"],
            "prompt_tokens": a["prompt_tokens"],
            "completion_tokens": a["completion_tokens"],
            "total_tokens": a["total_tokens"],
            "cost_reported_usd": round(a["cost_reported"], 6),
            "cost_estimated_usd": round(a["cost_est"], 6),
            "avg_dur_ms": round(a["dur_ms_sum"] / calls),
            "fallback_rate": round(a["fb_calls"] / calls, 3),
            "qc_rate": round(a["qc_discards"] / calls, 3),
            "wd_fail_rate": round(a["wd_fail"] / calls, 3),
            # quota di richieste con QUALSIASI segnale di problema
            # (fallback / scarto QC / watchdog); 0..1
            "bad_rate": round(a["bad_calls"] / calls, 3)}
    return out


@admin_api.get("/insights")
async def admin_insights(request: Request, days: int = 7,
                         group_by: str = "model"):
    """Burn usage/costi aggregato dal ledger. group_by: profile|model|
    deployment|day|kind; days: finestra retroattiva (max 90)."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    days = max(1, min(int(days or 7), 90))
    if group_by not in ("profile", "model", "deployment", "day", "kind",
                        "none"):
        return _err(400, f"group_by '{group_by}' non valido")
    cutoff = time.time() - days * 86400
    rows = [r for r in await gw.LEDGER.iter_rows_async()
            if (r.get("ts") or 0) >= cutoff]
    total = {"calls": len(rows), "days": days}
    if group_by == "none":
        agg = _insights_aggregate(rows, "kind")
        # collassa sotto un'unica chiave
        merged: dict = {}
        for v in agg.values():
            for kk, vv in v.items():
                if isinstance(vv, (int, float)) and kk.endswith(
                        ("tokens", "usd", "calls")):
                    merged[kk] = merged.get(kk, 0) + vv
                elif kk == "calls":
                    merged[kk] = merged.get(kk, 0) + vv
        return {"total": total, "aggregate": merged}
    return {"total": total, "by_" + group_by: _insights_aggregate(rows,
                                                                  group_by)}


@admin_api.get("/insights/summary")
async def admin_insights_summary(request: Request):
    """Ultime 24h in forma compatta (per TUI/dashboard)."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    cutoff = time.time() - 86400
    rows = [r for r in await gw.LEDGER.iter_rows_async() if (r.get("ts") or 0) >= cutoff]
    by_kind = _insights_aggregate(rows, "kind")
    tot_tok = sum(v["total_tokens"] for v in by_kind.values())
    tot_cost_r = sum(v["cost_reported_usd"] for v in by_kind.values())
    tot_cost_e = sum(v["cost_estimated_usd"] for v in by_kind.values())
    return {"window_hours": 24,
            "calls": len(rows), "total_tokens": tot_tok,
            "cost_reported_usd": round(tot_cost_r, 6),
            "cost_estimated_usd": round(tot_cost_e, 6),
            "by_kind": by_kind}


# ------------------------------------------------------- keyhealth (F3)
# Lifecycle chiavi: classificazione dead/retired PERSISTENTE. Regola
# assoluta: MAI cancellazioni dal CSV -- solo esclusione dal routing
# reversibile via unretire o sblocco automatico da probe riuscito.

@admin_api.post("/deployments/unretire")
async def deployments_unretire(request: Request):
    """Riattiva una chiave retired/dead: body {"unique": "..."}.

    Il CSV non viene toccato; si pulisce SOLO l'evidenza di salute
    (key_health.json) e lo streak locale del router.
    """
    gw = _gw()
    if err := _require_master(request):
        return err
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={
            "error": {"message": "invalid JSON body"}})
    uniq = str(payload.get("unique") or "")
    if not gw.config.deployment_by_unique(uniq):
        return JSONResponse(status_code=404, content={
            "error": {"message": f"unique '{uniq}' sconosciuto"}})
    kh = getattr(gw, "KEYHEALTH", None)
    if kh:
        kh.clear(uniq)
    s = gw.router.stats_for(uniq)          # riparti ottimisti
    s.fail_streak = 0
    s.success_ema = None
    gw.router._cooldown.pop(uniq, None)
    journal.record(gw.VAR_DIR, "unretire", {"unique": uniq})
    return {"ok": True, "unique": uniq, "state": "healthy"}


# --------------------------------------------------------------- logs (read)
# Viste read-only dei log di servizio (var/gateway.log, var/error-audit.log).
# Nessuna scrittura: solo lettura cronologica + parsing. Helper in app/logview
# (stdlib only, nessun import da app.main per restare importabile da solo).

@admin_api.get("/logs/calls")
async def admin_logs_calls(request: Request, tail: int = 500,
                           since: float | None = None,
                           tags: str = "summary,route,identity,fallback"):
    """Ultime chiamate/routing dal gateway.log (tag selezionabili)."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    base = os.path.join(str(gw.VAR_DIR), "gateway.log")
    paths = [p for p in (base + ".1", base) if os.path.exists(p)]
    tagset = {t.strip() for t in tags.split(",") if t.strip()}
    lines = logview._read_tail_lines(paths, max(tail * 6, 3000))
    events = logview.parse_summary_lines(lines, tagset, since, tail)
    return {"events": events}


@admin_api.get("/logs/errors")
async def admin_logs_errors(request: Request, tail: int = 500,
                            since: float | None = None,
                            filter: str | None = None):
    """Ultimi errori auditati (var/error-audit.log). filter=numeric status
    oppure substring case-insensitive su error_type/error_message."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    base = os.path.join(str(gw.VAR_DIR), "error-audit.log")
    paths = [p for p in (base + ".1", base) if os.path.exists(p)]
    lines = logview._read_tail_lines(paths, max(tail * 6, 3000))
    events = logview.parse_error_lines(lines, filter, since, tail)
    return {"events": events}


# --------------------------------------------------- insights/leaderboard
# Classifica dei deployment per volume/latency/errori su finestra mobile.

def _parse_window_days(window: str, default: float = 7.0) -> float:
    """'7d'/'24h'/'90m'/'3' -> giorni (float). Fallback a default."""
    try:
        w = (window or "").strip().lower()
        if not w:
            return default
        if w.endswith("d"):
            return float(w[:-1])
        if w.endswith("h"):
            return float(w[:-1]) / 24.0
        if w.endswith("m"):
            return float(w[:-1]) / 1440.0
        return float(w)
    except Exception:
        return default


def _pctl(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


@admin_api.get("/insights/leaderboard")
async def admin_insights_leaderboard(request: Request, window: str = "7d",
                                     sort: str = "calls", order: str = "desc",
                                     profile: str | None = None):
    """Classifica deployment: calls, latenza avg/p95, error_rate (proxy
    fb+qc), ultimo uso, provider/gruppo, health, probe. Finestra `window`."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    days = _parse_window_days(window)
    cutoff = time.time() - days * 86400.0
    rows = [r for r in await gw.LEDGER.iter_rows_async()
            if r.get("dep") and (r.get("ts") or 0) >= cutoff]
    if profile:
        rows = [r for r in rows if r.get("profile") == profile]

    agg = _insights_aggregate(rows, "deployment")  # dep -> {calls, avg_dur_ms, fallback_rate, qc_rate}
    # p95 per dep: raccogli i dur_ms validi
    durs: dict[str, list[float]] = {}
    for r in rows:
        d = r.get("dep")
        v = r.get("dur_ms")
        if d and isinstance(v, (int, float)):
            durs.setdefault(d, []).append(float(v))

    probes = _load_probe_results()
    stats = gw.router._stats
    khd = gw.KEYHEALTH.data

    out_rows = []
    for dep, a in agg.items():
        meta = gw.config.deployment_by_unique(dep) or {}
        # profilo dal group: <prefix><profilo>-<dim>k  -> togli prefix e -<dim>k
        prof = None
        led_model = None
        for r in rows:
            if r.get("dep") == dep:
                prof = r.get("profile")
                led_model = r.get("model")
                break
        group = meta.get("group") or dep.rsplit("__", 2)[0]
        model = meta.get("model") or led_model or ""
        if "/" in model:
            provider = model.split("/", 1)[0]
        elif meta.get("tier"):
            provider = meta["tier"]
        else:
            provider = ""
        dl = sorted(durs.get(dep, []))
        st = stats.get(dep)
        last_used = None
        if st is not None and getattr(st, "last_used", 0):
            last_used = float(st.last_used)
        # err% = quota richieste con QUALSIASI problema (fallback / scarto QC /
        # watchdog) su questo deployment, 0..1 (proxy: l'error-rate HTTP puro
        # per-deployment non e' nel ledger).
        err_rate = round(float(a.get("bad_rate", 0) or 0), 3)
        pr = probes.get(dep) or {}
        out_rows.append({
            "dep": dep,
            "profile": prof,
            "group": group,
            "provider": provider,
            "model": model,
            "calls": a.get("calls", 0),
            "avg_dur_ms": a.get("avg_dur_ms"),
            "p95_dur_ms": (round(_pctl(dl, 0.95)) if dl else None),
            "error_rate": err_rate,
            "fb_rate": round(float(a.get("fallback_rate", 0) or 0), 3),
            "qc_rate": round(float(a.get("qc_rate", 0) or 0), 3),
            "wd_rate": round(float(a.get("wd_fail_rate", 0) or 0), 3),
            "last_used": last_used,
            "health": (khd.get(dep) or {}).get("state"),
            "probe_ms": pr.get("latency_ms"),
        })

    valid_sort = {"dep", "profile", "group", "provider", "model", "calls",
                  "avg_dur_ms", "p95_dur_ms", "error_rate", "last_used",
                  "probe_ms"}
    key = sort if sort in valid_sort else "calls"
    rev = (order or "desc").lower() != "asc"

    def _sk(row):
        v = row.get(key)
        if v is None:
            return (1, 0) if not rev else (0, 0)  # None in fondo comunque
        if isinstance(v, str):
            return (0, v.lower())
        return (0, v)
    # ordina: prima i non-None, poi per valore
    out_rows.sort(key=lambda r: (r.get(key) is None, _sk(r)), reverse=rev)
    # ma i None devono restare in fondo a prescindere da reverse:
    non_none = [r for r in out_rows if r.get(key) is not None]
    none_rows = [r for r in out_rows if r.get(key) is None]
    non_none.sort(key=lambda r: (r[key].lower() if isinstance(r[key], str) else r[key]),
                  reverse=rev)
    out_rows = non_none + none_rows

    return {"window_days": days, "count": len(out_rows), "rows": out_rows}


# --------------------------------------------------- deployments/stats
# Punteggi PERSISTITI per deployment (var/adaptive_stats.json): successi e
# fallimenti cumulativi, latenza EMA, ultimo motivo, timestamp. Sopravvivono
# al restart perche' ricaricati da router.load_stats() allo startup.
@admin_api.get("/deployments/stats")
async def admin_deployments_stats(request: Request, profile: str | None = None):
    """Punteggi persistiti per provider/modello/chiave: ok/fail, latenza, reason."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    now = time.time()
    cfg = gw.config
    khd_obj = getattr(gw, "KEYHEALTH", None)
    khd = khd_obj.data if khd_obj is not None else {}
    rows = []
    for unique, s in gw.router._stats.items():
        dep = cfg.deployment_by_unique(unique) or {}
        group = dep.get("group") or unique.rsplit("__", 2)[0]
        model = dep.get("model") or ""
        prof = None
        if group.startswith(cfg.proxy_prefix):
            rest = group[len(cfg.proxy_prefix):]
            for p in cfg.profiles:
                if rest.startswith(p + "-"):
                    prof = p
                    break
        if profile and prof != profile:
            continue
        if "/" in model:
            provider = model.split("/", 1)[0]
        elif dep.get("tier"):
            provider = dep["tier"]
        else:
            provider = ""
        cool = gw.router._cooldown.get(unique, 0.0)
        rows.append({
            "dep": unique,
            "profile": prof,
            "group": group,
            "provider": provider,
            "model": model,
            "ok": s.ok_count,
            "fail": s.fail_count,
            "calls": s.ok_count + s.fail_count,
            "success_ema": s.success_ema,
            "fail_streak": s.fail_streak,
            "fail_count_24h": s.fail_count_24h,
            "ema_latency_ms": s.ema_latency_ms,
            "last_reason": s.last_reason,
            "last_used": s.last_used or None,
            "last_success_ts": s.last_success_ts or None,
            "last_fail_ts": s.last_fail_ts or None,
            "health": (khd.get(unique) or {}).get("state") or "healthy",
            "cooldown_remaining_s": max(0, int(cool - now)) if cool else 0,
        })
    rows.sort(key=lambda r: r["fail"], reverse=True)
    return {"count": len(rows), "rows": rows}


@admin_api.get("/providers/health")
async def admin_providers_health(request: Request):
    """Salute aggregata per provider: contatori, circuit breaker, latenze."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    cfg = gw.config
    router = gw.router

    # Aggrega per provider (api_base)
    providers = {}
    for unique, s in router._stats.items():
        dep = cfg.deployment_by_unique(unique) or {}
        api_base = dep.get("api_base", "unknown")
        model = dep.get("model", "")
        provider_key = api_base

        if provider_key not in providers:
            providers[provider_key] = {
                "provider": provider_key,
                "models": set(),
                "total_deployments": 0,
                "total_ok": 0,
                "total_fail": 0,
                "total_calls": 0,
                "ema_latency_ms": 0.0,
                "latency_count": 0,
                "circuit_breakers": {"closed": 0, "open": 0, "half_open": 0},
                "deployments": []
            }
        p = providers[provider_key]
        p["models"].add(model)
        p["total_deployments"] += 1
        p["total_ok"] += s.ok_count
        p["total_fail"] += s.fail_count
        p["total_calls"] += s.ok_count + s.fail_count
        if s.ema_latency_ms:
            p["ema_latency_ms"] += s.ema_latency_ms
            p["latency_count"] += 1
        p["deployments"].append({
            "unique": unique,
            "model": model,
            "ok": s.ok_count,
            "fail": s.fail_count,
            "ema_latency_ms": s.ema_latency_ms,
            "fail_streak": s.fail_streak,
            "last_reason": s.last_reason,
        })

    # Circuit breaker status per API key
    for ak, cb in router._circuit_breakers.items():
        # Find which provider this key belongs to
        for provider_key, p in providers.items():
            for dep_info in p["deployments"]:
                dep = cfg.deployment_by_unique(dep_info["unique"]) or {}
                if router._api_key_str(dep) == ak:
                    p["circuit_breakers"][cb["state"]] = p["circuit_breakers"].get(cb["state"], 0) + 1
                    break

    # Finalize
    result = []
    for p in providers.values():
        if p["latency_count"] > 0:
            p["ema_latency_ms"] = round(p["ema_latency_ms"] / p["latency_count"], 1)
        else:
            p["ema_latency_ms"] = 0.0
        p["models"] = sorted(p["models"])
        p["success_rate"] = round(p["total_ok"] / p["total_calls"] * 100, 1) if p["total_calls"] > 0 else 100.0
        result.append(p)

    result.sort(key=lambda x: x["total_calls"], reverse=True)
    return {"providers": result, "circuit_breaker_config": {
        "threshold": getattr(router.policy, "circuit_breaker_threshold", 5),
        "timeout": getattr(router.policy, "circuit_breaker_timeout", 60.0),
        "half_open_requests": getattr(router.policy, "circuit_breaker_half_open_requests", 3),
    }}


# ------------------------------------------------------------------ playground
# Simulatore di chat READ-ONLY: rigira UNA richiesta reale (canonicalize ->
# resolve_group_for_request -> initial_pick -> loop fallback_next) e riporta il
# TRACE di routing/fallback, senza toccare lo stato del router di produzione:
#   - MAI mark_failed / note_start / note_end (niente cooldown/EMA/streak);
#   - MAI scritture su _cooldown/_stats/keyhealth;
#   - lo stato interno eventualmente accarezzato dal giro (sticky/session/
#     defer-media/chain-cross) viene SNAPSHOT e RIPRISTINATO alla fine, così
#     i prossimi pick non vedono alcun effetto.
# Timeout PER TENTATIVO: se entro 40s il deployment non ha prodotto nulla
# (nessun token) lo si abbandona -> fallback + penalita'
# (mark_failed). Il client puo' attendere: NIENTE tetto wall-clock totale, la
# catena si cammina fino a esaurirla pur di dare una risposta (bounded solo da
# MAX_ATTEMPTS + catena finita).
_PLAYGROUND_TIMEOUT_S = 90.0
_PLAYGROUND_MAX_ATTEMPTS = 128
# _cooldown / _cooldown_since NON sono nello snapshot: una penalita' inflitta a
# un deployment appeso durante la prova DEVE persistere (il traffico reale
# eviterà quel deployment). Tutto il resto dello stato viene ripristinato.
_PLAYGROUND_STATE_KEYS = (
    "_sticky", "_stats",
    "media_deferred", "gen_cross_model", "_gen_last_model",
    "_session_group", "_defer_active", "_cap_strikes", "_esc_win",
)


def _playground_reason(err: BaseException) -> str:
    """Classificazione COMPATTA del motivo di fallimento per il trace."""
    if isinstance(err, UpstreamError):
        st = err.status
        return (f"http_{st}" if st is not None and st > 0
                else f"http_{-st}" if st is not None else "network")
    if isinstance(err, asyncio.TimeoutError):
        return "timeout"
    return type(err).__name__


def _playground_content(data) -> str | None:
    """Contenuto testuale dalla risposta chat (content stringa o lista)."""
    if not isinstance(data, dict):
        return None
    try:
        msg = ((data.get("choices") or [{}])[0].get("message") or {})
    except Exception:                        # noqa: BLE001 - risposta anomala
        return None
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        texts = [p.get("text", "") for p in c
                 if isinstance(p, dict) and p.get("text")]
        return "".join(texts) or None
    return None


def _playground_result(gw, model_raw: str, model: str, profile,
                       group, trace: list[dict], attempts: int,
                       fallbacks: int, error: str,
                       reason: str | None = None) -> dict:
    """Envelope 200 del playground per gli esiti NON riusciti."""
    journal.record(gw.VAR_DIR, "playground",
                   {"model": model, "profile": profile,
                    "attempts": attempts, "fallbacks": fallbacks,
                    "ok": False, "reason": reason or error})
    return {"ok": False, "model": model_raw, "resolved_model": model,
            "profile": profile, "group": group,
            "attempts": attempts, "fallbacks": fallbacks, "trace": trace,
            "error": {"message": error}}


@admin_api.post("/playground")
async def admin_playground(request: Request):
    """Rigira UNA chat di prova e restituisce il trace routing/fallback.

    Body: {"model": str, "messages": [{role, content}, ...],
           "profile"?: str, "max_tokens"?: int}. Risolve esattamente come una
    richiesta reale (canonicalize + resolve_group_for_request con
    need=caps_for(model) se routing_active + initial_pick + loop di fallback
    via fallback_next), chiamando forwarder.call per ogni tentativo (timeout
    20s). READ-ONLY: mai mark_failed/note_start/note_end, mai scritture su
    _cooldown/_stats/keyhealth; lo stato del router viene ripristinato a fine
    giro. Risposta 200 con `trace`, `attempts`, `fallbacks`, `content`.
    """
    gw = _gw()
    if err := _require_master(request):
        return err
    body, bad = await _json_body(request)
    if bad:
        return bad
    model_raw = str(body.get("model") or "").strip()
    messages = body.get("messages")
    if not model_raw or not isinstance(messages, list) or not messages:
        return _err(400, "model (stringa) e messages (lista non vuota) "
                         "sono obbligatori")

    router = gw.router
    policy = gw.policy
    model = policy.canonicalize(model_raw)
    _cip = _client_ip_of(request)
    _sess = _session_of(request)
    # snapshot dello stato interno: il giro NON deve lasciare tracce
    saved = {k: dict(getattr(router, k)) for k in _PLAYGROUND_STATE_KEYS}
    trace: list[dict] = []
    try:
        if policy.routing_active():
            need = policy.caps_for(model)
        else:
            need = frozenset()
        ctx = estimate_tokens(messages, policy.estimate_divisor,
                              getattr(policy, "image_token_estimate", 0) or 0)
        profile = str(body.get("profile") or "").strip() or None
        if not profile:
            profile = gw.config.profile_of_base(model.split("__")[0]) \
                or gw.config.profile_of_base(model)

        group_or_explicit = router.resolve_group_for_request(
            model, messages, None, need, ctx)
        if group_or_explicit is None:
            return _playground_result(
                gw, model_raw, model, profile, None, trace,
                attempts=0, fallbacks=0,
                error=f"nessun deployment instradabile per '{model}'")

        explicit = router.is_explicit(model)
        dep = router.config.deployment_by_unique(group_or_explicit)
        if dep is None:
            dep = router.initial_pick(profile, group_or_explicit,
                                      None if explicit else need,
                                      None if explicit else ctx)
        if dep is None:
            return _playground_result(
                gw, model_raw, model, profile, group_or_explicit, trace,
                attempts=0, fallbacks=0,
                error="nessun deployment disponibile")

        scope = "group" if explicit else "chain"
        up_payload: dict = {"model": model, "messages": messages}
        if body.get("max_tokens") is not None:
            mt = body.get("max_tokens")
            try:
                mt = int(mt)
            except (TypeError, ValueError):
                mt = None
            if mt is not None:
                up_payload["max_tokens"] = mt

        tried: set[str] = set()
        attempts = 0
        fallbacks = 0
        last_err: BaseException | None = None
        requested_group = dep["group"] if dep else None
        while dep is not None and attempts < _tune(gw, "playground_max_attempts", _PLAYGROUND_MAX_ATTEMPTS):
            cur = dep["unique"]
            if cur in tried:                 # catena che si ripete: fermo
                break
            tried.add(cur)
            attempts += 1
            trace.append({"step": attempts, "unique": cur,
                          "group": dep["group"], "profile": profile,
                          "reason": None, "verdict": "fail"})
            try:
                data = await asyncio.wait_for(
                    gw.forwarder.call(dep, up_payload,
                                      client_ip=_cip, session=_sess),
                    timeout=_tune(gw, "playground_timeout_sec", _PLAYGROUND_TIMEOUT_S))
            except (UpstreamError, asyncio.TimeoutError) as err:
                last_err = err
                trace[-1]["reason"] = _playground_reason(err)
                if isinstance(err, asyncio.TimeoutError):
                    # nessun token in 40s: deployment appeso -> penalizza
                    # (persiste: _cooldown non e' nello snapshot).
                    _cd = int(getattr(gw.policy.qc_json,
                                      "watchdog_cooldown_sec", 90) or 90)
                    try:
                        router.mark_failed(cur, seconds=_cd)
                        trace[-1]["reason"] = "timeout+penalized"
                    except Exception:            # noqa: BLE001
                        pass
                nxt = router.fallback_next(profile, dep, need, scope, ctx=ctx,
                                           requested_group=requested_group)
                if nxt is not None and nxt["unique"] not in tried:
                    dep, fallbacks = nxt, fallbacks + 1
                else:
                    dep = None
                continue
            except Exception as exc:         # noqa: BLE001 - mai rompere il trace
                last_err = exc
                trace[-1]["reason"] = _playground_reason(exc)
                nxt = router.fallback_next(profile, dep, need, scope, ctx=ctx,
                                           requested_group=requested_group)
                if nxt is not None and nxt["unique"] not in tried:
                    dep, fallbacks = nxt, fallbacks + 1
                else:
                    dep = None
                continue
            trace[-1]["verdict"] = "ok"
            used = dep
            content = _playground_content(data)
            journal.record(gw.VAR_DIR, "playground",
                           {"model": model, "profile": profile,
                            "unique": cur, "group": used["group"],
                            "attempts": attempts, "fallbacks": fallbacks,
                            "ok": True})
            return {"ok": True, "model": model_raw, "resolved_model": model,
                    "profile": profile, "group": used["group"],
                    "attempts": attempts, "fallbacks": fallbacks,
                    "trace": trace, "content": content,
                    "used": {"unique": cur, "group": used["group"]}}
        reason = (_playground_reason(last_err) if last_err is not None
                  else "chain-exhausted")
        return _playground_result(
            gw, model_raw, model, profile, group_or_explicit, trace,
            attempts=attempts, fallbacks=fallbacks, reason=reason,
            error=reason)
    finally:
        for key, val in saved.items():
            setattr(router, key, val)


# =============================================================================
# SESSIONI ATTIVE & STATISTICHE COMPLETE
# =============================================================================

@admin_api.get("/sessions", tags=["admin"])
async def list_sessions(request: Request):
    """Sessioni attive: sticky, cache holder, session dep guard, slow demote."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    router = gw.router
    now = time.time()

    # Sticky sessions
    sticky = []
    for sid, (target, ts) in router._sticky.items():
        age = now - ts
        ttl = router.policy.sticky_ttl_sec - age
        sticky.append({
            "session_id": sid,
            "type": "sticky",
            "target": target,
            "age_sec": round(age, 1),
            "ttl_sec": round(max(0, ttl), 1),
        })

    # Deployment sticky
    dep_sticky = []
    for sid, (unique, ts) in router._sticky_dep.items():
        age = now - ts
        ttl = router.policy.sticky_ttl_sec - age
        dep_sticky.append({
            "session_id": sid,
            "type": "dep_sticky",
            "unique": unique,
            "age_sec": round(age, 1),
            "ttl_sec": round(max(0, ttl), 1),
        })

    # Session deps (ownership)
    sess_deps = []
    for sid, deps in router._session_deps.items():
        sess_deps.append({
            "session_id": sid,
            "owned_count": len(deps),
            "uniques": list(deps)[:10],  # prime 10
        })

    # Cache holders
    cache_holders = []
    for sid, (unique, ts) in router._session_last_ok.items():
        age = now - ts
        ttl = router.policy.cache_holder_ttl_sec - age
        cache_holders.append({
            "session_id": sid,
            "unique": unique,
            "age_sec": round(age, 1),
            "ttl_sec": round(max(0, ttl), 1),
        })

    # Session slow demote
    sess_slow = []
    for sid, slow_map in router._session_slow.items():
        for unique, (ts, hard) in slow_map.items():
            age = now - ts
            sess_slow.append({
                "session_id": sid,
                "unique": unique,
                "age_sec": round(age, 1),
                "hard": hard,
            })

    return {
        "sticky_sessions": sticky,
        "dep_sticky_sessions": dep_sticky,
        "session_deps": sess_deps,
        "cache_holders": cache_holders,
        "slow_demoted": sess_slow,
        "totals": {
            "sticky": len(sticky),
            "dep_sticky": len(dep_sticky),
            "session_deps": len(sess_deps),
            "cache_holders": len(cache_holders),
            "slow_demoted": len(sess_slow),
        }
    }


# ---------------------------------------------------------------------------
# Helper condivisi: lettura ledger e classifiche (usati da /sessions/{id},
# /stats/summary, /stats/sessions e dal protocollo MCP).
# ---------------------------------------------------------------------------

def _parse_window_seconds(window: str | None, default: float = 86400.0) -> float:
    """Converte una finestra testuale (es. '24h', '7d', '30m') in secondi."""
    w = str(window or "").lower().strip()
    try:
        if w.endswith("h"):
            return float(w[:-1]) * 3600
        if w.endswith("d"):
            return float(w[:-1]) * 86400
        if w.endswith("m"):
            return float(w[:-1]) * 60
        if w.endswith("s"):
            return float(w[:-1])
    except ValueError:
        return default
    return default


def _ledger_row_ok(row: dict) -> bool:
    """Una riga del ledger e' un SUCCESSO se non e' stata scartata dal QC ne'
    dal watchdog e non porta un errore esplicito."""
    if row.get("qc"):
        return False
    if row.get("wd"):
        return False
    if row.get("error"):
        return False
    return True


def _rank_rows_by_deployment(rows: list[dict], config) -> list[dict]:
    """Classifica i deployment che hanno partecipato (ok/fail/token/latenze).

    Ordina per successi decrescenti: in cima chi ha servito con successo.
    """
    table: dict[str, dict] = {}
    for r in rows:
        dep = str(r.get("dep") or "")
        if not dep:
            continue
        e = table.get(dep)
        if e is None:
            model = r.get("model") or ""
            if not model:
                d = config.deployment_by_unique(dep)
                model = (d or {}).get("model", "")
            e = {
                "deployment": dep, "model": model,
                "calls": 0, "ok": 0, "fail": 0,
                "prompt_tokens": 0, "completion_tokens": 0,
                "total_tokens": 0, "cached_tokens": 0,
                "cost": 0.0, "cost_est": 0.0,
                "dur_ms_sum": 0, "ttfb_ms_sum": 0, "ttfb_n": 0,
                "fb": 0, "qc": 0, "wd": 0,
                "first_ts": None, "last_ts": None,
            }
            table[dep] = e
        u = r.get("usage") or {}
        e["calls"] += 1
        if _ledger_row_ok(r):
            e["ok"] += 1
        else:
            e["fail"] += 1
        e["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        e["completion_tokens"] += int(u.get("completion_tokens") or 0)
        e["total_tokens"] += int(u.get("total_tokens") or 0)
        e["cached_tokens"] += int(u.get("cached_tokens") or 0)
        e["cost"] += float(u.get("cost") or 0)
        e["cost_est"] += float(u.get("cost_est") or 0)
        e["dur_ms_sum"] += int(r.get("dur_ms") or 0)
        ttfb = r.get("ttfb_ms")
        if ttfb:
            e["ttfb_ms_sum"] += int(ttfb)
            e["ttfb_n"] += 1
        if r.get("fb"):
            e["fb"] += 1
        if r.get("qc"):
            e["qc"] += 1
        if r.get("wd"):
            e["wd"] += 1
        ts = r.get("ts") or 0
        if e["first_ts"] is None or ts < e["first_ts"]:
            e["first_ts"] = ts
        if e["last_ts"] is None or ts > e["last_ts"]:
            e["last_ts"] = ts
    out = []
    for e in table.values():
        calls = e["calls"]
        out.append({
            "deployment": e["deployment"],
            "model": e["model"],
            "calls": calls,
            "ok": e["ok"], "fail": e["fail"],
            "success_rate_percent": (round(e["ok"] / calls * 100, 2)
                                     if calls else 0.0),
            "prompt_tokens": e["prompt_tokens"],
            "completion_tokens": e["completion_tokens"],
            "total_tokens": e["total_tokens"],
            "cached_tokens": e["cached_tokens"],
            "cost_reported_usd": round(e["cost"], 6),
            "cost_estimated_usd": round(e["cost_est"], 6),
            "avg_duration_ms": (round(e["dur_ms_sum"] / calls) if calls else 0),
            "avg_ttfb_ms": (round(e["ttfb_ms_sum"] / e["ttfb_n"])
                            if e["ttfb_n"] else None),
            "fallbacks": e["fb"], "qc_discards": e["qc"], "watchdog": e["wd"],
            "first_ts": e["first_ts"], "last_ts": e["last_ts"],
        })
    out.sort(key=lambda x: (-x["ok"], x["deployment"]))
    return out


def _preferred_model(rows: list[dict], config) -> dict | None:
    """Modello PREFERITO di un insieme di righe ledger: chi ha servito piu'
    chiamate con successo (a parita', piu' token)."""
    by_model: dict[str, dict] = {}
    for r in rows:
        model = r.get("model") or ""
        if not model:
            dep = str(r.get("dep") or "")
            d = config.deployment_by_unique(dep) if dep else None
            model = (d or {}).get("model", "") or "unknown"
        e = by_model.setdefault(model, {
            "model": model, "calls": 0, "ok": 0, "fail": 0,
            "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0,
        })
        u = r.get("usage") or {}
        e["calls"] += 1
        if _ledger_row_ok(r):
            e["ok"] += 1
        else:
            e["fail"] += 1
        e["total_tokens"] += int(u.get("total_tokens") or 0)
        e["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        e["completion_tokens"] += int(u.get("completion_tokens") or 0)
    if not by_model:
        return None
    best = max(by_model.values(),
               key=lambda x: (x["ok"], x["total_tokens"], x["calls"]))
    calls = best["calls"]
    best = dict(best)
    best["success_rate_percent"] = (round(best["ok"] / calls * 100, 2)
                                    if calls else 0.0)
    return best


def _preferred_models_config(gw) -> dict:
    """Modelli preferiti DICHIARATI in policy (bucket -go/-fallback)."""
    try:
        raw = str(getattr(gw.policy, "go_preferred_models", "") or "")
    except Exception:                            # noqa: BLE001
        raw = ""
    items = [m.strip() for m in raw.replace(";", ",").split(",") if m.strip()]
    return {"go_preferred_models": items, "count": len(items)}


@admin_api.get("/sessions/{session_id}", tags=["admin"])
async def session_detail(request: Request, session_id: str,
                         window: str = "7d"):
    """Dettaglio di UNA sessione: deployment che vi hanno partecipato (con
    successo) in classifica, modello preferito e totali."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    router = gw.router
    now = time.time()
    window_seconds = _parse_window_seconds(window, default=7 * 86400.0)
    cutoff = now - window_seconds

    # --- righe ledger della sessione nella finestra
    rows: list[dict] = []
    try:
        all_rows = await gw.LEDGER.iter_rows_async()
        for row in all_rows:
            if str(row.get("ses") or "") != session_id:
                continue
            if (row.get("ts") or 0) >= cutoff:
                rows.append(row)
    except Exception:                            # noqa: BLE001
        pass

    ranking = _rank_rows_by_deployment(rows, gw.config)
    successful = [r for r in ranking if r["ok"] > 0]
    preferred = _preferred_model(rows, gw.config)

    # --- stato runtime della sessione (sticky / holder / warm / slow)
    sticky = None
    ent = router._sticky.get(session_id)
    if ent:
        target, ts = ent
        sticky = {"target": target, "age_sec": round(now - ts, 1),
                  "ttl_sec": router.policy.sticky_ttl_sec}
    sticky_dep = None
    ent = router._sticky_dep.get(session_id)
    if ent:
        target, ts = ent
        sticky_dep = {"target": target, "age_sec": round(now - ts, 1),
                      "ttl_sec": router.policy.sticky_ttl_sec}
    cache_holder = None
    ent = router._session_last_ok.get(session_id)
    if ent:
        unique, ts = ent
        cache_holder = {"unique": unique, "age_sec": round(now - ts, 1),
                        "ttl_sec": router.policy.cache_holder_ttl_sec}
    owned = sorted(router._session_deps.get(session_id) or [])
    slow_map = router._session_slow.get(session_id) or {}
    slow = [{"unique": u, "age_sec": round(now - ts, 1), "hard": bool(hard)}
            for u, (ts, hard) in slow_map.items()]

    # marcatori di partecipazione runtime per-deployment
    runtime_mark = {}
    for u in owned:
        runtime_mark[u] = {"warm": True}
    if cache_holder:
        runtime_mark.setdefault(cache_holder["unique"], {})["cache_holder"] = True
    for s in slow:
        runtime_mark.setdefault(s["unique"], {})["slow"] = True
    for r in ranking:
        m = runtime_mark.get(r["deployment"])
        if m:
            r["session_flags"] = m

    totals = {
        "calls": len(rows),
        "ok": sum(1 for r in rows if _ledger_row_ok(r)),
        "fail": sum(1 for r in rows if not _ledger_row_ok(r)),
        "deployments": len(ranking),
        "deployments_ok": len(successful),
        "prompt_tokens": sum(int((r.get("usage") or {}).get("prompt_tokens") or 0)
                             for r in rows),
        "completion_tokens": sum(int((r.get("usage") or {}).get("completion_tokens") or 0)
                                 for r in rows),
        "total_tokens": sum(int((r.get("usage") or {}).get("total_tokens") or 0)
                            for r in rows),
        "cached_tokens": sum(int((r.get("usage") or {}).get("cached_tokens") or 0)
                             for r in rows),
    }
    totals["success_rate_percent"] = (round(totals["ok"] / totals["calls"] * 100, 2)
                                      if totals["calls"] else 0.0)

    return {
        "session_id": session_id,
        "window": window,
        "window_seconds": window_seconds,
        "sticky": sticky,
        "sticky_dep": sticky_dep,
        "cache_holder": cache_holder,
        "owned_deployments": owned,
        "slow_demoted": slow,
        "preferred_model": preferred,
        "deployments": ranking,
        "successful_deployments": successful,
        "totals": totals,
    }


@admin_api.get("/stats/sessions", tags=["admin"])
async def stats_sessions(request: Request, window: str = "7d",
                         limit: int = 50):
    """Classifica delle SESSIONI: token, successi e modello preferito."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    window_seconds = _parse_window_seconds(window, default=7 * 86400.0)
    cutoff = time.time() - window_seconds
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 1000))

    by_ses: dict[str, list[dict]] = {}
    try:
        all_rows = await gw.LEDGER.iter_rows_async()
        for row in all_rows:
            if (row.get("ts") or 0) < cutoff:
                continue
            sid = str(row.get("ses") or "")
            if not sid:
                continue
            by_ses.setdefault(sid, []).append(row)
    except Exception:                            # noqa: BLE001
        pass

    sessions = []
    for sid, rows in by_ses.items():
        ok = sum(1 for r in rows if _ledger_row_ok(r))
        total_tokens = sum(int((r.get("usage") or {}).get("total_tokens") or 0)
                           for r in rows)
        preferred = _preferred_model(rows, gw.config)
        sessions.append({
            "session_id": sid,
            "calls": len(rows),
            "ok": ok,
            "fail": len(rows) - ok,
            "success_rate_percent": round(ok / len(rows) * 100, 2) if rows else 0.0,
            "total_tokens": total_tokens,
            "deployments": len({str(r.get("dep") or "") for r in rows
                                if r.get("dep")}),
            "preferred_model": (preferred or {}).get("model"),
            "last_ts": max((r.get("ts") or 0) for r in rows),
        })
    sessions.sort(key=lambda x: (-x["total_tokens"], -x["calls"]))
    return {
        "window": window,
        "window_seconds": window_seconds,
        "sessions_count": len(sessions),
        "sessions": sessions[:limit],
    }


@admin_api.get("/stats/summary", tags=["admin"])
async def stats_summary(request: Request):
    """Statistiche aggregate: token consumati/generati, cache, success rate."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    router = gw.router
    now = time.time()

    # Aggrega metriche dal ledger
    cutoff_1h = now - 3600
    cutoff_24h = now - 86400

    # Leggi ledger per aggregazione
    ledger_rows_1h = []
    ledger_rows_24h = []
    try:
        ledger = gw.LEDGER
        all_rows = await ledger.iter_rows_async()
        for row in all_rows:
            ts = row.get("ts", 0)
            if ts >= cutoff_1h:
                ledger_rows_1h.append(row)
            if ts >= cutoff_24h:
                ledger_rows_24h.append(row)
    except Exception:
        pass

    def aggregate_rows(rows):
        total = {
            "calls": len(rows),
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_reported": 0.0,
            "cost_estimated": 0.0,
            "duration_ms_sum": 0,
            "fb_calls": 0,
            "qc_discards": 0,
            "errors": 0,
        }
        for r in rows:
            u = r.get("usage") or {}
            total["prompt_tokens"] += u.get("prompt_tokens", 0)
            total["completion_tokens"] += u.get("completion_tokens", 0)
            total["total_tokens"] += u.get("total_tokens", 0)
            total["cost_reported"] += u.get("cost", 0)
            total["cost_estimated"] += u.get("cost_est", 0)
            total["duration_ms_sum"] += r.get("dur_ms", 0)
            if r.get("fb"):
                total["fb_calls"] += 1
            if r.get("qc"):
                total["qc_discards"] += 1
            if r.get("error"):
                total["errors"] += 1
        return total

    agg_1h = aggregate_rows(ledger_rows_1h)
    agg_24h = aggregate_rows(ledger_rows_24h)

    # Metriche runtime
    metrics_snapshot = metrics.snapshot()
    cache_hit = sum(v for k, v in metrics_snapshot.get("nx_cache_hit_requests_total", {}).items())
    cache_total = sum(v for k, v in metrics_snapshot.get("nx_requests_total", {}).items())
    coalesce_hit = sum(v for k, v in metrics_snapshot.get("nx_coalesce_total", {}).items() if "hit" in str(k))
    coalesce_total = sum(v for k, v in metrics_snapshot.get("nx_coalesce_total", {}).items())

    # Success rate da router stats
    total_ok = 0
    total_fail = 0
    for unique, s in router._stats.items():
        total_ok += s.ok_count
        total_fail += s.fail_count
    total_calls = total_ok + total_fail
    success_rate = round(total_ok / total_calls * 100, 2) if total_calls > 0 else 100.0

    # Model preference (deployment success ranking)
    model_stats = {}
    for unique, s in router._stats.items():
        dep = gw.config.deployment_by_unique(unique) or {}
        model = dep.get("model", unique.split("__")[0] if unique else "unknown")
        if model not in model_stats:
            model_stats[model] = {"ok": 0, "fail": 0, "unique": unique}
        model_stats[model]["ok"] += s.ok_count
        model_stats[model]["fail"] += s.fail_count

    model_ranking = []
    for model, stats in model_stats.items():
        calls = stats["ok"] + stats["fail"]
        if calls > 0:
            rate = round(stats["ok"] / calls * 100, 2)
            model_ranking.append({
                "model": model,
                "ok": stats["ok"],
                "fail": stats["fail"],
                "calls": calls,
                "success_rate": rate,
            })
    model_ranking.sort(key=lambda x: x["success_rate"], reverse=True)

    return {
        "runtime": {
            "uptime_seconds": int(time.time() - metrics._started),
            "tracked_deployments": len(router._stats),
            "cooldowns_active": len([e for u, e in router._cooldown.items() if e > now]),
            "sticky_sessions": len(router._sticky),
            "cache_holders": len(router._session_last_ok),
        },
        "tokens_1h": {
            "calls": agg_1h["calls"],
            "prompt_tokens": agg_1h["prompt_tokens"],
            "completion_tokens": agg_1h["completion_tokens"],
            "total_tokens": agg_1h["total_tokens"],
            "cost_reported_usd": round(agg_1h["cost_reported"], 6),
            "cost_estimated_usd": round(agg_1h["cost_estimated"], 6),
            "avg_duration_ms": round(agg_1h["duration_ms_sum"] / agg_1h["calls"]) if agg_1h["calls"] > 0 else 0,
        },
        "tokens_24h": {
            "calls": agg_24h["calls"],
            "prompt_tokens": agg_24h["prompt_tokens"],
            "completion_tokens": agg_24h["completion_tokens"],
            "total_tokens": agg_24h["total_tokens"],
            "cost_reported_usd": round(agg_24h["cost_reported"], 6),
            "cost_estimated_usd": round(agg_24h["cost_estimated"], 6),
            "avg_duration_ms": round(agg_24h["duration_ms_sum"] / agg_24h["calls"]) if agg_24h["calls"] > 0 else 0,
        },
        "cache": {
            "hit_rate": round(cache_hit / cache_total * 100, 2) if cache_total > 0 else 0,
            "hits": int(cache_hit),
            "total_requests": int(cache_total),
            "coalesce_hit_rate": round(coalesce_hit / coalesce_total * 100, 2) if coalesce_total > 0 else 0,
            "coalesce_hits": int(coalesce_hit),
        },
        "success_rate": {
            "total_ok": total_ok,
            "total_fail": total_fail,
            "total_calls": total_calls,
            "rate_percent": success_rate,
        },
        "model_ranking": model_ranking[:20],  # Top 20
        "preferred_model": _preferred_model(ledger_rows_24h, gw.config),
        "preferred_models_config": _preferred_models_config(gw),
        "metrics": {
            "nx_requests_total": dict(metrics_snapshot.get("nx_requests_total", {})),
            "nx_upstream_calls_total": dict(metrics_snapshot.get("nx_upstream_calls_total", {})),
            "nx_cache_hit_requests_total": int(cache_hit),
            "nx_coalesce_total": dict(metrics_snapshot.get("nx_coalesce_total", {})),
        }
    }


@admin_api.get("/stats/tokens", tags=["admin"])
async def stats_tokens(request: Request, window: str = "24h"):
    """Statistiche token dettagliate per finestra temporale."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()

    # Parse window
    window_seconds = 86400  # default 24h
    w = window.lower().strip()
    if w.endswith("h"):
        try:
            window_seconds = int(w[:-1]) * 3600
        except ValueError:
            pass
    elif w.endswith("d"):
        try:
            window_seconds = int(w[:-1]) * 86400
        except ValueError:
            pass
    elif w.endswith("m"):
        try:
            window_seconds = int(w[:-1]) * 60
        except ValueError:
            pass

    cutoff = time.time() - window_seconds
    rows = []
    try:
        all_rows = await gw.LEDGER.iter_rows_async()
        for row in all_rows:
            if row.get("ts", 0) >= cutoff:
                rows.append(row)
    except Exception:
        pass

    # Aggrega per modello
    by_model = {}
    for r in rows:
        model = r.get("model", "unknown")
        if model not in by_model:
            by_model[model] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                               "total_tokens": 0, "cost": 0.0, "cost_est": 0.0}
        u = r.get("usage") or {}
        by_model[model]["calls"] += 1
        by_model[model]["prompt_tokens"] += u.get("prompt_tokens", 0)
        by_model[model]["completion_tokens"] += u.get("completion_tokens", 0)
        by_model[model]["total_tokens"] += u.get("total_tokens", 0)
        by_model[model]["cost"] += u.get("cost", 0)
        by_model[model]["cost_est"] += u.get("cost_est", 0)

    # Aggrega per profilo
    by_profile = {}
    for r in rows:
        prof = r.get("profile", "unknown")
        if prof not in by_profile:
            by_profile[prof] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                                "total_tokens": 0, "cost": 0.0}
        u = r.get("usage") or {}
        by_profile[prof]["calls"] += 1
        by_profile[prof]["prompt_tokens"] += u.get("prompt_tokens", 0)
        by_profile[prof]["completion_tokens"] += u.get("completion_tokens", 0)
        by_profile[prof]["total_tokens"] += u.get("total_tokens", 0)
        by_profile[prof]["cost"] += u.get("cost", 0)

    # Aggrega per giorno
    by_day = {}
    for r in rows:
        day = time.strftime("%Y-%m-%d", time.localtime(r.get("ts", 0)))
        if day not in by_day:
            by_day[day] = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                           "total_tokens": 0, "cost": 0.0}
        u = r.get("usage") or {}
        by_day[day]["calls"] += 1
        by_day[day]["prompt_tokens"] += u.get("prompt_tokens", 0)
        by_day[day]["completion_tokens"] += u.get("completion_tokens", 0)
        by_day[day]["total_tokens"] += u.get("total_tokens", 0)
        by_day[day]["cost"] += u.get("cost", 0)

    return {
        "window": window,
        "window_seconds": window_seconds,
        "rows_count": len(rows),
        "by_model": {k: v for k, v in sorted(by_model.items(), key=lambda x: x[1]["total_tokens"], reverse=True)},
        "by_profile": {k: v for k, v in sorted(by_profile.items(), key=lambda x: x[1]["total_tokens"], reverse=True)},
        "by_day": dict(sorted(by_day.items())),
    }


@admin_api.get("/stats/cache", tags=["admin"])
async def stats_cache(request: Request):
    """Statistiche cache: hit rate, coalescing, retention."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    router = gw.router

    metrics_snapshot = metrics.snapshot()

    # Cache hits
    cache_hit = sum(v for k, v in metrics_snapshot.get("nx_cache_hit_requests_total", {}).items())
    cache_total = sum(v for k, v in metrics_snapshot.get("nx_requests_total", {}).items())

    # Coalescing
    coalesce = metrics_snapshot.get("nx_coalesce_total", {})
    coalesce_hit = sum(v for k, v in coalesce.items() if "hit" in str(k))
    coalesce_inflight = sum(v for k, v in coalesce.items() if "inflight" in str(k))

    # Cache holders stats
    cache_holders = []
    now = time.time()
    for sid, (unique, ts) in router._session_last_ok.items():
        age = now - ts
        ttl = router.policy.cache_holder_ttl_sec - age
        cache_holders.append({
            "session_id": sid,
            "unique": unique,
            "age_sec": round(age, 1),
            "ttl_remaining_sec": round(max(0, ttl), 1),
        })

    # Session deps
    session_deps_count = sum(len(deps) for deps in router._session_deps.values())

    return {
        "cache_hit_rate_percent": round(cache_hit / cache_total * 100, 2) if cache_total > 0 else 0,
        "cache_hits": int(cache_hit),
        "cache_total_requests": int(cache_total),
        "coalesce_hit_rate_percent": round(coalesce_hit / max(1, coalesce_hit + coalesce_inflight) * 100, 2),
        "coalesce_hits": int(coalesce_hit),
        "coalesce_inflight": int(coalesce_inflight),
        "cache_holders_active": len(cache_holders),
        "cache_holders": cache_holders[:50],  # prime 50
        "session_deps_total": session_deps_count,
        "cache_config": {
            "cache_holder_ttl_sec": router.policy.cache_holder_ttl_sec,
            "cache_aware_enabled": router.policy.cache_aware_enabled,
            "cache_prefix_audit": router.policy.cache_prefix_audit,
            "cache_ctx_truncation_enabled": router.policy.cache_ctx_truncation_enabled,
        }
    }


@admin_api.get("/stats/models", tags=["admin"])
async def stats_models(request: Request, window: str = "7d"):
    """Classifica modelli per success rate, latency, usage."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()

    # Parse window
    window_days = 7.0
    w = window.lower().strip()
    if w.endswith("d"):
        try:
            window_days = float(w[:-1])
        except ValueError:
            pass
    elif w.endswith("h"):
        try:
            window_days = float(w[:-1]) / 24.0
        except ValueError:
            pass

    cutoff = time.time() - window_days * 86400

    # Aggrega da ledger
    rows = []
    try:
        all_rows = await gw.LEDGER.iter_rows_async()
        for row in all_rows:
            if row.get("ts", 0) >= cutoff:
                rows.append(row)
    except Exception:
        pass

    # Aggrega per modello
    by_model = {}
    for r in rows:
        model = r.get("model", "unknown")
        if model not in by_model:
            by_model[model] = {
                "calls": 0, "ok": 0, "fail": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "duration_ms_sum": 0, "fb": 0, "qc": 0,
                "cost": 0.0, "cost_est": 0.0,
            }
        by_model[model]["calls"] += 1
        # ok/fail dal LEDGER (non dai contatori runtime per-deployment, che
        # sovrascriverebbero il modello con quelli di UN solo deployment).
        if _ledger_row_ok(r):
            by_model[model]["ok"] += 1
        else:
            by_model[model]["fail"] += 1
        u = r.get("usage") or {}
        by_model[model]["prompt_tokens"] += u.get("prompt_tokens", 0)
        by_model[model]["completion_tokens"] += u.get("completion_tokens", 0)
        by_model[model]["total_tokens"] += u.get("total_tokens", 0)
        by_model[model]["cost"] += float(u.get("cost") or 0)
        by_model[model]["cost_est"] += float(u.get("cost_est") or 0)
        by_model[model]["duration_ms_sum"] += r.get("dur_ms", 0)
        if r.get("fb"):
            by_model[model]["fb"] += 1
        if r.get("qc"):
            by_model[model]["qc"] += 1

    # Costruisci classifica
    ranking = []
    for model, stats in by_model.items():
        calls = stats["calls"]
        ok = stats["ok"]
        fail = stats["fail"]
        total = ok + fail
        success_rate = round(ok / total * 100, 2) if total > 0 else 100.0
        avg_latency = round(stats["duration_ms_sum"] / calls) if calls > 0 else 0

        ranking.append({
            "model": model,
            "calls": calls,
            "ok": ok,
            "fail": fail,
            "success_rate_percent": success_rate,
            "avg_latency_ms": avg_latency,
            "prompt_tokens": stats["prompt_tokens"],
            "completion_tokens": stats["completion_tokens"],
            "total_tokens": stats["total_tokens"],
            "cost_reported_usd": round(stats["cost"], 6),
            "cost_estimated_usd": round(stats["cost_est"], 6),
            "fb_rate_percent": round(stats["fb"] / calls * 100, 2) if calls > 0 else 0,
            "qc_rate_percent": round(stats["qc"] / calls * 100, 2) if calls > 0 else 0,
        })

    # Ordina per success rate
    ranking.sort(key=lambda x: x["success_rate_percent"], reverse=True)

    return {
        "window": window,
        "window_days": window_days,
        "models_count": len(ranking),
        "ranking": ranking,
    }


@admin_api.get("/stats/deployments", tags=["admin"])
async def stats_deployments(request: Request, profile: str | None = None,
                            sort: str = "success_rate", order: str = "desc"):
    """Statistiche dettagliate per deployment con classifica."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    router = gw.router
    cfg = gw.config
    now = time.time()

    rows = []
    for unique, s in router._stats.items():
        dep = gw.config.deployment_by_unique(unique) or {}
        group = dep.get("group") or unique.rsplit("__", 2)[0]
        model = dep.get("model", "")
        prof = None
        if group:
            rest = group[len(cfg.proxy_prefix):] if group.startswith(cfg.proxy_prefix) else group
            for p in cfg.profiles:
                if rest.startswith(p + "-"):
                    prof = p
                    break
        if profile and prof != profile:
            continue

        if "/" in model:
            provider = model.split("/", 1)[0]
        elif dep.get("tier"):
            provider = dep["tier"]
        else:
            provider = ""

        cool = router._cooldown.get(unique, 0.0)
        cool_remaining = max(0, int(cool - now)) if cool > now else 0

        calls = s.ok_count + s.fail_count
        success_rate = round(s.ok_count / calls * 100, 2) if calls > 0 else 100.0

        rows.append({
            "unique": unique,
            "profile": prof,
            "group": group,
            "provider": provider,
            "model": model,
            "ok": s.ok_count,
            "fail": s.fail_count,
            "calls": calls,
            "success_rate_percent": success_rate,
            "ema_latency_ms": s.ema_latency_ms,
            "fail_streak": s.fail_streak,
            "cooldown_remaining_sec": cool_remaining,
            "last_used": s.last_used,
            "last_success_ts": s.last_success_ts,
            "last_fail_ts": s.last_fail_ts,
            "last_reason": s.last_reason,
        })

    # Ordina
    valid_sorts = {"success_rate", "calls", "ok", "fail", "ema_latency_ms", "last_used"}
    if sort not in valid_sorts:
        sort = "success_rate"
    rev = order.lower() != "asc"

    def sort_key(r):
        v = r.get(sort if sort != "success_rate" else "success_rate_percent", 0)
        if v is None:
            return (1, 0)
        return (0, v)

    rows.sort(key=sort_key, reverse=rev)

    return {
        "count": len(rows),
        "sort": sort,
        "order": order,
        "deployments": rows,
    }


@admin_api.get("/stats/providers", tags=["admin"])
async def stats_providers(request: Request):
    """Statistiche aggregate per provider."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    router = gw.router

    providers = {}
    for unique, s in router._stats.items():
        dep = gw.config.deployment_by_unique(unique) or {}
        api_base = dep.get("api_base", "unknown")
        model = dep.get("model", "")

        if api_base not in providers:
            providers[api_base] = {
                "provider": api_base,
                "models": set(),
                "total_ok": 0,
                "total_fail": 0,
                "total_calls": 0,
                "ema_latency_ms_sum": 0.0,
                "latency_count": 0,
                "deployments": [],
            }

        p = providers[api_base]
        p["models"].add(model)
        p["total_ok"] += s.ok_count
        p["total_fail"] += s.fail_count
        p["total_calls"] += s.ok_count + s.fail_count
        if s.ema_latency_ms:
            p["ema_latency_ms_sum"] += s.ema_latency_ms
            p["latency_count"] += 1
        p["deployments"].append({
            "unique": unique,
            "model": model,
            "ok": s.ok_count,
            "fail": s.fail_count,
            "ema_latency_ms": s.ema_latency_ms,
        })

    result = []
    for p in providers.values():
        avg_lat = round(p["ema_latency_ms_sum"] / p["latency_count"], 1) if p["latency_count"] > 0 else 0
        success_rate = round(p["total_ok"] / p["total_calls"] * 100, 2) if p["total_calls"] > 0 else 100.0
        result.append({
            "provider": p["provider"],
            "models": sorted(p["models"]),
            "models_count": len(p["models"]),
            "total_ok": p["total_ok"],
            "total_fail": p["total_fail"],
            "total_calls": p["total_calls"],
            "success_rate_percent": success_rate,
            "avg_latency_ms": avg_lat,
            "deployments_count": len(p["deployments"]),
        })

    result.sort(key=lambda x: x["total_calls"], reverse=True)
    return {"providers": result}


@admin_api.get("/tuning", tags=["admin"])
async def get_tuning(request: Request):
    """Parametri di tuning EFFETTIVI a runtime (prima hardcoded): mostra i
    valori attuali dei moduli router/forwarder/admin/ledger/metrics, cosi'
    ogni manopola e' ispezionabile via API/TUI. Ogni voce e' anche
    configurabile da gateway.yaml (policy) o, per lo storage, da env."""
    denied = _require_master(request)
    if denied:
        return denied
    gw = _gw()
    from . import router as _r, forwarder as _f, ledger as _l, metrics as _m
    from . import keyhealth as _kh, ctxcompact as _cc
    from . import toolrepair as _tr, sniff as _sn
    policy = gw.policy

    router_c = {
        "LATENCY_ROTATE_THRESHOLD_MS": _r.LATENCY_ROTATE_THRESHOLD_MS,
        "SOFT_SLOW_LATENCY_MS": _r.SOFT_SLOW_LATENCY_MS,
        "SOFT_SLOW_CTX_MIN": _r.SOFT_SLOW_CTX_MIN,
        "CTX_BUCKETS": list(_r.CTX_BUCKETS),
        "CTX_BUCKET_COUNT": _r.CTX_BUCKET_COUNT,
        "TTFT_RATE_MIN_CTX": _r.TTFT_RATE_MIN_CTX,
        "TTFT_RATE_FLOOR_MS": _r.TTFT_RATE_FLOOR_MS,
        "SLOW_REL_BASELINE_MULT": _r.SLOW_REL_BASELINE_MULT,
        "SLOW_LATENCY_ABS_FLOOR_MS": _r.SLOW_LATENCY_ABS_FLOOR_MS,
        "SLOW_LATENCY_REL_MULT": _r.SLOW_LATENCY_REL_MULT,
        "SLOW_LATENCY_MIN_PEERS": _r.SLOW_LATENCY_MIN_PEERS,
        "SLOW_GEN_MULT": _r.SLOW_GEN_MULT,
        "SLOW_TYPICAL_COMPLETION_TOKENS": _r.SLOW_TYPICAL_COMPLETION_TOKENS,
        "EFFORT_CAPABLE_BONUS": _r.EFFORT_CAPABLE_BONUS,
        "LATENCY_PENALTY_PER_SEC": _r.LATENCY_PENALTY_PER_SEC,
        "PROVIDER_BIAS_NORMALIZATION": _r.PROVIDER_BIAS_NORMALIZATION,
        "STICKY_TTL_SECONDS": getattr(policy, "sticky_ttl_sec",
                                      _r.STICKY_TTL_SECONDS),
        "COOLDOWN_SECONDS": getattr(policy, "cooldown_sec",
                                    _r.COOLDOWN_SECONDS),
        "SCORING_WEIGHTS": dict(getattr(policy, "scoring_weights", {}) or {}),
    }
    forwarder_c = {
        "TIMEOUT_FLOOR_SEC": _f.TIMEOUT_FLOOR_SEC,
        "TIMEOUT_MULTIPLIER": _f.TIMEOUT_MULTIPLIER,
        "TIMEOUT_MAX_SEC": _f.TIMEOUT_MAX_SEC,
        "MODEL_MISSING_COOLDOWN_S": _f.MODEL_MISSING_COOLDOWN_S,
        "QUOTA_MIN_COOLDOWN_S": _f.QUOTA_MIN_COOLDOWN_S,
        "QUOTA_MAX_COOLDOWN_S": _f.QUOTA_MAX_COOLDOWN_S,
        "PROVIDER_TRANSIENT_COOLDOWN_S": _f.PROVIDER_TRANSIENT_COOLDOWN_S,
        "PERMISSION_DENIED_COOLDOWN_S": _f.PERMISSION_DENIED_COOLDOWN_S,
        "STREAM_LOOP_COOLDOWN_S": _f.STREAM_LOOP_COOLDOWN_S,
        "RETRY_AFTER_MIN_SEC": _f.RETRY_AFTER_MIN_SEC,
        "MIN_OUTPUT_FLOOR": _f.MIN_OUTPUT_FLOOR,
        "UPSTREAM_CONNECT_TIMEOUT_SEC": _f.UPSTREAM_TIMEOUT.connect,
        "UPSTREAM_READ_TIMEOUT_SEC": _f.UPSTREAM_TIMEOUT.read,
        "UPSTREAM_WRITE_TIMEOUT_SEC": _f.UPSTREAM_TIMEOUT.write,
        "UPSTREAM_POOL_TIMEOUT_SEC": _f.UPSTREAM_TIMEOUT.pool,
        "UPSTREAM_MAX_KEEPALIVE_CONNECTIONS":
            _f.UPSTREAM_LIMITS.max_keepalive_connections,
        "UPSTREAM_MAX_CONNECTIONS": _f.UPSTREAM_LIMITS.max_connections,
        "UPSTREAM_KEEPALIVE_EXPIRY_SEC": _f.UPSTREAM_LIMITS.keepalive_expiry,
        "RETRYABLE_STATUS_CODES": sorted(_f.RETRYABLE_STATUS),
        "EFFORT_INCOMPATIBLE_HOSTS": list(_f.EFFORT_INCOMPATIBLE_HOSTS),
    }
    admin_c = {
        "PROBE_CONCURRENCY": _PROBE_CONCURRENCY,
        "PROBE_TIMEOUT_S": _PROBE_TIMEOUT_S,
        "PLAYGROUND_TIMEOUT_S": _PLAYGROUND_TIMEOUT_S,
        "PLAYGROUND_MAX_ATTEMPTS": _PLAYGROUND_MAX_ATTEMPTS,
    }
    storage_c = {
        "LEDGER_MAX_BYTES": _l.LEDGER_MAX_BYTES,
        "LEDGER_KEEP": _l.LEDGER_KEEP,
        "LEDGER_SUMMARY_MIN_ROWS": _l.LEDGER_SUMMARY_MIN_ROWS,
        "METRICS_LATENCY_MAX": _m._LATENCY_MAX,
        "JOURNAL_MAX_BYTES": journal.JOURNAL_MAX_BYTES,
        "JOURNAL_KEEP": journal.JOURNAL_KEEP,
    }
    misc_c = {
        "COALESCE_CACHE_MAX": getattr(gw, "_COALESCE_CACHE_MAX", None),
        "VIDEO_JOB_TTL_SEC": getattr(gw, "VIDEO_JOB_TTL_SEC", None),
        "KEYHEALTH_STREAK_DEAD_THRESHOLD": _kh.STREAK_DEAD_THRESHOLD,
        "KEYHEALTH_SUCCESS_EMA_FLOOR": _kh.SUCCESS_EMA_FLOOR,
        "CTXCOMPACT_MIN_PROTECTED_MSGS": _cc._MIN_PROTECTED_MSGS,
        "TOOLREPAIR_MAX_UNWRAP_DEPTH": _tr._MAX_UNWRAP_DEPTH,
        "SNIFF_MAX_B64_CHARS": _sn._MAX_B64_CHARS,
        "SNIFF_MAX_STR_CHARS": _sn._MAX_STR_CHARS,
        "SNIFF_MAX_SSE_BYTES": _sn._MAX_SSE_BYTES,
    }
    return {
        "router": router_c,
        "forwarder": forwarder_c,
        "admin": admin_c,
        "storage": storage_c,
        "misc": misc_c,
        "policy_effective": {
            _k: getattr(policy, _k, None)
            for _k in (
                "latency_rotate_threshold_ms", "soft_slow_latency_ms",
                "soft_slow_ctx_min", "ctx_bucket_edges", "ttft_rate_min_ctx",
                "ttft_rate_floor_ms", "slow_latency_abs_floor_ms",
                "slow_latency_rel_mult", "slow_latency_min_peers",
                "slow_gen_mult", "slow_typical_completion_tokens",
                "slow_rel_baseline_mult", "effort_capable_bonus",
                "latency_penalty_per_sec", "provider_bias_normalization",
                "go_refund_enabled", "go_refund_pct", "go_refund_min_turns",
                "go_refund_max_turns",
                "dynamic_scoring_history_window",
                "probe_concurrency", "probe_timeout_sec",
                "playground_timeout_sec", "playground_max_attempts",
                "model_missing_cooldown_sec", "quota_min_cooldown_sec",
                "quota_max_cooldown_sec", "provider_transient_cooldown_sec",
                "permission_denied_cooldown_sec", "stream_loop_cooldown_sec",
                "retry_body_cap_sec", "min_output_floor",
                "coalesce_cache_max", "video_job_ttl_sec",
                "keyhealth_streak_dead_threshold",
                "keyhealth_success_ema_floor",
                "ctxcompact_min_protected_msgs",
                "toolrepair_max_unwrap_depth",
                "sniff_max_b64_chars", "sniff_max_str_chars",
                "sniff_max_sse_bytes",
                "upstream_connect_timeout_sec", "upstream_read_timeout_sec",
                "upstream_write_timeout_sec", "upstream_pool_timeout_sec",
                "upstream_max_keepalive_connections",
                "upstream_max_connections", "upstream_keepalive_expiry_sec",
                "retryable_status_codes", "effort_incompatible_hosts",
                "adaptive_timeout_enabled", "adaptive_timeout_floor_sec",
                "adaptive_timeout_multiplier", "adaptive_timeout_max_sec",
                "retry_after_min_sec", "stream_stall_sec",
                "cooldown_mode", "cooldown_base_min",
                "cooldown_linear_mult_min", "max_cooldown_sec",
                "scoring_weights",
            )
        },
        "note": ("Configurabile da gateway.yaml (policy). Storage: env "
                 "LEDGER_MAX_BYTES/LEDGER_KEEP/LEDGER_SUMMARY_MIN_ROWS/"
                 "METRICS_LATENCY_MAX/JOURNAL_MAX_BYTES/JOURNAL_KEEP."),
    }


# =============================================================================
# MCP CONFIGURATION PROTOCOL
# =============================================================================
# Protocollo MCP (Model Context Protocol) per la GESTIONE COMPLETA della
# configurazione del gateway. Espone TUTTE le operazioni di configurazione
# come tool JSON-RPC: policy, deployment (CRUD+bulk), profili, CSV, backup,
# capacita, sessioni, cooldown, statistiche e insights.
#
# Due superfici complementari:
#   - GET  /admin/mcp/config/tools   -> elenco tool + inputSchema (JSON)
#   - POST /admin/mcp/config/execute -> esegue un tool {tool, arguments}
#   - POST /admin/mcp/config/call    -> envelope JSON-RPC 2.0 (tools/list,
#                                        tools/call) per i client MCP nativi
#
# Gli handler riusano le funzioni admin esistenti (nessuna logica duplicata):
# il body degli `arguments` viene presentato come JSON alla request originale
# tramite _McpRequest, cosi' PATCH/POST funzionano identicamente via HTTP o MCP.

class _McpRequest:
    """Request-like che espone `arguments` come body JSON, inoltrando
    header/client della richiesta MCP originale. Serve a riusare gli handler
    admin (che leggono request.json()) senza duplicare la logica."""

    def __init__(self, base: Request, body: dict):
        self._base = base
        self._body = body if isinstance(body, dict) else {}

    @property
    def headers(self):
        return self._base.headers

    @property
    def client(self):
        return self._base.client

    async def json(self):
        return self._body


def _mcp_tool_specs() -> list[dict]:
    """Definizioni dei tool MCP di configurazione (name/description/schema)."""
    return [
        # ------------------------------------------------------ policy
        {"name": "policy_get",
         "description": "Legge la policy runtime effettiva (chiavi mascherate).",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "policy_patch",
         "description": "PATCH parziale della policy (hot-reload, validata).",
         "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True}},
        {"name": "policy_raw_get",
         "description": "Legge il gateway.yaml grezzo (master-only).",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "policy_raw_put",
         "description": "Sostituisce l'INTERO gateway.yaml (validato su tmp).",
         "inputSchema": {"type": "object", "properties": {"raw": {"type": "string"}},
                         "required": ["raw"]}},
        # -------------------------------------------------- deployments
        {"name": "deploy_list",
         "description": "Elenca i deployment (filtro opzionale per profilo).",
         "inputSchema": {"type": "object", "properties": {"profile": {"type": "string"}}}},
        {"name": "deploy_get",
         "description": "Recupera un deployment per id.",
         "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                         "required": ["id"]}},
        {"name": "deploy_create",
         "description": "Crea un deployment (profile, modello, endpoint, data, key, context obbligatori).",
         "inputSchema": {"type": "object", "properties": {
             "profile": {"type": "string"}, "modello": {"type": "string"},
             "provider": {"type": "string"}, "endpoint": {"type": "string"},
             "data": {"type": "string"}, "key": {"type": "string"},
             "context": {"type": "integer"}, "priority": {"type": "integer"},
             "caps": {"type": "string"}, "enabled": {"type": "boolean"}},
             "required": ["profile", "modello", "endpoint", "data", "key", "context"]}},
        {"name": "deploy_update",
         "description": "Aggiorna un deployment per id (key vuota = non ruotare).",
         "inputSchema": {"type": "object", "properties": {
             "id": {"type": "string"}, "profile": {"type": "string"},
             "modello": {"type": "string"}, "endpoint": {"type": "string"},
             "data": {"type": "string"}, "key": {"type": "string"},
             "context": {"type": "integer"}, "priority": {"type": "integer"},
             "caps": {"type": "string"}, "enabled": {"type": "boolean"}},
             "required": ["id"]}},
        {"name": "deploy_delete",
         "description": "Elimina un deployment per id.",
         "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                         "required": ["id"]}},
        {"name": "deploy_bulk",
         "description": "Operazioni bulk ATOMICHE: lista di {action, ...}.",
         "inputSchema": {"type": "object", "properties": {
             "operations": {"type": "array", "items": {"type": "object"}}},
             "required": ["operations"]}},
        {"name": "deploy_expiring",
         "description": "Deployment con chiave in scadenza entro N giorni.",
         "inputSchema": {"type": "object", "properties": {"days": {"type": "integer"}}}},
        {"name": "deploy_probe",
         "description": "Valida la chiave di un deployment (unique o id).",
         "inputSchema": {"type": "object", "properties": {
             "unique": {"type": "string"}, "id": {"type": "string"},
             "force": {"type": "boolean"}}}},
        {"name": "deploy_probe_bulk",
         "description": "Valida molte chiavi per filtro (all | cap:x | profilo).",
         "inputSchema": {"type": "object", "properties": {
             "filter": {"type": "string"}, "force": {"type": "boolean"}}}},
        {"name": "deploy_unretire",
         "description": "Riattiva una chiave ritirata/dead (unique).",
         "inputSchema": {"type": "object", "properties": {"unique": {"type": "string"}},
                         "required": ["unique"]}},
        # ------------------------------------------------------ profiles
        {"name": "profile_list",
         "description": "Elenca i profili di routing.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "profile_purge",
         "description": "Rimuove la colonna di un profilo vuoto dal CSV.",
         "inputSchema": {"type": "object", "properties": {"profile": {"type": "string"}},
                         "required": ["profile"]}},
        # ---------------------------------------------------- csv/backups
        {"name": "csv_get",
         "description": "Legge il CSV grezzo + parsed (chiavi mascherate).",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "csv_put",
         "description": "Sostituisce l'intero CSV (validato su tmp).",
         "inputSchema": {"type": "object", "properties": {"raw": {"type": "string"}},
                         "required": ["raw"]}},
        {"name": "backup_list",
         "description": "Elenca i backup CSV/YAML disponibili.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "backup_restore",
         "description": "Ripristina un backup per filename.",
         "inputSchema": {"type": "object", "properties": {"filename": {"type": "string"}},
                         "required": ["filename"]}},
        # ------------------------------------------------- capabilities
        {"name": "capabilities_seed",
         "description": "Semina la colonna caps dalla mappa modelli (dry_run per anteprima).",
         "inputSchema": {"type": "object", "properties": {"dry_run": {"type": "boolean"}}}},
        {"name": "capabilities_audit",
         "description": "Audit server-side della copertura capacita sugli account.",
         "inputSchema": {"type": "object", "properties": {}}},
        # -------------------------------------------------- runtime state
        {"name": "state_get",
         "description": "Stato aggregato (cooldown, sticky, budget, health).",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "tuning_get",
         "description": "Parametri di tuning EFFETTIVI a runtime (router/forwarder/admin/storage).",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "history_get",
         "description": "Journal delle operazioni admin (limit <= 100).",
         "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
        {"name": "reload_gateway",
         "description": "Forza il reload di CSV + policy.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "cooldowns_clear",
         "description": "Azzera i cooldown (un unique o tutti).",
         "inputSchema": {"type": "object", "properties": {"unique": {"type": "string"}}}},
        {"name": "pressure_clear",
         "description": "Azzera cooldown/penalita/finestre (per unique, model o tutto).",
         "inputSchema": {"type": "object", "properties": {
             "unique": {"type": "string"}, "model": {"type": "string"}}}},
        {"name": "pressure_inspect",
         "description": "Vista dettagliata del perche' i deployment vengono saltati.",
         "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
        # ------------------------------------------------------ sessions
        {"name": "sessions_list",
         "description": "Sessioni attive: sticky, cache holder, dep guard, slow.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "sessions_release",
         "description": "Rilascia le sessioni sticky (una o tutte).",
         "inputSchema": {"type": "object", "properties": {"session_id": {"type": "string"}}}},
        {"name": "sessions_detail",
         "description": "Dettaglio di UNA sessione: classifica dei deployment che vi hanno partecipato con successo, modello preferito, totali token.",
         "inputSchema": {"type": "object", "properties": {
             "session_id": {"type": "string"}, "window": {"type": "string"}},
             "required": ["session_id"]}},
        {"name": "stats_sessions",
         "description": "Classifica delle sessioni: token, successi, modello preferito.",
         "inputSchema": {"type": "object", "properties": {
             "window": {"type": "string"}, "limit": {"type": "integer"}}}},
        # --------------------------------------------------------- stats
        {"name": "stats_summary",
         "description": "Statistiche aggregate: token, cache, success rate, ranking modelli, modello preferito.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "stats_tokens",
         "description": "Token generati/consumati per modello/profilo/giorno.",
         "inputSchema": {"type": "object", "properties": {"window": {"type": "string"}}}},
        {"name": "stats_cache",
         "description": "Statistiche cache: hit rate, coalescing, holder.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "stats_models",
         "description": "Classifica modelli per success rate/latency/usage.",
         "inputSchema": {"type": "object", "properties": {"window": {"type": "string"}}}},
        {"name": "stats_deployments",
         "description": "Statistiche per deployment con ordinamento.",
         "inputSchema": {"type": "object", "properties": {
             "profile": {"type": "string"}, "sort": {"type": "string"},
             "order": {"type": "string"}}}},
        {"name": "stats_providers",
         "description": "Statistiche aggregate per provider.",
         "inputSchema": {"type": "object", "properties": {}}},
        # ------------------------------------------------------ insights
        {"name": "insights_get",
         "description": "Insight uso/costi (days, group_by).",
         "inputSchema": {"type": "object", "properties": {
             "days": {"type": "integer"}, "group_by": {"type": "string"}}}},
        {"name": "insights_summary",
         "description": "Sintesi uso ultime 24h.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "leaderboard_get",
         "description": "Classifica deployment (window, sort, order, profile).",
         "inputSchema": {"type": "object", "properties": {
             "window": {"type": "string"}, "sort": {"type": "string"},
             "order": {"type": "string"}, "profile": {"type": "string"}}}},
        # ---------------------------------------------------------- logs
        {"name": "logs_calls",
         "description": "Ultime chiamate/routing dal gateway.log.",
         "inputSchema": {"type": "object", "properties": {
             "tail": {"type": "integer"}, "tags": {"type": "string"}}}},
        {"name": "logs_errors",
         "description": "Ultimi errori auditati (error-audit.log).",
         "inputSchema": {"type": "object", "properties": {
             "tail": {"type": "integer"}, "filter": {"type": "string"}}}},
        # ----------------------------------------------------- playground
        {"name": "playground",
         "description": "Simula una chat di prova e ritorna il trace di routing.",
         "inputSchema": {"type": "object", "properties": {
             "model": {"type": "string"},
             "messages": {"type": "array", "items": {"type": "object"}},
             "profile": {"type": "string"}, "max_tokens": {"type": "integer"}},
             "required": ["model", "messages"]}},
        # ----------------------------------------- persisted / health / guide
        {"name": "deployments_stats",
         "description": "Punteggi PERSISTITI per deployment (ok/fail, latenza EMA).",
         "inputSchema": {"type": "object", "properties": {
             "profile": {"type": "string"}}}},
        {"name": "providers_health",
         "description": "Salute aggregata per provider: contatori, breaker, latenze.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "guide_get",
         "description": "Documento guida/agenti (docs/AGENT.md) servito dal gateway.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]


# Alias LEGACY dei tool MCP: i nomi storici (prefisso "get_"/"list_"/snake
# diverse) restano accettati e mappati sul nome canonico, cosi' i client piu'
# vecchi e i nuovi funzionano entrambi (regola: entrambe le opzioni valide).
_MCP_ALIASES: dict[str, str] = {
    "get_policy": "policy_get",
    "get_policy_raw": "policy_raw_get",
    "put_policy_raw": "policy_raw_put",
    "list_deployments": "deploy_list",
    "get_deployment": "deploy_get",
    "create_deployment": "deploy_create",
    "update_deployment": "deploy_update",
    "delete_deployment": "deploy_delete",
    "bulk_deployments": "deploy_bulk",
    "list_profiles": "profile_list",
    "purge_profile": "profile_purge",
    "get_csv": "csv_get",
    "put_csv": "csv_put",
    "list_backups": "backup_list",
    "restore_backup": "backup_restore",
    "get_state": "state_get",
    "get_history": "history_get",
    "reload": "reload_gateway",
    "clear_cooldowns": "cooldowns_clear",
    "get_stats_summary": "stats_summary",
    "stats_success": "stats_summary",
    "get_stats_tokens": "stats_tokens",
    "get_stats_cache": "stats_cache",
    "get_stats_models": "stats_models",
    "get_stats_deployments": "stats_deployments",
    "get_stats_providers": "stats_providers",
    "list_sessions": "sessions_list",
    "release_sessions": "sessions_release",
    "get_session": "sessions_detail",
    "session_detail": "sessions_detail",
    "get_stats_sessions": "stats_sessions",
    "get_insights": "insights_get",
    "get_insights_summary": "insights_summary",
    "get_leaderboard": "leaderboard_get",
    "get_logs_calls": "logs_calls",
    "get_logs_errors": "logs_errors",
    "get_deployments_stats": "deployments_stats",
    "deployment_stats": "deployments_stats",
    "get_providers_health": "providers_health",
    "provider_health": "providers_health",
    "get_guide": "guide_get",
    "guide": "guide_get",
    "agent_guide": "guide_get",
    # operazioni (alias brevi)
    "probe_bulk": "deploy_probe_bulk",
    "probe_one": "deploy_probe",
    "unretire": "deploy_unretire",
    "backups": "backup_list",
    "restore_backup": "backup_restore",
    "insights": "insights_get",
    "history": "history_get",
}


def _mcp_canonical(name: str) -> str:
    """Normalizza un nome tool MCP (alias legacy -> canonico)."""
    return _MCP_ALIASES.get(name, name)


def _mcp_known_names() -> set[str]:
    """Nomi accettati dal protocollo MCP (canonici + alias legacy)."""
    return {t["name"] for t in _mcp_tool_specs()} | set(_MCP_ALIASES)


async def _mcp_dispatch(tool_name: str, arguments: dict, request: Request):
    """Instrada un tool MCP verso l'handler admin corrispondente.

    Gli handler che leggono un body JSON ricevono un _McpRequest che presenta
    `arguments` come body: cosi' la logica (validazione, journal, backup,
    reload) resta UNICA tra HTTP e MCP."""
    tool_name = _mcp_canonical(tool_name)
    gw = _gw()
    args = arguments if isinstance(arguments, dict) else {}
    synth = _McpRequest(request, args)

    # ---------------------------------------------------------- policy
    if tool_name == "policy_get":
        return await get_policy(request)
    if tool_name == "policy_patch":
        return await patch_policy(synth)
    if tool_name == "policy_raw_get":
        return await get_policy_raw(request)
    if tool_name == "policy_raw_put":
        return await put_policy_raw(synth)
    # ----------------------------------------------------- deployments
    if tool_name == "deploy_list":
        return await list_deployments(request, profile=args.get("profile"))
    if tool_name == "deploy_get":
        dep_id = str(args.get("id") or "")
        if not dep_id:
            return _err(400, "id is required")
        header, rows = csv_store.load_table(gw.CSV_PATH)
        idx, row = csv_store.find_row(header, rows, dep_id)
        if idx is None:
            return _err(404, f"deployment '{dep_id}' non esiste")
        return {"deployment": _deployment_view(
            header, row, gw.config.proxy_prefix)}
    if tool_name == "deploy_create":
        return await create_deployment(synth)
    if tool_name == "deploy_update":
        dep_id = str(args.get("id") or "")
        if not dep_id:
            return _err(400, "id is required")
        return await update_deployment(dep_id, synth)
    if tool_name == "deploy_delete":
        dep_id = str(args.get("id") or "")
        if not dep_id:
            return _err(400, "id is required")
        return await delete_deployment(dep_id, request)
    if tool_name == "deploy_bulk":
        return await bulk_deployments(synth)
    if tool_name == "deploy_expiring":
        days = args.get("days", 7)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        return await deployments_expiring(request, days=days)
    if tool_name == "deploy_probe":
        return await deployments_probe(synth)
    if tool_name == "deploy_probe_bulk":
        return await deployments_probe_bulk(synth)
    if tool_name == "deploy_unretire":
        return await deployments_unretire(synth)
    # --------------------------------------------------------- profiles
    if tool_name == "profile_list":
        return await list_profiles(request)
    if tool_name == "profile_purge":
        return await purge_profile(synth)
    # ------------------------------------------------------ csv/backups
    if tool_name == "csv_get":
        return await admin_csv_get(request)
    if tool_name == "csv_put":
        return await admin_csv_put(synth)
    if tool_name == "backup_list":
        return await list_backups(request)
    if tool_name == "backup_restore":
        return await restore_backup(synth)
    # ---------------------------------------------------- capabilities
    if tool_name == "capabilities_seed":
        return await capabilities_seed_from_map(synth)
    if tool_name == "capabilities_audit":
        return await capabilities_audit(request)
    # --------------------------------------------------- runtime state
    if tool_name == "state_get":
        return await state(request)
    if tool_name == "tuning_get":
        return await get_tuning(request)
    if tool_name == "history_get":
        limit = args.get("limit", 50)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        return await admin_history(request, limit=limit)
    if tool_name == "reload_gateway":
        return await reload_gateway(request)
    if tool_name == "cooldowns_clear":
        return await clear_cooldowns(synth)
    if tool_name == "pressure_clear":
        return await clear_pressure(synth)
    if tool_name == "pressure_inspect":
        return await inspect_pressure(synth)
    # ------------------------------------------------------- sessions
    if tool_name == "sessions_list":
        return await list_sessions(request)
    if tool_name == "sessions_release":
        return await release_sessions(synth)
    if tool_name == "sessions_detail":
        sid = str(args.get("session_id") or "")
        if not sid:
            return _err(400, "session_id is required")
        return await session_detail(
            request, sid, window=str(args.get("window") or "7d"))
    if tool_name == "stats_sessions":
        limit = args.get("limit", 50)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        return await stats_sessions(
            request, window=str(args.get("window") or "7d"), limit=limit)
    # ---------------------------------------------------------- stats
    if tool_name == "stats_summary":
        return await stats_summary(request)
    if tool_name == "stats_tokens":
        return await stats_tokens(request, window=str(args.get("window") or "24h"))
    if tool_name == "stats_cache":
        return await stats_cache(request)
    if tool_name == "stats_models":
        return await stats_models(request, window=str(args.get("window") or "7d"))
    if tool_name == "stats_deployments":
        return await stats_deployments(
            request, profile=args.get("profile"),
            sort=str(args.get("sort") or "success_rate"),
            order=str(args.get("order") or "desc"))
    if tool_name == "stats_providers":
        return await stats_providers(request)
    # ------------------------------------------------------- insights
    if tool_name == "insights_get":
        days = args.get("days", 7)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        return await admin_insights(
            request, days=days, group_by=str(args.get("group_by") or "model"))
    if tool_name == "insights_summary":
        return await admin_insights_summary(request)
    if tool_name == "leaderboard_get":
        return await admin_insights_leaderboard(
            request, window=str(args.get("window") or "7d"),
            sort=str(args.get("sort") or "calls"),
            order=str(args.get("order") or "desc"),
            profile=args.get("profile"))
    # ----------------------------------------------------------- logs
    if tool_name == "logs_calls":
        tail = args.get("tail", 500)
        try:
            tail = int(tail)
        except (TypeError, ValueError):
            tail = 500
        return await admin_logs_calls(
            request, tail=tail,
            tags=str(args.get("tags") or "summary,route,identity,fallback"))
    if tool_name == "logs_errors":
        tail = args.get("tail", 500)
        try:
            tail = int(tail)
        except (TypeError, ValueError):
            tail = 500
        return await admin_logs_errors(
            request, tail=tail, filter=args.get("filter"))
    # ------------------------------------------------------- playground
    if tool_name == "playground":
        return await admin_playground(synth)
    # ------------------------------------------ persisted / health / guide
    if tool_name == "deployments_stats":
        return await admin_deployments_stats(
            request, profile=args.get("profile"))
    if tool_name == "providers_health":
        return await admin_providers_health(request)
    if tool_name == "guide_get":
        return await agent_guide()

    return _err(404, f"tool MCP sconosciuto: {tool_name}")


@admin_api.get("/mcp/config/tools", tags=["admin"])
async def mcp_config_tools(request: Request):
    """Tool definitions per il protocollo MCP di configurazione."""
    denied = _require_master(request)
    if denied:
        return denied
    tools = _mcp_tool_specs()
    return {"tools": tools, "count": len(tools)}


@admin_api.post("/mcp/config/execute", tags=["admin"])
async def mcp_config_execute(request: Request):
    """Esegue un tool MCP: body {tool, arguments} -> risultato dell'handler.

    L'esito e' l'envelope MCP standard: {content: [{type:text, text}], isError}.
    """
    denied = _require_master(request)
    if denied:
        return denied
    body, bad = await _json_body(request)
    if bad:
        return bad
    tool_name = str((body or {}).get("tool") or "")
    arguments = (body or {}).get("arguments") or {}
    if not tool_name:
        return _err(400, "tool name is required")
    if not isinstance(arguments, dict):
        return _err(400, "arguments deve essere un oggetto")
    known = _mcp_known_names()
    if tool_name not in known:
        return _err(404, f"tool MCP sconosciuto: {tool_name}")
    try:
        result = await _mcp_dispatch(tool_name, arguments, request)
    except Exception as exc:                     # noqa: BLE001
        log.error("[mcp] tool %s failed: %s", tool_name, exc)
        return _err(500, f"tool execution failed: {exc}")
    return _mcp_envelope(result)


def _mcp_envelope(result):
    """Normalizza il risultato di un handler nell'envelope MCP standard."""
    import json as _json
    if isinstance(result, JSONResponse):
        try:
            payload = _json.loads(result.body.decode("utf-8"))
        except Exception:                        # noqa: BLE001
            payload = {"raw": result.body.decode("utf-8", "replace")}
        is_error = result.status_code >= 400
    else:
        payload = result
        is_error = False
    return {
        "content": [{"type": "text",
                     "text": _json.dumps(payload, ensure_ascii=False,
                                         default=str)}],
        "isError": is_error,
    }


@admin_api.post("/mcp/config/call", tags=["admin"])
async def mcp_config_call(request: Request):
    """Endpoint JSON-RPC 2.0 compatibile con i client MCP.

    Metodi supportati:
      - initialize            -> capabilities/serverInfo
      - tools/list            -> {tools:[...]}
      - tools/call            -> {content, isError} (params: {name, arguments})
    """
    denied = _require_master(request)
    if denied:
        return denied
    body, bad = await _json_body(request)
    if bad:
        return bad
    body = body or {}
    rpc_id = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params") or {}

    def _rpc_ok(result):
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    def _rpc_err(code, message):
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        return _rpc_ok({
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "scrocco-llm-config", "version": "1.0.0"},
        })
    if method in ("notifications/initialized", "initialized"):
        return _rpc_ok({})
    if method == "tools/list":
        return _rpc_ok({"tools": _mcp_tool_specs()})
    if method == "tools/call":
        name = str((params or {}).get("name") or "")
        args = (params or {}).get("arguments") or {}
        known = _mcp_known_names()
        if name not in known:
            return _rpc_err(-32602, f"tool MCP sconosciuto: {name}")
        try:
            result = await _mcp_dispatch(name, args, request)
        except Exception as exc:                 # noqa: BLE001
            log.error("[mcp] tools/call %s failed: %s", name, exc)
            return _rpc_err(-32603, f"tool execution failed: {exc}")
        return _rpc_ok(_mcp_envelope(result))
    return _rpc_err(-32601, f"metodo non supportato: {method}")
