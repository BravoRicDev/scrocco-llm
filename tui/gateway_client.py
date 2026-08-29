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
                msg = resp.text[:300]
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
