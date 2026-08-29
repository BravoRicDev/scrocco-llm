import { readFileSync } from "node:fs";
import { z } from "zod";
import config from "../config.js";
import { logger } from "./logger.js";

export class GatewayError extends Error {
  constructor(status, message) {
    super(message || `Gateway error (HTTP ${status})`);
    this.name = "GatewayError";
    this.status = status;
  }
}

const truncate = (s, n = 300) => {
  if (typeof s !== "string") return "";
  return s.length > n ? s.slice(0, n) : s;
};

const deepClone = (o) => (o === undefined ? undefined : JSON.parse(JSON.stringify(o)));
const isoNow = () => new Date().toISOString();
const stamp = () => new Date().toISOString().replace(/\D/g, "").slice(0, 14);

function maskKey(key) {
  const s = String(key ?? "");
  if (s.length <= 10) return s;
  return s.slice(0, 6) + "...********..." + s.slice(-4);
}

function randHash(len = 8) {
  const chars = "abcdef0123456789";
  let out = "";
  for (let i = 0; i < len; i++) out += chars[Math.floor(Math.random() * chars.length)];
  return out;
}

function deriveProvider(endpoint) {
  const e = String(endpoint || "").toLowerCase();
  if (e.includes("openrouter")) return "openrouter";
  if (e.includes("azure")) return "azure";
  return "generic";
}

function fail(status, message) {
  throw new GatewayError(status, message);
}

function parseCsv(raw) {
  const lines = String(raw || "")
    .trim()
    .split(/\r?\n/)
    .filter((l) => l.trim() !== "");
  if (!lines.length) return null;
  const split = (l) => l.split(",").map((c) => c.trim());
  const header = split(lines[0]);
  if (!header.length) return null;
  return { header, rows: lines.slice(1).map(split) };
}

/* ------------------------------------------------------------------ */
/* Mock in-memory gateway (GATEWAY_MOCK=1)                             */
/* ------------------------------------------------------------------ */

let mock = null;
function getMock() {
  if (mock) return mock;
  const fixtureUrl = new URL("../../test/fixtures/gateway.json", import.meta.url);
  const seed = JSON.parse(readFileSync(fixtureUrl, "utf8"));
  mock = {
    deployments: deepClone(seed.deployments || []),
    history: deepClone(seed.history || []),
    cooldowns_active: deepClone(seed.cooldowns_active || []),
    sticky_sessions: deepClone(seed.sticky_sessions || []),
    backups: deepClone(seed.backups || []),
    guide: seed.guide,
    profiles: deepClone(seed.profiles),
    adminState: deepClone(seed.adminState),
    policy: deepClone(seed.policy),
    insights: deepClone(seed.insights),
    insightsSummary: deepClone(seed.insightsSummary),
    leaderboard: deepClone(seed.leaderboard),
    logsCalls: deepClone(seed.logsCalls),
    logsErrors: deepClone(seed.logsErrors),
    healthz: deepClone(seed.healthz),
    models: deepClone(seed.models),
    bootstrap: deepClone(seed.bootstrap),
    bootstrapStatus: deepClone(seed.bootstrapStatus),
    bootstrapProviders: deepClone(seed.bootstrapProviders),
    csv: deepClone(seed.csv),
    policyRaw: seed.policyRaw,
    capProposals: deepClone(seed.capProposals || []),
    capAudit: deepClone(seed.capAudit),
  };
  return mock;
}

function findDep(s, hash) {
  return s.deployments.find((d) => d.id === hash || d.id.endsWith(`:${hash}`));
}

/* ------------------------------------------------------------------ */
/* Zod schemas for input validation                                    */
/* ------------------------------------------------------------------ */

const createDeploymentSchema = z.object({
  profile: z.string().min(1).max(64),
  modello: z.string().min(1).max(128),
  endpoint: z.string().min(1).max(512).refine((u) => /^https?:\/\//.test(u), {
    message: "endpoint deve essere un URL http(s) valido",
  }),
  data: z.string().min(1).max(64),
  key: z.string().min(1).max(1024),
  context: z.union([z.coerce.number().int().min(0), z.string().min(1)]).optional(),
  priority: z.coerce.number().int().min(-100).max(100).optional(),
  group: z.string().max(255).optional(),
  category: z.string().max(64).optional(),
  capabilities: z.array(z.string().max(64)).optional(),
  caps: z.array(z.string().max(64)).optional(),
  cap_groups: z.array(z.string().max(64)).optional(),
  provider: z.string().max(64).optional(),
}).strict();

const updateDeploymentSchema = z.object({
  profile: z.string().min(1).max(64).optional(),
  modello: z.string().min(1).max(128).optional(),
  endpoint: z.string().min(1).max(512).refine((u) => /^https?:\/\//.test(u), {
    message: "endpoint deve essere un URL http(s) valido",
  }).optional(),
  data: z.string().min(1).max(64).optional(),
  key: z.string().min(1).max(1024).optional(),
  context: z.union([z.coerce.number().int().min(0), z.string().min(1)]).optional(),
  priority: z.coerce.number().int().min(-100).max(100).optional(),
  group: z.string().max(255).optional(),
  category: z.string().max(64).optional(),
  capabilities: z.array(z.string().max(64)).optional(),
  caps: z.array(z.string().max(64)).optional(),
  cap_groups: z.array(z.string().max(64)).optional(),
  provider: z.string().max(64).optional(),
}).strict();

const bulkOpSchema = z.discriminatedUnion("action", [
  z.object({ action: z.literal("create"), profile: z.string().min(1).max(64), modello: z.string().min(1).max(128), endpoint: z.string().min(1).max(512).refine((u) => /^https?:\/\//.test(u), { message: "endpoint deve essere un URL http(s) valido" }), data: z.string().min(1).max(64), key: z.string().min(1).max(1024), context: z.union([z.coerce.number().int().min(0), z.string().min(1)]).optional(), priority: z.coerce.number().int().min(-100).max(100).optional(), group: z.string().max(255).optional(), category: z.string().max(64).optional(), capabilities: z.array(z.string().max(64)).optional(), caps: z.array(z.string().max(64)).optional(), cap_groups: z.array(z.string().max(64)).optional(), provider: z.string().max(64).optional() }).strict(),
  z.object({ action: z.literal("update"), id: z.string().min(1), profile: z.string().min(1).max(64).optional(), modello: z.string().min(1).max(128).optional(), endpoint: z.string().min(1).max(512).refine((u) => /^https?:\/\//.test(u), { message: "endpoint deve essere un URL http(s) valido" }).optional(), data: z.string().min(1).max(64).optional(), key: z.string().min(1).max(1024).optional(), context: z.union([z.coerce.number().int().min(0), z.string().min(1)]).optional(), priority: z.coerce.number().int().min(-100).max(100).optional(), group: z.string().max(255).optional(), category: z.string().max(64).optional(), capabilities: z.array(z.string().max(64)).optional(), caps: z.array(z.string().max(64)).optional(), cap_groups: z.array(z.string().max(64)).optional(), provider: z.string().max(64).optional() }).strict(),
  z.object({ action: z.literal("delete"), id: z.string().min(1) }).strict(),
  z.object({ action: z.literal("unretire"), id: z.string().min(1) }).strict(),
]);

const purgeSchema = z.object({ profile: z.string().min(1).max(64) }).strict();


function applyCreate(s, body, pushHistory = true) {
  const parsed = createDeploymentSchema.safeParse(body);
  if (!parsed.success) {
    const issues = parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ");
    fail(400, `validazione fallita: ${issues}`);
  }
  const data = parsed.data;
  const provider = data.provider || deriveProvider(data.endpoint);
  const id = `${data.profile}:${provider}:${randHash()}`;
  const context_k = data.context ? Number(data.context) : undefined;
  const dep = {
    id,
    profile: data.profile,
    modello: data.modello,
    provider,
    endpoint: data.endpoint,
    data: data.data,
    category: data.category || String(data.data).toLowerCase() || "free",
    context_k,
    max_input: data.max_input ?? context_k * 1000,
    priority: data.priority ?? 0,
    key_masked: maskKey(data.key),
    group: data.group || `${data.profile}-${context_k}k`,
    capabilities: data.capabilities || ["text"],
    caps: data.caps || [],
    cap_groups: data.cap_groups || [],
  };
  s.deployments.push(dep);
  if (pushHistory) s.history.push({ ts: isoNow(), op: "create", id, profile: data.profile });
  return dep;
}

function applyUpdate(s, dep, body) {
  const parsed = updateDeploymentSchema.safeParse(body);
  if (!parsed.success) {
    const issues = parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ");
    fail(400, `validazione fallita: ${issues}`);
  }
  const data = parsed.data;
  if (data.profile !== undefined) dep.profile = data.profile;
  if (data.modello !== undefined) dep.modello = data.modello;
  if (data.endpoint !== undefined) dep.endpoint = data.endpoint;
  if (data.data !== undefined) dep.data = data.data;
  if (data.category !== undefined) dep.category = data.category;
  if (data.priority !== undefined) dep.priority = data.priority;
  if (data.context !== undefined) {
    const c = Number(data.context);
    if (Number.isInteger(c) && c >= 0) {
      dep.context_k = c;
      dep.group = `${dep.profile}-${c}k`;
    }
  }
  if (data.key !== undefined && String(data.key).trim() !== "") dep.key_masked = maskKey(data.key);
  if (data.capabilities !== undefined) dep.capabilities = data.capabilities;
  if (data.caps !== undefined) dep.caps = data.caps;
  if (data.cap_groups !== undefined) dep.cap_groups = data.cap_groups;
  return dep;
}

function validateBulkOp(s, op) {
  if (!op || typeof op.action !== "string") fail(400, "azione non valida");
  const parsed = bulkOpSchema.safeParse(op);
  if (!parsed.success) {
    const issues = parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ");
    fail(400, `validazione fallita: ${issues}`);
  }
  const { action, id, ...rest } = parsed.data;
  if (action === "create") {
    applyCreate(s, rest, false);
  } else if (action === "update") {
    if (!id) fail(400, "id mancante per update");
    const dep = findDep(s, id);
    if (!dep) fail(404, `deployment non trovato: ${id}`);
    applyUpdate(s, dep, rest);
  } else if (action === "delete") {
    if (!id) fail(400, "id mancante per delete");
    if (!findDep(s, id)) fail(404, `deployment non trovato: ${id}`);
  } else if (action === "unretire") {
    if (!id) fail(400, "id mancante per unretire");
    const dep = findDep(s, id);
    if (!dep) fail(404, `deployment non trovato: ${id}`);
    dep.state = "active";
  } else {
    fail(400, `azione non valida: ${action}`);
  }
}

const routes = [
  ["GET", /^\/admin\/deployments$/, (q) => {
    const s = getMock();
    let arr = s.deployments;
    if (q.profile) arr = arr.filter((d) => d.profile === q.profile);
    return deepClone(arr);
  }],
  ["POST", /^\/admin\/deployments$/, (q, body) => {
    const s = getMock();
    const dep = applyCreate(s, body || {});
    return dep;
  }],
  ["GET", /^\/admin\/profiles$/, () => {
    const s = getMock();
    const profiles = (s.profiles?.profiles || []).map((p) => ({
      ...p,
      deployments: s.deployments.filter((d) => d.profile === p.name).length,
    }));
    return { count: profiles.length, profiles };
  }],
  ["GET", /^\/admin\/state$/, () => {
    const s = getMock();
    const st = deepClone(s.adminState || {});
    st.cooldowns_active = deepClone(s.cooldowns_active);
    st.sticky_sessions = deepClone(s.sticky_sessions);
    return st;
  }],
  ["GET", /^\/admin\/policy$/, () => deepClone(getMock().policy)],
  ["PATCH", /^\/admin\/policy$/, (q, body) => {
    const s = getMock();
    s.policy = s.policy || { file: "gateway.yaml", configured: {}, effective: {} };
    s.policy.effective = { ...(s.policy.effective || {}), ...(body || {}) };
    return { ok: true, effective: deepClone(s.policy.effective) };
  }],
  ["GET", /^\/admin\/history$/, (q) => {
    const s = getMock();
    const entries = s.history || [];
    const limit = Math.min(parseInt(q.limit || "100", 10) || 100, 100);
    return { total: entries.length, entries: deepClone(entries.slice(-limit)) };
  }],
  ["GET", /^\/admin\/deployments\/expiring$/, (q) => {
    const s = getMock();
    const days = parseInt(q.days || "30", 10) || 30;
    const expiring = s.deployments
      .filter((d) => d._expires_in_days !== undefined)
      .map((d) => ({ id: d.id, modello: d.modello, in_days: d._expires_in_days, data_raw: d.data ?? "" }));
    return { days, expiring };
  }],
  ["POST", /^\/admin\/deployments\/bulk$/, (q, body) => {
    const s = getMock();
    const ops = Array.isArray(body?.operations) ? body.operations : [];
    if (!ops.length) fail(400, "operations mancanti");
    // Validate all ops first, collect cleaned data
    const validatedOps = [];
    for (const op of ops) {
      const parsed = bulkOpSchema.safeParse(op);
      if (!parsed.success) {
        const issues = parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ");
        fail(400, `validazione fallita: ${issues}`);
      }
      const { action, id, ...rest } = parsed.data;
      validatedOps.push({ action, id, data: rest });
    }
    // Apply validated operations
    const ids = [];
    for (const { action, id, data } of validatedOps) {
      if (action === "create") {
        ids.push(applyCreate(s, data, false).id);
      } else if (action === "update") {
        const dep = findDep(s, id);
        if (!dep) fail(404, `deployment non trovato: ${id}`);
        applyUpdate(s, dep, data);
        ids.push(dep.id);
      } else if (action === "delete") {
        const idx = s.deployments.findIndex((d) => d.id === id || d.id.endsWith(`:${id}`));
        if (idx < 0) fail(404, `deployment non trovato: ${id}`);
        const [removed] = s.deployments.splice(idx, 1);
        ids.push(removed.id);
      } else if (action === "unretire") {
        const dep = findDep(s, id);
        if (!dep) fail(404, `deployment non trovato: ${id}`);
        dep.state = "active";
        ids.push(dep.id);
      }
    }
    s.history.push({ ts: isoNow(), op: "bulk", count: ops.length, ids });
    return { ok: true, applied: ops.length, ids };
  }],
  ["POST", /^\/admin\/deployments\/probe\/bulk$/, (q, body) => {
    const s = getMock();
    const filter = body?.filter || "all";
    let deps = s.deployments;
    if (filter === "cap:vision") deps = deps.filter((d) => (d.capabilities || []).includes("vision"));
    else if (filter !== "all") deps = deps.filter((d) => d.profile === filter);
    const results = deps.map((d) => ({
      unique: d.id,
      ok: true,
      latency_ms: 40 + Math.floor(Math.random() * 200),
    }));
    return { filter, count: results.length, results };
  }],
  ["POST", /^\/admin\/deployments\/probe$/, (q, body) => {
    const s = getMock();
    const unique = body?.unique || body?.id;
    const dep = findDep(s, unique || "");
    if (!dep) fail(404, `deployment non trovato: ${unique}`);
    const force = body?.force === true;
    return { unique: dep.id, ok: true, latency_ms: 123, cached: !force && Math.random() > 0.3, error_class: null };
  }],
  ["POST", /^\/admin\/deployments\/unretire$/, (q, body) => {
    const s = getMock();
    const unique = body?.unique;
    const dep = findDep(s, unique || "");
    if (!dep) fail(404, `deployment non trovato: ${unique}`);
    dep.state = "active";
    return { ok: true, unique: dep.id, state: "active" };
  }],
  ["PUT", /^\/admin\/deployments\/([^/]+)$/, (q, body, m) => {
    const s = getMock();
    const dep = findDep(s, m[1]);
    if (!dep) fail(404, `deployment non trovato: ${m[1]}`);
    applyUpdate(s, dep, body || {});
    s.history.push({ ts: isoNow(), op: "update", id: dep.id, profile: dep.profile });
    return dep;
  }],
  ["DELETE", /^\/admin\/deployments\/([^/]+)$/, (q, body, m) => {
    const s = getMock();
    const idx = s.deployments.findIndex((d) => d.id === m[1] || d.id.endsWith(`:${m[1]}`));
    if (idx < 0) fail(404, `deployment non trovato: ${m[1]}`);
    const [removed] = s.deployments.splice(idx, 1);
    s.history.push({ ts: isoNow(), op: "delete", id: removed.id, profile: removed.profile });
    return { ok: true, removed: 1, id: removed.id };
  }],
  ["GET", /^\/admin\/insights\/summary$/, () => deepClone(getMock().insightsSummary)],
  ["GET", /^\/admin\/insights\/leaderboard$/, (q) => {
    const s = getMock();
    const window = parseInt(q.window || "7", 10) || 7;
    const rows = s.leaderboard?.rows || [];
    return { window_days: window, count: rows.length, rows: deepClone(rows) };
  }],
  ["GET", /^\/admin\/insights$/, (q) => {
    const s = getMock();
    const blob = s.insights || {};
    const total = blob.total ?? 0;
    if (q.group_by) {
      const key = "by_" + q.group_by;
      return { total, [key]: deepClone(blob[key] || {}) };
    }
    return { total, aggregate: deepClone(blob.aggregate || {}) };
  }],
  ["GET", /^\/admin\/logs\/calls$/, (q) => {
    const s = getMock();
    let events = s.logsCalls?.events || [];
    if (q.tail) events = events.slice(-parseInt(q.tail, 10));
    return { events: deepClone(events) };
  }],
  ["GET", /^\/admin\/logs\/errors$/, (q) => {
    const s = getMock();
    let events = s.logsErrors?.events || [];
    if (q.tail) events = events.slice(-parseInt(q.tail, 10));
    return { events: deepClone(events) };
  }],
["POST", /^\/admin\/profiles\/purge$/, (q, body) => {
    const parsed = purgeSchema.safeParse(body);
    if (!parsed.success) {
      const issues = parsed.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`).join("; ");
      fail(400, `validazione fallita: ${issues}`);
    }
    const s = getMock();
    const profile = parsed.data.profile;
    const inUse = s.deployments.filter((d) => d.profile === profile).length;
    if (inUse > 0) fail(409, `profilo "${profile}" ancora in uso da ${inUse} deployment`);
    const before = s.profiles?.profiles?.length || 0;
    s.profiles.profiles = (s.profiles.profiles || []).filter((p) => p.name !== profile);
    return { ok: true, removed: before - s.profiles.profiles.length };
  }],
  ["POST", /^\/admin\/cooldowns\/clear$/, (q, body) => {
    const s = getMock();
    const unique = body?.unique;
    let cleared;
    if (unique) {
      const before = s.cooldowns_active.length;
      s.cooldowns_active = s.cooldowns_active.filter((c) => c.unique !== unique);
      cleared = before - s.cooldowns_active.length;
    } else {
      cleared = s.cooldowns_active.length;
      s.cooldowns_active = [];
    }
    return { ok: true, cleared };
  }],
  ["POST", /^\/admin\/sessions\/release$/, (q, body) => {
    const s = getMock();
    const sessionId = body?.session_id;
    let released;
    if (sessionId) {
      const before = s.sticky_sessions.length;
      s.sticky_sessions = s.sticky_sessions.filter((x) => x.session_id !== sessionId);
      released = before - s.sticky_sessions.length;
    } else {
      released = s.sticky_sessions.length;
      s.sticky_sessions = [];
    }
    return { ok: true, released };
  }],
  ["POST", /^\/admin\/reload$/, () => {
    const s = getMock();
    return {
      reloaded: true,
      profiles: s.profiles?.profiles?.length || 0,
      deployments: s.deployments.length,
      policy: deepClone(s.policy),
    };
  }],
  ["POST", /^\/admin\/capabilities\/seed-from-map$/, (q, body) => {
    const s = getMock();
    if (body?.dry_run) {
      const proposals = s.capProposals || [];
      return { dry_run: true, count: proposals.length, total: s.deployments.length, proposals };
    }
    return { ok: true, applied: s.deployments.length, skipped: 0, errors: [] };
  }],
  ["POST", /^\/admin\/capabilities\/audit$/, () => deepClone(getMock().capAudit)],
  ["POST", /^\/admin\/playground$/, (q, body) => {
    const s = getMock();
    const model = body?.model || "deepseek/deepseek-chat";
    const target = body?.deployment || s.deployments[0]?.id || null;
    const content = body?.content ?? "Ciao, mock playground.";
    const trace = [];
    const seen = new Set();
    let attempts = 0;
    let fallbacks = 0;
    let assigned = target;
    for (let i = 0; i < s.deployments.length; i++) {
      const d = s.deployments[i];
      if (seen.has(d.id)) continue;
      seen.add(d.id);
      attempts++;
      if (!target || d.id === target) {
        trace.push({ step: i, unique: d.id, group: d.group, profile: d.profile, attempted: true, ok: true, scartato_a: null, reason: null, verdict: "ok" });
        assigned = d.id;
        break;
      }
      trace.push({ step: i, unique: d.id, group: d.group, profile: d.profile, attempted: false, ok: false, scartato_a: target, reason: "target non selezionato", verdict: "fallback" });
      fallbacks++;
    }
    const prompt = 240;
    const completion = 480;
    return {
      model,
      deployment: assigned,
      content,
      usage: { prompt_tokens: prompt, completion_tokens: completion, total_tokens: prompt + completion },
      trace,
      attempts,
      fallbacks,
    };
  }],
  ["GET", /^\/admin\/csv$/, () => {
    const s = getMock();
    const backups = s.backups
      .filter((b) => b.kind === "csv")
      .map(({ filename, size, mtime }) => ({ filename, size, mtime }));
    return { ...deepClone(s.csv || {}), backups };
  }],
  ["PUT", /^\/admin\/csv$/, (q, body) => {
    const s = getMock();
    const raw = typeof body?.raw === "string" ? body.raw : "";
    if (!raw.trim()) fail(400, "campo raw obbligatorio");
    const parsed = parseCsv(raw);
    if (!parsed) fail(400, "CSV non valido");
    const filename = `csv-backup-${stamp()}.csv`;
    s.csv = { path: s.csv?.path || "clients.csv", raw, parsed, count: parsed.rows.length };
    s.backups.push({ kind: "csv", filename, size: Buffer.byteLength(raw), mtime: isoNow(), raw });
    return { ok: true, backup: filename, rows: parsed.rows.length };
  }],
  ["GET", /^\/admin\/policy\/raw$/, () => {
    const s = getMock();
    return { path: s.policy?.file || "gateway.yaml", raw: s.policyRaw ?? "" };
  }],
  ["PUT", /^\/admin\/policy\/raw$/, async (q, body) => {
    const s = getMock();
    const raw = typeof body?.raw === "string" ? body.raw : "";
    if (!raw.trim()) fail(400, "campo raw obbligatorio");
    let effective = {};
    try {
      const yaml = await import("js-yaml");
      const parsed = yaml.load(raw);
      if (parsed && typeof parsed === "object") effective = parsed;
    } catch { /* testo non yaml: lo teniamo come testo */ }
    s.policyRaw = raw;
    s.policy = { file: s.policy?.file || "gateway.yaml", configured: s.policy?.configured || {}, effective };
    const filename = `policy-backup-${stamp()}.yaml`;
    s.backups.push({ kind: "yaml", filename, size: Buffer.byteLength(raw), mtime: isoNow(), raw });
    return { ok: true, validated: true, reloaded: true, effective: deepClone(effective) };
  }],
  ["GET", /^\/admin\/backups$/, () => {
    const s = getMock();
    const pick = (kind) =>
      s.backups
        .filter((b) => b.kind === kind)
        .map(({ filename, size, mtime }) => ({ filename, size, mtime }));
    return { dir: "/data/backups", csv: pick("csv"), yaml: pick("yaml") };
  }],
  ["POST", /^\/admin\/backups\/restore$/, async (q, body) => {
    const s = getMock();
    const filename = body?.filename;
    const b = s.backups.find((x) => x.filename === filename);
    if (!b) fail(404, `backup non trovato: ${filename}`);
    if (b.kind === "csv") {
      const parsed = parseCsv(b.raw || "");
      s.csv = { path: s.csv?.path || "clients.csv", raw: b.raw || "", parsed, count: parsed ? parsed.rows.length : 0 };
      return { ok: true, restored: filename, rows: parsed ? parsed.rows.length : 0 };
    }
    let effective = {};
    try {
      const yaml = await import("js-yaml");
      const parsed = yaml.load(b.raw || "");
      if (parsed && typeof parsed === "object") effective = parsed;
    } catch { /* ignore */ }
    s.policyRaw = b.raw || "";
    s.policy = { ...(s.policy || {}), effective };
    return { ok: true, restored: filename, effective: deepClone(effective) };
  }],
  ["GET", /^\/admin\/guide$/, () => getMock().guide],
  ["GET", /^\/healthz$/, () => {
    const s = getMock();
    return { ok: true, version: s.healthz?.version || "0.1.0-mock", uptime_s: Math.floor(process.uptime()) };
  }],
  ["GET", /^\/v1\/models$/, () => deepClone(getMock().models)],
  ["GET", /^\/bootstrap\/status$/, () => deepClone(getMock().bootstrapStatus)],
  ["GET", /^\/bootstrap\/providers$/, () => deepClone(getMock().bootstrapProviders)],
  ["GET", /^\/bootstrap$/, () => deepClone(getMock().bootstrap)],
];

async function mockRequest(method, pathname, query, body) {
  for (const [m, re, handler] of routes) {
    if (m !== method) continue;
    const match = re.exec(pathname);
    if (!match) continue;
    return await handler(query, body, match);
  }
  fail(404, `endpoint non supportato dal mock: ${method} ${pathname}`);
}

/* ------------------------------------------------------------------ */
/* Real network path                                                   */
/* ------------------------------------------------------------------ */

async function networkRequest(method, path, opts = {}, { raw = false } = {}) {
  const { params, json, timeout, requestId } = opts;
  const base = config.gatewayUrl.replace(/\/+$/, "");
  const url = new URL(base + path);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
  }
  const headers = { "Content-Type": "application/json" };
  if (config.gatewayMasterKey) headers.Authorization = `Bearer ${config.gatewayMasterKey}`;
  if (requestId) headers["X-Request-Id"] = String(requestId);
  const timeoutMs = Number.isInteger(timeout) ? timeout : config.gatewayTimeoutMs;

  const attempt = async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, {
        method,
        headers,
        body: json !== undefined ? JSON.stringify(json) : undefined,
        signal: controller.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  };

  let res;
  try {
    res = await attempt();
  } catch (err) {
    if (method === "GET") {
      logger.warn("gateway retry (GET)", { path });
      res = await attempt();
    } else {
      throw err;
    }
  }

  const text = await res.text();
  if (!res.ok) {
    let message = null;
    try {
      const parsed = text ? JSON.parse(text) : null;
      message = parsed?.error?.message || null;
    } catch { /* ignore */ }
    throw new GatewayError(res.status, message || truncate(text, 300));
  }
  if (raw) return text;
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/* ------------------------------------------------------------------ */
/* Public API                                                          */
/* ------------------------------------------------------------------ */

async function request(method, path, opts = {}) {
  if (config.gatewayMock) {
    const { params, json } = opts;
    const [pathname, qs = ""] = String(path).split("?");
    const query = {};
    for (const [k, v] of new URLSearchParams(qs)) query[k] = v;
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        if (v !== undefined && v !== null) query[k] = String(v);
      }
    }
    return mockRequest(method, pathname, query, json !== undefined ? json : null);
  }
  return networkRequest(method, path, opts);
}

async function rawGet(path, opts = {}) {
  if (config.gatewayMock) {
    const result = await request("GET", path, opts);
    return typeof result === "string" ? result : JSON.stringify(result);
  }
  return networkRequest("GET", path, opts, { raw: true });
}

async function health() {
  try {
    const payload = await request("GET", "/healthz");
    return { ok: true, payload };
  } catch (err) {
    return { ok: false, payload: { status: err.status, error: err.message } };
  }
}

export default {
  request,
  get: (path, opts = {}) => request("GET", path, opts),
  post: (path, opts = {}) => request("POST", path, opts),
  put: (path, opts = {}) => request("PUT", path, opts),
  patch: (path, opts = {}) => request("PATCH", path, opts),
  del: (path, opts = {}) => request("DELETE", path, opts),
  rawGet,
  health,
};
