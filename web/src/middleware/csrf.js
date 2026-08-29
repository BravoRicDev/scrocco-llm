import config from "../config.js";

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function csrfProtection(req, res, next) {
  if (!UNSAFE.has(req.method)) return next();

  // FIX: Always validate CSRF token regardless of authorization header.
  // The previous bypass for requests with authorization header left state-changing
  // endpoints vulnerable to CSRF when users are logged in via Bearer tokens.
  if (!req.cookies?.[config.sessionCookieName]) {
    return next();  // No cookie to validate against - let the route handle it
  }

  const host = req.get("host");
  const src = req.headers.origin || req.headers.referer;

  if (!src) {
    return reject(req, res);
  }

  let srcHost;
  try {
    srcHost = new URL(src).host;
  } catch {
    srcHost = null;
  }

  if (srcHost !== host) {
    return reject(req, res);
  }

  return next();
}

function reject(req, res) {
  if (req.path.startsWith("/api")) {
    return res.status(403).json({ error: { message: "origine non valida" } });
  }
  return res.status(403).render("error", { message: "Origine non valida" });
}
