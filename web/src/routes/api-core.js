import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

const fail = (res, err) => {
  const status = err instanceof GatewayError ? (err.status && err.status > 0 ? err.status : 502) : 500;
  return res.status(status).json({ error: { message: err.message || "errore" } });
};

const proxyJson = (path, resource, mapper) =>
  async (req, res) => {
    try {
      const data = await gateway.get(path, { params: req.query });
      res.json(mapper ? mapper(data) : data);
    } catch (err) { fail(res, err); }
  };

const proxyText = (path, resource) =>
  async (req, res) => {
    try {
      const text = await gateway.rawGet(path);
      res.json({ text: typeof text === "string" ? text : JSON.stringify(text) });
    } catch (err) { fail(res, err); }
  };

// policy: NON inoltrare `configured` grezzo (potrebbe contenere client_keys in
// chiaro se lo yaml le ha). Solo file + effective (che ha gia' i *_masked).
router.get("/api/v1/policy", requireAuth, authorize("policy", "read"),
  proxyJson("/admin/policy", "policy", (d) => ({ file: d.file, effective: d.effective })));

router.get("/api/v1/state", requireAuth, authorize("state", "read"),
  proxyJson("/admin/state", "state"));

router.get("/api/v1/history", requireAuth, authorize("history", "read"),
  proxyJson("/admin/history", "history"));

router.get("/api/v1/insights", requireAuth, authorize("insights", "read"),
  proxyJson("/admin/insights", "insights"));

router.get("/api/v1/insights/summary", requireAuth, authorize("insights", "read"),
  proxyJson("/admin/insights/summary", "insights"));

router.get("/api/v1/insights/leaderboard", requireAuth, authorize("insights", "read"),
  proxyJson("/admin/insights/leaderboard", "insights"));

router.get("/api/v1/bootstrap", requireAuth, authorize("bootstrap", "read"),
  proxyText("/bootstrap", "bootstrap"));

router.get("/api/v1/bootstrap/status", requireAuth, authorize("bootstrap", "read"),
  proxyJson("/bootstrap/status", "bootstrap"));

router.get("/api/v1/bootstrap/providers", requireAuth, authorize("bootstrap", "read"),
  proxyJson("/bootstrap/providers", "bootstrap"));

router.get("/api/v1/guide", requireAuth, authorize("guide", "read"),
  proxyText("/admin/guide", "guide"));

router.get("/api/v1/logs/calls", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/logs/calls", "observability"));

router.get("/api/v1/logs/errors", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/logs/errors", "observability"));

// --------------------------------------------------- statistics & sessions
// Superficie di osservabilità/configurazione (prima solo in TUI): ora esposta
// anche qui cosi' l'MCP web la scopre e gli agenti la possono usare.
router.get("/api/v1/stats/summary", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/stats/summary", "observability"));

router.get("/api/v1/stats/tokens", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/stats/tokens", "observability"));

router.get("/api/v1/stats/cache", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/stats/cache", "observability"));

router.get("/api/v1/stats/models", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/stats/models", "observability"));

router.get("/api/v1/stats/deployments", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/stats/deployments", "observability"));

router.get("/api/v1/stats/providers", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/stats/providers", "observability"));

router.get("/api/v1/stats/sessions", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/stats/sessions", "observability"));

router.get("/api/v1/sessions", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/sessions", "observability"));

router.get("/api/v1/sessions/:id", requireAuth, authorize("observability", "read"),
  async (req, res) => {
    try {
      const data = await gateway.get(
        `/admin/sessions/${encodeURIComponent(req.params.id)}`,
        { params: req.query });
      res.json(data);
    } catch (err) { fail(res, err); }
  });

router.get("/api/v1/tuning", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/tuning", "observability"));

// persisted deployment scores / provider health (observability)
router.get("/api/v1/deployments/stats", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/deployments/stats", "observability"));

router.get("/api/v1/providers/health", requireAuth, authorize("observability", "read"),
  proxyJson("/admin/providers/health", "observability"));

// raw policy / CSV (master-only a monte; qui solo se autorizzati)
router.get("/api/v1/policy/raw", requireAuth, authorize("policy", "read"),
  proxyJson("/admin/policy/raw", "policy"));

router.get("/api/v1/csv", requireAuth, authorize("csv", "read"),
  proxyJson("/admin/csv", "csv"));

router.get("/api/v1/backups", requireAuth, authorize("config_snapshots", "read"),
  proxyJson("/admin/backups", "config_snapshots"));

// --------------------------------------------------- MCP config protocol
router.get("/api/v1/mcp/config/tools", requireAuth, authorize("mcp_config", "read"),
  proxyJson("/admin/mcp/config/tools", "mcp_config"));

export default router;
