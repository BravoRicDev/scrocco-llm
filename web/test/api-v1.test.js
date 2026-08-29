import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startTestApp, createTestUser, createTestApiToken, closeDb } from "./helpers.js";
import { query } from "../src/db.js";

let ctx, opTok, viewerTok, adminCookie, opCookie;

async function login(email) {
  const r = await fetch(`${ctx.base}/api/auth/login`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password: "password123" }),
  });
  assert.equal(r.status, 200);
  return r.headers.get("set-cookie").split(";")[0];
}

before(async () => {
  ctx = await startTestApp();
  const op = await createTestUser({ role: "operator", password: "password123" });
  const vw = await createTestUser({ role: "viewer", password: "password123" });
  const ad = await createTestUser({ role: "admin", password: "password123" });
  opTok = await createTestApiToken(op.id);
  viewerTok = await createTestApiToken(vw.id);
  opCookie = await login(op.email);
  adminCookie = await login(ad.email);
});
after(async () => { ctx.server.close(); await closeDb(); });

const G = (p, tok) => fetch(`${ctx.base}${p}`, { headers: { authorization: `Bearer ${tok}` } });
const W = (method, p, cookie, body) => fetch(`${ctx.base}${p}`, {
  method, headers: { cookie, "content-type": "application/json", origin: ctx.base },
  body: JSON.stringify(body || {}), redirect: "manual",
});

// ---- F5-01: read deployments/profiles ----
test("GET /api/v1/deployments con agtok_ -> 200 {count,deployments}", async () => {
  const r = await G("/api/v1/deployments", opTok);
  assert.equal(r.status, 200);
  const j = await r.json();
  assert.ok(Array.isArray(j.deployments) && typeof j.count === "number");
});

test("GET /api/v1/deployments senza token -> 401", async () => {
  const r = await fetch(`${ctx.base}/api/v1/deployments`);
  assert.equal(r.status, 401);
});

test("GET /api/v1/profiles viewer -> 200", async () => {
  const r = await G("/api/v1/profiles", viewerTok);
  assert.equal(r.status, 200);
  assert.ok("profiles" in (await r.json()));
});

test("GET /api/v1/deployments/expiring -> 200", async () => {
  assert.equal((await G("/api/v1/deployments/expiring?days=14", opTok)).status, 200);
});

// ---- F5-02: read core ----
test("GET /api/v1/policy -> 200 senza chiavi in chiaro", async () => {
  const r = await G("/api/v1/policy", opTok);
  assert.equal(r.status, 200);
  const j = await r.json();
  assert.ok(!("configured" in j), "non deve inoltrare `configured` grezzo");
  if (j.effective) {
    assert.ok(!("alias_keys" in j.effective), "alias_keys in chiaro!");
  }
});

for (const p of ["/api/v1/state", "/api/v1/history", "/api/v1/insights",
  "/api/v1/insights/summary", "/api/v1/insights/leaderboard",
  "/api/v1/bootstrap", "/api/v1/bootstrap/status", "/api/v1/bootstrap/providers", "/api/v1/guide"]) {
  test(`GET ${p} -> 200`, async () => {
    const r = await G(p, opTok);
    assert.equal(r.status, 200, `${p} -> ${r.status}`);
  });
}

// ---- F5-03: write ----
test("POST /api/v1/deployments operator -> 201 + presente", async () => {
  const r = await W("POST", "/api/v1/deployments", opCookie, {
    profile: "apitest", modello: "m/x", provider: "openrouter", endpoint: "https://e",
    data: "free", key: "sk-apitest", context: 200, priority: 0,
  });
  assert.equal(r.status, 201, await r.text());
  const list = await G("/api/v1/deployments", opTok);
  assert.match(await list.text(), /m\/x/);
});

test("POST /api/v1/deployments/bulk -> 200", async () => {
  const r = await W("POST", "/api/v1/deployments/bulk", opCookie, {
    operations: [{ action: "create", profile: "b", modello: "b/1", endpoint: "https://e", data: "free", key: "k", context: 8 }],
  });
  assert.ok([200, 201].includes(r.status), await r.text());
});

test("POST /api/v1/system/reload operator -> 200", async () => {
  assert.equal((await W("POST", "/api/v1/system/reload", opCookie)).status, 200);
});

test("PATCH /api/v1/policy operator -> 403, admin -> 200", async () => {
  assert.equal((await W("PATCH", "/api/v1/policy", opCookie, { step_up_pct: 30 })).status, 403);
  const r = await W("PATCH", "/api/v1/policy", adminCookie, { step_up_pct: 30 });
  assert.equal(r.status, 200, await r.text());
});

test("audit_log popolato dalle write api", async () => {
  const { rows } = await query("SELECT COUNT(*)::int AS n FROM audit_log WHERE op LIKE 'api.%'");
  assert.ok(rows[0].n > 0);
});
