import jwt from "jsonwebtoken";
import config from "../config.js";
import { query } from "../db.js";
import { isApiTokenFormat, verifyApiToken } from "../services/api-tokens.js";

function unauthorized(req, res) {
  if (req.path.startsWith("/api")) {
    return res.status(401).json({ error: { message: "autenticazione richiesta" } });
  }
  return res.redirect("/login");
}

export async function requireAuth(req, res, next) {
  const fromCookie = !!req.cookies?.[config.sessionCookieName];
  const token = fromCookie
    ? req.cookies[config.sessionCookieName]
    : req.headers.authorization?.replace("Bearer ", "");

  if (!token) {
    return unauthorized(req, res);
  }

  if (isApiTokenFormat(token)) {
    const u = await verifyApiToken(token).catch(() => null);
    if (!u) {
      return res.status(401).json({ error: { message: "token non valido" } });
    }
    req.user = u;
    res.locals.user = u;
    return next();
  }

  let decoded;
  try {
    decoded = jwt.verify(token, config.jwtSecret, { algorithms: ["HS256"] });
  } catch (err) {
    if (fromCookie) res.clearCookie(config.sessionCookieName);
    return unauthorized(req, res);
  }

  try {
    const result = await query(
      "SELECT token_version, status FROM users WHERE id=$1",
      [decoded.sub]
    );
    const row = result.rows[0];
    if (!row || row.status === "disabled" || row.token_version !== decoded.token_version) {
      if (fromCookie) res.clearCookie(config.sessionCookieName);
      return unauthorized(req, res);
    }
  } catch (err) {
    return unauthorized(req, res);
  }

  req.user = decoded;
  res.locals.user = decoded;
  return next();
}
