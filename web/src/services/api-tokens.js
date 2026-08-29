import crypto from "crypto";
import { query } from "../db.js";

const TOKEN_PREFIX = "agtok_";

function hashToken(rawToken) {
  return crypto.createHash("sha256").update(rawToken).digest("hex");
}

/**
 * Creates a new API token for an agent/automation.
 * The raw token is returned once.
 */
export async function createApiToken(userId, name, expiresInDays) {
  const raw = TOKEN_PREFIX + crypto.randomBytes(32).toString("hex");
  const prefix = raw.slice(0, 12) + "…";   // <= 16 (token_prefix e' VARCHAR(16))
  const expiresAt = new Date(Date.now() + expiresInDays * 86400000);
  
  const result = await query(
    `INSERT INTO api_tokens (user_id, name, token_hash, token_prefix, expires_at)
     VALUES ($1, $2, $3, $4, $5) RETURNING id, created_at`,
    [userId, name.slice(0, 255), hashToken(raw), prefix, expiresAt]
  );
  
  return {
    id: result.rows[0].id,
    token: raw,
    prefix,
    expiresAt,
    createdAt: result.rows[0].created_at
  };
}

/**
 * Verifies the API token and returns user information if valid.
 * Returns null if invalid or error occurs.
 */
export async function verifyApiToken(rawToken) {
  if (typeof rawToken !== "string" || !rawToken.startsWith(TOKEN_PREFIX)) {
    return null;
  }

  try {
    const result = await query(
      `SELECT t.id AS token_id, u.id, u.email, u.name, u.role, u.token_version, u.status
       FROM api_tokens t 
       JOIN users u ON u.id = t.user_id
       WHERE t.token_hash = $1 AND t.revoked_at IS NULL AND t.expires_at > NOW()`,
      [hashToken(rawToken)]
    );

    const row = result.rows[0];
    if (!row || row.status !== "active") {
      return null;
    }

    // Fire-and-forget update for last_used_at
    // FIX: Log errors instead of silently swallowing them
    query("UPDATE api_tokens SET last_used_at = NOW() WHERE id = $1", [row.token_id])
      .catch((err) => { logger.debug("api-token: last_used_at update failed", { error: err.message }); });

    return {
      sub: row.id,
      email: row.email,
      name: row.name,
      role: row.role,
      token_version: row.token_version,
      agent: true,
      api_token: true,
    };
  } catch (err) {
    return null;
  }
}

/**
 * Checks if the provided string follows the API token format.
 */
export function isApiTokenFormat(rawToken) {
  return typeof rawToken === "string" && rawToken.startsWith(TOKEN_PREFIX);
}

/**
 * Lists all API tokens for a specific user.
 */
export async function listApiTokens(userId) {
  try {
    const result = await query(
      `SELECT id, name, token_prefix, expires_at, last_used_at, revoked_at, created_at
       FROM api_tokens 
       WHERE user_id = $1 
       ORDER BY created_at DESC`,
      [userId]
    );
    return result.rows;
  } catch (err) {
    return [];
  }
}

/**
 * Revokes a specific API token for a user.
 */
export async function revokeApiToken(userId, tokenId) {
  try {
    await query(
      "UPDATE api_tokens SET revoked_at = NOW() WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL",
      [tokenId, userId]
    );
  } catch (err) {
    // Silently fail or handle as needed; task implies simple revocation
  }
}
