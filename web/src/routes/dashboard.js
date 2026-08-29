import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

router.get("/", requireAuth, authorize("state", "read"), async (req, res, next) => {
  try {
    const state = await gateway.get("/admin/state");
    res.render("dashboard/index", { state });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

export default router;
