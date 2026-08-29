import { Router } from "express";
import { z } from "zod";
import { query } from "../db.js";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import { hashPassword } from "../services/password.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const ROLES = ["admin", "operator", "viewer"];
const STATUSES = ["active", "disabled"];

function flashRedirect(path, msg, type) {
  return `${path}?flash=${encodeURIComponent(msg)}&flashType=${type}`;
}

const createSchema = z.object({
  email: z.string().email(),
  name: z.string().optional().default(""),
  password: z.string().min(8),
  role: z.enum(ROLES),
});

const roleSchema = z.object({
  role: z.enum(ROLES),
});

const passwordSchema = z.object({
  password: z.string().min(8),
});

router.get("/users", requireAuth, authorize("users", "list"), async (req, res, next) => {
  try {
    const { rows } = await query(
      "SELECT id, email, name, role, status, created_at FROM users ORDER BY email"
    );
    res.render("users/index", {
      users: rows,
      currentUserId: req.user.sub,
      ROLES,
      STATUSES,
    });
  } catch (err) {
    next(err);
  }
});

router.get("/users/new", requireAuth, authorize("users", "create"), (req, res) => {
  res.render("users/form", { user: null, userForm: {}, ROLES });
});

router.post("/users", requireAuth, authorize("users", "create"), async (req, res, next) => {
  const parsed = createSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.redirect(
      flashRedirect("/users/new", "dati non validi: email, password (min 8) e role obbligatori", "error")
    );
  }
  try {
    const email = String(parsed.data.email).trim().toLowerCase();
    const passwordHash = await hashPassword(parsed.data.password);
    const inserted = await query(
      "INSERT INTO users (email, name, password_hash, role, status) VALUES ($1, $2, $3, $4, 'active') RETURNING id",
      [email, parsed.data.name, passwordHash, parsed.data.role]
    );
    await auditLog({
      user: req.user,
      op: "users.create",
      target: inserted.rows[0].id,
      detail: { email, name: parsed.data.name, role: parsed.data.role, password: undefined },
      routeMethod: req.method,
      gatewayPath: null,
      ip: req.ip,
    });
    return res.redirect(flashRedirect("/users", "utente creato", "success"));
  } catch (err) {
    if (err?.code === "23505") {
      return res.redirect(flashRedirect("/users", "email già esistente", "error"));
    }
    if (err instanceof Error && err.message.includes("password troppo corta")) {
      return res.redirect(flashRedirect("/users", err.message, "error"));
    }
    next(err);
  }
});

router.post("/users/:id/toggle", requireAuth, authorize("users", "update"), async (req, res, next) => {
  const id = parseInt(req.params.id, 10);
  if (id === req.user.sub) {
    return res.status(403).render("error", { message: "Non puoi disabilitare il tuo account" });
  }
  try {
    const { rows } = await query("SELECT status FROM users WHERE id = $1", [id]);
    if (rows.length === 0) {
      return res.redirect(flashRedirect("/users", "utente non trovato", "error"));
    }
    const nextStatus = rows[0].status === "active" ? "disabled" : "active";
    await query("UPDATE users SET status = $1, updated_at = NOW() WHERE id = $2", [nextStatus, id]);
    await auditLog({
      user: req.user,
      op: "users.update",
      target: id,
      detail: { status: nextStatus },
      routeMethod: req.method,
      gatewayPath: null,
      ip: req.ip,
    });
    return res.redirect(flashRedirect("/users", "stato aggiornato", "success"));
  } catch (err) {
    next(err);
  }
});

router.post("/users/:id/role", requireAuth, authorize("users", "update"), async (req, res, next) => {
  const id = parseInt(req.params.id, 10);
  if (id === req.user.sub) {
    return res.status(403).render("error", { message: "Non puoi cambiare il tuo ruolo" });
  }
  const parsed = roleSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.redirect(flashRedirect("/users", "ruolo non valido", "error"));
  }
  try {
    await query("UPDATE users SET role = $1, updated_at = NOW() WHERE id = $2", [parsed.data.role, id]);
    await auditLog({
      user: req.user,
      op: "users.update",
      target: id,
      detail: { role: parsed.data.role },
      routeMethod: req.method,
      gatewayPath: null,
      ip: req.ip,
    });
    return res.redirect(flashRedirect("/users", "ruolo aggiornato", "success"));
  } catch (err) {
    next(err);
  }
});

router.post("/users/:id/password", requireAuth, authorize("users", "update"), async (req, res, next) => {
  const id = parseInt(req.params.id, 10);
  const parsed = passwordSchema.safeParse(req.body);
  if (!parsed.success) {
    return res.redirect(flashRedirect("/users", "password non valida (min 8)", "error"));
  }
  try {
    const passwordHash = await hashPassword(parsed.data.password);
    await query(
      "UPDATE users SET password_hash = $1, token_version = token_version + 1, updated_at = NOW() WHERE id = $2",
      [passwordHash, id]
    );
    await auditLog({
      user: req.user,
      op: "users.update",
      target: id,
      detail: { resetPassword: true, password: undefined },
      routeMethod: req.method,
      gatewayPath: null,
      ip: req.ip,
    });
    return res.redirect(flashRedirect("/users", "password reimpostata", "success"));
  } catch (err) {
    if (err instanceof Error && err.message.includes("password troppo corta")) {
      return res.redirect(flashRedirect("/users", err.message, "error"));
    }
    next(err);
  }
});

export default router;
