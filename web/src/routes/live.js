import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

router.get("/observability/live", requireAuth, authorize("observability", "read"), async (req, res, next) => {
  try {
    const data = await gateway.get("/admin/logs/calls", { params: { tail: 200, tags: "summary" } });
    res.render("observability/live", { events: data.events || [] });
  } catch (err) {
    if (err instanceof GatewayError) return res.render("observability/live", { events: [] });
    next(err);
  }
});

router.get("/api/live/events", requireAuth, authorize("observability", "read"), async (req, res) => {
  try {
    const data = await gateway.get("/admin/logs/calls", {
      params: { tail: 500, since: req.query.since, tags: "summary" },
    });
    res.json({ events: data.events || [], up: true });
  } catch {
    res.json({ events: [], up: false });
  }
});

export default router;
