import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

const clampDays = (value) => {
  const n = parseInt(value, 10);
  if (Number.isNaN(n)) return 7;
  return Math.min(90, Math.max(1, n));
};

router.get("/expiring", requireAuth, authorize("expiring", "read"), async (req, res, next) => {
  try {
    const days = clampDays(req.query.days);
    const profile = req.query.profile ? String(req.query.profile) : "";

    const [expData, profilesRes] = await Promise.all([
      gateway.get("/admin/deployments/expiring", { params: { days } }),
      gateway.get("/admin/profiles"),
    ]);

    const profiles = Array.isArray(profilesRes) ? profilesRes : (profilesRes?.profiles ?? []);
    const modelToProfile = new Map(
      profiles
        .map((p) => [String(p.base_model ?? ""), String(p.name ?? "")])
        .filter(([m]) => m !== "")
    );

    const expiring = (Array.isArray(expData) ? expData : (expData?.expiring ?? [])).map((e) => ({
      id: e.id,
      modello: e.modello,
      in_days: e.in_days,
      data_raw: e.data_raw,
      profile: modelToProfile.get(String(e.modello ?? "")) || "",
    }));

    res.render("expiring/index", { days, expiring, profiles, profile });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

export default router;