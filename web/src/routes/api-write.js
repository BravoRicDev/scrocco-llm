import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const fail = (res, err) => {
  if (err instanceof GatewayError) {
    const s = err.status && err.status > 0 ? err.status : 502;
    return res.status(s === 502 ? 503 : s).json({ error: { message: err.message } });
  }
  return res.status(500).json({ error: { message: err.message || "errore interno" } });
};

// wrapper: valida -> chiama gateway -> audit -> risposta
function writeRoute({ method, path, resource, action, gatewayPath, schema, verb, ok = 200, sanitize }) {
  return [
    requireAuth,
    authorize(resource, action),
    async (req, res) => {
      let body = req.body || {};
      if (schema) {
        const parsed = schema.safeParse(body);
        if (!parsed.success) return res.status(400).json({ error: { message: "dati non validi" } });
        body = parsed.data;
      }
      const gp = typeof gatewayPath === "function" ? gatewayPath(req) : gatewayPath;
      try {
        const opts = verb === "del" ? undefined : { json: body };
        const result = await gateway[verb](gp, opts);
        await auditLog({
          user: req.user,
          op: `api.${resource}.${action}`,
          target: req.params.id || body.unique || body.session_id || null,
          detail: sanitize ? sanitize(body, result) : { keys: Object.keys(body) },
          routeMethod: req.method,
          gatewayPath: gp,
          ip: req.ip,
        });
        return res.status(ok).json(result && typeof result === "object" ? { ok: true, ...result } : { ok: true });
      } catch (err) { return fail(res, err); }
    },
  ];
}

// --- deployments ---
router.post("/api/v1/deployments", ...writeRoute({
  resource: "deployments", action: "create", gatewayPath: "/admin/deployments", verb: "post", ok: 201,
  schema: z.object({ profile: z.string().min(1), modello: z.string().min(1), provider: z.string().optional(),
    endpoint: z.string().min(1), data: z.string().min(1), key: z.string().min(1),
    context: z.coerce.number().int().min(0), priority: z.coerce.number().int().optional(), caps: z.string().optional() }),
  sanitize: (b, r) => ({ profile: b.profile, modello: b.modello, id: r?.id ?? null }),
}));

router.put("/api/v1/deployments/:id", ...writeRoute({
  resource: "deployments", action: "update", verb: "put",
  gatewayPath: (req) => `/admin/deployments/${encodeURIComponent(req.params.id)}`,
  schema: z.object({ profile: z.string().optional(), modello: z.string().optional(), provider: z.string().optional(),
    endpoint: z.string().optional(), data: z.string().optional(), key: z.string().optional(),
    context: z.coerce.number().int().min(0).optional(), priority: z.coerce.number().int().optional(), caps: z.string().optional() }),
  sanitize: (b) => ({ fields: Object.keys(b).filter((k) => k !== "key") }),
}));

router.delete("/api/v1/deployments/:id", ...writeRoute({
  resource: "deployments", action: "delete", verb: "del",
  gatewayPath: (req) => `/admin/deployments/${encodeURIComponent(req.params.id)}`,
  sanitize: () => ({}),
}));

router.post("/api/v1/deployments/bulk", ...writeRoute({
  resource: "deployments", action: "bulk", gatewayPath: "/admin/deployments/bulk", verb: "post",
  schema: z.object({ operations: z.array(z.object({ action: z.enum(["create", "update", "delete"]) }).passthrough()).min(1).max(50) }),
  sanitize: (b) => ({ ops: b.operations.length }),
}));

router.post("/api/v1/deployments/probe", ...writeRoute({
  resource: "deployments", action: "probe", gatewayPath: "/admin/deployments/probe", verb: "post",
  schema: z.object({ id: z.string().optional(), unique: z.string().optional(), force: z.boolean().optional() }),
}));

router.post("/api/v1/deployments/probe/bulk", ...writeRoute({
  resource: "deployments", action: "probe", gatewayPath: "/admin/deployments/probe/bulk", verb: "post",
  schema: z.object({ filter: z.string().optional(), force: z.boolean().optional() }),
}));

router.post("/api/v1/deployments/unretire", ...writeRoute({
  resource: "deployments", action: "unretire", gatewayPath: "/admin/deployments/unretire", verb: "post",
  schema: z.object({ unique: z.string().min(1) }),
}));

// --- system ---
router.post("/api/v1/system/cooldowns/clear", ...writeRoute({
  resource: "system", action: "cooldowns", gatewayPath: "/admin/cooldowns/clear", verb: "post",
  schema: z.object({ unique: z.string().optional() }),
}));

router.post("/api/v1/system/sessions/release", ...writeRoute({
  resource: "system", action: "sessions", gatewayPath: "/admin/sessions/release", verb: "post",
  schema: z.object({ session_id: z.string().optional() }),
}));

router.post("/api/v1/system/reload", ...writeRoute({
  resource: "system", action: "reload", gatewayPath: "/admin/reload", verb: "post",
}));

// --- capabilities ---
router.post("/api/v1/capabilities/seed", ...writeRoute({
  resource: "capabilities", action: "seed", gatewayPath: "/admin/capabilities/seed-from-map", verb: "post",
  schema: z.object({ dry_run: z.boolean().optional() }),
}));

router.post("/api/v1/capabilities/audit", ...writeRoute({
  resource: "capabilities", action: "audit", gatewayPath: "/admin/capabilities/audit", verb: "post",
}));

// --- policy (SOLO admin) ---
router.patch("/api/v1/policy", ...writeRoute({
  resource: "policy", action: "update", gatewayPath: "/admin/policy", verb: "patch",
  schema: z.object({}).passthrough(),
  sanitize: (b) => ({ fields: Object.keys(b) }),
}));

export default router;
