"""MCP di configurazione: l'admin API esposta come tool JSON-RPC.

[IT] Tre superfici (master key richiesta, come tutto /admin):
- GET  /admin/mcp/config/tools   -> elenco tool + inputSchema;
- POST /admin/mcp/config/execute -> esegue un tool {tool, arguments};
- POST /admin/mcp/config/call    -> envelope JSON-RPC 2.0 (tools/list,
  tools/call) per i client MCP nativi.
Ogni tool riusa l'handler admin corrispondente (nessuna logica duplicata):
gli `arguments` arrivano all'handler come body JSON tramite `_McpRequest`.
Estratto da app/admin.py senza modifiche di logica (logger "nx.admin").

[EN] MCP configuration tools over the admin API.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import admin as _admin
from . import csv_store
from . import state as gw_state

log = logging.getLogger("nx.admin")

mcp_api = APIRouter(prefix="/admin", tags=["admin"])

# Gli handler si risolvono su app.admin (`_admin.<nome>`) a ogni chiamata,
# come quando questo codice stava li': sostituire un handler admin lo
# sostituisce anche per i tool MCP.


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
        # ------------------------------------------- Fasi 2-4 (nuovi endpoint)
        {"name": "policy_schema_get",
         "description": "Schema JSON della policy (behavior knobs).",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "warm_get",
         "description": "Stato del pool warm (holder/caldi, TTL, per profilo).",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "keys_soft_get",
         "description": "Chiavi soft-disabled (soppresse senza retirement).",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "circuits_get",
         "description": "Stato circuit breaker per provider/unique.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "warm_wake",
         "description": "Forza il risveglio del pool warm (sweep/wake).",
         "inputSchema": {"type": "object", "properties": {
             "unique": {"type": "string"}, "group": {"type": "string"},
             "session_id": {"type": "string"},
             "min_age_sec": {"type": "number"},
             "limit": {"type": "integer"},
             "only_zen": {"type": "boolean"}}}},
        {"name": "hosts_drain",
         "description": "Mette in drain un host (esclude i suoi deployment).",
         "inputSchema": {"type": "object", "properties": {
             "unique": {"type": "string"}, "inflight": {"type": "integer"}},
             "required": ["unique"]}},
        {"name": "hosts_undrain",
         "description": "Rimuove il drain da un host.",
         "inputSchema": {"type": "object", "properties": {
             "unique": {"type": "string"}}, "required": ["unique"]}},
        {"name": "metrics_reset",
         "description": "Azzera i contatori/istogrammi runtime in memoria.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "scores_reset",
         "description": "Azzera i punteggi persistiti (opzionale per profilo).",
         "inputSchema": {"type": "object", "properties": {
             "unique": {"type": "string"}, "model": {"type": "string"}}}},
        {"name": "sessions_purge",
         "description": "Rimuove sessioni sticky/cache (tutte o una).",
         "inputSchema": {"type": "object", "properties": {
             "session_id": {"type": "string"}}}},
        {"name": "keys_leases_clear",
         "description": "Rilascia i lease chiave in-flight (tutti o un unique).",
         "inputSchema": {"type": "object", "properties": {
             "unique": {"type": "string"}}}},
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
    args = arguments if isinstance(arguments, dict) else {}
    synth = _McpRequest(request, args)

    # ---------------------------------------------------------- policy
    if tool_name == "policy_get":
        return await _admin.get_policy(request)
    if tool_name == "policy_patch":
        return await _admin.patch_policy(synth)
    if tool_name == "policy_raw_get":
        return await _admin.get_policy_raw(request)
    if tool_name == "policy_raw_put":
        return await _admin.put_policy_raw(synth)
    # ----------------------------------------------------- deployments
    if tool_name == "deploy_list":
        return await _admin.list_deployments(request, profile=args.get("profile"))
    if tool_name == "deploy_get":
        dep_id = str(args.get("id") or "")
        if not dep_id:
            return _admin._err(400, "id is required")
        header, rows = csv_store.load_table(gw_state.CSV_PATH)
        idx, row = csv_store.find_row(header, rows, dep_id)
        if idx is None:
            return _admin._err(404, f"deployment '{dep_id}' non esiste")
        return {"deployment": _admin._deployment_view(
            header, row, gw_state.config.proxy_prefix)}
    if tool_name == "deploy_create":
        return await _admin.create_deployment(synth)
    if tool_name == "deploy_update":
        dep_id = str(args.get("id") or "")
        if not dep_id:
            return _admin._err(400, "id is required")
        return await _admin.update_deployment(dep_id, synth)
    if tool_name == "deploy_delete":
        dep_id = str(args.get("id") or "")
        if not dep_id:
            return _admin._err(400, "id is required")
        return await _admin.delete_deployment(dep_id, request)
    if tool_name == "deploy_bulk":
        return await _admin.bulk_deployments(synth)
    if tool_name == "deploy_expiring":
        days = args.get("days", 7)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        return await _admin.deployments_expiring(request, days=days)
    if tool_name == "deploy_probe":
        return await _admin.deployments_probe(synth)
    if tool_name == "deploy_probe_bulk":
        return await _admin.deployments_probe_bulk(synth)
    if tool_name == "deploy_unretire":
        return await _admin.deployments_unretire(synth)
    # --------------------------------------------------------- profiles
    if tool_name == "profile_list":
        return await _admin.list_profiles(request)
    if tool_name == "profile_purge":
        return await _admin.purge_profile(synth)
    # ------------------------------------------------------ csv/backups
    if tool_name == "csv_get":
        return await _admin.admin_csv_get(request)
    if tool_name == "csv_put":
        return await _admin.admin_csv_put(synth)
    if tool_name == "backup_list":
        return await _admin.list_backups(request)
    if tool_name == "backup_restore":
        return await _admin.restore_backup(synth)
    # ---------------------------------------------------- capabilities
    if tool_name == "capabilities_seed":
        return await _admin.capabilities_seed_from_map(synth)
    if tool_name == "capabilities_audit":
        return await _admin.capabilities_audit(request)
    # --------------------------------------------------- runtime state
    if tool_name == "state_get":
        return await _admin.state(request)
    if tool_name == "tuning_get":
        return await _admin.get_tuning(request)
    if tool_name == "history_get":
        limit = args.get("limit", 50)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        return await _admin.admin_history(request, limit=limit)
    if tool_name == "reload_gateway":
        return await _admin.reload_gateway(request)
    if tool_name == "cooldowns_clear":
        return await _admin.clear_cooldowns(synth)
    if tool_name == "pressure_clear":
        return await _admin.clear_pressure(synth)
    if tool_name == "pressure_inspect":
        return await _admin.inspect_pressure(synth)
    # ------------------------------------------------------- sessions
    if tool_name == "sessions_list":
        return await _admin.list_sessions(request)
    if tool_name == "sessions_release":
        return await _admin.release_sessions(synth)
    if tool_name == "sessions_detail":
        sid = str(args.get("session_id") or "")
        if not sid:
            return _admin._err(400, "session_id is required")
        return await _admin.session_detail(
            request, sid, window=str(args.get("window") or "7d"))
    if tool_name == "stats_sessions":
        limit = args.get("limit", 50)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        return await _admin.stats_sessions(
            request, window=str(args.get("window") or "7d"), limit=limit)
    # ---------------------------------------------------------- stats
    if tool_name == "stats_summary":
        return await _admin.stats_summary(request)
    if tool_name == "stats_tokens":
        return await _admin.stats_tokens(request, window=str(args.get("window") or "24h"))
    if tool_name == "stats_cache":
        return await _admin.stats_cache(request)
    if tool_name == "stats_models":
        return await _admin.stats_models(request, window=str(args.get("window") or "7d"))
    if tool_name == "stats_deployments":
        return await _admin.stats_deployments(
            request, profile=args.get("profile"),
            sort=str(args.get("sort") or "success_rate"),
            order=str(args.get("order") or "desc"))
    if tool_name == "stats_providers":
        return await _admin.stats_providers(request)
    # ------------------------------------------------------- insights
    if tool_name == "insights_get":
        days = args.get("days", 7)
        try:
            days = int(days)
        except (TypeError, ValueError):
            days = 7
        return await _admin.admin_insights(
            request, days=days, group_by=str(args.get("group_by") or "model"))
    if tool_name == "insights_summary":
        return await _admin.admin_insights_summary(request)
    if tool_name == "leaderboard_get":
        return await _admin.admin_insights_leaderboard(
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
        return await _admin.admin_logs_calls(
            request, tail=tail,
            tags=str(args.get("tags") or "summary,route,identity,fallback"))
    if tool_name == "logs_errors":
        tail = args.get("tail", 500)
        try:
            tail = int(tail)
        except (TypeError, ValueError):
            tail = 500
        return await _admin.admin_logs_errors(
            request, tail=tail, filter=args.get("filter"))
    # ------------------------------------------------------- playground
    if tool_name == "playground":
        return await _admin.admin_playground(synth)
    # ------------------------------------------ persisted / health / guide
    if tool_name == "deployments_stats":
        return await _admin.admin_deployments_stats(
            request, profile=args.get("profile"))
    if tool_name == "providers_health":
        return await _admin.admin_providers_health(request)
    if tool_name == "guide_get":
        return await _admin.agent_guide()

    # ------------------------------------------- Fasi 2-4 (nuovi endpoint MCP)
    if tool_name == "policy_schema_get":
        return await _admin.get_policy_schema(request)
    if tool_name == "warm_get":
        return await _admin.warm_state(request)
    if tool_name == "keys_soft_get":
        return await _admin.keys_soft(request)
    if tool_name == "circuits_get":
        return await _admin.circuits(request)
    if tool_name == "warm_wake":
        return await _admin.warm_wake(synth)
    if tool_name == "hosts_drain":
        return await _admin.drain_host(synth)
    if tool_name == "hosts_undrain":
        return await _admin.undrain_host(synth)
    if tool_name == "metrics_reset":
        return await _admin.reset_metrics(synth)
    if tool_name == "scores_reset":
        return await _admin.reset_scores(synth)
    if tool_name == "sessions_purge":
        return await _admin.purge_sessions(synth)
    if tool_name == "keys_leases_clear":
        return await _admin.clear_key_leases(synth)

    return _admin._err(404, f"tool MCP sconosciuto: {tool_name}")


@mcp_api.get("/mcp/config/tools", tags=["admin"])
async def mcp_config_tools(request: Request):
    """Tool definitions per il protocollo MCP di configurazione."""
    denied = _admin._require_master(request)
    if denied:
        return denied
    tools = _mcp_tool_specs()
    return {"tools": tools, "count": len(tools)}


@mcp_api.post("/mcp/config/execute", tags=["admin"])
async def mcp_config_execute(request: Request):
    """Esegue un tool MCP: body {tool, arguments} -> risultato dell'handler.

    L'esito e' l'envelope MCP standard: {content: [{type:text, text}], isError}.
    """
    denied = _admin._require_master(request)
    if denied:
        return denied
    body, bad = await _admin._json_body(request)
    if bad:
        return bad
    tool_name = str((body or {}).get("tool") or "")
    arguments = (body or {}).get("arguments") or {}
    if not tool_name:
        return _admin._err(400, "tool name is required")
    if not isinstance(arguments, dict):
        return _admin._err(400, "arguments deve essere un oggetto")
    known = _mcp_known_names()
    if tool_name not in known:
        return _admin._err(404, f"tool MCP sconosciuto: {tool_name}")
    try:
        result = await _mcp_dispatch(tool_name, arguments, request)
    except Exception as exc:                     # noqa: BLE001
        log.error("[mcp] tool %s failed: %s", tool_name, exc)
        return _admin._err(500, f"tool execution failed: {exc}")
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


@mcp_api.post("/mcp/config/call", tags=["admin"])
async def mcp_config_call(request: Request):
    """Endpoint JSON-RPC 2.0 compatibile con i client MCP.

    Metodi supportati:
      - initialize            -> capabilities/serverInfo
      - tools/list            -> {tools:[...]}
      - tools/call            -> {content, isError} (params: {name, arguments})
    """
    denied = _admin._require_master(request)
    if denied:
        return denied
    body, bad = await _admin._json_body(request)
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
