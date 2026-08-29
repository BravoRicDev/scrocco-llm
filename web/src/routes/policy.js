import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

// Mappa metadati: -> tipo di cast per il campo (scalari, liste, mappe).
const META = {
  scalar: {
    "server.host": "str",
    "server.port": "int",
    "server.ssl_enabled": "bool",
    "server.max_body": "num",
    "timeouts.connect": "int",
    "timeouts.read": "int",
    "timeouts.write": "int",
    "rate_limit.rpm": "int",
    "rate_limit.tpm": "int",
    "log.level": "str",
    "log.enabled": "bool",
    "debug": "bool",
    "adaptive.enabled": "bool",
    "adaptive.learn": "bool",
    "adaptive.min_samples": "int",
    "adaptive.confidence": "num",
    "qc.enabled": "bool",
    "qc.dedup_window": "int",
    "routing.stickiness": "bool",
    "routing.prefer": "str",
    "routing.fallback": "str",
  },
  list: {
    "routing.allowed_providers": "list",
    "routing.models": "list",
    "capability_routing.models": "list",
    "capability_routing.dedicated_models": "list",
    "server.allowed_origins": "list",
    "health.bind_hosts": "list",
  },
  map: {
    "capability_routing.model_capabilities": "map",
  },
};

const LISTS = META.list;
const MAPS = META.map;

router.get("/policy", requireAuth, authorize("policy", "read"), async (req, res, next) => {
  try {
    const policy = await gateway.get("/admin/policy");
    res.render("policy/index", { policy });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

router.get("/policy/edit", requireAuth, authorize("policy", "update"), async (req, res, next) => {
  try {
    const policy = await gateway.get("/admin/policy");
    res.render("policy/edit", { policy, meta: { scalar: META.scalar, list: META.list, map: META.map } });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

const fieldSchema = z.object({
  field: z.string().min(1),
  value: z.union([z.string(), z.number(), z.boolean()]).nullable().optional(),
});

function buildNested(segments, value) {
  const root = {};
  let cur = root;
  segments.forEach((seg, i) => {
    if (i === segments.length - 1) {
      cur[seg] = value;
    } else {
      cur[seg] = {};
      cur = cur[seg];
    }
  });
  return root;
}

function resolveValue(policy, field) {
  let cur = policy;
  for (const seg of field.split(".")) {
    if (cur === null || cur === undefined) return { found: false, value: undefined };
    cur = cur[seg];
  }
  return { found: cur !== undefined, value: cur };
}

function castValue(field, raw) {
  const type =
    MAPS[field] ?? LISTS[field] ?? META.scalar[field] ?? null;

  if (type === "bool") return { ok: true, value: raw === true || raw === "true" || raw === "1" || raw === 1 };
  if (type === "int") {
    const n = Number(raw);
    if (!Number.isFinite(n)) return { ok: false, msg: "valore numerico atteso" };
    return { ok: true, value: Math.trunc(n) };
  }
  if (type === "num") {
    const n = Number(raw);
    if (!Number.isFinite(n)) return { ok: false, msg: "valore numerico atteso" };
    return { ok: true, value: n };
  }
  if (type === "list") {
    if (Array.isArray(raw)) return { ok: true, value: raw };
    const par = String(raw ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    return { ok: true, value: par };
  }
  if (type === "map") {
    let obj;
    if (typeof raw === "string") {
      try {
        obj = JSON.parse(raw);
      } catch {
        return { ok: false, msg: "JSON della mappa non valido" };
      }
    } else {
      obj = raw;
    }
    if (!obj || typeof obj !== "object" || Array.isArray(obj)) {
      return { ok: false, msg: "mappa (oggetto) attesa" };
    }
    return { ok: true, value: obj };
  }

  // fallback: scala come stringa
  return { ok: true, value: String(raw ?? "") };
}

router.post("/policy/field", requireAuth, authorize("policy", "update"), async (req, res, next) => {
  const parsed = fieldSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.redirect("/policy/edit?flash=" + encodeURIComponent("dati non validi") + "&flashType=error");
  }
  const { field, value } = parsed.data;

  let policy;
  try {
    policy = await gateway.get("/admin/policy");
  } catch (err) {
    if (err instanceof GatewayError) return res.redirect("/policy/edit?flash=" + encodeURIComponent("Gateway: " + err.message) + "&flashType=error");
    return next(err);
  }

  const effective = (policy && policy.effective) || {};
  const { found, value: cur } = resolveValue(effective, field);
  const type = MAPS[field] ?? LISTS[field] ?? META.scalar[field] ?? (typeof cur === "boolean" ? "bool" : typeof cur === "number" ? (Number.isInteger(cur) ? "int" : "num") : Array.isArray(cur) ? "list" : "str");

  const cast = castValue(field, value);
  if (!cast.ok) {
    return res.redirect("/policy/edit?flash=" + encodeURIComponent(cast.msg ?? "valore non valido") + "&flashType=error");
  }

  const patch = buildNested(field.split("."), cast.value);

  try {
    await gateway.patch("/admin/policy", { json: patch });
    await auditLog({
      user: req.user,
      op: "policy.update",
      target: "policy",
      detail: { field, type, newData: { field } },
      routeMethod: req.method,
      gatewayPath: "/admin/policy",
      ip: req.ip,
    });
    return res.redirect("/policy/edit?flash=" + encodeURIComponent("Campo \"" + field + "\" aggiornato") + "&flashType=success");
  } catch (err) {
    if (err instanceof GatewayError) return res.redirect("/policy/edit?flash=" + encodeURIComponent("Gateway: " + err.message) + "&flashType=error");
    return next(err);
  }
});

export default router;
