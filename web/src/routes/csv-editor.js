import { Router } from "express";
import { z } from "zod";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";
import gateway, { GatewayError } from "../services/gateway.js";
import { auditLog } from "../services/audit.js";
import { computeCsvHash, mergeCsvSecrets } from "../services/csv-merge.js";

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
    keyIdx,
    rows,
    count: (data && data.count) ?? rows.length,
    backups: (data && data.backups) || [],
  };
}

router.get("/csv-editor", requireAuth, authorize("csv", "read"), async (req, res, next) => {
  try {
    const data = unwrap(await gateway.get("/admin/csv"));
    res.render("csv-editor/index", { ...data, hash: computeCsvHash(data.raw) });
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
  const parsed = z.object({ raw: z.string(), hash: z.string().optional() }).safeParse(req.body);
  if (!parsed.success) return apiError(res, 400, "dati non validi");
  try {
    const baseData = await gateway.get("/admin/csv");
    const baseRaw = (baseData && baseData.raw) || "";

    // Il ramo senza hash resta valido (client legacy / test): si salta solo il 409.
    if (parsed.data.hash) {
      const currentBaseHash = computeCsvHash(baseRaw);
      if (parsed.data.hash !== currentBaseHash) {
        return res.status(409).json({
          error: {
            message: "Il file CSV è stato modificato sul server da un altro processo. Ricaricare la pagina prima di salvare.",
          },
        });
      }
    }

    const merged = mergeCsvSecrets(parsed.data.raw, baseRaw);
    if (!merged.ok) return apiError(res, merged.status, merged.error);

    const result = await gateway.put("/admin/csv", { json: { raw: merged.mergedRaw } });

    // Ciclo dell'hash: il gateway riscrive il file con csv.writer (terminatori,
    // header strippati, righe vuote via), quindi l'hash va calcolato sui byte
    // REALMENTE persistiti e non su mergedRaw, altrimenti il salvataggio
    // successivo confronta l'hash con sé stesso e va in falso 409.
    const fresh = await gateway.get("/admin/csv");
    const freshRaw = (fresh && fresh.raw) || merged.mergedRaw;
    const newHash = computeCsvHash(freshRaw);
    res.locals.lastSavedRaw = freshRaw;

    const freshRows = (fresh && fresh.parsed && fresh.parsed.rows && fresh.parsed.rows.length) ?? null;
    await auditLog({
      user: req.user,
      op: "csv.save",
      target: "keys_rotation.csv",
      detail: { bytes: Buffer.byteLength(freshRaw), rows: (fresh && fresh.count) ?? freshRows },
      routeMethod: req.method,
      gatewayPath: "/admin/csv",
      ip: req.ip,
    });
    return res.json({
      ok: true,
      backup: result?.backup ?? null,
      rows: (fresh && fresh.count) ?? freshRows,
      hash: newHash,
    });
  } catch (err) {
    if (err instanceof GatewayError) return apiError(res, err.status || 400, err.message);
    next(err);
  }
});

export default router;
