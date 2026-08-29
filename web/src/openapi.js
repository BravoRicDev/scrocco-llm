import apiDeploymentsRouter from "./routes/api-deployments.js";
import apiCoreRouter from "./routes/api-core.js";
import apiWriteRouter from "./routes/api-write.js";

// Costruisce `paths` per OpenAPI introspezionando i 3 router /api/v1 reali:
// una route aggiunta/rimossa lì compare/scompare qui al riavvio successivo.

const SUMMARIES = {
  "GET /api/v1/deployments": "Elenca i deployment (filtro profile / q)",
  "GET /api/v1/deployments/expiring": "Deployment con chiave in scadenza",
  "GET /api/v1/deployments/:id": "Singolo deployment per id",
  "GET /api/v1/profiles": "Elenca i profili di routing",
  "POST /api/v1/deployments": "Crea un deployment",
  "PUT /api/v1/deployments/:id": "Aggiorna un deployment",
  "DELETE /api/v1/deployments/:id": "Elimina un deployment",
  "POST /api/v1/deployments/bulk": "Operazioni bulk atomiche",
  "POST /api/v1/deployments/probe": "Sonda una chiave",
  "POST /api/v1/deployments/probe/bulk": "Sonda molte chiavi",
  "POST /api/v1/deployments/unretire": "Riattiva un deployment ritirato",
  "GET /api/v1/policy": "Policy di routing effettiva (mascherata)",
  "PATCH /api/v1/policy": "Patch della policy (SOLO admin)",
  "GET /api/v1/state": "Stato aggregato del gateway",
  "GET /api/v1/history": "Journal operazioni",
  "GET /api/v1/insights": "Insight uso/costi",
  "GET /api/v1/insights/summary": "Sintesi uso 24h",
  "GET /api/v1/insights/leaderboard": "Leaderboard deployment",
  "GET /api/v1/bootstrap": "Playbook di bootstrap (testo)",
  "GET /api/v1/bootstrap/status": "Gap analysis di bootstrap",
  "GET /api/v1/bootstrap/providers": "Registry provider",
  "GET /api/v1/guide": "Guida protocollo agente (markdown)",
  "POST /api/v1/system/reload": "Ricarica config gateway",
  "POST /api/v1/system/cooldowns/clear": "Azzera i cooldown",
  "POST /api/v1/system/sessions/release": "Rilascia sessioni sticky",
  "POST /api/v1/capabilities/seed": "Semina capacità dalla mappa modelli",
  "POST /api/v1/capabilities/audit": "Audit copertura capacità",
};

const TAG_FOR = (p) => {
  if (p.includes("/deployments")) return "deployments";
  if (p.includes("/profiles")) return "profiles";
  if (p.includes("/policy")) return "policy";
  if (p.includes("/state")) return "state";
  if (p.includes("/history")) return "history";
  if (p.includes("/insights")) return "insights";
  if (p.includes("/bootstrap")) return "bootstrap";
  if (p.includes("/guide")) return "guide";
  if (p.includes("/system")) return "system";
  if (p.includes("/capabilities")) return "capabilities";
  return "api";
};

export function buildPaths() {
  const paths = {};
  for (const routerMod of [apiDeploymentsRouter, apiCoreRouter, apiWriteRouter]) {
    for (const layer of routerMod.stack || []) {
      if (!layer.route || typeof layer.route.path !== "string") continue;
      const rp = layer.route.path;
      const oaPath = rp.replace(/:([a-zA-Z0-9_]+)/g, "{$1}");
      const methods = Object.keys(layer.route.methods || {}).filter((m) => layer.route.methods[m] && m !== "_all");
      for (const method of methods) {
        const key = `${method.toUpperCase()} ${rp}`;
        const op = {
          summary: SUMMARIES[key] || `${method.toUpperCase()} ${rp}`,
          tags: [TAG_FOR(rp)],
          security: [{ BearerAuth: [] }, { CookieAuth: [] }],
          responses: {
            200: { description: "OK" },
            401: { description: "Non autenticato", content: { "application/json": { schema: { $ref: "#/components/schemas/Error" } } } },
            403: { description: "Permesso negato", content: { "application/json": { schema: { $ref: "#/components/schemas/Error" } } } },
            404: { description: "Non trovato", content: { "application/json": { schema: { $ref: "#/components/schemas/Error" } } } },
            503: { description: "Gateway non disponibile", content: { "application/json": { schema: { $ref: "#/components/schemas/Error" } } } },
          },
        };
        const params = (rp.match(/:([a-zA-Z0-9_]+)/g) || []).map((p) => ({
          name: p.slice(1), in: "path", required: true, schema: { type: "string" },
        }));
        if (params.length) op.parameters = params;
        if (["post", "put", "patch"].includes(method)) {
          op.requestBody = { required: method !== "patch", content: { "application/json": { schema: { type: "object" } } } };
          if (method === "post" && rp === "/api/v1/deployments") op.responses[201] = { description: "Creato" };
        }
        paths[oaPath] = paths[oaPath] || {};
        paths[oaPath][method] = op;
      }
    }
  }
  return paths;
}

const SPEC = {
  openapi: "3.0.0",
  info: {
    title: "scrocco-web API",
    version: "1.0.0",
    description: "Surface JSON `/api/v1` del pannello scrocco-web davanti al gateway LLM scrocco-llm. Auth: `Authorization: Bearer <token>` (JWT-agent o API token `agtok_…`) oppure sessione cookie.",
  },
  servers: [{ url: "/api/v1", description: "Surface agenti" }],
  security: [{ BearerAuth: [] }, { CookieAuth: [] }],
  tags: [
    { name: "deployments" }, { name: "profiles" }, { name: "policy" },
    { name: "capabilities" }, { name: "state" }, { name: "history" },
    { name: "insights" }, { name: "bootstrap" }, { name: "guide" },
    { name: "system" }, { name: "api" },
  ],
  paths: buildPaths(),
  components: {
    securitySchemes: {
      BearerAuth: { type: "http", scheme: "bearer", bearerFormat: "agtok_…", description: "API token di lunga durata (`agtok_…`) o JWT-agent." },
      CookieAuth: { type: "apiKey", in: "cookie", name: "token", description: "Sessione browser (JWT httpOnly)." },
    },
    schemas: {
      Error: { type: "object", properties: { error: { type: "object", properties: { message: { type: "string" } } } } },
      Deployment: {
        type: "object",
        properties: {
          id: { type: "string" }, profile: { type: "string" }, modello: { type: "string" },
          provider: { type: "string" }, endpoint: { type: "string" }, data: { type: "string" },
          category: { type: "string" }, context_k: { type: "integer" }, priority: { type: "integer" },
          key_masked: { type: "string" }, group: { type: "string" },
          capabilities: { type: "array", items: { type: "string" } },
        },
      },
      DeploymentList: {
        type: "object",
        properties: { count: { type: "integer" }, deployments: { type: "array", items: { $ref: "#/components/schemas/Deployment" } } },
      },
    },
  },
};

export default SPEC;
