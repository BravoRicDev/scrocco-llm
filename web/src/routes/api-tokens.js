import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { createApiToken, listApiTokens, revokeApiToken } from "../services/api-tokens.js";

const router = Router();

const ALLOWED_DAYS = new Set([30, 60, 90, 120, 180, 365]);

function parseDays(value) {
  const days = parseInt(value, 10);
  return ALLOWED_DAYS.has(days) ? days : 120;
}

router.get("/admin/api-tokens", requireAuth, async (req, res, next) => {
  try {
    const tokens = await listApiTokens(req.user.sub);
    res.render("admin/api-tokens/index", {
      tokens,
      newToken: null,
      baseUrl: req.protocol + "://" + req.get("host"),
    });
  } catch (err) {
    next(err);
  }
});

router.post("/admin/api-tokens", requireAuth, async (req, res, next) => {
  try {
    const name = String(req.body.name || "").trim();
    const days = parseDays(req.body.expires_days);
    if (!name) {
      return res.status(400).render("error", { message: "Il nome e' obbligatorio." });
    }
    const created = await createApiToken(req.user.sub, name, days);
    const tokens = await listApiTokens(req.user.sub);
    res.render("admin/api-tokens/index", {
      tokens,
      newToken: created,
      baseUrl: req.protocol + "://" + req.get("host"),
    });
  } catch (err) {
    next(err);
  }
});

router.post("/admin/api-tokens/:id/revoke", requireAuth, async (req, res, next) => {
  try {
    await revokeApiToken(req.user.sub, req.params.id);
    res.redirect("/admin/api-tokens");
  } catch (err) {
    next(err);
  }
});

router.get("/api/agent/api-tokens", requireAuth, async (req, res, next) => {
  try {
    res.json({ tokens: await listApiTokens(req.user.sub) });
  } catch (err) {
    next(err);
  }
});

const agentCreateBody = z.object({
  name: z.string().min(1),
  expires_days: z.number().int().optional(),
});

router.post("/api/agent/api-tokens", requireAuth, async (req, res, next) => {
  try {
    const parsed = agentCreateBody.safeParse(req.body);
    if (!parsed.success) {
      return res.status(400).json({ error: { message: "dati non validi" } });
    }
    const name = parsed.data.name.trim();
    const days = ALLOWED_DAYS.has(parsed.data.expires_days)
      ? parsed.data.expires_days
      : 120;
    const c = await createApiToken(req.user.sub, name, days);
    res.status(201).json({ token: c.token, prefix: c.prefix, expires_at: c.expiresAt });
  } catch (err) {
    next(err);
  }
});

router.delete("/api/agent/api-tokens/:id", requireAuth, async (req, res, next) => {
  try {
    await revokeApiToken(req.user.sub, req.params.id);
    res.json({ ok: true });
  } catch (err) {
    next(err);
  }
});

export default router;
