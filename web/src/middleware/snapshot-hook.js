import gateway from "../services/gateway.js";
import { createSnapshot } from "../services/config-snapshots.js";
import { logger } from "../services/logger.js";

// Dopo un save riuscito di CSV o gateway.yaml, crea uno snapshot in
// config_snapshots (fire-and-forget). Le route F4-01/F4-03 possono mettere il
// raw salvato su res.locals.lastSavedRaw; se assente, il hook rilegge il
// contenuto corrente dal gateway (2 chiamate extra, accettabile).
export function withSnapshot(kind) {
  if (kind !== "csv" && kind !== "yaml") throw new Error("kind csv|yaml");
  return function snapshotHook(req, res, next) {
    res.on("finish", () => {
      if (res.statusCode >= 400) return;
      (async () => {
        // priorita': quello che la route ha esposto -> il body della richiesta
        // di save -> rilettura live dal gateway.
        let raw = res.locals && res.locals.lastSavedRaw;
        if (typeof raw !== "string" && req.body && typeof req.body.raw === "string") {
          raw = req.body.raw;
        }
        if (typeof raw !== "string") {
          const path = kind === "csv" ? "/admin/csv" : "/admin/policy/raw";
          raw = (await gateway.get(path))?.raw || "";
        }
        if (!raw) return;
        const r = await createSnapshot({
          kind,
          content: raw,
          source: "save-hook",
          userId: req.user && (req.user.sub ?? req.user.id),
        });
        if (!r.deduped) logger.info(`snapshot ${kind} #${r.id} creato dal save-hook`);
      })().catch((err) => logger.error(`snapshot-hook (${kind}) fallito: ${err.message}`));
    });
    next();
  };
}

export default { withSnapshot };
