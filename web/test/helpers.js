process.env.ALERT_POLLER_DISABLED = "1";
process.env.GATEWAY_MOCK = "1";
process.env.GATEWAY_URL = "http://127.0.0.1:1";
process.env.NODE_ENV = "test";
process.env.JWT_SECRET = process.env.JWT_SECRET || "test-secret-f0";
process.env.SMTP_HOST = "";
process.env.DATABASE_URL = process.env.DATABASE_URL || "postgres://postgres:testpass@localhost:15999/scrocco_web_test";

import crypto from "node:crypto";
import { query } from "../src/db.js";
import { hashPassword } from "../src/services/password.js";
import { createApiToken } from "../src/services/api-tokens.js";

export const uniqueEmail = (p = "u") => `${p}+${Date.now()}_${crypto.randomBytes(4).toString("hex")}@test.local`;

export async function createTestUser({ role = "admin", password = "password123", status = "active" } = {}) {
  const password_hash = await hashPassword(password);
  const email = uniqueEmail(role);
  const result = await query(
    "INSERT INTO users (email, name, password_hash, role, status) VALUES ($1,$2,$3,$4,$5) RETURNING id, email, role",
    [email, email, password_hash, role, status]
  );
  const row = result.rows[0];
  return { id: row.id, email: row.email, role: row.role, password };
}

export async function createTestApiToken(userId) {
  const { token } = await createApiToken(userId, "test", 30);
  return token;   // stringa raw agtok_... da usare come Bearer
}

export async function startTestApp() {
  const { createApp } = await import("../src/index.js");
  const app = await createApp();
  const server = app.listen(0);
  await new Promise((r) => server.once("listening", r));
  const port = server.address().port;
  // il server MCP fa fetch loopback su config.port: allinealo alla porta reale
  const { default: config } = await import("../src/config.js");
  config.port = port;
  return { app, server, port, base: `http://127.0.0.1:${port}` };
}

export async function closeDb() {
  const db = await import("../src/db.js");
  await db.default?.end?.();
}
