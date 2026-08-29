import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { query } from "../src/db.js";
import config from "../src/config.js";
import { createPoller } from "../src/services/alert-poller.js";

let poller;
const calls = [];
let origTgToken;

before(async () => {
  origTgToken = config.telegramBotToken;
  config.telegramBotToken = "test-bot-token";   // stub: notify() prova l'invio Telegram
  const { rows } = await query(
    `CREATE TABLE IF NOT EXISTS alert_rules (
       id BIGSERIAL PRIMARY KEY,
       name VARCHAR(120) NOT NULL,
       enabled BOOLEAN NOT NULL DEFAULT TRUE,
       pool_filter VARCHAR(255),
       health_threshold_pct INTEGER NOT NULL DEFAULT 50,
       check_every_sec INTEGER NOT NULL DEFAULT 120,
       webhook_url VARCHAR(500),
       telegram_chat_id VARCHAR(64),
       notify_min_interval_sec INTEGER NOT NULL DEFAULT 900,
       last_notified_at TIMESTAMPTZ,
       created_by INTEGER,
       created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
     )`
  );
  poller = createPoller();
  globalThis.__origFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), opts: opts || {} });
    return { ok: true, status: 200 };
  };
});

after(async () => {
  globalThis.fetch = globalThis.__origFetch;
  config.telegramBotToken = origTgToken;
  await query("DELETE FROM alert_rules");
});

const makeRule = (overrides = {}) => ({
  id: 1,
  name: "test-mioaruba",
  enabled: true,
  pool_filter: "scrocco-llm-mioaruba",
  health_threshold_pct: 80,
  check_every_sec: 120,
  webhook_url: "https://example.com/hook",
  telegram_chat_id: "-100123456",
  notify_min_interval_sec: 900,
  last_notified_at: null,
  ...overrides,
});

test("checkAndNotify: leaderboard con error_rate alto -> notifica webhook+telegram e setta last_notified_at", async () => {
  const rule = makeRule();
  const res = await poller.checkAndNotify(rule);
  assert.equal(res.triggered, true, JSON.stringify(res));
  assert.ok(res.health_pct < rule.health_threshold_pct, `health ${res.health_pct} >= soglia`);

  const webhookCall = calls.find((c) => c.url === rule.webhook_url);
  assert.ok(webhookCall, "webhook non invocato");
  const body = JSON.parse(webhookCall.opts.body);
  assert.equal(body.health_pct, res.health_pct);
  assert.ok(Array.isArray(body.rows));
  assert.ok(body.rows.length > 0);
  assert.ok(body.timestamp);
  assert.ok(calls.some((c) => c.url.includes("api.telegram.org/bot")), "telegram non invocato");

  const { rows } = await query("SELECT last_notified_at FROM alert_rules WHERE id = $1", [rule.id]);
  if (rows.length) assert.ok(rows[0].last_notified_at, "last_notified_at non aggiornato");
});

test("checkAndNotify: cooldown non scaduto -> non notifica", async () => {
  calls.length = 0;
  const rule = makeRule({ last_notified_at: new Date() });
  const res = await poller.checkAndNotify(rule);
  assert.equal(res.triggered, false);
  assert.equal(res.reason, "cooldown");
  assert.equal(calls.length, 0, "fetch chiamato durante cooldown");
});

test("status: expose started, activeRules, lastRunAt", () => {
  const st = poller.status();
  assert.equal(typeof st.started, "boolean");
  assert.equal(typeof st.activeRules, "number");
});