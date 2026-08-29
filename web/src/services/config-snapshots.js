import crypto from "node:crypto";
import { query } from "../db.js";

// Snapshot di configurazione (CSV o gateway.yaml) salvati in Postgres per la
// config-history / rollback. `content` e' il testo raw; `source_sha256` ne e'
// l'hash (per dedup / integrita').

export function sha256(text) {
  return crypto.createHash("sha256").update(String(text ?? ""), "utf8").digest("hex");
}

export async function createSnapshot({ kind, content, source = null, userId = null, note = null }) {
  if (kind !== "csv" && kind !== "yaml") throw new Error("kind deve essere 'csv' o 'yaml'");
  const digest = sha256(content);
  // dedup: se l'ultimo snapshot di questo kind ha lo stesso sha, non duplico
  const last = await query(
    "SELECT source_sha256 FROM config_snapshots WHERE kind = $1 ORDER BY id DESC LIMIT 1",
    [kind]
  );
  if (last.rows[0] && last.rows[0].source_sha256 === digest) {
    return { deduped: true };
  }
  const { rows } = await query(
    `INSERT INTO config_snapshots (kind, content, source_sha256, created_by, note)
     VALUES ($1, $2, $3, $4, $5) RETURNING id, created_at`,
    [kind, String(content ?? ""), digest, Number.isInteger(userId) ? userId : null, note ? String(note).slice(0, 255) : source]
  );
  return { id: rows[0].id, created_at: rows[0].created_at, sha256: digest };
}

export async function listSnapshots({ kind = null, limit = 50 } = {}) {
  const lim = Math.min(Math.max(parseInt(limit, 10) || 50, 1), 200);
  const params = [];
  let where = "";
  if (kind) {
    params.push(kind);
    where = `WHERE kind = $${params.length}`;
  }
  params.push(lim);
  const { rows } = await query(
    `SELECT id, kind, source_sha256, created_by, note, created_at,
            octet_length(content) AS size
       FROM config_snapshots ${where}
      ORDER BY id DESC
      LIMIT $${params.length}`,
    params
  );
  return rows;
}

export async function getSnapshot(id) {
  const { rows } = await query(
    "SELECT id, kind, content, source_sha256, created_by, note, created_at FROM config_snapshots WHERE id = $1",
    [id]
  );
  return rows[0] || null;
}

export default { sha256, createSnapshot, listSnapshots, getSnapshot };
