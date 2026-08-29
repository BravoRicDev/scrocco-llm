import config from "../config.js";
import { query } from "../db.js";
import gateway from "./gateway.js";
import { logger } from "./logger.js";

const TELEGRAM_API = "https://api.telegram.org";
const WEBHOOK_TIMEOUT_MS = 5000;

function matchesFilter(rule, row) {
  const filter = String(rule.pool_filter ?? "").trim();
  if (!filter) return true;
  const hay = String(row.group ?? "") + " " + String(row.profile ?? "") + " " + String(row.dep ?? "");
  return hay.includes(filter);
}

function computeHealthPct(rows) {
  if (!rows.length) return 100;
  const totalErr = rows.reduce((acc, r) => acc + (Number(r.error_rate) || 0), 0);
  return 100 * (1 - totalErr / rows.length);
}

function cooldownPassed(rule, now = Date.now()) {
  if (!rule.last_notified_at) return true;
  const last = new Date(rule.last_notified_at).getTime();
  if (Number.isNaN(last)) return true;
  const intervalSec = parseInt(rule.notify_min_interval_sec, 10) || 900;
  return now - last >= intervalSec * 1000;
}

async function postWithTimeout(url, body, { headers = {} } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), WEBHOOK_TIMEOUT_MS);
  try {
    return await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
  }
}

function buildRows(rows) {
  return rows.map((r) => ({
    dep: String(r.dep ?? ""),
    error_rate: Number(r.error_rate) || 0,
    group: String(r.group ?? ""),
  }));
}

function buildTelegramText(rule, healthPct, rows) {
  const lines = [
    `[Scrocco] Pool non sano (${rule.name || "senza nome"})`,
    `Salute pool: ${healthPct.toFixed(1)}% (soglia ${rule.health_threshold_pct}%)`,
    `Righe: ${rows.length}`,
  ];
  rows.slice(0, 5).forEach((r) => {
    lines.push(`- ${r.dep || r.group || "?"} (err ${((Number(r.error_rate) || 0) * 100).toFixed(1)}%)`);
  });
  lines.push(`Quando: ${new Date().toISOString()}`);
  return lines.join("\n");
}

async function notify(rule, healthPct, rows) {
  const payloadRows = buildRows(rows);
  const timestamp = new Date().toISOString();

  if (rule.webhook_url) {
    try {
      await postWithTimeout(rule.webhook_url, {
        rule: { id: rule.id, name: rule.name, pool_filter: rule.pool_filter, health_threshold_pct: rule.health_threshold_pct },
        health_pct: healthPct,
        timestamp,
        rows: payloadRows,
      });
      logger.info("alert webhook inviato", { rule: rule.id, health_pct: healthPct });
    } catch (err) {
      logger.error(`alert webhook fallito: ${err.message}`, { rule: rule.id });
    }
  }

  if (rule.telegram_chat_id && config.telegramBotToken) {
    try {
      const url = `${TELEGRAM_API}/bot${config.telegramBotToken}/sendMessage`;
      await postWithTimeout(url, {
        chat_id: String(rule.telegram_chat_id),
        text: buildTelegramText(rule, healthPct, payloadRows),
      });
      logger.info("alert telegram inviato", { rule: rule.id });
    } catch (err) {
      logger.error(`alert telegram fallito: ${err.message}`, { rule: rule.id });
    }
  }
}

async function checkAndNotify(rule) {
  try {
    const data = await gateway.get("/admin/insights/leaderboard", {
      params: { window: "1h", sort: "error_rate", order: "desc" },
    });
    const allRows = (data && Array.isArray(data.rows)) ? data.rows : [];
    const matched = allRows.filter((r) => matchesFilter(rule, r));

    const healthPct = computeHealthPct(matched);
    const threshold = parseInt(rule.health_threshold_pct, 10) || 50;

    if (healthPct >= threshold) return { triggered: false, health_pct: healthPct, reason: "healthy" };
    if (!cooldownPassed(rule)) return { triggered: false, health_pct: healthPct, reason: "cooldown" };

    await notify(rule, healthPct, matched);

    await query("UPDATE alert_rules SET last_notified_at = NOW() WHERE id = $1", [rule.id]);

    return { triggered: true, health_pct: healthPct, rows: matched.length };
  } catch (err) {
    logger.error(`alert check fallito: ${err.message}`, { rule: rule && rule.id });
    return { triggered: false, health_pct: null, reason: "error" };
  }
}

export function createPoller({ run = null } = {}) {
  const timers = new Map();
  const state = {
    started: false,
    activeRules: 0,
    lastRunAt: null,
  };

  function record(rules) {
    state.activeRules = rules.length;
    state.lastRunAt = new Date().toISOString();
  }

  async function loadRules() {
    const { rows } = await query("SELECT * FROM alert_rules WHERE enabled = TRUE");
    return rows;
  }

  async function start() {
    if (state.started) return { stop };
    state.started = true;
    let rules = [];
    try {
      rules = await loadRules();
    } catch (err) {
      logger.error(`alert poller: lettura regole fallita: ${err.message}`);
    }
    if (run) {
      try { await run(rules, checkAndNotify); } catch (err) { logger.error(`alert poller run fallita: ${err.message}`); }
    }
    record(rules);
    for (const rule of rules) {
      const intervalMs = (parseInt(rule.check_every_sec, 10) || 120) * 1000;
      const timer = setInterval(() => {
        state.lastRunAt = new Date().toISOString();
        const res = checkAndNotify(rule);
        if (res && res.then) res.then(() => {}).catch((err) => logger.error(`alert poller: ${err.message}`));
      }, intervalMs);
      if (timer.unref) timer.unref();
      timers.set(rule.id, timer);
    }
    logger.info("alert poller avviato", { rules: rules.length });
    return { stop };
  }

  function stop() {
    for (const timer of timers.values()) clearInterval(timer);
    timers.clear();
    state.started = false;
    state.activeRules = 0;
  }

  function status() {
    return {
      started: state.started,
      activeRules: state.activeRules,
      lastRunAt: state.lastRunAt,
    };
  }

  return { start, stop, checkAndNotify, status };
}

export default createPoller;

export { checkAndNotify };
