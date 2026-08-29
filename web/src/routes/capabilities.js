import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const seedSchema = z.object({
  dry_run: z.coerce.boolean().optional().default(true),
});

router.get("/capabilities", requireAuth, authorize("capabilities", "read"), async (req, res, next) => {
  try {
    const [state, profilesResp] = await Promise.all([
      gateway.get("/admin/state"),
      gateway.get("/admin/profiles"),
    ]);
    const profiles = profilesResp && Array.isArray(profilesResp.profiles) ? profilesResp.profiles : [];
    const currentProfile = req.query.profile || (profiles[0]?.name) || null;
    res.render("capabilities/index", { state, profiles, currentProfile });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

router.post("/capabilities/seed", requireAuth, authorize("capabilities", "seed"), async (req, res, next) => {
  const parsed = seedSchema.safeParse(req.body);
  if (!parsed.success) return res.redirect("/capabilities?flash=" + encodeURIComponent("dati non validi") + "&flashType=error");

  try {
    const result = await gateway.post("/admin/capabilities/seed-from-map", { json: { dry_run: parsed.data.dry_run } });

    if (parsed.data.dry_run) {
      const [state, profilesResp] = await Promise.all([
        gateway.get("/admin/state"),
        gateway.get("/admin/profiles"),
      ]);
      const profiles = profilesResp && Array.isArray(profilesResp.profiles) ? profilesResp.profiles : [];
      const currentProfile = req.query.profile || (profiles[0]?.name) || null;
      const proposals = result && Array.isArray(result.proposals) ? result.proposals : [];
      await auditLog({ user: req.user, op: "capabilities.seed", target: "dry-run",
        detail: { dry_run: true, count: proposals.length, total: result?.total ?? 0 },
        routeMethod: req.method, gatewayPath: "/admin/capabilities/seed-from-map", ip: req.ip });
      return res.render("capabilities/index", { state, profiles, currentProfile, seedProposals: proposals, seed: result });
    }

    await auditLog({ user: req.user, op: "capabilities.seed", target: "map",
      detail: { dry_run: false, applied: result?.applied ?? 0, skipped: result?.skipped ?? 0, errors: result?.errors?.length ?? 0 },
      routeMethod: req.method, gatewayPath: "/admin/capabilities/seed-from-map", ip: req.ip });
    return res.redirect("/capabilities?flash=" + encodeURIComponent(String(result?.applied ?? 0) + " deployment aggiornati, " + String(result?.skipped ?? 0) + " saltati") + "&flashType=success");
  } catch (err) {
    if (err instanceof GatewayError) return res.redirect("/capabilities?flash=" + encodeURIComponent("Gateway: " + err.message) + "&flashType=error");
    next(err);
  }
});

router.get("/capabilities/audit", requireAuth, authorize("capabilities", "audit"), async (req, res, next) => {
  try {
    const result = await gateway.post("/admin/capabilities/audit", { json: {} });
    return res.render("capabilities/audit", { audit: result });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

export default router;