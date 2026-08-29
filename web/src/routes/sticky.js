import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

router.get("/sticky", requireAuth, authorize("observability", "read"), async (req, res, next) => {
  try {
    const data = await gateway.get("/admin/state");
    const sessions = Array.isArray(data?.sticky_sessions) ? data.sticky_sessions : [];
    res.render("sticky/index", { sessions });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

const releaseSchema = z.object({
  session_id: z.string().trim().optional(),
});

router.post("/sticky/release", requireAuth, authorize("system", "sessions"), async (req, res, next) => {
  const parsed = releaseSchema.safeParse(req.body);
  if (!parsed.success)
    return res.redirect("/sticky?flash=" + encodeURIComponent("dati non validi") + "&flashType=error");
  const { session_id } = parsed.data;
  try {
    const result = await gateway.post("/admin/sessions/release", { json: { session_id } });
    await auditLog({
      user: req.user,
      op: "session_release",
      target: session_id ?? "*",
      detail: { session_id: session_id ?? null },
      routeMethod: req.method,
      gatewayPath: "/admin/sessions/release",
      ip: req.ip,
    });
    const msg = session_id
      ? "sessione rilasciata"
      : "tutte le " + (result?.released ?? 0) + " sessioni rilasciate";
    return res.redirect("/sticky?flash=" + encodeURIComponent(msg) + "&flashType=success");
  } catch (err) {
    if (err instanceof GatewayError)
      return res.redirect("/sticky?flash=" + encodeURIComponent("Gateway: " + err.message) + "&flashType=error");
    next(err);
  }
});

export default router;
