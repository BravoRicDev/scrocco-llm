import dotenv from "dotenv";
import crypto from "crypto";
dotenv.config();

// Rail anti-incidente (2026-10-11): la suite web aveva scritto sul gateway VERO
// (URL di default risolvibile in rete + mock non ancora attivo al primo import)
// e un fixture di prova ha sostituito il catalogo 1620 righe -> 1, uccidendo
// ogni chat con 400. In test il mock e' SEMPRE attivo e l'URL e' morto,
// qualunque cosa dica l'ambiente: la suite non puo' toccare la rete.
const isTest =
  (process.env.NODE_ENV || "") === "test" || Boolean(process.env.NODE_TEST_CONTEXT);

export default {
  port: parseInt(process.env.PORT || "3000", 10),
  nodeEnv: process.env.NODE_ENV || "development",

  databaseUrl: process.env.DATABASE_URL,
  jwtSecret: process.env.JWT_SECRET || generateJwtSecret(),
  jwtExpiresIn: process.env.JWT_EXPIRES_IN || "24h",

  sessionCookieName: process.env.SESSION_COOKIE_NAME || "token",
  gatewayUrl: isTest
    ? "http://127.0.0.1:1"
    : process.env.GATEWAY_URL || "http://scrocco-llm:4001",
  gatewayMasterKey: process.env.GATEWAY_MASTER_KEY || "",
  gatewayTimeoutMs: parseInt(process.env.GATEWAY_TIMEOUT_MS || "10000", 10),
  gatewayMock: isTest || process.env.GATEWAY_MOCK === "1",

  logLevel: process.env.LOG_LEVEL || "info",
  appName: process.env.APP_NAME || "scrocco-web — Gateway LLM",

  bootstrapAdminEmail: process.env.BOOTSTRAP_ADMIN_EMAIL || "",
  bootstrapAdminPassword: process.env.BOOTSTRAP_ADMIN_PASSWORD || "",

  smtpHost: process.env.SMTP_HOST || "",
  smtpPort: parseInt(process.env.SMTP_PORT || "465", 10),
  smtpUser: process.env.SMTP_USER || "",
  smtpPass: process.env.SMTP_PASS || "",
  smtpFrom: process.env.EMAIL_FROM || process.env.SMTP_FROM || "",

  magicLinkBaseUrl: process.env.MAGIC_LINK_BASE_URL || "http://localhost:3000",
  magicLinkExpiryMs: 15 * 60 * 1000,

  telegramBotToken: process.env.TELEGRAM_BOT_TOKEN || "",
  alertPollerDisabled: process.env.ALERT_POLLER_DISABLED === "1",
  // cookie di sessione `Secure`: disaccoppiato da NODE_ENV perche' il pannello
  // gira in HTTP puro dietro la VPN. Metti COOKIE_SECURE=1 solo se c'e' TLS.
  cookieSecure: process.env.COOKIE_SECURE === "1",
};

function generateJwtSecret() {
  if (process.env.JWT_SECRET) return process.env.JWT_SECRET;
  // Generate a secure random JWT secret for development;
  // in production, set JWT_SECRET in the environment
  return crypto.randomBytes(64).toString("hex");
}