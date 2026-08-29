import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startTestApp, closeDb } from "./helpers.js";

let ctx;
before(async () => { ctx = await startTestApp(); });
after(async () => { ctx.server.close(); await closeDb(); });

test("GET /health -> 200 {ok:true} con gateway mock e DB up", async () => {
  const r = await fetch(`${ctx.base}/health`);
  assert.equal(r.status, 200);
  const j = await r.json();
  assert.equal(j.ok, true);
});
