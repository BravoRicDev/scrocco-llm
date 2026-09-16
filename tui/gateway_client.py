"""Client HTTP verso le admin API di scrocco-llm.

La TUI NON tocca mai il CSV: tutto passa dalle API (protocollo AGENT.md).
Il gateway è l'unica fonte di verità e applica ogni modifica in modo
atomico con reload immediato.

Uso:
    cli = GatewayClient()                       # env GATEWAY_URL/GATEWAY_MASTER_KEY
    st = await cli.state()
    await cli.create_deployment({...})
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from . import tui_config as cfg

DEFAULT_BASE = "http://127.0.0.1:{port}".format(
    port=os.environ.get("GATEWAY_PORT", "4001"))


class GatewayError(Exception):
    """Errore API con status HTTP e messaggio pronto per la UI."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


def load_master_key(env_file: str | Path | None = None) -> str:
    """Master key da env, altrimenti da .env.gateway accanto al progetto."""
    key = os.environ.get("GATEWAY_MASTER_KEY", "").strip()
    if key:
        return key
    candidates: list[Path] = []
    if env_file:
        candidates.append(Path(env_file))
    here = Path(__file__).resolve().parent.parent
    candidates += [here / ".env.gateway", Path.home() / "scrocco-llm" /
                   ".env.gateway"]
    for p in candidates:
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("GATEWAY_MASTER_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


class GatewayClient:
    """Wrapper async minimo sopra httpx per tutte le route admin."""

    def __init__(self, base_url: str | None = None,
                 master_key: str | None = None, timeout: float = 6.0):
        base = base_url or os.environ.get("GATEWAY_URL") or DEFAULT_BASE
        self.base_url = base.rstrip("/")
        self.master_key = master_key or load_master_key()
        self.http = httpx.AsyncClient(base_url=self.base_url,
                                      timeout=timeout)

    async def aclose(self) -> None:
        await self.http.aclose()

    # ------------------------------------------------------------- internals
    def _headers(self) -> dict[str, str]:
        if not self.master_key:
            raise GatewayError(0,
                               "master key non trovata: imposta "
                               "GATEWAY_MASTER_KEY o compila .env.gateway")
        return {"Authorization": f"Bearer {self.master_key}"}

    @staticmethod
    async def _parse(resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            try:
                msg = resp.json()["error"]["message"]
            except Exception:
                msg = resp.text[:cfg.HTTP_ERR_SNIPPET_CHARS]
            raise GatewayError(resp.status_code, msg)
        return resp.json()

    async def _send(self, method: str, path: str,
                    params: dict | None = None, json: dict | None = None):
        try:
            return await self.http.request(
                method, path, headers=self._headers(), params=params,
                json=json)
        except httpx.HTTPError as exc:
            raise GatewayError(0, f"gateway non raggiungibile "
                                  f"({self.base_url}): {exc}") from exc

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._parse(await self._send("GET", path, params))

    async def raw(self, path: str, params: dict | None = None) -> str:
        """GET che ritorna il corpo come testo (per endpoint non-JSON)."""
        resp = await self._send("GET", path, params)
        if resp.status_code >= 400:
            try:
                msg = resp.json()["error"]["message"]
            except Exception:
                msg = resp.text[:cfg.HTTP_ERR_SNIPPET_CHARS]
            raise GatewayError(resp.status_code, msg)
        return resp.text

    async def post(self, path: str, json: dict | None = None) -> Any:
        return await self._parse(await self._send("POST", path, json=json))

    async def put(self, path: str, json: dict) -> Any:
        return await self._parse(await self._send("PUT", path, json=json))

    async def patch(self, path: str, json: dict) -> Any:
        return await self._parse(await self._send("PATCH", path, json=json))

    async def delete(self, path: str) -> Any:
        return await self._parse(await self._send("DELETE", path))

    # ------------------------------------------------------------- endpoints
    async def healthz(self) -> dict:
        return await self._parse(await self._send("GET", "/healthz"))

    async def state(self) -> dict:
        return await self.get("/admin/state")

    async def profiles(self) -> list[dict]:
        data = await self.get("/admin/profiles")
        return data.get("profiles", [])

    async def deployments(self, profile: str | None = None) -> list[dict]:
        params = {"profile": profile} if profile else None
        data = await self.get("/admin/deployments", params)
        return data.get("deployments", [])

    async def create_deployment(self, payload: dict) -> dict:
        return await self.post("/admin/deployments", payload)

    async def update_deployment(self, dep_id: str, patch: dict) -> dict:
        return await self.put(f"/admin/deployments/{dep_id}", patch)

    async def delete_deployment(self, dep_id: str) -> dict:
        return await self.delete(f"/admin/deployments/{dep_id}")

    async def bulk(self, operations: list[dict]) -> dict:
        return await self.post("/admin/deployments/bulk",
                               {"operations": operations})

    async def expiring(self, days: int = 7) -> list[dict]:
        data = await self.get("/admin/deployments/expiring",
                              {"days": int(days)})
        return data.get("expiring", [])

    async def calls(self, tail: int = 300, since: float | None = None,
                    tags: str | None = None) -> dict:
        params: dict = {"tail": tail}
        if since is not None:
            params["since"] = since
        if tags:
            params["tags"] = tags
        return await self.get("/admin/logs/calls", params)

    async def errors(self, filter: str | None = None, tail: int = 500,
                     since: float | None = None) -> dict:
        params: dict = {"tail": tail}
        if filter:
            params["filter"] = filter
        if since is not None:
            params["since"] = since
        return await self.get("/admin/logs/errors", params)

    async def leaderboard(self, window: str = "7d", sort: str = "calls",
                          order: str = "desc", profile: str | None = None) -> dict:
        params: dict = {"window": window, "sort": sort, "order": order}
        if profile:
            params["profile"] = profile
        return await self.get("/admin/insights/leaderboard", params)

    async def policy_get(self) -> dict:
        return await self.get("/admin/policy")

    async def policy_patch(self, patch: dict) -> dict:
        """PATCH parziale: scalari sostituiti; 'profiles' unito per-profilo;
        liste/aliases sostituiti INTERI (il client ricostruisce la mappa)."""
        return await self.patch("/admin/policy", patch)

    async def clear_cooldowns(self, unique: str | None = None) -> dict:
        body = {"unique": unique} if unique else {}
        return await self.post("/admin/cooldowns/clear", body)

    async def release_sessions(self, session_id: str | None = None) -> dict:
        body = {"session_id": session_id} if session_id else {}
        return await self.post("/admin/sessions/release", body)

    async def reload(self) -> dict:
        return await self.post("/admin/reload", {})

    async def models(self) -> list[str]:
        """Nomi pubblici visibili alla master key."""
        data = await self.get("/v1/models")
        return sorted({m["id"] for m in data["data"]})

    # ------------------------------------------------------------- nuove API
    async def sessions(self) -> dict:
        """Sessioni attive: sticky, cache holder, session dep guard, slow demote."""
        return await self.get("/admin/sessions")

    async def session_detail(self, session_id: str, window: str = "7d") -> dict:
        """Dettaglio di UNA sessione: classifica deployment, modello preferito."""
        return await self.get(f"/admin/sessions/{session_id}",
                              params={"window": window})

    async def stats_sessions(self, window: str = "7d", limit: int = 50) -> dict:
        """Classifica delle sessioni: token, successi, modello preferito."""
        return await self.get("/admin/stats/sessions",
                              params={"window": window, "limit": limit})

    async def tuning(self) -> dict:
        """Parametri di tuning effettivi a runtime (router/forwarder/admin/storage)."""
        return await self.get("/admin/tuning")

    async def stats_tokens(self, window: str = "24h") -> dict:
        """Statistiche token generati/consumati."""
        return await self.get("/admin/stats/tokens", {"window": window})

    async def stats_cache(self) -> dict:
        """Statistiche cache: hit rate, coalescing, etc."""
        return await self.get("/admin/stats/cache")

    async def stats_success(self, window: str = "24h") -> dict:
        """Success rate aggregato e per modello (dallo stats/summary)."""
        return await self.get("/admin/stats/summary")

    async def stats_models(self, window: str = "7d") -> dict:
        """Classifica modelli per successi, token, latenza."""
        return await self.get("/admin/stats/models", {"window": window})

    async def stats_summary(self) -> dict:
        """Statistiche aggregate complete (token, cache, success rate, ranking)."""
        return await self.get("/admin/stats/summary")

    async def stats_deployments(self, profile: str | None = None,
                                sort: str = "success_rate",
                                order: str = "desc") -> dict:
        """Statistiche per deployment con ordinamento."""
        params: dict = {"sort": sort, "order": order}
        if profile:
            params["profile"] = profile
        return await self.get("/admin/stats/deployments", params)

    async def stats_providers(self) -> dict:
        """Statistiche aggregate per provider."""
        return await self.get("/admin/stats/providers")

    async def deployments_stats(self, profile: str | None = None) -> dict:
        """Punteggi persistiti per deployment."""
        params = {"profile": profile} if profile else None
        return await self.get("/admin/deployments/stats", params)

    async def providers_health(self) -> dict:
        """Salute aggregata per provider."""
        return await self.get("/admin/providers/health")

    async def guide(self) -> dict:
        """Documento guida/agenti (docs/AGENT.md) servito dal gateway.

        L'endpoint risponde text/markdown: il client normalizza in dict."""
        text = await self.raw("/admin/guide")
        return {"text": text}

    async def insights(self, days: int = 7, group_by: str = "model") -> dict:
        """Burn usage/costi aggregato dal ledger."""
        return await self.get("/admin/insights", {"days": days, "group_by": group_by})

    async def insights_summary(self) -> dict:
        """Riepilogo 24h compatto."""
        return await self.get("/admin/insights/summary")

    async def backups(self) -> dict:
        """Lista backup disponibili."""
        return await self.get("/admin/backups")

    async def restore_backup(self, filename: str) -> dict:
        """Ripristina un backup."""
        return await self.post("/admin/backups/restore", {"filename": filename})

    async def csv_raw(self) -> dict:
        """CSV grezzo delle configurazioni."""
        return await self.get("/admin/csv")

    async def put_csv_raw(self, raw: str) -> dict:
        """Sostituisci il CSV di configurazione."""
        return await self.put("/admin/csv", {"raw": raw})

    async def policy_raw(self) -> dict:
        """Policy YAML grezza."""
        return await self.get("/admin/policy/raw")

    async def put_policy_raw(self, raw: str) -> dict:
        """Sostituisci la policy YAML."""
        return await self.put("/admin/policy/raw", {"raw": raw})

    async def probe_bulk(self, filter: str = "all", force: bool = False) -> dict:
        """Valida deployment in blocco."""
        return await self.post("/admin/deployments/probe/bulk",
                               {"filter": filter, "force": force})

    async def probe_one(self, unique: str, force: bool = False) -> dict:
        """Valida un deployment."""
        return await self.post("/admin/deployments/probe",
                               {"unique": unique, "force": force})

    async def capabilities_audit(self) -> dict:
        """Audit server-side delle capacita."""
        return await self.post("/admin/capabilities/audit", {})

    async def capabilities_seed(self, dry_run: bool = False) -> dict:
        """Propone/ applica seed delle capacita da mappa."""
        return await self.post("/admin/capabilities/seed-from-map",
                               {"dry_run": dry_run})

    async def unretire(self, unique: str) -> dict:
        """Riattiva un deployment ritirato."""
        return await self.post("/admin/deployments/unretire", {"unique": unique})

    async def purge_profile(self, profile: str) -> dict:
        """Elimina la colonna profilo dal CSV (richiede 0 deployment)."""
        return await self.post("/admin/profiles/purge", {"profile": profile})

    async def playground(self, model: str, messages: list[dict],
                         profile: str | None = None,
                         max_tokens: int | None = None) -> dict:
        """Simula una chat (read-only, trace di routing)."""
        body: dict = {"model": model, "messages": messages}
        if profile:
            body["profile"] = profile
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return await self.post("/admin/playground", body)

    async def history(self, limit: int = 50) -> dict:
        """Journal operazioni (total, entries)."""
        return await self.get("/admin/history", {"limit": int(limit)})

    async def purge_profile(self, profile: str) -> dict:
        """Elimina la colonna profilo dal CSV (richiede 0 deployment)."""
        return await self.post("/admin/profiles/purge", {"profile": profile})

    async def history(self, limit: int = 50) -> dict:
        """Journal delle operazioni (limit <=100)."""
        return await self.get("/admin/history", {"limit": int(limit)})

    async def playground(self, model: str, messages: list[dict],
                         profile: str | None = None,
                         max_tokens: int | None = None) -> dict:
        """Simula una chat (read-only, trace di routing)."""
        body: dict = {"model": model, "messages": messages}
        if profile:
            body["profile"] = profile
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        return await self.post("/admin/playground", body)

    async def pressure_clear(self, unique: str | None = None,
                             model: str | None = None) -> dict:
        """Azzera cooldown/penalita/finestre (per unique, model o tutto)."""
        body: dict = {}
        if unique:
            body["unique"] = unique
        if model:
            body["model"] = model
        return await self.post("/admin/pressure/clear", body)

    async def pressure_inspect(self, limit: int = 40) -> dict:
        """Vista dettagliata del perche' i deployment vengono saltati."""
        return await self.post("/admin/pressure/inspect", {"limit": limit})

    # --------------------------------------------------------------- MCP
    async def mcp_tools(self) -> dict:
        """Elenco dei tool MCP di configurazione."""
        return await self.get("/admin/mcp/config/tools")

    async def mcp_execute(self, tool: str, arguments: dict | None = None) -> dict:
        """Esegue un tool MCP di configurazione."""
        return await self.post("/admin/mcp/config/execute",
                               {"tool": tool, "arguments": arguments or {}})

    async def mcp_call(self, method: str, params: dict | None = None,
                       rpc_id: int = 1) -> dict:
        """Chiamata JSON-RPC 2.0 al server MCP (tools/list, tools/call...)."""
        return await self.post("/admin/mcp/config/call",
                               {"jsonrpc": "2.0", "id": rpc_id,
                                "method": method, "params": params or {}})
