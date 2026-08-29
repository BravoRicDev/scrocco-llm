import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import { query } from "../db.js";
import { auditLog } from "../services/audit.js";
import { createPoller } from "../services/alert-poller.js";

const router = Router();
const poller = createPoller();

const createSchema = z.object({
  name: z.string().trim().min(1).max(120),
  pool_filter: z.string().trim().max(255).optional().default(""),
  health_threshold_pct: z.coerce.number().int().min(0).max(100).default(50),
  check_every_sec: z.coerce.number().int().min(5).max(86400).default(120),
  webhook_url: z.string().trim().max(500).optional().default(""),
  telegram_chat_id: z.string().trim().max(64).optional().default(""),
  notify_min_interval_sec: z.coerce.number().int().min(0).max(86400).default(900),
});

function flashRedirect(res, msg, type) {
  return res.redirect("/alerts?flash=" + encodeURIComponent(msg) + "&flashType=" + type);
}

router.get("/alerts", requireAuth, authorize("alerts", "read"), async (req, res, next) => {
  try {
    const { rows } = await query("SELECT * FROM alert_rules ORDER BY id ASC");
    res.render("alerts/index", { rules: rows, count: rows.length, poller: poller.status() });
  } catch (err) {
    next(err);
  }
});

router.post("/alerts", requireAuth, authorize("alerts", "create"), async (req, res, next) => {
  const parsed = createSchema.safeParse(req.body);
  if (!parsed.success) return flashRedirect(res, "dati non validi", "error");
  const d = parsed.data;
  try {
    const { rows } = await query(
      `INSERT INTO alert_rules
         (name, pool_filter, health_threshold_pct, check_every_sec, webhook_url,
          telegram_chat_id, notify_min_interval_sec, created_by)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
       RETURNING id`,
      [d.name, d.pool_filter || null, d.health_threshold_pct, d.check_every_sec,
       d.webhook_url || null, d.telegram_chat_id || null, d.notify_min_interval_sec,
       (req.user && (req.user.sub ?? req.user.id)) ?? null]
    );
    await auditLog({
      user: req.user,
      op: "alerts.create",
      target: rows[0].id,
      detail: { name: d.name, pool_filter: d.pool_filter || null, health_threshold_pct: d.health_threshold_pct },
      routeMethod: req.method,
      ip: req.ip,
    });
    return flashRedirect(res, "regola alert creata", "success");
  } catch (err) {
    next(err);
  }
});

router.post("/alerts/:id/toggle", requireAuth, authorize("alerts", "update"), async (req, res, next) => {
  const id = parseInt(req.params.id, 10);
  if (!Number.isInteger(id)) return flashRedirect(res, "id non valido", "error");
  try {
    const { rows } = await query(
      "UPDATE alert_rules SET enabled = NOT enabled WHERE id = $1 RETURNING id, enabled",
      [id]
    );
    if (!rows.length) return flashRedirect(res, "regola non trovata", "error");
    await auditLog({
      user: req.user,
      op: "alerts.update",
      target: id,
      detail: { enabled: rows[0].enabled },
      routeMethod: req.method,
      ip: req.ip,
    });
    return flashRedirect(res, rows[0].enabled ? "regola attivata" : "regola disattivata", "success");
  } catch (err) {
    next(err);
  }
});

router.post("/alerts/:id/delete", requireAuth, authorize("alerts", "delete"), async (req, res, next) => {
  const id = parseInt(req.params.id, 10);
  if (!Number.isInteger(id)) return flashRedirect(res, "id non valido", "error");
  try {
    const { rows } = await query("DELETE FROM alert_rules WHERE id = $1 RETURNING id, name", [id]);
    if (!rows.length) return flashRedirect(res, "regola non trovata", "error");
    await auditLog({
      user: req.user,
      op: "alerts.delete",
      target: id,
      detail: { name: rows[0].name },
      routeMethod: req.method,
      ip: req.ip,
    });
    return flashRedirect(res, "regola eliminata", "success");
  } catch (err) {
    next(err);
  }
});

export default router;
