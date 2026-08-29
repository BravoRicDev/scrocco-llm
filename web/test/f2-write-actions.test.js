import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startTestApp, createTestUser, closeDb } from "./helpers.js";
import { query } from "../src/db.js";

let ctx, opCookie, viewerCookie, adminCookie;
async function login(base, email, pw) {
  const r = await fetch(`${base}/api/auth/login`, { method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password: pw }) });
  assert.equal(r.status, 200, `login ${email} -> ${r.status}`);
  return r.headers.get("set-cookie").split(";")[0];
}
before(async () => {
  ctx = await startTestApp();
  const op = await createTestUser({ role: "operator", password: "password123" });
  const vw = await createTestUser({ role: "viewer", password: "password123" });
  const ad = await createTestUser({ role: "admin", password: "password123" });
  opCookie = await login(ctx.base, op.email, "password123");
  viewerCookie = await login(ctx.base, vw.email, "password123");
  adminCookie = await login(ctx.base, ad.email, "password123");
});
after(async () => { ctx.server.close(); await closeDb(); });

const form = (obj) => {
  const b = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) b.set(k, v);
  return b;
};
// Origin uguale all'host: i browser lo mandano sempre su POST, la CSRF guard lo richiede.
const post = (p, cookie, body) => fetch(`${ctx.base}${p}`, { method: "POST",
  headers: { cookie, "content-type": "application/x-www-form-urlencoded", origin: ctx.base },
  body: body instanceof URLSearchParams ? body : form(body || {}), redirect: "manual" });

test("operator: create deployment -> redirect e compare in lista", async () => {
  const r = await post("/deployments", opCookie, {
    profile: "smoke", modello: "m/x", provider: "openrouter",
    endpoint: "https://e", data: "free", key: "sk-smoke", context: "200", priority: "0",
  });
  assert.ok([302, 303].includes(r.status), `create -> ${r.status}`);
  const list = await fetch(`${ctx.base}/deployments`, { headers: { cookie: opCookie } });
  assert.equal(list.status, 200);
  assert.match(await list.text(), /m\/x/);
});

test("operator: reload di sistema -> redirect", async () => {
  const r = await post("/system/reload", opCookie, {});
  assert.ok([302, 303].includes(r.status), `reload -> ${r.status}`);
});

test("viewer: POST /deployments -> 403", async () => {
  const r = await post("/deployments", viewerCookie, {
    profile: "x", modello: "m", provider: "p", endpoint: "https://e",
    data: "free", key: "k", context: "8",
  });
  assert.equal(r.status, 403);
});

test("users: admin 200, viewer/operator 403", async () => {
  assert.equal((await fetch(`${ctx.base}/users`, { headers: { cookie: adminCookie } })).status, 200);
  assert.equal((await fetch(`${ctx.base}/users`, { headers: { cookie: viewerCookie } })).status, 403);
  assert.equal((await fetch(`${ctx.base}/users`, { headers: { cookie: opCookie } })).status, 403);
});

test("audit_log ha righe per le mutazioni", async () => {
  const { rows } = await query("SELECT op FROM audit_log ORDER BY id DESC LIMIT 20");
  assert.ok(rows.some((r) => /deployment/i.test(r.op)), "manca audit per deployment");
});
