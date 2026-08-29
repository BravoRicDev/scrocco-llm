import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

router.get("/profiles", requireAuth, authorize("profiles", "read"), async (req, res, next) => {
  try {
    const data = await gateway.get("/admin/profiles");
    const profiles = Array.isArray(data) ? data : (data?.profiles ?? []);
    res.render("profiles/index", { profiles, count: profiles.length });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

router.get("/profiles/new", requireAuth, authorize("profiles", "create"), (req, res) => {
  res.render("profiles/new", { profile: req.query.profile || "" });
});

const createSchema = z.object({
  name: z.string().trim().min(1),
});

router.post("/profiles", requireAuth, authorize("profiles", "create"), async (req, res, next) => {
  const parsed = createSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.redirect("/profiles/new?flash=" + encodeURIComponent("nome profilo non valido") + "&flashType=error");
  }
  const name = parsed.data.name;
  try {
    await gateway.post("/admin/deployments", {
      json: { profile: name, modello: "", endpoint: "", data: "", key: "", context: 0 },
    });
    await auditLog({
      user: req.user,
      op: "profile_create",
      target: name,
      detail: { profile: name, key: undefined },
      routeMethod: req.method,
      gatewayPath: "/admin/deployments",
      ip: req.ip,
    });
    return res.redirect("/deployments?profile=" + encodeURIComponent(name) + "&flash=" + encodeURIComponent("profilo creato") + "&flashType=success");
  } catch (err) {
    if (err instanceof GatewayError && err.status === 400) {
      await auditLog({
        user: req.user,
        op: "profile_create",
        target: name,
        detail: { profile: name, endpoint_mancante: true, key: undefined },
        routeMethod: req.method,
        gatewayPath: "/admin/deployments",
        ip: req.ip,
      });
      return res.redirect("/deployments/new?profile=" + encodeURIComponent(name) + "&flash=" + encodeURIComponent("profilo creato: definisci il primo deployment") + "&flashType=success");
    }
    if (err instanceof GatewayError) {
      return res.redirect("/profiles/new?flash=" + encodeURIComponent("Gateway: " + err.message) + "&flashType=error");
    }
    next(err);
  }
});

router.post("/profiles/:name/purge", requireAuth, authorize("profiles", "purge"), async (req, res, next) => {
  const name = String(req.params.name);
  try {
    await gateway.post("/admin/profiles/purge", { json: { profile: name } });
    await auditLog({
      user: req.user,
      op: "profile_purge",
      target: name,
      detail: { profile: name, key: undefined },
      routeMethod: req.method,
      gatewayPath: "/admin/profiles/purge",
      ip: req.ip,
    });
    return res.redirect("/profiles?flash=" + encodeURIComponent("profilo purgato") + "&flashType=success");
  } catch (err) {
    if (err instanceof GatewayError) {
      return res.redirect("/profiles?flash=" + encodeURIComponent("Gateway: " + err.message) + "&flashType=error");
    }
    next(err);
  }
});

export default router;
