import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

const GROUP_BY = ["model", "profile", "group"];

function clampDays(v) {
  const n = parseInt(v, 10);
  if (Number.isNaN(n)) return 7;
  return Math.min(90, Math.max(1, n));
}

router.get("/insights", requireAuth, authorize("insights", "read"), async (req, res, next) => {
  try {
    const days = clampDays(req.query.days);
    let groupBy = String(req.query.group_by ?? "model");
    if (!GROUP_BY.includes(groupBy)) groupBy = "model";

    const baseParams = { days };
    const [data, summary] = await Promise.all([
      gateway.get("/admin/insights", { params: baseParams }),
      gateway.get("/admin/insights/summary", { params: baseParams }),
    ]);

    let rows = [];
    const none = groupBy === "none" || groupBy === "";
    if (!none) {
      const bucket = data?.["by_" + groupBy] ?? {};
      rows = Object.entries(bucket).map(([key, v]) => ({
        key,
        calls: Number(v.calls ?? 0),
        errors: Number(v.errors ?? 0),
        prompt_tokens: Number(v.prompt_tokens ?? 0),
        completion_tokens: Number(v.completion_tokens ?? 0),
        total_tokens: Number(v.total_tokens ?? 0),
        cost_usd: Number(v.cost_usd ?? 0),
        cost_est: Number(v.cost_est ?? v.cost_estimated ?? 0),
        avg_dur_ms: Number(v.avg_dur_ms ?? v.avg_ms ?? 0),
        fb_rate: Number(v.fb_rate ?? 0),
        qc_rate: Number(v.qc_rate ?? 0),
        wd_rate: Number(v.wd_rate ?? 0),
        bad_rate: Number(v.bad_rate ?? 0),
      }));
    }

    res.render("insights/index", { data, summary, rows, none, days, groupBy });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

export default router;
