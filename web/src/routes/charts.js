import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway from "../services/gateway.js";

const router = Router();

function clampDays(v) {
  const n = parseInt(v, 10);
  if (Number.isNaN(n)) return 30;
  return Math.min(90, Math.max(1, n));
}

function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function movingAvg(values, window = 3) {
  return values.map((_, i) => {
    const from = Math.max(0, i - window + 1);
    const slice = values.slice(from, i + 1);
    return slice.reduce((a, b) => a + b, 0) / slice.length;
  });
}

router.get("/observability/charts", requireAuth, authorize("observability", "read"), (req, res) => {
  const days = clampDays(req.query.days);
  res.render("observability/charts", { days });
});

router.get("/api/charts/data", requireAuth, async (req, res) => {
  const days = clampDays(req.query.days);
  const empty = { labels: [], p95: [], calls: [], cost: [], err_rate: [] };
  try {
    const groupBy = req.query.group_by === "day" ? "day" : "day";
    const [insights, leaderboard] = await Promise.all([
      gateway.get("/admin/insights", { params: { days, group_by: groupBy } }),
      gateway.get("/admin/insights/leaderboard", { params: { window: days + "d", sort: "p95_dur_ms", order: "asc" } }),
    ]);

    const byDay = insights?.by_day ?? {};
    const labels = Object.keys(byDay).sort();
    const calls = labels.map((k) => toNum(byDay[k]?.calls));
    const cost = labels.map((k) => {
      const reported = toNum(byDay[k]?.cost_reported_usd);
      const estimated = toNum(byDay[k]?.cost_estimated_usd);
      return reported > 0 ? reported : estimated;
    });
    const errRate = labels.map((k) => toNum(byDay[k]?.bad_rate) * 100);

    let p95;
    let p95Source = "series";
    const lbSeries = leaderboard?.series?.p95;
    if (Array.isArray(lbSeries) && lbSeries.length === labels.length) {
      p95 = lbSeries.map(toNum);
    } else {
      p95 = movingAvg(labels.map((k) => toNum(byDay[k]?.avg_dur_ms)));
      p95Source = "avg";
    }

    res.json({ days, p95_source: p95Source, series: { labels, p95, calls, cost, err_rate: errRate }, up: true });
  } catch {
    res.json({ days, up: false, p95_source: "avg", series: empty });
  }
});

export default router;
