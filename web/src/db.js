import pg from "pg";
import config from "./config.js";
import { logger } from "./services/logger.js";

if (!config.databaseUrl) {
  throw new Error("DATABASE_URL non configurata");
}

/* ------------------------------------------------------------------ */
/* Circuit breaker pattern                                              */
/* ------------------------------------------------------------------ */

const circuitBreaker = {
  failures: 0,
  lastFailure: 0,
  state: "closed", // closed | open | half-open
  threshold: 5,
  resetTimeoutMs: 60000, // after this long in open state -> half-open

  recordFailure() {
    this.failures++;
    this.lastFailure = Date.now();
    if (this.failures >= this.threshold && this.state !== "open") {
      this.state = "open";
      logger.warn("Database circuit breaker OPENED", { failures: this.failures });
    }
  },

  recordSuccess() {
    this.failures = 0;
    this.state = "closed";
  },

  canExecute() {
    if (this.state === "closed") return true;
    if (this.state === "open") {
      if (Date.now() - this.lastFailure > this.resetTimeoutMs) {
        this.state = "half-open";
        logger.info("Database circuit breaker HALF-OPEN (tentativo di recupero)");
        return true;
      }
      return false;
    }
    return true; // half-open: allow single probe request
  },

  getState() {
    return {
      state: this.state,
      failures: this.failures,
      lastFailureAt: this.lastFailure,
    };
  },
};

/* ------------------------------------------------------------------ */
/* Retry utility                                                        */
/* ------------------------------------------------------------------ */

const MAX_RETRIES = 3;
const RETRY_BASE_DELAY = 100;
const RETRY_MAX_DELAY = 2000;

function isRetryableError(err) {
  if (!err) return false;
  const retryableCodes = [
    "ECONNREFUSED", "ETIMEDOUT", "ENOTFOUND", "ENETUNREACH",
    "EHOSTUNREACH", "ECONNRESET",
    "57P01", "57P02", "57P03", "08006", "08001",
  ];
  if (err.code && retryableCodes.includes(err.code)) return true;
  return /connection.*refused|connection.*reset|timeout|ECONNREFUSED|ETIMEDOUT|ENOTFOUND/i.test(
    err.message || ""
  );
}

async function withRetry(fn, context = "") {
  let lastError;
  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      if (!isRetryableError(err) || attempt === MAX_RETRIES) {
        throw err;
      }
      const delay = Math.min(RETRY_BASE_DELAY * Math.pow(2, attempt - 1), RETRY_MAX_DELAY);
      const jitter = Math.floor(Math.random() * 50);
      await new Promise((r) => setTimeout(r, delay + jitter));
      logger.warn(`DB retry ${attempt}/${MAX_RETRIES}: ${context}`, {
        error: err.code || err.message,
        retryInMs: delay + jitter,
      });
    }
  }
  throw lastError;
}

/* ------------------------------------------------------------------ */
/* Pool (defined before query uses it)                                  */
/* ------------------------------------------------------------------ */

const pool = new pg.Pool({
  connectionString: config.databaseUrl,
  max: 20,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 5000,
  // FIX: SSL solo se richiesto esplicitamente via PGSSL=1. Prima veniva forzato
  // da NODE_ENV=production, il che rompeva la connessione al postgres del
  // compose (locale, senza SSL) con "The server does not support SSL connections".
  ssl: process.env.PGSSL === "1" ? { rejectUnauthorized: false } : false,
});

pool.on("error", (err) => {
  logger.error("Unexpected error on idle client", { error: err.message, code: err.code });
  circuitBreaker.recordFailure();
});

pool.on("connect", () => {
  logger.debug("New database connection established");
});

/* ------------------------------------------------------------------ */
/* Query / client wrappers with circuit breaker + retry                  */
/* ------------------------------------------------------------------ */

export async function query(text, params, context = "") {
  if (!circuitBreaker.canExecute()) {
    const state = circuitBreaker.getState();
    throw new Error(`Database circuit breaker is ${state.state}: ${state.failures} failures`);
  }
  try {
    const result = await withRetry(
      () => pool.query(text, params),
      context || `query ${text.slice(0, 50)}`
    );
    circuitBreaker.recordSuccess();
    return result;
  } catch (err) {
    circuitBreaker.recordFailure();
    throw err;
  }
}

export async function getClient() {
  if (!circuitBreaker.canExecute()) {
    const state = circuitBreaker.getState();
    throw new Error(`Database circuit breaker is ${state.state}`);
  }
  return withRetry(() => pool.connect(), "getClient");
}

/* ------------------------------------------------------------------ */
/* Health checks                                                         */
/* ------------------------------------------------------------------ */

export async function healthCheck() {
  try {
    const start = Date.now();
    const result = await withRetry(
      () => pool.query("SELECT 1 AS health, NOW() AS ts"),
      "healthCheck"
    );
    const row = result.rows[0];
    return {
      healthy: true,
      latency_ms: Date.now() - start,
      timestamp: row?.ts || new Date().toISOString(),
      circuitBreaker: circuitBreaker.getState(),
      pool: { total: pool.totalCount, idle: pool.idleCount, waiting: pool.waitingCount },
    };
  } catch (err) {
    return {
      healthy: false,
      error: err.message,
      circuitBreaker: circuitBreaker.getState(),
      pool: { total: pool.totalCount, idle: pool.idleCount, waiting: pool.waitingCount },
    };
  }
}

export function getPoolStats() {
  return {
    total: pool.totalCount,
    idle: pool.idleCount,
    waiting: pool.waitingCount,
  };
}

export default pool;

export { circuitBreaker, withRetry };