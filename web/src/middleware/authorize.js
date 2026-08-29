import PERMISSIONS from "../constants/permissions.js";

export function authorize(resource, action) {
  return (req, res, next) => {
    if (!req.user) {
      if (req.path.startsWith("/api")) {
        return res.status(401).json({ error: { message: "autenticazione richiesta" } });
      }
      return res.status(401).render("error", { message: "Autenticazione richiesta" });
    }

    if (PERMISSIONS[req.user.role]?.[resource]?.[action]) {
      return next();
    }

    if (req.path.startsWith("/api")) {
      return res.status(403).json({ error: { message: "permesso negato" } });
    }
    return res.status(403).render("error", { message: "Permesso negato" });
  };
}
