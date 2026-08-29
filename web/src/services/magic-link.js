import crypto from "crypto";
import nodemailer from "nodemailer";
import config from "../config.js";
import { query } from "../db.js";
import { logger } from "./logger.js";

function sha256(s) {
  return crypto.createHash("sha256").update(String(s)).digest("hex");
}

function smtpConfigured() {
  return !!config.smtpHost;
}

export async function generateAndSend(email) {
  const normalized = String(email || "").trim().toLowerCase();

  let user;
  try {
    const result = await query(
      "SELECT id, name, email FROM users WHERE email=$1 AND status='active'",
      [normalized]
    );
    user = result.rows[0];
  } catch (err) {
    logger.error("magic-link: errore query utente", { error: err.message });
    return { sent: false, reason: "utente non trovato o disabilitato" };
  }

  if (!user) {
    await new Promise((r) => setTimeout(r, 250 + Math.random() * 200));
    return { sent: false, reason: "utente non trovato o disabilitato" };
  }

  if (!smtpConfigured()) {
    return { sent: false, reason: "SMTP non configurato" };
  }

  const token = crypto.randomBytes(48).toString("hex");
  const otp = crypto.randomInt(100000, 1000000).toString();
  const expiresAt = new Date(Date.now() + 15 * 60 * 1000);

  try {
    await query(
      "INSERT INTO magic_links (user_id, token_hash, otp_hash, expires_at) VALUES ($1, $2, $3, $4)",
      [user.id, sha256(token), sha256(otp), expiresAt]
    );
  } catch (err) {
    logger.error("magic-link: errore inserimento", { error: err.message });
    return { sent: false, reason: "invio email fallito" };
  }

  // FIX: Log errors instead of silently swallowing them
  query("DELETE FROM magic_links WHERE expires_at < NOW() - INTERVAL '24 hours'")
    .catch((err) => { logger.debug("magic-link: cleanup failed", { error: err.message }); });

  const link = `${config.magicLinkBaseUrl}/verify?token=${token}`;
  const tx = nodemailer.createTransport({
    host: config.smtpHost,
    port: config.smtpPort,
    secure: config.smtpPort === 465,
    auth: config.smtpUser
      ? { user: config.smtpUser, pass: config.smtpPass }
      : undefined,
  });

  try {
    await tx.sendMail({
      from: config.smtpFrom,
      to: normalized,
      subject: "Accesso scrocco-web",
      text: "Link: " + link + "\nOTP: " + otp,
    });
  } catch (err) {
    logger.error("magic-link: invio email fallito", { error: err.message });
    return { sent: false, reason: "invio email fallito" };
  }

  return { sent: true };
}

export async function verify(token, otp) {
  let row;
  try {
    const result = await query(
      "SELECT id, user_id, otp_hash, failed_attempts FROM magic_links WHERE token_hash=$1 AND consumed_at IS NULL AND expires_at > NOW()",
      [sha256(token)]
    );
    row = result.rows[0];
  } catch (err) {
    logger.error("magic-link: errore verifica token", { error: err.message });
    return null;
  }

  if (!row) return null;
  if (row.failed_attempts >= 5) return null;

  let otpOk = false;
  try {
    const expected = Buffer.from(row.otp_hash, "hex");
    const provided = Buffer.from(sha256(otp), "hex");
    otpOk = expected.length === provided.length && crypto.timingSafeEqual(expected, provided);
  } catch {
    otpOk = false;
  }

  if (!otpOk) {
    try {
      await query(
        "UPDATE magic_links SET failed_attempts = failed_attempts + 1 WHERE id=$1 AND failed_attempts < 5",
        [row.id]
      );
    } catch {
      // ignore
    }
    return null;
  }

  let consumed;
  try {
    const res = await query(
      "UPDATE magic_links SET consumed_at = NOW() WHERE id=$1 AND consumed_at IS NULL RETURNING user_id",
      [row.id]
    );
    consumed = res.rows[0];
  } catch (err) {
    logger.error("magic-link: errore consumazione", { error: err.message });
    return null;
  }

  if (!consumed) return null;

  let user;
  try {
    const result = await query(
      "SELECT id, email, name, role, status, token_version FROM users WHERE id=$1",
      [consumed.user_id]
    );
    user = result.rows[0];
  } catch (err) {
    logger.error("magic-link: errore caricamento utente", { error: err.message });
    return null;
  }

  if (!user || user.status === "disabled") return null;

  return {
    id: user.id,
    email: user.email,
    name: user.name,
    role: user.role,
    token_version: user.token_version,
  };
}
