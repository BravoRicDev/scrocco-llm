import { Router } from "express";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";

const router = Router();

// Il contenuto e' statico (fa parte dell'immagine): letto una volta per lingua,
// in modo asincrono, invece di un readFileSync che blocca a ogni richiesta.
const guideCache = new Map();

async function loadGuide(lang) {
  if (guideCache.has(lang)) return guideCache.get(lang);
  const candidates = [
    new URL(`../../locales/${lang}/AGENT.md`, import.meta.url),
    new URL("../../docs/AGENT.md", import.meta.url),
  ];
  let md = "";
  for (const u of candidates) {
    try { md = await readFile(fileURLToPath(u), "utf8"); break; } catch { /* next */ }
  }
  if (md) guideCache.set(lang, md);
  return md;
}

// serve docs/AGENT.md (localizzato se disponibile) come testo/markdown
router.get("/agent-guide", requireAuth, authorize("guide", "read"), async (req, res, next) => {
  try {
    const lang = (res.locals && res.locals.lang) || "it";
    const md = await loadGuide(lang);
    if (!md) return res.status(404).type("text/plain").send("AGENT.md non trovato");
    res.type("text/markdown; charset=utf-8").send(md);
  } catch (err) {
    next(err);
  }
});

export default router;
