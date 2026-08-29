---
id: F5-03-api-write
fase: F5
dipende_da: [F2-01-deployment-crud, F2-02-deployments-bulk, F2-06-system-actions, F2-07-probe, F2-08-capabilities-write, F1-11-integration]
puo_parallelo_con: F5-01-api-read-deployments, F5-02-api-read-core, F5-06-agent-docs
---

# F5-03 — Surface `/api/v1` WRITE (azioni operative)

## Obiettivo

Endpoint API-write per agenti (parità azioni UI): CRUD deployment, bulk,
probe, azioni sistema, unretire, relazioni policy admin-only, seed capacità.
Ogni mutazione: zod, authorize, gateway, **audit_log**.

## File da creare/modificare

- `scrocco-web/src/routes/api-write.js` (crea)
- `scrocco-web/test/api-write.test.js` (crea)

## Contratto

- `POST /api/v1/deployments` [authorize deployments create] → body come UI →
  `gateway.post('/admin/deployments',{json})` → 201 {ok,id}.
- `PUT /api/v1/deployments/:id` [update].
- `DELETE /api/v1/deployments/:id` [delete].
- `POST /api/v1/deployments/bulk` [bulk].
- `POST /api/v1/deployments/probe` {id|unique?} e `/probe/bulk`
  [probe, operator/admin].
- `POST /api/v1/deployments/unretire` {unique} [unretire].
- `POST /api/v1/system/cooldowns/clear` {unique?} [system].
- `POST /api/v1/system/sessions/release` {session_id?} [system].
- `POST /api/v1/system/reload` [system].
- `POST /api/v1/capabilities/seed` {dry_run} [seed].
- `POST /api/v1/capabilities/audit` [audit] (read-informed).
- `PATCH /api/v1/policy` {patch} → **SOLO admin** (authorize policy update).
- Codici: 201 create, 200 ok, 400 validazione, 403 permessi, 404 non trovato,
  503 gateway giù. Audit su ogni write (entityType, action, newData sanitized).

## Criterio di done

Test con operator: POST create → 201 + presente; bulk → 200; DELETE → 204/200;
PATCH policy con operator → 403; PATCH policy con admin → 200 (mock). audit ha
righe. `node --check`.

## Rischi / note

- `authorize policy update` = admin: l'API NON deve abbassare la sicurezza.
- Il body di PATCH policy può contenere chiavi nuove (fine: admin-only);
  audit con newData {fields} SOLO, no valori.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea gli endpoint API-write.

Crea:
1. `src/routes/api-write.js` — default Router con TUTTE le route sopra
   (requireAuth + authorize per risorsa/azione giuste; zod body; 
   `gateway.post/put/del`/... ; auditLog su ogni mutazione —
   sanitize nel newData (mai chiavi; per policy solo {fields}).
   Codici HTTP coerenti; `GatewayError` mappato (status→HTTP).
2. `test/api-write.test.js` — startTestApp, utente operator con token e utente
   admin; assert come da contratto (create/bulk/delete 2**, PATCH policy 403
   operator / 200 admin, auditer `audit_log` count>0).

Verifica `node --check` + npm test. Non toccare altri file. Riepiloga in 2
righe.