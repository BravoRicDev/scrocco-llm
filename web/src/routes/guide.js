import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

router.get("/guide", requireAuth, authorize("guide", "read"), async (req, res, next) => {
  try {
    const markdown = await gateway.rawGet("/admin/guide");
    if (req.query.download !== undefined) {
      res.setHeader("Content-Type", "text/markdown; charset=utf-8");
      res.setHeader("Content-Disposition", 'attachment; filename="guida-gateway.md"');
      return res.send(markdown);
    }
    res.render("guide/index", { markdown });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

export default router;
