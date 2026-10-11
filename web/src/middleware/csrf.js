import config from "../config.js";

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function csrfProtection(req, res, next) {
  if (!UNSAFE.has(req.method)) return next();

  // Modello di protezione CSRF (razionalizzato in Fase 6 della roadmap).
  // NON esiste alcun token CSRF di sessione: la difesa poggia su due elementi
  //  1) il cookie di sessione (`config.sessionCookieName`) e' SameSite=Lax,
  //     quindi non viene inviato sulle richieste cross-site non-GET;
  //  2) qui si valida che Origin/Referer corrispondano all'Host servito,
  //     rifiutando con 403 ogni unsafe method senza origine affidabile.
  // Una richiesta senza cookie di sessione non ha stato da proteggere: le route
  // autenticate rispondono comunque 401.
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
