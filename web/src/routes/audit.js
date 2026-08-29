import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import { recentAudit } from "../services/audit.js";

const router = Router();

// La matrice F0-07 dà `audit/read` a tutti, ma il PANNELLO e' admin-only:
// check esplicito sul ruolo dopo authorize.
router.get("/admin/audit", requireAuth, authorize("audit", "read"), async (req, res, next) => {
  if (!req.user || req.user.role !== "admin") {
    return res.status(403).render("error", { message: "Solo gli amministratori possono vedere l'audit." });
  }
  try {
    const rows = await recentAudit({ limit: 200, op: req.query.op || null });
    res.render("admin/audit/index", { rows });
  } catch (err) {
    next(err);
  }
});

export default router;
