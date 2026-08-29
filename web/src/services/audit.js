import { query } from "../db.js";
import { logger } from "./logger.js";

// Log di audit di ogni mutazione fatta dal pannello verso il gateway o sul DB
// locale. Scrive su `audit_log` (vedi db/001_schema.sql). Non deve MAI far
// fallire l'operazione chiamante: in caso di errore logga e prosegue.
//
// Campi:
//  - user_id / actor_email : chi ha fatto l'azione (da req.user)
//  - op                    : verbo azione, es. "deployment.create", "policy.update"
//  - target                : identificatore della risorsa toccata (id, nome, "*")
//  - detail                : oggetto JSON con i dettagli (MAI chiavi/segreti in chiaro)
//  - route_method          : metodo HTTP della route del pannello
//  - gateway_path          : path /admin/* chiamato sul gateway (se applicabile)
//  - ip                    : req.ip
export async function auditLog({
  user = null,
  userId = null,
  actorEmail = null,
  op,
  target = null,
  detail = null,
  routeMethod = null,
  gatewayPath = null,
  ip = null,
} = {}) {
  try {
    const uid = userId ?? (user && (user.sub ?? user.id)) ?? null;
    const email = actorEmail ?? (user && user.email) ?? null;
    await query(
      `INSERT INTO audit_log
         (user_id, actor_email, op, target, detail, route_method, gateway_path, ip)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
      [
        Number.isInteger(uid) ? uid : null,
        email,
        String(op || "unknown").slice(0, 80),
        target != null ? String(target).slice(0, 255) : null,
        detail != null ? JSON.stringify(detail) : null,
        routeMethod ? String(routeMethod).slice(0, 10) : null,
        gatewayPath ? String(gatewayPath).slice(0, 255) : null,
        ip ? String(ip).slice(0, 64) : null,
      ]
    );
  } catch (err) {
    logger.error(`auditLog fallito (${op}): ${err.message}`);
  }
}

// Rilegge le ultime N righe dell'audit (per il pannello read-only F5-07).
export async function recentAudit({ limit = 100, op = null } = {}) {
  const lim = Math.min(Math.max(parseInt(limit, 10) || 100, 1), 500);
  const params = [];
  let where = "";
  if (op) {
    params.push(op);
    where = `WHERE op = $${params.length}`;
  }
  params.push(lim);
  const { rows } = await query(
    `SELECT id, user_id, actor_email, op, target, detail, route_method,
            gateway_path, ip, created_at
       FROM audit_log ${where}
      ORDER BY id DESC
      LIMIT $${params.length}`,
    params
  );
  return rows;
}

export default { auditLog, recentAudit };
