import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const flash = (res, msg, type) =>
  res.redirect("/system?flash=" + encodeURIComponent(msg) + "&flashType=" + type);

router.get("/system", requireAuth, authorize("system", "reload"), async (req, res, next) => {
  try {
    const data = await gateway.get("/admin/state");
    const cooldowns = Array.isArray(data?.cooldowns_active) ? data.cooldowns_active : [];
    const sessions = Array.isArray(data?.sticky_sessions) ? data.sticky_sessions : [];
    res.render("system-actions/index", { state: data, cooldowns, sessions });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

const cooldownSchema = z.object({
  unique: z.string().trim().optional(),
});

router.post("/system/cooldowns/clear", requireAuth, authorize("system", "cooldowns"), async (req, res, next) => {
  const parsed = cooldownSchema.safeParse(req.body);
  if (!parsed.success) return flash(res, "dati non validi", "error");
  const { unique } = parsed.data;
  try {
    const result = await gateway.post("/admin/cooldowns/clear", { json: { unique } });
    await auditLog({
      user: req.user,
      op: unique ? "cooldown_clear" : "clear_all",
      target: unique ?? "*",
      detail: { unique: unique ?? null },
      routeMethod: req.method,
      gatewayPath: "/admin/cooldowns/clear",
      ip: req.ip,
    });
    return flash(res, unique ? "cooldown sbloccato" : "tutti i " + (result?.cleared ?? 0) + " cooldown sbloccati", "success");
  } catch (err) {
    if (err instanceof GatewayError) return flash(res, "Gateway: " + err.message, "error");
    next(err);
  }
});

const sessionSchema = z.object({
  session_id: z.string().trim().optional(),
});

router.post("/system/sessions/release", requireAuth, authorize("system", "sessions"), async (req, res, next) => {
  const parsed = sessionSchema.safeParse(req.body);
  if (!parsed.success) return flash(res, "dati non validi", "error");
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
    return flash(res, session_id ? "sessione rilasciata" : "tutte le " + (result?.released ?? 0) + " sessioni rilasciate", "success");
  } catch (err) {
    if (err instanceof GatewayError) return flash(res, "Gateway: " + err.message, "error");
    next(err);
  }
});

router.post("/system/reload", requireAuth, authorize("system", "reload"), async (req, res, next) => {
  try {
    await gateway.post("/admin/reload");
    await auditLog({
      user: req.user,
      op: "system.reload",
      target: "*",
      detail: {},
      routeMethod: req.method,
      gatewayPath: "/admin/reload",
      ip: req.ip,
    });
    return flash(res, "configurazione ricaricata", "success");
  } catch (err) {
    if (err instanceof GatewayError) return flash(res, "Gateway: " + err.message, "error");
    next(err);
  }
});

const unretireSchema = z.object({
  unique: z.string().trim().min(1),
});

router.post("/system/unretire", requireAuth, authorize("deployments", "unretire"), async (req, res, next) => {
  const parsed = unretireSchema.safeParse(req.body);
  if (!parsed.success) return flash(res, "dati non validi", "error");
  const { unique } = parsed.data;
  try {
    await gateway.post("/admin/deployments/unretire", { json: { unique } });
    await auditLog({
      user: req.user,
      op: "deployments.unretire",
      target: unique,
      detail: { unique },
      routeMethod: req.method,
      gatewayPath: "/admin/deployments/unretire",
      ip: req.ip,
    });
    return flash(res, "deployment riattivato", "success");
  } catch (err) {
    if (err instanceof GatewayError) return flash(res, "Gateway: " + err.message, "error");
    next(err);
  }
});

export default router;
