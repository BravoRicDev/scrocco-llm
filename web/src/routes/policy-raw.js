import { Router } from "express";
import { z } from "zod";
import { diffLines } from "diff";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const rawSchema = z.object({
  raw: z.string(),
});

const apiError = (res, status, message) =>
  res.status(status).json({ error: { message } });

router.get("/policy-raw", requireAuth, authorize("policy", "update"), async (req, res, next) => {
  try {
    const data = await gateway.get("/admin/policy/raw");
    res.render("policy-raw/index", {
      path: (data && data.path) || "gateway.yaml",
      raw: (data && data.raw) || "",
    });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

router.get("/api/policy-raw/data", requireAuth, authorize("policy", "update"), async (req, res, next) => {
  try {
    const data = await gateway.get("/admin/policy/raw");
    return res.json({
      path: (data && data.path) || "gateway.yaml",
      raw: (data && data.raw) || "",
    });
  } catch (err) {
    if (err instanceof GatewayError) return apiError(res, err.status || 502, err.message);
    next(err);
  }
});

router.post("/api/policy-raw/diff", requireAuth, authorize("policy", "update"), async (req, res, next) => {
  const parsed = rawSchema.safeParse(req.body);
  if (!parsed.success) return apiError(res, 400, "dati non validi");

  let current;
  try {
    current = await gateway.get("/admin/policy/raw");
  } catch (err) {
    if (err instanceof GatewayError) return apiError(res, err.status || 502, err.message);
    return next(err);
  }

  const previous = (current && current.raw) || "";
  const lines = diffLines(previous, parsed.data.raw).map((part) => ({
    type: part.added ? "+" : part.removed ? "-" : " ",
    text: part.value,
  }));

  return res.json({ previous, lines });
});

router.post("/api/policy-raw/save", requireAuth, authorize("policy", "update"), async (req, res, next) => {
  const parsed = rawSchema.safeParse(req.body);
  if (!parsed.success) return apiError(res, 400, "dati non validi");

  const raw = parsed.data.raw;
  try {
    const result = await gateway.put("/admin/policy/raw", { json: { raw } });
    await auditLog({
      user: req.user,
      op: "policy.raw.save",
      target: "gateway.yaml",
      detail: { bytes: Buffer.byteLength(raw), validated: !!result?.validated, reloaded: !!result?.reloaded },
      routeMethod: req.method,
      gatewayPath: "/admin/policy/raw",
      ip: req.ip,
    });
    return res.json({ ok: true, validated: !!result?.validated, reloaded: !!result?.reloaded });
  } catch (err) {
    if (err instanceof GatewayError) return apiError(res, err.status || 400, err.message);
    next(err);
  }
});

export default router;