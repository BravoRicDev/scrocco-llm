import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const singleSchema = z.object({
  force: z.coerce.boolean().optional().default(false),
});

const bulkSchema = z.object({
  filter: z.string().trim().min(1).default("all"),
  force: z.coerce.boolean().optional().default(false),
});

async function loadData() {
  const [deploymentsRes, profilesRes] = await Promise.all([
    gateway.get("/admin/deployments"),
    gateway.get("/admin/profiles"),
  ]);
  const deployments = Array.isArray(deploymentsRes) ? deploymentsRes : (deploymentsRes?.deployments ?? []);
  const profiles = profilesRes && Array.isArray(profilesRes.profiles) ? profilesRes.profiles : [];
  return { deployments, profiles };
}

router.get("/probe", requireAuth, authorize("deployments", "probe"), async (req, res, next) => {
  try {
    const { deployments, profiles } = await loadData();
    res.render("probe/index", {
      deployments,
      profiles,
      probeResult: null,
      results: null,
      filter: "all",
      force: false,
    });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

router.post("/probe/bulk", requireAuth, authorize("deployments", "probe"), async (req, res, next) => {
  const parsed = bulkSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.redirect("/probe?flash=" + encodeURIComponent("dati non validi") + "&flashType=error");
  }

  try {
    const payload = { filter: parsed.data.filter };
    if (parsed.data.force) payload.force = true;
    const result = await gateway.post("/admin/deployments/probe/bulk", { json: payload });
    const results = Array.isArray(result?.results) ? result.results : [];
    await auditLog({
      user: req.user,
      op: "deployments.probe",
      target: "bulk",
      detail: { filter: parsed.data.filter, force: parsed.data.force, count: results.length },
      routeMethod: req.method,
      gatewayPath: "/admin/deployments/probe/bulk",
      ip: req.ip,
    });
    const { deployments, profiles } = await loadData();
    res.render("probe/index", {
      deployments,
      profiles,
      probeResult: null,
      results,
      filter: parsed.data.filter,
      force: parsed.data.force,
    });
  } catch (err) {
    if (err instanceof GatewayError) {
      return res.redirect("/probe?flash=" + encodeURIComponent("Gateway: " + err.message) + "&flashType=error");
    }
    next(err);
  }
});

router.post("/probe/:id", requireAuth, authorize("deployments", "probe"), async (req, res, next) => {
  const parsed = singleSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.redirect("/probe?flash=" + encodeURIComponent("dati non validi") + "&flashType=error");
  }

  try {
    const result = await gateway.post("/admin/deployments/probe", {
      json: { id: req.params.id, force: parsed.data.force },
    });
    await auditLog({
      user: req.user,
      op: "deployments.probe",
      target: result?.unique ?? req.params.id,
      detail: { force: parsed.data.force },
      routeMethod: req.method,
      gatewayPath: "/admin/deployments/probe",
      ip: req.ip,
    });
    const { deployments, profiles } = await loadData();
    res.render("probe/index", {
      deployments,
      profiles,
      probeResult: result,
      results: null,
      filter: "all",
      force: parsed.data.force,
    });
  } catch (err) {
    if (err instanceof GatewayError) {
      return res.redirect("/probe?flash=" + encodeURIComponent("Gateway: " + err.message) + "&flashType=error");
    }
    next(err);
  }
});

export default router;
