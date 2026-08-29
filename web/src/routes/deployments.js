import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const nonEmpty = (msg) =>
  z.string().trim().min(1, msg);

const createSchema = z.object({
  profile: nonEmpty("profilo richiesto"),
  commento: z.string().trim().optional(),
  modello: nonEmpty("modello richiesto"),
  provider: z.string().trim().optional(),
  endpoint: nonEmpty("endpoint richiesto"),
  data: nonEmpty("data (categoria) richiesta"),
  key: nonEmpty("key richiesta"),
  context: z.coerce.number().int().nonnegative().optional(),
  priority: z.coerce.number().int().optional(),
  caps: z.string().trim().optional(),
});

const updateSchema = createSchema.partial().extend({
  key: z.string().trim().optional(),
});

function sanitize(body) {
  const clone = { ...body };
  delete clone.key;
  return clone;
}

function toCaps(raw) {
  if (!raw || !String(raw).trim()) return undefined;
  return String(raw)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

async function loadFormData(req, res, next) {
  try {
    const [listRes, profilesRes] = await Promise.all([
      gateway.get("/admin/deployments"),
      gateway.get("/admin/profiles"),
    ]);
    const all = Array.isArray(listRes) ? listRes : (listRes?.deployments ?? []);
    const deployment = req.params && req.params.id
      ? all.find((d) => d.id === req.params.id || String(d.id).endsWith(":" + req.params.id)) ?? null
      : null;
    res.render("deployments/form", {
      deployment,
      profiles: profilesRes?.profiles ?? [],
      currentPath: req.path,
    });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
}

const flash = (res, path, msg, type) =>
  res.redirect(path + "?flash=" + encodeURIComponent(msg) + "&flashType=" + type);

router.get("/deployments", requireAuth, authorize("deployments", "read"), async (req, res, next) => {
  try {
    const qRaw = String(req.query.q ?? "");
    const profile = req.query.profile ? String(req.query.profile) : "";
    const params = profile ? { profile } : undefined;

    const [data, profilesRes] = await Promise.all([
      gateway.get("/admin/deployments", params ? { params } : {}),
      gateway.get("/admin/profiles"),
    ]);

    let deployments = Array.isArray(data) ? data : (data?.deployments ?? []);
    const term = qRaw.trim().toLowerCase();
    if (term) {
      deployments = deployments.filter((d) =>
        [d.modello, d.provider, d.endpoint, d.group, d.id]
          .some((v) => String(v ?? "").toLowerCase().includes(term))
      );
    }

    res.render("deployments/list", {
      deployments,
      profiles: profilesRes?.profiles ?? [],
      count: deployments.length,
      q: qRaw,
      currentProfile: profile,
      currentPath: req.path,
    });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

router.get("/deployments/new", requireAuth, authorize("deployments", "create"), loadFormData);

router.post("/deployments", requireAuth, authorize("deployments", "create"), async (req, res, next) => {
  const parsed = createSchema.safeParse(req.body);
  if (!parsed.success) {
    return flash(res, "/deployments/new", "dati non validi: " + parsed.error.issues.map((i) => i.message).join(", "), "error");
  }
  const data = { ...parsed.data };
  if (data.caps) data.caps = toCaps(data.caps);
  try {
    const created = await gateway.post("/admin/deployments", { json: data });
    await auditLog({
      user: req.user,
      op: "deployment.create",
      target: created?.id ?? null,
      detail: { ...sanitize(data), ...(created ? { id: created.id } : {}) },
      routeMethod: req.method,
      gatewayPath: "/admin/deployments",
      ip: req.ip,
    });
    return flash(res, "/deployments", "deployment creato", "success");
  } catch (err) {
    if (err instanceof GatewayError) return flash(res, "/deployments/new", "Gateway: " + err.message, "error");
    next(err);
  }
});

router.get("/deployments/:id/edit", requireAuth, authorize("deployments", "update"), loadFormData);

router.put("/deployments/:id", requireAuth, authorize("deployments", "update"), async (req, res, next) => {
  const { id } = req.params;
  const parsed = updateSchema.safeParse(req.body);
  if (!parsed.success) {
    return flash(res, `/deployments/${id}/edit`, "dati non validi", "error");
  }
  const data = { ...parsed.data };
  if (data.caps) data.caps = toCaps(data.caps);
  try {
    const updated = await gateway.put(`/admin/deployments/${id}`, { json: data });
    const detail = sanitize(data);
    if (data.key) detail.key_rotated = true;
    await auditLog({
      user: req.user,
      op: "deployment.update",
      target: updated?.id ?? id,
      detail,
      routeMethod: "PUT",
      gatewayPath: `/admin/deployments/${id}`,
      ip: req.ip,
    });
    return flash(res, "/deployments", "deployment aggiornato", "success");
  } catch (err) {
    if (err instanceof GatewayError) return flash(res, `/deployments/${id}/edit`, "Gateway: " + err.message, "error");
    next(err);
  }
});

router.post("/deployments/:id/delete", requireAuth, authorize("deployments", "delete"), async (req, res, next) => {
  const { id } = req.params;
  try {
    const del = await gateway.del(`/admin/deployments/${id}`);
    await auditLog({
      user: req.user,
      op: "deployment.delete",
      target: del?.id ?? id,
      detail: { id },
      routeMethod: "POST",
      gatewayPath: `/admin/deployments/${id}`,
      ip: req.ip,
    });
    return flash(res, "/deployments", "deployment eliminato", "success");
  } catch (err) {
    if (err instanceof GatewayError) return flash(res, "/deployments", "Gateway: " + err.message, "error");
    next(err);
  }
});

export default router;
