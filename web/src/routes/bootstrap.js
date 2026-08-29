import { Router } from "express";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";

const router = Router();

async function attempt(promise) {
  try {
    return { ok: true, data: await promise };
  } catch (err) {
    return { ok: false, error: err };
  }
}

router.get("/bootstrap", requireAuth, authorize("bootstrap", "read"), async (req, res, next) => {
  try {
    const [playbook, status, providers] = await Promise.all([
      attempt(gateway.get("/bootstrap")),
      attempt(gateway.get("/bootstrap/status")),
      attempt(gateway.get("/bootstrap/providers")),
    ]);

    const errors = { playbook: null, status: null, providers: null };
    if (!playbook.ok) errors.playbook = playbook.error instanceof GatewayError ? playbook.error.message : "errore gateway";
    if (!status.ok) errors.status = status.error instanceof GatewayError ? status.error.message : "errore gateway";
    if (!providers.ok) errors.providers = providers.error instanceof GatewayError ? providers.error.message : "errore gateway";

    if (!playbook.ok && !status.ok && !providers.ok) {
      const err = playbook.error ?? status.error ?? providers.error;
      if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
      return next(err);
    }

    res.render("bootstrap/index", {
      playbook: playbook.ok ? playbook.data : null,
      status: status.ok ? status.data : null,
      providers: providers.ok ? providers.data : null,
      errors,
    });
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

export default router;