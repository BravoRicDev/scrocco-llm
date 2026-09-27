import "./helpers.js";
import { test } from "node:test";
import assert from "node:assert/strict";
import { query } from "../src/db.js";
import { healthCheck } from "../src/db.js";

test("db: errori SQL ripetuti non aprono il circuit breaker", async () => {
  for (let i = 0; i < 8; i++) {
    await assert.rejects(query("SELECT * FROM tabella_che_non_esiste_xyz"));
  }
  const res = await query("SELECT 1 AS ok");
  assert.equal(res.rows[0].ok, 1);
  const h = await healthCheck();
  assert.equal(h.circuitBreaker.state, "closed");
});
