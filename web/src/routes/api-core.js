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

export default router;
