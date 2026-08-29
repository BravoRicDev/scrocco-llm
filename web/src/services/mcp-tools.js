import { z } from "zod";
import config from "../config.js";
import { logger } from "./logger.js";
import apiDeploymentsRouter from "../routes/api-deployments.js";
import apiCoreRouter from "../routes/api-core.js";
import apiWriteRouter from "../routes/api-write.js";

// Tool set MCP = introspezione dei 3 router /api/v1 reali. Un endpoint
// aggiunto/rimosso lì compare/scompare qui al riavvio successivo. TOOL_META
// arricchisce con nome/descrizione/schema gli endpoint noti; gli altri
// restano tool funzionanti con schema generico.

const pick = (lang, d) => (!d ? undefined : (d[lang] || d.en));

function camelToSnake(s) {
  return s.replace(/[A-Z]/g, (c) => "_" + c.toLowerCase());
}

function autoName(method, path) {
  const parts = path.replace(/^\/api\/v1\/?/, "").split("/").filter((p) => p && !p.startsWith(":"));
  const verb = { GET: "get", POST: "create", PUT: "update", PATCH: "patch", DELETE: "delete" }[method] || method.toLowerCase();
  return (parts.join("_") || "root") + "_" + verb;
}

const S = (shape) => shape; // inputSchema = { key: zodType } (SDK vuole la shape, non z.object)

const TOOL_META = {
  "GET /api/v1/deployments": {
    name: "deploy_list",
    description: { en: "List gateway deployments (filter by profile / free-text q).", it: "Elenca i deployment del gateway (filtro per profilo / ricerca testuale q)." },
    inputSchema: S({ profile: z.string().optional(), q: z.string().optional() }),
  },
  "GET /api/v1/deployments/:id": {
    name: "deploy_get",
    description: { en: "Get a single deployment by id (404 if missing).", it: "Recupera un singolo deployment per id (404 se assente)." },
    inputSchema: S({ id: z.string() }),
  },
  "GET /api/v1/deployments/expiring": {
    name: "deploy_expiring",
    description: { en: "Deployments whose key expires within N days.", it: "Deployment con chiave in scadenza entro N giorni." },
    inputSchema: S({ days: z.number().int().optional() }),
  },
  "GET /api/v1/profiles": {
    name: "profile_list",
    description: { en: "List routing profiles.", it: "Elenca i profili di routing." },
    inputSchema: S({}),
  },
  "POST /api/v1/deployments": {
    name: "deploy_create",
    description: { en: "Create a deployment (profile, modello, endpoint, data, key, context required).", it: "Crea un deployment (profile, modello, endpoint, data, key, context obbligatori)." },
    inputSchema: S({ profile: z.string(), modello: z.string(), provider: z.string().optional(), endpoint: z.string(), data: z.string(), key: z.string(), context: z.number().int(), priority: z.number().int().optional(), caps: z.string().optional() }),
  },
  "PUT /api/v1/deployments/:id": {
    name: "deploy_update",
    description: { en: "Update a deployment by id (empty key = keep current).", it: "Aggiorna un deployment per id (key vuota = non ruotare)." },
    inputSchema: S({ id: z.string(), profile: z.string().optional(), modello: z.string().optional(), endpoint: z.string().optional(), data: z.string().optional(), key: z.string().optional(), context: z.number().int().optional(), priority: z.number().int().optional(), caps: z.string().optional() }),
  },
  "DELETE /api/v1/deployments/:id": {
    name: "deploy_delete",
    description: { en: "Delete a deployment by id.", it: "Elimina un deployment per id." },
    inputSchema: S({ id: z.string() }),
  },
  "POST /api/v1/deployments/bulk": {
    name: "deploy_bulk",
    description: { en: "Atomic bulk operations (1..50): create/update/delete.", it: "Operazioni bulk atomiche (1..50): create/update/delete." },
    inputSchema: S({ operations: z.array(z.record(z.any())).min(1).max(50) }),
  },
  "POST /api/v1/deployments/probe": {
    name: "deploy_probe",
    description: { en: "Probe one deployment key (consumes upstream quota; prefer cached).", it: "Sonda la chiave di un deployment (consuma quota upstream; preferisci cached)." },
    inputSchema: S({ id: z.string().optional(), unique: z.string().optional(), force: z.boolean().optional() }),
  },
  "POST /api/v1/deployments/probe/bulk": {
    name: "deploy_probe_bulk",
    description: { en: "Probe many keys by filter (all | cap:x | profile).", it: "Sonda molte chiavi per filtro (all | cap:x | profilo)." },
    inputSchema: S({ filter: z.string().optional(), force: z.boolean().optional() }),
  },
  "POST /api/v1/deployments/unretire": {
    name: "deploy_unretire",
    description: { en: "Re-activate a retired deployment.", it: "Riattiva un deployment ritirato." },
    inputSchema: S({ unique: z.string() }),
  },
  "GET /api/v1/policy": {
    name: "policy_get",
    description: { en: "Read the effective routing policy (masked keys).", it: "Legge la policy di routing effettiva (chiavi mascherate)." },
    inputSchema: S({}),
  },
  "PATCH /api/v1/policy": {
    name: "policy_patch",
    description: { en: "Patch the routing policy (ADMIN ONLY).", it: "Applica una patch alla policy (SOLO admin)." },
    inputSchema: S({}),
  },
  "GET /api/v1/state": { name: "state_get", description: { en: "Aggregate gateway state (cooldowns, sticky, health, budget).", it: "Stato aggregato del gateway (cooldown, sticky, health, budget)." }, inputSchema: S({}) },
  "GET /api/v1/history": { name: "history_get", description: { en: "Gateway operations journal (limit <= 100).", it: "Journal operazioni del gateway (limit <= 100)." }, inputSchema: S({ limit: z.number().int().optional() }) },
  "GET /api/v1/insights": { name: "insights_get", description: { en: "Usage/cost insights (days, group_by).", it: "Insight uso/costi (days, group_by)." }, inputSchema: S({ days: z.number().int().optional(), group_by: z.string().optional() }) },
  "GET /api/v1/insights/summary": { name: "insights_summary", description: { en: "24h usage summary.", it: "Sintesi uso 24h." }, inputSchema: S({}) },
  "GET /api/v1/insights/leaderboard": { name: "leaderboard_get", description: { en: "Deployment leaderboard (window, sort, order, profile).", it: "Leaderboard deployment (window, sort, order, profilo)." }, inputSchema: S({ window: z.string().optional(), sort: z.string().optional(), order: z.string().optional(), profile: z.string().optional() }) },
  "GET /api/v1/bootstrap": { name: "bootstrap_playbook", description: { en: "Bootstrap playbook (text).", it: "Playbook di bootstrap (testo)." }, inputSchema: S({}) },
  "GET /api/v1/bootstrap/status": { name: "bootstrap_status", description: { en: "Bootstrap gap analysis.", it: "Gap analysis di bootstrap." }, inputSchema: S({}) },
  "GET /api/v1/bootstrap/providers": { name: "bootstrap_providers", description: { en: "Provider registry.", it: "Registry dei provider." }, inputSchema: S({}) },
  "GET /api/v1/guide": { name: "guide_read", description: { en: "Official agent protocol guide (markdown).", it: "Guida ufficiale del protocollo agente (markdown)." }, inputSchema: S({}) },
  "POST /api/v1/system/reload": { name: "system_reload", description: { en: "Reload gateway config from CSV/yaml.", it: "Ricarica la config del gateway da CSV/yaml." }, inputSchema: S({}) },
  "POST /api/v1/system/cooldowns/clear": { name: "system_clear_cooldown", description: { en: "Clear cooldowns (one unique or all).", it: "Azzera i cooldown (un unique o tutti)." }, inputSchema: S({ unique: z.string().optional() }) },
  "POST /api/v1/system/sessions/release": { name: "system_release_sessions", description: { en: "Release sticky sessions (one session_id or all).", it: "Rilascia le sessioni sticky (un session_id o tutte)." }, inputSchema: S({ session_id: z.string().optional() }) },
  "POST /api/v1/capabilities/seed": { name: "capabilities_seed", description: { en: "Seed capabilities from the model map (dry_run to preview).", it: "Semina le capacità dalla mappa modelli (dry_run per anteprima)." }, inputSchema: S({ dry_run: z.boolean().optional() }) },
  "POST /api/v1/capabilities/audit": { name: "capabilities_audit", description: { en: "Audit capabilities coverage across accounts.", it: "Audit della copertura capacità sugli account." }, inputSchema: S({}) },
};

function genericInputSchema(path) {
  const shape = {};
  (path.match(/:([a-zA-Z0-9_]+)/g) || []).forEach((p) => { shape[camelToSnake(p.slice(1))] = z.string(); });
  return shape;
}

export function discoverTools(lang = "en") {
  const tools = [];
  const seen = new Set();
  for (const routerMod of [apiDeploymentsRouter, apiCoreRouter, apiWriteRouter]) {
    for (const layer of routerMod.stack || []) {
      if (!layer.route || typeof layer.route.path !== "string") continue;
      const routePath = layer.route.path;
      const methods = Object.keys(layer.route.methods || {}).filter((m) => layer.route.methods[m] && m !== "_all");
      for (const method of methods) {
        const httpMethod = method.toUpperCase();
        const key = `${httpMethod} ${routePath}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const meta = TOOL_META[key];
        tools.push({
          method: httpMethod,
          path: routePath,
          name: meta?.name || autoName(httpMethod, routePath),
          description: meta ? pick(lang, meta.description) : `Gateway API ${httpMethod} ${routePath} — generic schema, see AGENT.md.`,
          inputSchema: meta?.inputSchema || genericInputSchema(routePath),
          enriched: !!meta,
        });
      }
    }
  }
  return tools;
}

function resolvePath(pathTemplate, args) {
  const consumed = new Set();
  const resolved = pathTemplate.replace(/:([a-zA-Z0-9_]+)/g, (_, camelParam) => {
    const snakeKey = camelToSnake(camelParam);
    consumed.add(snakeKey);
    const val = args[snakeKey] ?? args[camelParam];
    if (val === undefined || val === null) throw new Error(`Parametro mancante: ${snakeKey}`);
    return encodeURIComponent(String(val));
  });
  const rest = {};
  for (const [k, v] of Object.entries(args || {})) {
    if (consumed.has(k)) continue;
    if (v !== undefined) rest[k] = v;
  }
  return { resolved, rest };
}

async function contentFromResponse(resp) {
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    const data = await resp.json().catch(() => null);
    return { content: [{ type: "text", text: JSON.stringify(data) }], isError: !resp.ok };
  }
  const text = await resp.text();
  return { content: [{ type: "text", text }], isError: !resp.ok };
}

export function makeToolHandler(tool) {
  return async (args, extra) => {
    const authHeader = extra?.requestInfo?.headers?.authorization;
    if (!authHeader) {
      return { content: [{ type: "text", text: JSON.stringify({ error: "Authorization header mancante nella richiesta MCP" }) }], isError: true };
    }
    let resolved, rest;
    try {
      ({ resolved, rest } = resolvePath(tool.path, args || {}));
    } catch (err) {
      return { content: [{ type: "text", text: JSON.stringify({ error: err.message }) }], isError: true };
    }
    const isBodyMethod = ["POST", "PUT", "PATCH"].includes(tool.method);
    let url = `http://127.0.0.1:${config.port}${resolved}`;
    const opts = { method: tool.method, headers: { Authorization: authHeader } };
    if (isBodyMethod) {
      opts.headers["Content-Type"] = "application/json";
      opts.headers["Origin"] = `http://127.0.0.1:${config.port}`;
      opts.body = Object.keys(rest).length ? JSON.stringify(rest) : "{}";
    } else if (Object.keys(rest).length) {
      url += "?" + new URLSearchParams(Object.fromEntries(Object.entries(rest).map(([k, v]) => [k, String(v)]))).toString();
    }
    try {
      const resp = await fetch(url, { ...opts, signal: AbortSignal.timeout(60_000) });
      return await contentFromResponse(resp);
    } catch (err) {
      logger.error(`MCP proxy: ${tool.method} ${resolved} fallita: ${err.message}`);
      return { content: [{ type: "text", text: JSON.stringify({ error: "chiamata interna fallita: " + err.message }) }], isError: true };
    }
  };
}

export default { discoverTools, makeToolHandler };
