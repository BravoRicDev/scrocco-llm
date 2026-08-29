---
id: F5-01-api-read-deployments
fase: F5
dipende_da: [F1-01-deployments-list, F2-01-deployment-crud, F1-11-integration]
puo_parallelo_con: F5-02-api-read-core, F5-03-api-write, F5-06-agent-docs
---

# F5-01 — Surface `/api/v1` READ (deployments + profiles)

## Obiettivo

Esporre come API JSON per agenti la lettura di deployments e profili.
Stile: envelope JSON, auth via Bearer (JWT agent oppure API token
`agtok_`), ratelimit, documentazione in openapi (F5-05). Endpoint
paritetici alle azioni UI.

## File da creare/modificare

- `scrocco-web/src/routes/api-deployments.js` (crea)
- `scrocco-web/test/api-read.test.js` (crea)

NON toccare `src/index.js` (mount è F5-07).

## Contratto

Router (default export) da montare sotto `/api/v1`:
- `GET /api/v1/deployments?profile=&q=` [requireAuth (accetta bearer agent o
  agtok_) + authorize deployments read] → `{count, deployments:[view]}` con lo
  stesso shape del gateway CON `key_masked` (mai in chiaro).
- `GET /api/v1/deployments/expiring?days=` [read] → `{days, expiring}`.
- `GET /api/v1/profiles` [read] → `{count, profiles}`.
- `GET /api/v1/deployments/:id` [read] → singolo view (404 se assente) o
  redirect a GET con id.
- Envelope errori: `{error:{message}}` con status HTTP (401/403/404/503).
- `authorize` con le stesse risorse UI.

## Criterio di done

`node --check`; test con `startTestApp()` + utente operator e API token:
fetch GET /api/v1/deployments con Bearer agtok_ → 200 JSON; viewer → 200;
senza token → 401. Mock deploy: `count` coerente.

## Rischi / note

- Mai chiavi in chiaro: il gateway già maschera; lo shape passa intatto.
- `authorize` deve eseguire anche su /api: già così (JSON errors).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la surface API-read per agenti.

Crea:
1. `src/routes/api-deployments.js` — default export Router con le route
   `GET /api/v1/deployments` (`?profile`,`?q`), `GET /api/v1/deployments/
   expiring?days`, `GET /api/v1/profiles`, `GET /api/v1/deployments/:id`.
   Ogni route: `requireAuth` + `authorize(<resource>, "read")`; usa
   `gateway.get(...)`; risponde JSON envelope; gestisce 404 su id mancante.
   Import da `src/middleware/auth.js` / `src/middleware/authorize.js`.
2. `test/api-read.test.js` — con `startTestApp()`: crea user operator + API
   token (`createApiToken`), GET con `Authorization: Bearer <agtok>` →
   200 JSON con `deployments`; GET senza token → 401; viewer → 200 read.
   Chiudi con closeDb().

Verifica `node --check` + `npm test` se possibile. Non toccare altri file.
Riepiloga in 2 righe.