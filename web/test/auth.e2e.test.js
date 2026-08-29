import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startTestApp, createTestUser, closeDb } from "./helpers.js";

let ctx, user;
before(async () => { ctx = await startTestApp(); user = await createTestUser({ role: "admin", password: "password123" }); });
after(async () => { ctx.server.close(); await closeDb(); });

test("login corretto -> 200 + cookie token; /api/auth/me lo accetta", async () => {
  const r = await fetch(`${ctx.base}/api/auth/login`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: user.email, password: "password123" }),
  });
  assert.equal(r.status, 200);
  const cookie = r.headers.get("set-cookie");
  assert.ok(cookie && /token=/.test(cookie));
  const me = await fetch(`${ctx.base}/api/auth/me`, { headers: { cookie: cookie.split(";")[0] } });
  assert.equal(me.status, 200);
  const mj = await me.json();
  assert.equal(mj.user.email, user.email);
});

test("login password errata -> 401", async () => {
  const r = await fetch(`${ctx.base}/api/auth/login`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: user.email, password: "sbagliata" }),
  });
  assert.equal(r.status, 401);
});

test("GET / senza cookie -> redirect /login", async () => {
  const r = await fetch(`${ctx.base}/`, { redirect: "manual" });
  assert.equal(r.status, 302);
  assert.equal(r.headers.get("location"), "/login");
});

test("GET /api/nope -> 404 JSON", async () => {
  const r = await fetch(`${ctx.base}/api/nope`);
  assert.equal(r.status, 404);
});
