import { Router } from "express";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { requireAuth } from "../middleware/auth.js";
import { authorize } from "../middleware/authorize.js";

const router = Router();

// serve docs/AGENT.md (localizzato se disponibile) come testo/markdown
router.get("/agent-guide", requireAuth, authorize("guide", "read"), (req, res) => {
  const lang = (res.locals && res.locals.lang) || "it";
  const candidates = [
    new URL(`../../locales/${lang}/AGENT.md`, import.meta.url),
    new URL("../../docs/AGENT.md", import.meta.url),
  ];
  let md = "";
  for (const u of candidates) {
    try { md = readFileSync(fileURLToPath(u), "utf8"); break; } catch { /* next */ }
  }
  if (!md) return res.status(404).type("text/plain").send("AGENT.md non trovato");
  res.type("text/markdown; charset=utf-8").send(md);
});

export default router;
