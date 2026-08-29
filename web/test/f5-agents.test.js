import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startTestApp, createTestUser, createTestApiToken, closeDb } from "./helpers.js";
import { query } from "../src/db.js";

let ctx, agentTok, viewerTok, adminCookie, opCookie, viewerCookie;

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
  agentTok = await createTestApiToken(op.id);
  viewerTok = await createTestApiToken(vw.id);
  opCookie = await login(op.email);
  viewerCookie = await login(vw.email);
  adminCookie = await login(ad.email);
});
after(async () => { ctx.server.close(); await closeDb(); });

// ---- F5-05: OpenAPI ----
test("GET /api/v1/openapi.json -> 200, paths + schemas", async () => {
  const r = await fetch(`${ctx.base}/api/v1/openapi.json`);
  assert.equal(r.status, 200);
  const spec = await r.json();
  assert.equal(spec.openapi, "3.0.0");
  assert.ok(spec.paths["/api/v1/deployments"], "manca /api/v1/deployments");
  assert.ok(spec.paths["/api/v1/deployments/{id}"], "manca il path con {id}");
  assert.ok(Object.keys(spec.components.schemas).length >= 2);
});

test("GET /api/v1/docs -> 200 html", async () => {
  const r = await fetch(`${ctx.base}/api/v1/docs`);
  assert.equal(r.status, 200);
  assert.match(await r.text(), /openapi-render\.js/);
});

// ---- F5-04: MCP ----
const mcpReq = (cookieOrTok, body, isTok) => fetch(`${ctx.base}/api/mcp`, {
  method: "POST",
  headers: {
    "content-type": "application/json",
    accept: "application/json, text/event-stream",
    ...(isTok ? { authorization: `Bearer ${cookieOrTok}` } : { cookie: cookieOrTok }),
  },
  body: JSON.stringify(body),
});

async function mcpCall(tok, method, params, id) {
  const r = await mcpReq(tok, { jsonrpc: "2.0", id: id ?? 1, method, params: params || {} }, true);
  const ct = r.headers.get("content-type") || "";
  let payload;
  const text = await r.text();
  if (ct.includes("text/event-stream")) {
    // estrai l'ultima riga `data: {...}`
    const lines = text.split("\n").filter((l) => l.startsWith("data:"));
    payload = JSON.parse(lines[lines.length - 1].slice(5).trim());
  } else {
    payload = JSON.parse(text);
  }
  return { status: r.status, payload };
}

test("POST /api/mcp senza token agente -> 403", async () => {
  const r = await mcpReq(viewerCookie, { jsonrpc: "2.0", id: 1, method: "tools/list", params: {} }, false);
  assert.equal(r.status, 403);
});

test("MCP initialize + tools/list -> contiene deploy_list", async () => {
  const init = await mcpCall(agentTok, "initialize", {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "test", version: "1" },
  }, 1);
  assert.ok([200].includes(init.status), `initialize -> ${init.status} ${JSON.stringify(init.payload)}`);
  const list = await mcpCall(agentTok, "tools/list", {}, 2);
  assert.equal(list.status, 200, JSON.stringify(list.payload));
  const names = (list.payload.result?.tools || []).map((t) => t.name);
  assert.ok(names.includes("deploy_list"), `tools: ${names.join(",")}`);
});

test("MCP tools/call deploy_list -> dati JSON", async () => {
  await mcpCall(agentTok, "initialize", { protocolVersion: "2024-11-05", capabilities: {}, clientInfo: { name: "t", version: "1" } }, 1);
  const call = await mcpCall(agentTok, "tools/call", { name: "deploy_list", arguments: {} }, 3);
  assert.equal(call.status, 200, JSON.stringify(call.payload));
  const content = call.payload.result?.content?.[0]?.text || "";
  assert.match(content, /deployments/);
});

// ---- F5-07: audit panel + agent guide ----
test("GET /admin/audit -> 200 admin, 403 viewer/operator", async () => {
  assert.equal((await fetch(`${ctx.base}/admin/audit`, { headers: { cookie: adminCookie } })).status, 200);
  assert.equal((await fetch(`${ctx.base}/admin/audit`, { headers: { cookie: viewerCookie } })).status, 403);
  assert.equal((await fetch(`${ctx.base}/admin/audit`, { headers: { cookie: opCookie } })).status, 403);
});

test("GET /agent-guide -> 200 markdown", async () => {
  const r = await fetch(`${ctx.base}/agent-guide`, { headers: { cookie: viewerCookie } });
  assert.equal(r.status, 200);
  assert.match(r.headers.get("content-type") || "", /markdown/);
  assert.match(await r.text(), /Bearer/);
});

// ---- e2e: API create -> audit row ----
test("e2e: create via API -> presente in lista + audit", async () => {
  const before = (await query("SELECT COUNT(*)::int AS n FROM audit_log")).rows[0].n;
  const c = await fetch(`${ctx.base}/api/v1/deployments`, {
    method: "POST",
    headers: { authorization: `Bearer ${agentTok}`, "content-type": "application/json", origin: ctx.base },
    body: JSON.stringify({ profile: "e2e", modello: "e2e/model", endpoint: "https://e", data: "free", key: "sk-e2e", context: 128 }),
  });
  assert.equal(c.status, 201, await c.text());
  const list = await fetch(`${ctx.base}/api/v1/deployments`, { headers: { authorization: `Bearer ${agentTok}` } });
  assert.match(await list.text(), /e2e\/model/);
  const after = (await query("SELECT COUNT(*)::int AS n FROM audit_log")).rows[0].n;
  assert.ok(after > before);
});
