import { Router } from "express";
import SPEC from "../openapi.js";

const router = Router();

// spec pubblica (documentazione, nessun auth — come /v1/openapi.json del CMS)
router.get("/api/v1/openapi.json", (_req, res) => {
  res.json(SPEC);
});

// pagina HTML minimale, nessuna CDN: carica lo spec e disegna un accordeon
router.get("/api/v1/docs", (_req, res) => {
  res.render("api-docs", { layout: false, title: "API docs" });
});

export default router;
