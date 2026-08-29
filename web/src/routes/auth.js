import { Router } from "express";
import jwt from "jsonwebtoken";
import { z } from "zod";
import rateLimit from "express-rate-limit";
import config from "../config.js";
import { query } from "../db.js";
import { requireAuth } from "../middleware/auth.js";
import { verify as mlVerify, generateAndSend } from "../services/magic-link.js";
import { verifyPassword } from "../services/password.js";

const router = Router();

function signSession(user, expiresIn) {
  return jwt.sign(
    {
      sub: user.id,
      email: user.email,
      name: user.name,
      role: user.role,
      token_version: user.token_version,
    },
    config.jwtSecret,
    { algorithm: "HS256", expiresIn }
  );
}

function parseExpiresIn(expiresIn) {
  const m = /^(\d+)([hdms]?)$/.exec(String(expiresIn || "24h"));
  if (!m) return 24 * 3600 * 1000;
  const n = parseInt(m[1], 10);
  const unit = m[2] || "h";
  const ms = { h: 3600 * 1000, d: 24 * 3600 * 1000, m: 60 * 1000, s: 1000 }[unit];
  return n * ms;
}

function cookieOpts() {
  return {
    httpOnly: true,
    sameSite: "lax",
    secure: config.cookieSecure,
    maxAge: parseExpiresIn(config.jwtExpiresIn),
  };
}

const loginLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 10,
  standardHeaders: true,
  legacyHeaders: false,
});

const loginAccountLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 10,
  keyGenerator: (req) => (req.body?.email || "").toLowerCase() || req.ip,
  standardHeaders: true,
  legacyHeaders: false,
});

const loginBody = z.object({ email: z.string().email(), password: z.string().min(1) });
const magicBody = z.object({ email: z.string() });
const verifyBody = z.object({ token: z.string(), otp: z.string() });
const agentVerifyBody = z.object({ email: z.string(), token: z.string(), otp: z.string() });

router.get("/login", (_req, res) => {
  res.render("auth/login");
});

router.post("/api/auth/login", loginLimiter, loginAccountLimiter, async (req, res) => {
  const parsed = loginBody.safeParse(req.body);
  if (!parsed.success) {
    return res.status(401).json({ error: { message: "accesso non riuscito" } });
  }

  const email = parsed.data.email.trim().toLowerCase();
  let row;
  try {
    const result = await query(
      "SELECT id, email, name, role, status, token_version, password_hash FROM users WHERE email=$1",
      [email]
    );
    row = result.rows[0];
  } catch {
    return res.status(401).json({ error: { message: "accesso non riuscito" } });
  }

  if (!row || !row.password_hash) {
    return res.status(401).json({ error: { message: "accesso non riuscito" } });
  }

  const ok = await verifyPassword(parsed.data.password, row.password_hash);
  if (!ok) {
    return res.status(401).json({ error: { message: "accesso non riuscito" } });
  }

  if (row.status === "disabled") {
    return res.status(403).json({ error: { message: "utente disabilitato" } });
  }

  res.cookie(config.sessionCookieName, signSession(row, config.jwtExpiresIn), cookieOpts());
  return res.json({ ok: true, user: { email: row.email, name: row.name, role: row.role } });
});

router.post("/api/auth/login/magic", loginLimiter, async (req, res) => {
  const parsed = magicBody.safeParse(req.body);
  if (!parsed.success) {
    return res.json({ sent: false, reason: "utente non trovato o disabilitato" });
  }
  const r = await generateAndSend(parsed.data.email);
  return res.json(r);
});

router.post("/api/auth/verify", loginLimiter, async (req, res) => {
  const parsed = verifyBody.safeParse(req.body);
  if (!parsed.success) {
    return res.status(401).json({ error: { message: "codice non valido o scaduto" } });
  }
  const u = await mlVerify(parsed.data.token, parsed.data.otp);
  if (!u) {
    return res.status(401).json({ error: { message: "codice non valido o scaduto" } });
  }
  res.cookie(config.sessionCookieName, signSession(u, config.jwtExpiresIn), cookieOpts());
  return res.json({ ok: true });
});

router.get("/api/auth/me", requireAuth, (req, res) => {
  res.json({ user: req.user });
});

router.post("/api/auth/logout", requireAuth, async (req, res) => {
  if (!req.user.api_token && req.user.sub) {
    await query("UPDATE users SET token_version = token_version + 1 WHERE id=$1", [req.user.sub]).catch(
      () => {}
    );
  }
  res.clearCookie(config.sessionCookieName);
  return res.json({ ok: true });
});

router.post("/api/agent/verify-otp", loginLimiter, async (req, res) => {
  const parsed = agentVerifyBody.safeParse(req.body);
  if (!parsed.success) {
    return res.status(401).json({ error: { message: "codice non valido o scaduto" } });
  }
  const u = await mlVerify(parsed.data.token, parsed.data.otp);
  if (!u) {
    return res.status(401).json({ error: { message: "codice non valido o scaduto" } });
  }
  const agentToken = jwt.sign(
    {
      sub: u.id,
      email: u.email,
      name: u.name,
      role: u.role,
      token_version: u.token_version,
      agent: true,
    },
    config.jwtSecret,
    { algorithm: "HS256", expiresIn: "7d" }
  );
  return res.json({ token: agentToken });
});

// --- F0-10: login agent stateless (email+password -> JWT agent 7d) ---
const agentLoginBody = z.object({ email: z.string().email(), password: z.string().min(1) });
router.post("/api/agent/login", loginLimiter, loginAccountLimiter, async (req, res) => {
  const parsed = agentLoginBody.safeParse(req.body);
  if (!parsed.success) return res.status(401).json({ error: { message: "accesso non riuscito" } });
  const email = parsed.data.email.trim().toLowerCase();
  let row;
  try {
    row = (await query(
      "SELECT id, email, name, role, status, token_version, password_hash FROM users WHERE email=$1",
      [email]
    )).rows[0];
  } catch { return res.status(401).json({ error: { message: "accesso non riuscito" } }); }
  if (!row || !row.password_hash || row.status === "disabled") {
    return res.status(401).json({ error: { message: "accesso non riuscito" } });
  }
  const ok = await verifyPassword(parsed.data.password, row.password_hash);
  if (!ok) return res.status(401).json({ error: { message: "accesso non riuscito" } });
  const token = jwt.sign(
    { sub: row.id, email: row.email, name: row.name, role: row.role, token_version: row.token_version, agent: true },
    config.jwtSecret, { algorithm: "HS256", expiresIn: "7d" }
  );
  return res.json({ token, user: { id: row.id, email: row.email, name: row.name, role: row.role } });
});

export default router;
