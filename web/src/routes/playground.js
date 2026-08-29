import { Router } from "express";
import { z } from "zod";
import rateLimit from "express-rate-limit";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const apiError = (res, status, message) =>
  res.status(status).json({ error: { message } });

// 20 run/min per utente (il playground consuma quota upstream).
const runLimiter = rateLimit({
  windowMs: 60 * 1000,
  max: 20,
  standardHeaders: true,
  legacyHeaders: false,
  keyGenerator: (req) => String(req.user?.sub || req.ip),
});

function modelNames(payload) {
  const data = (payload && payload.data) || payload || [];
  if (!Array.isArray(data)) return [];
  return data.map((m) => m.id || m.name).filter(Boolean);
}

router.get("/playground", requireAuth, authorize("playground", "use"), async (req, res, next) => {
  try {
    const [models, profiles] = await Promise.allSettled([
      gateway.get("/v1/models"),
      gateway.get("/admin/profiles"),
    ]);
    res.render("playground/index", {
      models: models.status === "fulfilled" ? modelNames(models.value) : [],
      profiles: profiles.status === "fulfilled" ? (profiles.value.profiles || []) : [],
      result: null,
      error: null,
    });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

router.post("/playground/run", requireAuth, authorize("playground", "use"), runLimiter, async (req, res, next) => {
  const parsed = z.object({
    model: z.string().trim().min(1),
    prompt: z.string().trim().min(1),
    stream: z.boolean().optional(),
  }).safeParse(req.body);
  if (!parsed.success) return apiError(res, 400, "model e prompt sono obbligatori");

  const { model, prompt } = parsed.data;
  try {
    const result = await gateway.post("/admin/playground", {
      json: { model, messages: [{ role: "user", content: prompt }], stream: false },
      timeout: 600000,
    });
    await auditLog({
      user: req.user,
      op: "playground.run",
      target: model,
      detail: { attempts: result?.attempts ?? null, fallbacks: result?.fallbacks ?? null, ok: result?.ok ?? null },
      routeMethod: req.method,
      gatewayPath: "/admin/playground",
      ip: req.ip,
    });
    // la risposta GP-01 ha `content` (non `choices`)
    return res.json({
      ok: result?.ok ?? true,
      model: result?.resolved_model || model,
      content: result?.content ?? "",
      trace: result?.trace || [],
      attempts: result?.attempts ?? (result?.trace ? result.trace.length : 0),
      fallbacks: result?.fallbacks ?? 0,
      used: result?.used || null,
      error: result?.error?.message || null,
    });
  } catch (err) {
    if (err instanceof GatewayError) return apiError(res, err.status || 502, err.message);
    next(err);
  }
});

export default router;
