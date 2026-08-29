---
id: F5-07-integration
fase: F5
dipende_da: [F5-01-api-read-deployments, F5-02-api-read-core, F5-03-api-write, F5-04-mcp-server, F5-05-openapi-full, F5-06-agent-docs, F4-09-integration]
puo_parallelo_con: []
---

# F5-07 — Integrazione F5: mount API+MCP, pannello audit, test e2e, chiusura roadmap

## Obiettivo

Finale: montare in `src/index.js` i router `/api/v1`, `/api/mcp` e
`/api/v1/docs`+openapi, abilitare i rate-limit adeguati (skip loopback per
MCP), aggiungere il pannello read-only del NOSTRO `audit_log`, verificare il
flusso completo con test e2e (UI + API + MCP) e scrivere
il README definitivo + verifica roadmap (tutti i task chiusi).

## File da creare/modificare

- `scrocco-web/src/index.js` (modifica — mount)
- `scrocco-web/src/routes/audit.js` (crea — `GET /admin/audit`, admin-only)
- `scrocco-web/src/routes/agent-doc.js` (crea — `GET /agent-guide` che serve
  `docs/AGENT.md` renderizzato localmente)
- `scrocco-web/views/admin/audit/index.ejs` (crea — pannello read-only)
- `scrocco-web/views/partials/sidebar.ejs` (modifica se serve — link docs,
  agent docs e Audit)
- `scrocco-web/test/e2e-suite.test.js` (crea)
- `scrocco-web/README.md` (modifica — sezione agenti/API/MCP + endpoint)

## Contratto

- In `src/index.js`:
  - mount `apiDeploymentsRouter`, `apiCoreRouter`, `apiWriteRouter` sotto
    `/api/v1` (usa `app.use('/api/v1', router)` con `mergeParams`);
  - `app.use('/api/mcp', mcpRoutes)` con rate-limit che SKIPPA il loopback
    (pattern CMS);
  - `app.use('/', apiDocsRoutes)` per openapi/docs;
  - ordine: docs Public PRIMA di requireAuth? No: requireAuth nei router stessi.
    Monta tutto prima del 404 handler e dopo il body parsing.
- `test/e2e-suite.test.js`: 
  1. login UI (password) → cookie.
  2. GET `/deployments` → 200.
  3. API: GET `/api/v1/deployments` (bearer agtok_) → 200.
  4. MCP: POST `/api/mcp` tools/list → 200.
  5. openapi: GET `/api/v1/openapi.json` → 200.
  6. poke appliance: create via API → leggilo via UI.
  7. `GET /admin/audit` come `admin` → 200 con righe; come `viewer` → 403.
- README: install.md, dev, test, deploy (Caddy/:4002), agenti (agent-login,
  API, MCP), repo structure, PG.

### Pannello audit_log (read-only, admin)

- `src/routes/audit.js` — `GET /admin/audit` [requireAuth,
  `authorize("audit","read")` ma la pagina è visibile SOLO a `admin`: la
  matrice F0-07 dà `audit/read` a tutti, quindi qui esegui anche un check
  esplicito `req.user.role === 'admin'` → 403 per operator/viewer] →
  query `SELECT ... FROM audit_log ORDER BY id DESC LIMIT 200` → render.
- `views/admin/audit/index.ejs` — tabella (timestamp, utente, action,
  entity_type, route_method, ip_address, dettagli JSON troncati a 300 char,
  link `gateway_path` quando presente); nessuna azione (read-only).
- Sidebar: voce "Audit" (sezione admin) solo per `role === 'admin'`.
- Test e2e point 7 sopra.

### Guida agente locale

- `src/routes/agent-doc.js` — `GET /agent-guide` [requireAuth,
  `authorize("guide","read")`] → legge `docs/AGENT.md` con `readFileSync` e
  render di una piccola vista markdown (o `res.send` con header text/markdown).
  Serve il link sidebar "Guida agente" con un path HTTP reale (non un path di
  filesystem).

## Criterio di done

`npm test` verde TUTTO (fino e incluso e2e). `docker build -t scrocco-web .`
ok. README aggiornato.

## Rischi / note

- Sequence con rate-limit: NON mettere limiti troppo stretti su /api/mcp con
  skip loopback.
- Il modulo è "completo" quando: app starta, UI+API+MCP girano, e lo smoke
  e2e copre tutti e tre. Se un F5-xx è incompleto, segnalalo e apri una
  sotto-nota.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Completa l'integrazione finale con agenti e MCP.

Modifica:
1. `src/index.js` — aggiungi import e mount: `app.use('/api/v1',
   apiDeploymentsRouter)`; `app.use('/api/v1', apiCoreRouter)`; `app.use(
   '/api/v1', apiWriteRouter)`; `app.use('/api/mcp', mcpLimiter, mcpRoutes)`
   dove `mcpLimiter` è un express-rate-limit con `skip: loopback` (come il
   CMS); `app.use(apiDocsRoutes)`; mount `auditRoutes` e `agentDocRoutes`.
   Assicurati l'ordine corretto (docs e api PRIMA del 404 handler).
2. `views/partials/sidebar.ejs` — sezione "Agenti": link `API docs`
   `/api/v1/docs` e `Guida agente` `/agent-guide`; sezione "Admin": link
   `Audit` `/admin/audit` (solo `role === 'admin'`).
3. `src/routes/audit.js` + `views/admin/audit/index.ejs` — pannello read-only
   dell'`audit_log` (admin-only, 200 righe, tabella con dettagli troncati).
4. `src/routes/agent-doc.js` — serve `docs/AGENT.md` su `/agent-guide`.
5. `test/e2e-suite.test.js` — suite finale come da contratto (incl. audit
   200 per admin / 403 per viewer).
6. `README.md` — aggiorna con la sezione "Agenti" (auth, API, MCP, agent-login)
   e "Deploy" (Caddy :4002, edge_net).

Verifica `npm test` TUTTO verde + `docker build -t scrocco-web .` ok. Non
toccare altri file. Riepiloga in 6 righe: cosa è stato mountato, test, stato
finale.