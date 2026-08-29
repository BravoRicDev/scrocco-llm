import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";

const router = Router();

const apiError = (res, status, message) =>
  res.status(status).json({ error: { message } });

// Non mostriamo MAI una chiave intera nella tabella: prefisso + 3 + coda.
function maskCell(v) {
  const s = String(v ?? "");
  if (s.length < 12) return s;
  if (/^(sk-|gsk_|xai-|AI|key-|Bearer )/i.test(s) || s.length > 24) {
    return s.slice(0, 6) + "…" + s.slice(-3);
  }
  return s;
}

// unwrap del contratto GP-02: {path, raw, parsed:{header,rows}, count, backups}
function unwrap(data) {
  const parsed = (data && data.parsed) || {};
  const header = parsed.header || [];
  const keyIdx = new Set(
    header.map((h, i) => (/key|api_key/i.test(h) || h.startsWith("scrocco-llm-") ? i : -1)).filter((i) => i >= 0)
  );
  const rows = (parsed.rows || []).map((row) => {
    const out = {};
    header.forEach((h, i) => {
      out[h] = keyIdx.has(i) ? maskCell(row[h]) : row[h];
    });
    return out;
  });
  return {
    path: (data && data.path) || "keys_rotation.csv",
    raw: (data && data.raw) || "",
    header,
    rows,
    count: (data && data.count) ?? rows.length,
    backups: (data && data.backups) || [],
  };
}

router.get("/csv-editor", requireAuth, authorize("csv", "read"), async (req, res, next) => {
  try {
    const data = unwrap(await gateway.get("/admin/csv"));
    res.render("csv-editor/index", data);
  } catch (err) {
    if (err instanceof GatewayError) return res.status(502).render("error", { message: "Gateway: " + err.message });
    next(err);
  }
});

router.get("/api/csv-editor/data", requireAuth, authorize("csv", "read"), async (req, res, next) => {
  try {
    return res.json(unwrap(await gateway.get("/admin/csv")));
  } catch (err) {
    if (err instanceof GatewayError) return apiError(res, err.status || 502, err.message);
    next(err);
  }
});

router.post("/api/csv-editor/save", requireAuth, authorize("csv", "write"), async (req, res, next) => {
  const parsed = z.object({ raw: z.string() }).safeParse(req.body);
  if (!parsed.success) return apiError(res, 400, "dati non validi");
  try {
    const result = await gateway.put("/admin/csv", { json: { raw: parsed.data.raw } });
    await auditLog({
      user: req.user,
      op: "csv.save",
      target: "keys_rotation.csv",
      detail: { bytes: Buffer.byteLength(parsed.data.raw), rows: result?.rows ?? null },
      routeMethod: req.method,
      gatewayPath: "/admin/csv",
      ip: req.ip,
    });
    return res.json({ ok: true, backup: result?.backup ?? null, rows: result?.rows ?? null });
  } catch (err) {
    if (err instanceof GatewayError) return apiError(res, err.status || 400, err.message);
    next(err);
  }
});

export default router;
