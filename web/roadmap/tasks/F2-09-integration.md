---
id: F2-09-integration
fase: F2
dipende_da: [F2-01-deployment-crud, F2-02-deployments-bulk, F2-03-profiles-write, F2-04-policy-edit, F2-05-policy-keys, F2-06-system-actions, F2-07-probe, F2-08-capabilities-write, F2-10-users-admin, F1-11-integration]
puo_parallelo_con: []
---

# F2-09 — Integrazione F2: flash parsing, mount, smoke write

## Obiettivo

Completare F2: parse del query param `?flash=` (usato dalle route write),
mount dei nuovi router (bulk, system, probe, policy-keys, users) in
`src/index.js`,
abilita link sidebar, e scrivere lo smoke test che esegue una mutazione reale
end-to-end sul mock (create→read→update→delete→bulk→probe→reload) verificando
anche `audit_log`.

## File da creare/modificare

- `scrocco-web/src/index.js` (modifica)
- `scrocco-web/views/partials/sidebar.ejs` (modifica)
- `scrocco-web/test/f2-write-actions.test.js` (crea)

## Contratto

- In `src/index.js`: parse `res.locals.flash` DA `req.query.flash` (un array
  o string splitata) PRIMA del render; mount `bulkRoutes`,
  `systemRoutes`, `probeRoutes`, `policyKeysRoutes`, `usersRoutes`;
  nessun altro cambio.
- Sidebar: link Bulk (`/deployments/bulk`), System (`/system`),
  Probe (`/probe`), Policy keys (`/policy/keys` — admin), **Utenti
  (`/users` — admin, sblocca il link previsto da F0-08)**, e abilita "Modifica"
  dove serve.
- `test/f2-write-actions.test.js`: con GATEWAY_MOCK e utente OPERATOR
  (password login): 1) POST /deployments (create valido) → 302; verifica che
  la riga appaia in GET /deployments; 2) PUT /deployments/:id → 302 e dati
  aggiornati; 3) POST /deployments/:id/delete → sparita; 4) POST /deployments/
  bulk con 2 op valide → 302; 5) POST /system/reload → 302; 6) POST /probe/:id
  → 200/fixture ok; 7) check `audit_log` ha righe per create/update/delete
  (query diretta); 8) controller su opera con VIEWER → 403 per POST
  /deployments; 9) GET /users come admin → 200, come viewer/operator → 403
  (users è admin-only).

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
DATABASE_URL=postgres://<test> GATEWAY_MOCK=1 npm test   # verde, include f2-write-actions
```

## Rischi / note

- Il flash parsing a monte risolve le redirect "?flash=" di F2-01/F2-02/etc.
- I test di autorizzazione usano un viewer: assicurati `authorize` funzioni
  come da matrice.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Chiudi la fase WRITE.

Modifica SOLO:
1. `src/index.js` — nell'handler `app.use((req,res,next)=>...)` che setta
   `res.locals`: se `req.query.flash` è una stringa, splitta per `;` e assegna
   `res.locals.flash = [{type:'info', message:...}]`; monta i router
   `src/routes/deployments-bulk.js`, `src/routes/system-actions.js`,
   `src/routes/probe.js`, `src/routes/policy-keys.js`, `src/routes/users.js`.
2. `views/partials/sidebar.ejs` — aggiungi link: Operazioni bulk
   `/deployments/bulk`, Probe `/probe`, Sistema `/system`, "Chiavi policy"
   `/policy/keys` (solo admin) e **Utenti `/users` (solo admin, sblocca la
   voce già creata da F0-08)**.
3. Crea `test/f2-write-actions.test.js` con il flusso descritto nel contratto
   (helper `startTestApp()`, utente operator+viewer, verifica audit_log nel
   DB). Nota: la bulk su operator è permessa; viewer → 403 su tutte le POST.

Verifica `DATABASE_URL=<test> GATEWAY_MOCK=1 npm test` verde. Non toccare
altri file. Riepiloga in 3 righe.