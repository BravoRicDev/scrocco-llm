import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

function clampLimit(v) {
  const n = parseInt(v, 10);
  if (Number.isNaN(n)) return 50;
  return Math.min(100, Math.max(1, n));
}

router.get("/history", requireAuth, authorize("history", "read"), async (req, res, next) => {
  try {
    const limit = clampLimit(req.query.limit);
    const data = await gateway.get("/admin/history", { params: { limit } });
    const entries = Array.isArray(data) ? data : (data?.entries ?? []);
    res.render("history/index", { total: Number(data?.total ?? entries.length), entries, limit });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

export default router;
