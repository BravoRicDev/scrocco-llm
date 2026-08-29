import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const nowIso = () => new Date().toISOString();
const daysBetween = (fromIso, toIso) => {
  const a = new Date(fromIso).getTime();
  const b = new Date(toIso).getTime();
  if (!Number.isFinite(a) || !Number.isFinite(b)) return 0;
  return Math.max(0, Math.floor((b - a) / 86400000));
};

function short(unique) {
  const s = String(unique ?? "");
  return s.length <= 16 ? s : s.slice(0, 8) + "…" + s.slice(-4);
}

function inCooldown(cooldown, unique) {
  if (!cooldown || !Array.isArray(cooldown)) return null;
  const entry = cooldown.find((c) => c?.unique === unique);
  if (!entry || !entry.until) return null;
  const ms = new Date(entry.until).getTime() - Date.now();
  if (ms <= 0) return null;
  return Math.ceil(ms / 1000);
}

async function fetchAll() {
  const [deploymentsRes, stateRes, bootstrapRes, leaderboardRes] = await Promise.all([
    gateway.get("/admin/deployments"),
    gateway.get("/admin/state"),
    gateway.get("/bootstrap/status"),
    gateway.get("/admin/insights/leaderboard", { params: { window: 30 } }),
  ]);
  return {
    deployments: Array.isArray(deploymentsRes) ? deploymentsRes : (deploymentsRes?.deployments ?? []),
    state: stateRes ?? {},
    bootstrap: bootstrapRes ?? {},
    leaderboard: leaderboardRes ?? {},
  };
}

function buildTimeline({ deployments, state, leaderboard }) {
  const cooldowns = Array.isArray(state?.cooldowns_active) ? state.cooldowns_active : [];
  const retired = new Set();
  const retiredKeys = state?.issues?.retired_keys;
  if (Array.isArray(retiredKeys)) {
    for (const k of retiredKeys) retired.add(String(k));
  }
  for (const d of deployments) {
    const issues = d.blob?.issues ?? d.issues;
    if (issues && Array.isArray(issues.retired_keys)) {
      for (const k of issues.retired_keys) retired.add(String(k));
    }
  }

  const leaderRows = Array.isArray(leaderboard?.rows) ? leaderboard.rows : [];
  const probeByUnique = new Map();
  for (const row of leaderRows) {
    const unique = row?.dep ?? row?.unique;
    if (unique && !probeByUnique.has(unique)) probeByUnique.set(unique, row);
  }

  const timeline = deployments.map((d) => {
    const unique = d.unique ?? d.id;
    const cdRemaining = inCooldown(cooldowns, unique);
    const failStreak = Number(d.fail_streak ?? 0) || 0;
    const lead = probeByUnique.get(unique);
    const deadSinceIso = d.dead_since ?? d.blob?.dead_since;

    let status = "ok";
    let last_reason = d.last_reason ?? null;
    if (retired.has(unique)) {
      status = "retired";
    } else if (cdRemaining !== null || failStreak >= 3) {
      status = "dead_suspect";
      last_reason = last_reason ?? (cdRemaining !== null ? "cooldown attivo" : "fail_streak >= 3");
    } else if (lead && lead.probe_ms !== undefined && lead.probe_ms !== null) {
      status = lead.probe_status === "ok" || lead.ok === true || lead.probe_ms < 60000 ? "recovered" : "never_probed";
    } else if (!lead) {
      status = "never_probed";
      last_reason = last_reason ?? "mai sondato";
    }

    return {
      unique,
      short: short(unique),
      profile: d.profile ?? null,
      modello: d.modello ?? d.model ?? null,
      provider: d.provider ?? null,
      group: d.group ?? null,
      status,
      last_reason,
      dead_since_days: deadSinceIso ? daysBetween(deadSinceIso, nowIso()) : null,
      fail_streak: failStreak,
      cooldown_remaining: cdRemaining,
      probe_ms: lead && (lead.probe_ms ?? null) != null ? Number(lead.probe_ms) : null,
    };
  });

  const order = { retired: 0, dead_suspect: 1, never_probed: 2, recovered: 3, ok: 4 };
  timeline.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));

  const counts = timeline.reduce((acc, x) => {
    acc[x.status] = (acc[x.status] ?? 0) + 1;
    return acc;
  }, {});
  return { rows: timeline, counts };
}

router.get("/key-health", requireAuth, authorize("deployments", "read"), async (req, res, next) => {
  try {
    const data = await fetchAll();
    const { rows, counts } = buildTimeline(data);
    res.render("key-health/index", { timeline: rows, counts, state: data.state, bootstrap: data.bootstrap });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

const unretireSchema = z.object({
  unique: z.string().trim().min(1),
});

router.post("/key-health/:unique/unretire", requireAuth, authorize("deployments", "unretire"), async (req, res, next) => {
  const parsed = unretireSchema.safeParse({ unique: req.params.unique });
  if (!parsed.success) {
    return res.redirect("/key-health?flash=" + encodeURIComponent("dati non validi") + "&flashType=error");
  }
  const { unique } = parsed.data;
  try {
    await gateway.post("/admin/deployments/unretire", { json: { unique } });
    await auditLog({
      user: req.user,
      op: "deployments.unretire",
      target: unique,
      detail: { unique },
      routeMethod: req.method,
      gatewayPath: "/admin/deployments/unretire",
      ip: req.ip,
    });
    return res.status(200).json({ ok: true, unique });
  } catch (err) {
    if (err instanceof GatewayError) {
      return res.status(502).json({ ok: false, error: err.message });
    }
    next(err);
  }
});

export default router;
