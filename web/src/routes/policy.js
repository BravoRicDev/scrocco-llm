import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";
import { castValue, META, LISTS, MAPS } from "../services/policy-cast.js";

const router = Router();

// Manopole che il riassunto compatto `effective` puo' non esporre (nuove):
// le rendiamo comunque editabili in /policy/edit, prefillando SOLO queste
// chiavi dal `configured` (nessun segreto).
const EXTRA_FIELDS = [
  "stream_slow_race_after_ms",
  "nonstream_slow_race_after_ms",
  "stream_slow_race_canaries",
];

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
    const conf = (policy && policy.configured) || {};
    const extra = {};
    EXTRA_FIELDS.forEach((f) => { extra[f] = conf[f]; });
    res.render("policy/edit", { policy, extra,
      meta: { scalar: META.scalar, list: META.list, map: META.map } });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

const fieldSchema = z.object({
  field: z.string().min(1),
  value: z.union([z.string(), z.number(), z.boolean()]).nullable().optional(),
});

// ATTENZIONE: il merge lato gateway e' SHALLOW a livello di BLOCCO
// (`_apply_policy_patch`: `merged[k] = v`), quindi inviare solo la foglia
// CANCELLA gli altri campi dello stesso blocco (es. `adaptive.learn`
// avrebbe azzerato il resto di `adaptive`). Qui si ricostruisce il blocco
// COMPLETO partendo dal documento del gateway (configured, o effective
// come ripiego) e si sostituisce solo la foglia richiesta.
function buildPatch(policy, field, value) {
  const parts = field.split(".");
  if (parts.length === 1) return { [parts[0]]: value };
  const conf = (policy && policy.configured) || {};
  const eff = (policy && policy.effective) || {};
  const base = conf[parts[0]] ?? eff[parts[0]];
  const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
  const block = isObj(base) ? JSON.parse(JSON.stringify(base)) : {};
  let node = block;
  for (let i = 1; i < parts.length - 1; i++) {
    const nxt = node[parts[i]];
    node[parts[i]] = isObj(nxt) ? { ...nxt } : {};
    node = node[parts[i]];
  }
  node[parts[parts.length - 1]] = value;
  return { [parts[0]]: block };
}

function resolveValue(policy, field) {
  let cur = policy;
  for (const seg of field.split(".")) {
    if (cur === null || cur === undefined) return { found: false, value: undefined };
    cur = cur[seg];
  }
  return { found: cur !== undefined, value: cur };
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

  const patch = buildPatch(policy, field, cast.value);

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
