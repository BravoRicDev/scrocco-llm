import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const createFields = {
  profile: z.string().min(1),
  modello: z.string().min(1),
  endpoint: z.string().min(1),
  data: z.string().min(1),
  key: z.string().min(1),
  context: z.union([z.string().min(1), z.number()]),
};

const updateFields = {
  id: z.string().min(1),
  profile: z.string().min(1).optional(),
  modello: z.string().min(1).optional(),
  endpoint: z.string().min(1).optional(),
  data: z.string().min(1).optional(),
  key: z.string().min(1).optional(),
  context: z.union([z.string().min(1), z.number()]).optional(),
  priority: z.coerce.number().optional(),
};

const deleteFields = {
  id: z.string().min(1),
};

const operationSchema = z
  .discriminatedUnion("action", [
    z.object({ action: z.literal("create"), ...createFields }),
    z.object({ action: z.literal("update"), ...updateFields }),
    z.object({ action: z.literal("delete"), ...deleteFields }),
  ]);

const bulkSchema = z.object({
  operations: z.array(operationSchema).min(1).max(50),
});

router.get("/deployments/bulk", requireAuth, authorize("deployments", "bulk"), async (req, res, next) => {
  try {
    const [data, profilesRes] = await Promise.all([
      gateway.get("/admin/deployments"),
      gateway.get("/admin/profiles"),
    ]);
    const deployments = Array.isArray(data) ? data : (data?.deployments ?? []);
    const profiles = profilesRes?.profiles ?? [];
    res.render("deployments/bulk", { deployments, profiles, errorsResults: null });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

router.post("/deployments/bulk", requireAuth, authorize("deployments", "bulk"), async (req, res, next) => {
  const parsed = bulkSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.redirect("/deployments/bulk?flash=" + encodeURIComponent("dati non validi") + "&flashType=error");
  }

  const operations = parsed.data.operations;
  const safeOps = operations.map((op) => {
    const out = { ...op };
    if (out.key !== undefined) out.key = undefined;
    return out;
  });

  try {
    const result = await gateway.post("/admin/deployments/bulk", { json: { operations } });
    await auditLog({ user: req.user, op: "deployments.bulk", target: "bulk",
      detail: { count: operations.length, applied: result?.applied ?? operations.length, operations: safeOps },
      routeMethod: req.method, gatewayPath: "/admin/deployments/bulk", ip: req.ip });
    const count = result?.applied ?? operations.length;
    return res.redirect("/deployments?flash=" + encodeURIComponent("Bulk eseguito: " + count + " operazioni applicate") + "&flashType=success");
  } catch (err) {
    if (err instanceof GatewayError) {
      if (err.status === 400) {
        const [data, profilesRes] = await Promise.all([
          gateway.get("/admin/deployments"),
          gateway.get("/admin/profiles"),
        ]);
        const deployments = Array.isArray(data) ? data : (data?.deployments ?? []);
        const profiles = profilesRes?.profiles ?? [];
        const errorsResults = Array.isArray(err.results) ? err.results : [];
        return res.render("deployments/bulk", { deployments, profiles, errorsResults, message: err.message });
      }
      return res.redirect("/deployments/bulk?flash=" + encodeURIComponent("Gateway: " + err.message) + "&flashType=error");
    }
    next(err);
  }
});

export default router;
