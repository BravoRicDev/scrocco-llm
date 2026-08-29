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

import yaml
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from . import csv_store, journal, logview
from .config import MODEL_HEADER, PROVIDER_HEADER, DATA_HEADER, _classify
from .forwarder import UpstreamError
from .router import estimate_tokens

log = logging.getLogger("nx.admin")

admin_api = APIRouter(prefix="/admin", tags=["admin"])


def _gw():
    """Accesso ai globali del servizio a runtime (evita import circolari)."""
    from . import main as gw
    return gw


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
                "sticky_ttl_sec": pol.sticky_ttl_sec,
                "cooldown_sec": pol.cooldown_sec,
                "hotwords": pol.hotwords,
                "speed_hotwords": pol.speed_hotwords,
                "speed_min_dim_k": pol.speed_min_dim_k,
                "speed_qualify_pct": pol.speed_qualify_pct,
                "profile_speed_min_dim_k": pol.profile_speed_min_dim_k,
                "profile_speed_qualify_pct": pol.profile_speed_qualify_pct,
                "hotwords_window": pol.hotwords_window,
                "response_model": pol.response_model,
                "adaptive_pick": pol.adaptive_pick,
                "recency_halflife_sec": pol.recency_halflife_sec,
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
import re as _re                              # noqa: E402

_BACKUP_NAME_RE = _re.compile(r"^(keys_rotation-|gateway\.yaml-)[A-Za-z0-9._-]+$")


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
    import re as _re
    s = name.strip().lower()
    for p in ("openai/", "mistral/", "nvidia/", "cloudflare/", "meta-llama/",
              "models/"):
        if s.startswith(p):
            s = s[len(p):]
    return _re.sub(r"[^a-z0-9]+", "", s)


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
    accounts: dict[tuple[str, str], set[str]] = {}
    for deps in gw.config.groups.values():
        for d in deps:
            acc = accounts.setdefault((d["api_base"], d["api_key"]), set())
            acc.add(d["model"])

    missing: list[dict] = []
    suggestions: dict[str, list[str]] = {}
    checked = 0
    errors: list[str] = []

    async def _one(base: str, key: str, models: set[str]):
        nonlocal checked
        masked = f"{key[:6]}…{key[-3:]}" if len(key) > 10 else "***"
        try:
            async with httpx.AsyncClient(timeout=15.0) as http:
                r = await http.get(f"{base.rstrip('/')}/models",
                                   headers={"Authorization": f"Bearer {key}"})
            checked += 1
            if r.status_code != 200:
                errors.append(f"{base} ({masked}): HTTP {r.status_code}")
                return
            items = r.json().get("data") or []
            ids = {m.get("id", "") for m in items}
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
        except Exception as exc:             # noqa: BLE001
            errors.append(f"{base} ({masked}): {type(exc).__name__}: {exc}")

    import asyncio as _asyncio
    sem = _asyncio.Semaphore(4)

    async def _guarded(base, key, models):
        async with sem:
            await _one(base, key, models)

    await _asyncio.gather(*(_guarded(b, k, ms)
                            for (b, k), ms in sorted(accounts.items())))
    return {"checked_at": int(time.time()), "accounts": len(accounts),
            "accounts_checked": checked, "missing_models": missing,
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
                     force: bool) -> dict:
    """UNA chiamata di verifica (max_tokens=1). Niente note_result/mark_failed:
    il probe e' informativo e non deve avvelenare la rotazione adattiva."""
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
                headers={"Authorization": f"Bearer {dep['api_key']}"},
                timeout=_PROBE_TIMEOUT_S)
            ok = resp.status_code == 200
        else:
            resp = await http.post(
                f"{dep['api_base'].rstrip('/')}/chat/completions",
                json={"model": dep["model"], "max_tokens": 1,
                      "messages": [{"role": "user",
                                    "content": "Reply with the single letter A"}]},
                headers={"Authorization": f"Bearer {dep['api_key']}"},
                timeout=_PROBE_TIMEOUT_S)
            ok = False
            try:
                ok = resp.status_code == 200 and "choices" in (resp.json() or {})
            except ValueError:          # body non-JSON: non un successo
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
        out = await _probe_one(http, dep, bool(payload.get("force")))
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

    async def _run(dep):
        async with _aio.Semaphore(_PROBE_CONCURRENCY):
            return await _probe_one(shared_http, dep, force)

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
        a["calls"] += 1
        a["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        a["completion_tokens"] += int(u.get("completion_tokens") or 0)
        a["total_tokens"] += int(u.get("total_tokens")
                                 or ((u.get("prompt_tokens") or 0)
                                     + (u.get("completion_tokens") or 0)))
        a["cost_reported"] += float(u.get("cost") or 0)
        a["cost_est"] += float(u.get("cost_est") or 0)
        a["dur_ms_sum"] += int(r.get("dur_ms") or 0)
        _fb = bool((r.get("fb") or 0) and r["fb"] > 0)
        _qc = bool(r.get("qc"))
        _wd = r.get("wd")
        # wd=tier2-no-done = risposta completa, il provider omette solo [DONE]
        # -> NON e' un fallimento; ogni altro wd non-vuoto lo e'.
        _wd_fail = bool(_wd) and _wd != "tier2-no-done"
        if _fb:
            a["fb_calls"] += 1
        if _qc:
            a["qc_discards"] += 1
        if _wd_fail:
            a["wd_fail"] += 1
        if _fb or _qc or _wd_fail:
            a["bad_calls"] += 1
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
    rows = [r for r in gw.LEDGER.iter_rows()
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
    rows = [r for r in gw.LEDGER.iter_rows() if (r.get("ts") or 0) >= cutoff]
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
    rows = [r for r in gw.LEDGER.iter_rows()
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
    "_session_group", "_defer_active", "_cap_strikes",
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
        while dep is not None and attempts < _PLAYGROUND_MAX_ATTEMPTS:
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
                    gw.forwarder.call(dep, up_payload),
                    timeout=_PLAYGROUND_TIMEOUT_S)
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
                nxt = router.fallback_next(profile, dep, need, scope, ctx=ctx)
                if nxt is not None and nxt["unique"] not in tried:
                    dep, fallbacks = nxt, fallbacks + 1
                else:
                    dep = None
                continue
            except Exception as exc:         # noqa: BLE001 - mai rompere il trace
                last_err = exc
                trace[-1]["reason"] = _playground_reason(exc)
                nxt = router.fallback_next(profile, dep, need, scope, ctx=ctx)
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
