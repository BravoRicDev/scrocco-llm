import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

function cleanParams(query) {
  const params = {};
  if (query.filter !== undefined && query.filter !== "") params.filter = String(query.filter);
  if (query.tail !== undefined && query.tail !== "") params.tail = String(query.tail);
  if (query.since !== undefined && query.since !== "") params.since = String(query.since);
  return params;
}

router.get("/observability/errors", requireAuth, authorize("observability", "read"), async (req, res, next) => {
  try {
    const params = cleanParams(req.query);
    if (!params.tail) params.tail = "200";
    const seed = await gateway.get("/admin/logs/errors", { params });
    res.render("observability/errors", { events: seed.events || [], up: true });
  } catch (err) {
    if (err instanceof GatewayError) return res.render("observability/errors", { events: [], up: false });
    next(err);
  }
});

router.get("/api/errors/events", requireAuth, authorize("observability", "read"), async (req, res) => {
  try {
    const params = cleanParams(req.query);
    if (!params.tail) params.tail = "500";
    const data = await gateway.get("/admin/logs/errors", { params });
    res.json({ events: data.events || [], up: true });
  } catch {
    res.json({ events: [], up: false });
  }
});

export default router;
