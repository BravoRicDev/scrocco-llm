import express from "express";
import helmet from "helmet";
import cookieParser from "cookie-parser";
import jwt from "jsonwebtoken";
import expressLayouts from "express-ejs-layouts";
import { fileURLToPath } from "node:url";
import config from "./config.js";
import { logger } from "./services/logger.js";
import { requestId } from "./middleware/request-id.js";
import { csrfProtection } from "./middleware/csrf.js";
import { translate } from "./services/i18n.js";
import authRoutes from "./routes/auth.js";
import healthRoutes from "./routes/health.js";
import apiTokensRoutes from "./routes/api-tokens.js";
import deploymentsRoutes from "./routes/deployments.js";
import profilesRoutes from "./routes/profiles.js";
import policyRoutes from "./routes/policy.js";
import capabilitiesRoutes from "./routes/capabilities.js";
import dashboardRoutes from "./routes/dashboard.js";
import expiringRoutes from "./routes/expiring.js";
import historyRoutes from "./routes/history.js";
import insightsRoutes from "./routes/insights.js";
import bootstrapRoutes from "./routes/bootstrap.js";
import guideRoutes from "./routes/guide.js";
import bulkRoutes from "./routes/deployments-bulk.js";
import systemRoutes from "./routes/system-actions.js";
import probeRoutes from "./routes/probe.js";
import policyKeysRoutes from "./routes/policy-keys.js";
import usersRoutes from "./routes/users.js";
import liveRoutes from "./routes/live.js";
import errorsRoutes from "./routes/errors.js";
import leaderboardRoutes from "./routes/leaderboard.js";
import chartsRoutes from "./routes/charts.js";
import csvEditorRoutes from "./routes/csv-editor.js";
import playgroundRoutes from "./routes/playground.js";
import policyRawRoutes from "./routes/policy-raw.js";
import configHistoryRoutes from "./routes/config-history.js";
import keyHealthRoutes from "./routes/key-health.js";
import alertsRoutes from "./routes/alerts.js";
import stickyRoutes from "./routes/sticky.js";
import { withSnapshot } from "./middleware/snapshot-hook.js";
import { createPoller } from "./services/alert-poller.js";
import apiDeploymentsRoutes from "./routes/api-deployments.js";
import apiCoreRoutes from "./routes/api-core.js";
import apiWriteRoutes from "./routes/api-write.js";
import mcpRoutes from "./routes/mcp.js";
import apiDocsRoutes from "./routes/api-docs.js";
import auditRoutes from "./routes/audit.js";
import agentDocRoutes from "./routes/agent-doc.js";

export async function createApp() {
  const app = express();
  app.set("trust proxy", 1);
  app.use(helmet({ contentSecurityPolicy: false, crossOriginEmbedderPolicy: false }));
  app.use(cookieParser());
  app.use(requestId);
  app.use(express.urlencoded({ extended: false, limit: "50mb" }));
  app.use(express.json({ limit: "50mb" }));
  app.use(csrfProtection);

  // bootstrap utente da cookie (best-effort; requireAuth resta l'autorita')
  app.use((req, _res, next) => {
    const token = req.cookies?.[config.sessionCookieName];
    if (token) { try { req.user = jwt.verify(token, config.jwtSecret, { algorithms: ["HS256"] }); } catch { req.user = null; } }
    next();
  });

  app.set("view engine", "ejs");
  app.set("views", [fileURLToPath(new URL("../views", import.meta.url))]);
  app.use(expressLayouts);
  app.set("layout", "layouts/admin");
  app.use(express.static(fileURLToPath(new URL("../public", import.meta.url))));

  app.use((req, res, next) => {
    res.locals.user = req.user || null;
    res.locals.path = req.path;
    res.locals.query = req.query || {};
    if (req.query && req.query.flash) {
      const type = (req.query.flashType === "success" || req.query.flashType === "error") ? req.query.flashType : "info";
      res.locals.flash = String(req.query.flash).split(";").filter(Boolean).map((m) => ({ type, msg: m }));
    } else {
      res.locals.flash = [];
    }
    res.locals.lang = "it";
    res.locals.t = (key) => translate("it", key);
    res.locals.escapeAttr = (v) => String(v ?? "").replace(/&/g,"&amp;").replace(/\"/g,"&quot;").replace(/'/g,"&#39;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
    res.locals.app = { name: config.appName };
    res.locals.currentPath = req.path;
    res.locals.telegramHint = !!config.telegramBotToken;
    next();
  });

  app.use(authRoutes);
  app.use(healthRoutes);
  app.use(apiTokensRoutes);
  app.use(dashboardRoutes);
  app.use(bulkRoutes);
  app.use(deploymentsRoutes);
  app.use(profilesRoutes);
  app.use(policyRoutes);
  app.use(capabilitiesRoutes);
  app.use(expiringRoutes);
  app.use(historyRoutes);
  app.use(insightsRoutes);
  app.use(bootstrapRoutes);
  app.use(guideRoutes);
  app.use(systemRoutes);
  app.use(probeRoutes);
  app.use(policyKeysRoutes);
  app.use(usersRoutes);
  app.use(liveRoutes);
  app.use(errorsRoutes);
  app.use(leaderboardRoutes);
  app.use(chartsRoutes);

  // snapshot automatico in config_snapshots dopo un save CSV/yaml riuscito
  app.use("/api/csv-editor/save", withSnapshot("csv"));
  app.use("/api/policy-raw/save", withSnapshot("yaml"));
  app.use(csvEditorRoutes);
  app.use(playgroundRoutes);
  app.use(policyRawRoutes);
  app.use(configHistoryRoutes);
  app.use(keyHealthRoutes);
  app.use(alertsRoutes);
  app.use(stickyRoutes);

  // surface /api/v1 per agenti (Bearer JWT-agent o agtok_)
  app.use(apiDeploymentsRoutes);
  app.use(apiCoreRoutes);
  app.use(apiWriteRoutes);
  app.use(apiDocsRoutes);      // /api/v1/openapi.json (pubblico) + /api/v1/docs
  app.use(mcpRoutes);          // POST /api/mcp
  app.use(auditRoutes);        // GET /admin/audit (admin-only)
  app.use(agentDocRoutes);     // GET /agent-guide

  // 404
  app.use((req, res) => {
    if (req.path.startsWith("/api")) return res.status(404).json({ error: { message: "endpoint non trovato" } });
    return res.redirect("/login");
  });

  // error handler finale
  app.use((err, req, res, _next) => {
    // Log errors internally (never leak to clients)
    if (config.nodeEnv === "production") {
      logger.error(`[ERROR] ${req.method} ${req.path}: ${err.message}`, err);
    } else {
      // In development, log to console but don't include full details in response
      console.error(`[ERROR] ${req.method} ${req.path}: ${err.message}`);
    }
    // Never expose PostgreSQL-specific error codes to clients
    if (err && err.code === "22P02") {
      if (req.path.startsWith("/api")) return res.status(404).json({ error: { message: "non trovato" } });
      return res.status(404).render("error", { message: "Non trovato" });
    }
    if (req.path.startsWith("/api")) return res.status(err.status || 500).json({ error: { message: "errore interno" } });
    const msg = config.nodeEnv === "production" ? "Errore interno" : (err?.message || "Errore interno");
    return res.status(err?.status || 500).render("error", { message: msg });
  });

  return app;
}

// bootstrap check
if (!config.jwtSecret || !config.databaseUrl) {
  logger.error("FATAL: JWT_SECRET e DATABASE_URL devono essere configurati");
  process.exit(1);
}
if (!config.gatewayMasterKey && config.nodeEnv === "development" && !config.gatewayMock) {
  logger.warn("GATEWAY_MASTER_KEY assente (ok in dev con GATEWAY_MOCK=1)");
}
if (config.gatewayMock) logger.info("GATEWAY_MOCK=1 (nessuna chiamata reale al gateway)");

const isMain = process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1];
if (isMain) {
  createApp().then((app) => {
    const server = app.listen(config.port, () => logger.info(`${config.appName} in ascolto sulla porta ${config.port}`));
    // il playground puo' attendere il gateway anche parecchi minuti: niente
    // cap sulla durata della richiesta (default Node = 5 min).
    server.requestTimeout = 0;
    server.headersTimeout = 65000;
    if (!config.alertPollerDisabled) {
      const poller = createPoller();
      poller.start().catch((err) => logger.error(`alert poller: avvio fallito: ${err.message}`));
      const shutdown = () => { try { poller.stop(); } catch { /* noop */ } };
      process.on("SIGTERM", shutdown);
      process.on("exit", shutdown);
    }
  }).catch((err) => { logger.error("avvio fallito", { error: err.message }); process.exit(1); });
}

process.on("unhandledRejection", (r) => console.error("Unhandled Rejection:", r));
process.on("uncaughtException", (e) => { console.error("Uncaught Exception:", e); process.exit(1); });
