import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startTestApp, createTestUser, closeDb } from "./helpers.js";

let ctx, cookie;
before(async () => {
  ctx = await startTestApp();
  const u = await createTestUser({ role: "viewer", password: "password123" });
  const r = await fetch(`${ctx.base}/api/auth/login`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: u.email, password: "password123" }),
  });
  assert.equal(r.status, 200);
  cookie = r.headers.get("set-cookie").split(";")[0];
});
after(async () => { ctx.server.close(); await closeDb(); });

const paths = [
  "/", "/deployments", "/deployments?profile=scrocco-llm-mioaruba",
  "/profiles", "/policy", "/capabilities", "/expiring?days=14",
  "/history", "/insights?days=30&group_by=day", "/bootstrap", "/guide",
];

for (const p of paths) {
  test(`GET ${p} -> 200 con cookie viewer`, async () => {
    const r = await fetch(`${ctx.base}${p}`, { headers: { cookie } });
    assert.equal(r.status, 200, `${p} ha dato ${r.status}`);
    const body = await r.text();
    assert.ok(body.length > 100, `${p} body troppo corto`);
  });
}
