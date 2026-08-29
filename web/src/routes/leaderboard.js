import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

const WINDOWS = { "24h": 1, "7d": 7, "30d": 30, "90d": 90 };
const WINDOW_TITLES = { "24h": "24 ore", "7d": "7 giorni", "30d": "30 giorni", "90d": "90 giorni" };
const SORT_KEYS = ["dep", "profile", "group", "provider", "model", "calls", "error_rate", "fb_rate", "qc_rate", "wd_rate", "probe_ms"];
const NUMERIC_KEYS = ["calls", "error_rate", "fb_rate", "qc_rate", "wd_rate", "probe_ms"];

function parseWindow(v) {
  const key = String(v ?? "7d");
  return Object.prototype.hasOwnProperty.call(WINDOWS, key) ? key : "7d";
}

function parseSort(v) {
  const key = String(v ?? "calls");
  return SORT_KEYS.includes(key) ? key : "calls";
}

function parseOrder(v) {
  return v === "asc" ? "asc" : "desc";
}

function normalizeRows(rows) {
  return Array.isArray(rows) ? rows : [];
}

function sortRows(rows, sort, order) {
  const dir = order === "asc" ? 1 : -1;
  const numeric = NUMERIC_KEYS.includes(sort);
  return [...rows].sort((a, b) => {
    let cmp;
    if (numeric) {
      cmp = (Number(a?.[sort]) || 0) - (Number(b?.[sort]) || 0);
    } else {
      cmp = String(a?.[sort] ?? "").localeCompare(String(b?.[sort] ?? ""), "it");
    }
    if (cmp === 0) cmp = String(a?.dep ?? "").localeCompare(String(b?.dep ?? ""), "it");
    return cmp * dir;
  });
}

async function fetchLeaderboard(query) {
  const window = parseWindow(query.window);
  const sort = parseSort(query.sort);
  const order = parseOrder(query.order);
  const profile = String(query.profile ?? "").trim();

  const params = { window: WINDOWS[window] };
  if (profile) params.profile = profile;

  const data = await gateway.get("/admin/insights/leaderboard", { params });
  const rows = sortRows(normalizeRows(data.rows), sort, order);

  return {
    window,
    sort,
    order,
    profile,
    window_days: Number(data.window_days ?? WINDOWS[window]),
    rows,
  };
}

router.get("/observability/leaderboard", requireAuth, authorize("observability", "read"), async (req, res, next) => {
  try {
    const view = await fetchLeaderboard(req.query);
    res.render("observability/leaderboard", { ...view, titles: WINDOW_TITLES });
  } catch (err) {
    if (err instanceof GatewayError) {
      return res.render("observability/leaderboard", {
        window: parseWindow(req.query.window),
        sort: parseSort(req.query.sort),
        order: parseOrder(req.query.order),
        profile: String(req.query.profile ?? "").trim(),
        window_days: WINDOWS[parseWindow(req.query.window)],
        rows: [],
        titles: WINDOW_TITLES,
      });
    }
    next(err);
  }
});

router.get("/api/leaderboard/data", requireAuth, async (req, res) => {
  try {
    const view = await fetchLeaderboard(req.query);
    res.json({ rows: view.rows, window_days: view.window_days, count: view.rows.length });
  } catch {
    const window = parseWindow(req.query.window);
    res.json({ rows: [], window_days: WINDOWS[window], count: 0 });
  }
});

export default router;
