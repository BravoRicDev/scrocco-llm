import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startTestApp, createTestUser, closeDb } from "./helpers.js";
import { query } from "../src/db.js";

let ctx, opCookie, adminCookie;

async function login(email, pw) {
  const r = await fetch(`${ctx.base}/api/auth/login`, {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password: pw }),
  });
  assert.equal(r.status, 200, `login ${email} -> ${r.status}`);
  return r.headers.get("set-cookie").split(";")[0];
}

before(async () => {
  ctx = await startTestApp();
  const op = await createTestUser({ role: "operator", password: "password123" });
  const ad = await createTestUser({ role: "admin", password: "password123" });
  opCookie = await login(op.email, "password123");
  adminCookie = await login(ad.email, "password123");
});
after(async () => { ctx.server.close(); await closeDb(); });

const jpost = (p, cookie, body) => fetch(`${ctx.base}${p}`, {
  method: "POST",
  headers: { cookie, "content-type": "application/json", origin: ctx.base },
  body: JSON.stringify(body || {}), redirect: "manual",
});

for (const p of ["/playground", "/csv-editor", "/policy-raw", "/config-history", "/key-health", "/sticky"]) {
  test(`GET ${p} -> 200 (operator/admin)`, async () => {
    const cookie = (p === "/policy-raw") ? adminCookie : opCookie;
    const r = await fetch(`${ctx.base}${p}`, { headers: { cookie } });
    assert.equal(r.status, 200, `${p} -> ${r.status}`);
    assert.ok((await r.text()).length > 100);
  });
}

test("GET /alerts -> 200 admin, 403 operator", async () => {
  assert.equal((await fetch(`${ctx.base}/alerts`, { headers: { cookie: adminCookie } })).status, 200);
  assert.equal((await fetch(`${ctx.base}/alerts`, { headers: { cookie: opCookie } })).status, 403);
});

test("POST /playground/run -> 200 con content + trace", async () => {
  const r = await jpost("/playground/run", opCookie, { model: "scrocco-llm-mioaruba", prompt: "ciao" });
  const j = await r.json();
  assert.equal(r.status, 200, JSON.stringify(j));
  assert.ok("content" in j && Array.isArray(j.trace));
});

test("POST /api/csv-editor/save valido -> ok + snapshot in config_snapshots", async () => {
  const before = (await query("SELECT COUNT(*)::int AS n FROM config_snapshots WHERE kind='csv'")).rows[0].n;
  // raw unico per run: evita il dedup-by-sha di createSnapshot
  const raw = `# ${Date.now()}\ncommento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-x,caps\n` +
              "t,m/a,groq,https://a/v1,free,200,8000,0,K,\n";
  const r = await jpost("/api/csv-editor/save", adminCookie, { raw });
  const j = await r.json();
  assert.equal(r.status, 200, JSON.stringify(j));
  assert.equal(j.ok, true);
  // il hook e' fire-and-forget su res.on('finish'): piccola attesa
  await new Promise((res) => setTimeout(res, 300));
  const after = (await query("SELECT COUNT(*)::int AS n FROM config_snapshots WHERE kind='csv'")).rows[0].n;
  assert.ok(after >= before + 1, `snapshot non creato (${before} -> ${after})`);
});

test("viewer non puo' salvare csv (403)", async () => {
  const vw = await createTestUser({ role: "viewer", password: "password123" });
  const c = await login(vw.email, "password123");
  const r = await jpost("/api/csv-editor/save", c, { raw: "x\n" });
  assert.equal(r.status, 403);
});
