import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startTestApp, createTestUser, closeDb } from "./helpers.js";

let ctx, cookie;
before(async () => {
  ctx = await startTestApp();
  const u = await createTestUser({ role: "viewer", password: "password123" });
  const r = await fetch(`${ctx.base}/api/auth/login`, { method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email: u.email, password: "password123" }) });
  assert.equal(r.status, 200);
  cookie = r.headers.get("set-cookie").split(";")[0];
});
after(async () => { ctx.server.close(); await closeDb(); });

const pages = ["/observability/live", "/observability/errors", "/observability/leaderboard", "/observability/charts"];
for (const p of pages) {
  test(`GET ${p} -> 200`, async () => {
    const r = await fetch(`${ctx.base}${p}`, { headers: { cookie } });
    assert.equal(r.status, 200, `${p} -> ${r.status}`);
    assert.ok((await r.text()).length > 100);
  });
}

const apis = [
  ["/api/live/events", "events"],
  ["/api/errors/events", "events"],
  ["/api/leaderboard/data", "rows"],
  ["/api/charts/data", null],
];
for (const [p, key] of apis) {
  test(`GET ${p} -> 200 JSON`, async () => {
    const r = await fetch(`${ctx.base}${p}`, { headers: { cookie } });
    assert.equal(r.status, 200, `${p} -> ${r.status}`);
    assert.match(r.headers.get("content-type") || "", /application\/json/);
    const j = await r.json();
    if (key) assert.ok(key in j, `${p}: manca ${key}`);
  });
}
