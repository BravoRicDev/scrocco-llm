import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

const fail = (res, err) => {
  const status = err instanceof GatewayError ? (err.status && err.status > 0 ? err.status : 502) : 500;
  return res.status(status).json({ error: { message: err.message || "errore" } });
};

router.get("/api/v1/deployments", requireAuth, authorize("deployments", "read"), async (req, res) => {
  try {
    const list = await gateway.get("/admin/deployments", { params: { profile: req.query.profile } });
    let deployments = Array.isArray(list) ? list : (list.deployments || []);
    const q = String(req.query.q || "").trim().toLowerCase();
    if (q) {
      deployments = deployments.filter((d) =>
        [d.modello, d.provider, d.endpoint, d.group, d.id].some((v) => String(v || "").toLowerCase().includes(q))
      );
    }
    res.json({ count: deployments.length, deployments });
  } catch (err) { fail(res, err); }
});

router.get("/api/v1/deployments/expiring", requireAuth, authorize("expiring", "read"), async (req, res) => {
  try {
    const days = Math.min(Math.max(parseInt(req.query.days, 10) || 7, 1), 90);
    const data = await gateway.get("/admin/deployments/expiring", { params: { days } });
    res.json({ days, expiring: data.expiring || [] });
  } catch (err) { fail(res, err); }
});

router.get("/api/v1/profiles", requireAuth, authorize("profiles", "read"), async (req, res) => {
  try {
    const data = await gateway.get("/admin/profiles");
    res.json({ count: data.count ?? (data.profiles || []).length, profiles: data.profiles || [] });
  } catch (err) { fail(res, err); }
});

router.get("/api/v1/deployments/:id", requireAuth, authorize("deployments", "read"), async (req, res) => {
  try {
    const list = await gateway.get("/admin/deployments");
    const deployments = Array.isArray(list) ? list : (list.deployments || []);
    const found = deployments.find((d) => String(d.id) === String(req.params.id));
    if (!found) return res.status(404).json({ error: { message: "deployment non trovato" } });
    res.json(found);
  } catch (err) { fail(res, err); }
});

export default router;
