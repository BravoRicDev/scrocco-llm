import { Router } from "express";
import { diffLines } from "diff";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";
import { listSnapshots, getSnapshot } from "../services/config-snapshots.js";

const router = Router();

const apiError = (res, status, message) =>
  res.status(status).json({ error: { message } });

// legge il contenuto raw CORRENTE dal gateway per il kind dato
async function currentRaw(kind) {
  if (kind === "csv") return (await gateway.get("/admin/csv"))?.raw || "";
  return (await gateway.get("/admin/policy/raw"))?.raw || "";
}

router.get("/config-history", requireAuth, authorize("config_snapshots", "read"), async (req, res, next) => {
  try {
    const snapshots = await listSnapshots({ kind: req.query.kind || null, limit: 100 });
    res.render("config-history/index", { snapshots, diffLines: null });
  } catch (err) {
    next(err);
  }
});

router.get("/config-history/:id/diff", requireAuth, authorize("config_snapshots", "read"), async (req, res, next) => {
  try {
    const snap = await getSnapshot(parseInt(req.params.id, 10));
    if (!snap) return apiError(res, 404, "snapshot non trovato");
    let current = "";
    try {
      current = await currentRaw(snap.kind);
    } catch (err) {
      if (err instanceof GatewayError) return apiError(res, err.status || 502, err.message);
      throw err;
    }
    const lines = diffLines(current, snap.content).map((part) => ({
      type: part.added ? "+" : part.removed ? "-" : " ",
      text: part.value,
    }));
    return res.json({ id: snap.id, kind: snap.kind, lines });
  } catch (err) {
    next(err);
  }
});

router.post("/config-history/:id/restore", requireAuth, authorize("config_snapshots", "restore"), async (req, res, next) => {
  const redir = (msg, type) =>
    res.redirect("/config-history?flash=" + encodeURIComponent(msg) + "&flashType=" + type);
  try {
    const snap = await getSnapshot(parseInt(req.params.id, 10));
    if (!snap) return redir("snapshot non trovato", "error");
    const path = snap.kind === "csv" ? "/admin/csv" : "/admin/policy/raw";
    await gateway.put(path, { json: { raw: snap.content } });
    await auditLog({
      user: req.user,
      op: "config.restore",
      target: `snapshot:${snap.id}`,
      detail: { kind: snap.kind, sha256: snap.source_sha256 },
      routeMethod: req.method,
      gatewayPath: path,
      ip: req.ip,
    });
    return redir(`Snapshot #${snap.id} (${snap.kind}) ripristinato`, "success");
  } catch (err) {
    if (err instanceof GatewayError) return redir("Gateway: " + err.message, "error");
    next(err);
  }
});

export default router;
