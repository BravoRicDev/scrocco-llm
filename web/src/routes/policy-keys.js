import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const flash = (res, path, msg, type) =>
  res.redirect(path + "?flash=" + encodeURIComponent(msg) + "&flashType=" + type);

const mask = (s) => {
  const str = String(s ?? "");
  if (str.length <= 10) return str;
  return str.slice(0, 6) + "...********..." + str.slice(-4);
};

const asObject = (v) =>
  v && typeof v === "object" && !Array.isArray(v) ? v : {};

// Il gateway espone sempre le chiavi mascherate (`*_masked`); se mancanti
// (mock) maschera al volo la controparte in chiaro senza mai renderizzarla.
function maskedMap(masked, plain) {
  const src = asObject(masked);
  const entries = Object.keys(src).length
    ? Object.entries(src)
    : Object.entries(asObject(plain)).map(([k, v]) => [k, mask(v)]);
  return Object.fromEntries(
    entries.filter(([, v]) => v !== null && v !== undefined && String(v).trim() !== "")
  );
}

const postSchema = z.discriminatedUnion("kind", [
  z.object({
    kind: z.literal("aliases"),
    object: z.record(z.string(), z.string()),
  }),
  z.object({
    kind: z.literal("alias_key"),
    alias: z.string().trim().min(1, "alias richiesto"),
    key: z.string().trim().min(1, "key richiesta"),
  }),
  z.object({
    kind: z.literal("client_key"),
    profile: z.string().trim().min(1, "profilo richiesto"),
    key: z.string().trim().min(1, "key richiesta"),
  }),
]);

const deleteSchema = z.object({
  kind: z.enum(["aliases", "alias_key", "client_key"]),
  name: z.string().trim().min(1, "nome richiesto"),
});

router.get("/policy/keys", requireAuth, authorize("keys", "rotate"), async (req, res, next) => {
  try {
    const policy = await gateway.get("/admin/policy");
    const eff = (policy && policy.effective) || {};
    res.render("policy/keys", {
      aliases: asObject(eff.aliases),
      aliasKeysMasked: maskedMap(eff.alias_keys_masked, eff.alias_keys),
      clientKeysMasked: maskedMap(eff.client_keys_masked, eff.client_keys),
    });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

router.post("/policy/keys", requireAuth, authorize("keys", "rotate"), async (req, res, next) => {
  const parsed = postSchema.safeParse(req.body);
  if (!parsed.success) {
    return flash(res, "/policy/keys", "dati non validi", "error");
  }

  const { kind } = parsed.data;
  let patch;
  let target;
  if (kind === "aliases") {
    patch = { aliases: parsed.data.object };
    target = "aliases";
  } else if (kind === "alias_key") {
    patch = { alias_keys: { [parsed.data.alias]: parsed.data.key } };
    target = parsed.data.alias;
  } else {
    patch = { client_keys: { [parsed.data.profile]: parsed.data.key } };
    target = parsed.data.profile;
  }

  try {
    await gateway.patch("/admin/policy", { json: patch });
    await auditLog({
      user: req.user,
      op: "policy.keys.set",
      target,
      detail: { kind, name: target },
      routeMethod: req.method,
      gatewayPath: "/admin/policy",
      ip: req.ip,
    });
    return flash(res, "/policy/keys", "chiavi aggiornate", "success");
  } catch (err) {
    if (err instanceof GatewayError) return flash(res, "/policy/keys", "Gateway: " + err.message, "error");
    next(err);
  }
});

router.post("/policy/keys/delete", requireAuth, authorize("keys", "rotate"), async (req, res, next) => {
  const parsed = deleteSchema.safeParse(req.body);
  if (!parsed.success) {
    return flash(res, "/policy/keys", "dati non validi", "error");
  }

  const { kind, name } = parsed.data;
  const patch =
    kind === "aliases"
      ? { aliases: { [name]: null } }
      : kind === "alias_key"
        ? { alias_keys: { [name]: null } }
        : { client_keys: { [name]: null } };

  try {
    await gateway.patch("/admin/policy", { json: patch });
    await auditLog({
      user: req.user,
      op: "policy.keys.delete",
      target: name,
      detail: { kind, name },
      routeMethod: req.method,
      gatewayPath: "/admin/policy",
      ip: req.ip,
    });
    return flash(res, "/policy/keys", "voce eliminata", "success");
  } catch (err) {
    if (err instanceof GatewayError) return flash(res, "/policy/keys", "Gateway: " + err.message, "error");
    next(err);
  }
});

export default router;
