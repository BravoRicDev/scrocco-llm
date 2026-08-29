---
id: F5-04-mcp-server
fase: F5
dipende_da: [F5-01-api-read-deployments, F5-02-api-read-core, F5-03-api-write, F0-10-api-tokens-route]
puo_parallelo_con: F5-05-openapi-full
---

# F5-04 — Server MCP (StreamableHTTP) che espone le route `/api/v1` come tool

## Obiettivo

Esporre le operazioni `/api/v1/*` come tool MCP allo stesso modo del CMS:
`src/routes/mcp.js` (endpoint `POST /api/mcp`, auth requireAuth+agent) +
`src/services/mcp-tools.js` (discoverTools dal router reale + TOOL_META
arricchito). Nessuna duplicazione di tool: si introspeziona il Router.

**Decisione di progetto (UNICA):** ogni tool call fa un **fetch loopback su
`http://127.0.0.1:PORT/api/v1/...` con il cookie di sessione del chiamante**
(esattamente come `gestione-siti-riccardom/src/services/mcp-tools.js` fa con
`http://127.0.0.1:${config.port}${resolved}` e `Authorization` header). NON
reimplementare il routing express in-process (niente `matchRouter`,
`createReqStub`, proxy di `res`). La chiamata loopback riusa tutto il middleware
già montato (requireAuth, authorize, body-parser, error handler).

## File da creare/modificare

- `scrocco-web/src/routes/mcp.js` (crea — calco di
  `gestione-siti-riccardom/src/routes/mcp.js`)
- `scrocco-web/src/services/mcp-tools.js` (crea — calco di
  `gestione-siti-riccardom/src/services/mcp-tools.js`, adattato)
- `scrocco-web/test/mcp.test.js` (crea)

NON toccare `src/index.js` (mount F5-07).

## Contratto

- `routes/mcp.js`: `POST /api/mcp`, requireAuth + guarda `req.user.agent ===
  true || req.user.api_token === true`. StreamableHTTPServerTransport senza
  sessioni; build del server per lingua (usando `res.locals.lang` o "en");
  errori JSON-RPC. `GET/DELETE /api/mcp` → 405.
- `services/mcp-tools.js`:
  - `discoverTools(lang)`: introspetta `apiDeploymentsRouter`,
    `apiCoreRouter`, `apiWriteRouter` (li importa) — per ogni
    `METHOD /path` costruisce name (camel), description (da TOOL_META o
    generica), inputSchema (da TOOL_META con `z.string().optional()` ecc. per
    i param non conosciuti).
  - `makeToolHandler(tool)`: **calco esatto del CMS
    (`gestione-siti-riccardom/src/services/mcp-tools.js`,
    funzione `makeToolHandler`)**: legge `authorization` header della richiesta
    MCP (`extra?.requestInfo?.headers?.authorization`), risolve il path con i
    param (`resolvePath`), e fa `fetch` loopback su
    `http://127.0.0.1:${config.port}${resolved}` con `Authorization` identica
    a quella del chiamante MCP; per POST/PUT/PATCH costruisce il body JSON;
    gestisce timeout (60s) ed errori → content `isError: true`.
  - TOOL_META: copre DEPLOYMENTS (list/get/create/update/delete/bulk/probe/
    unretire), PROFILES, POLICY (read per tutti, patch per admin), STATE,
    HISTORY, INSIGHTS, LEADERBOARD, BOOTSTRAP, SYSTEM (reload/cooldown/
    release), CSV (read/write admin), PLAYGROUND, GUIDE.
  - Descrizioni bilingue {en,it} per i tool principali.
- Il loopback deve essere **ESENTE dai rate-limit per-IP**: il limiter sul
  mount `/api/mcp` in F5-07 (`mcpLimiter`) usa `skip: loopback` (pattern CMS);
  qui non serve altra logica.

## Criterio di done

`node --check`; test mcp verde; il set tools contiene almeno il tool
`deploy_list` e `deploy_create`. Il test tool call gira SE l'app è avviata
in-process (`startTestApp()` con `config.port` reale o mock fetch in test).
Non modificare le route (introspezione).

## Rischi / note

- Il loopback deve riusare l'header `Authorization` del chiamante MCP
  (é il cookie JWT o il Bearer API token): cosí la RBAC (requireAuth +
  authorize via `req.user`) del chiamante viene rispettata automaticamente.
- Zero reimplementazione del routing: /api/v1 sono già montati e girano;
  il fetch loopback li usa così come sono.
- Se l'app in test usa una porta effimera (port 0), leggere `config.port`
  impostato da `startTestApp()`; alternativa: nel test stub `makeToolHandler`
  con un secondo app listen su una porta nota.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa il server MCP.

LEGGI PRIMA i calchi: `gestione-siti-riccardom/src/routes/mcp.js` e
`gestione-siti-riccardom/src/services/mcp-tools.js` (in particolare la
funzione `makeToolHandler` a righe ~1991-2029: fetch loopback su
`http://127.0.0.1:${config.port}` + header `Authorization` del chiamante +
`resolvePath` + `contentFromResponse`). Copia quel pattern QUASI identico:
NON reimplementare il routing express in-process (niente matchRouter/
createReqStub/proxy di res).

Crea:
1. `src/services/mcp-tools.js` — importa i 3 router api (`routes/api-deployments.js`,
   `routes/api-core.js`, `routes/api-write.js`); `discoverTools(lang)` che
   introspeziona e produce i tool (name snake→camel; TOOL_META con
   descrizioni {en,it} per: deploy_list, deploy_get,
   deploy_create/update/delete, deploy_bulk, deploy_probe, deploy_unretire,
   profile_list, policy_get/policy_patch(admin), state, history, insights,
   leaderboard, system_reload, system_clear_cooldown, system_release_sessions,
   csv_get/csv_save(admin), playground_run, bootstrap_status, guide_read);
   `makeToolHandler(tool)` che fa **fetch loopback**:
   `const authHeader = extra?.requestInfo?.headers?.authorization;`
   `url = http://127.0.0.1:${config.port}${resolved(path, args)}` con
   `headers: { Authorization: authHeader }` (+ Content-Type per POST/PUT/PATCH,
   body JSON), timeout 60s, `contentFromResponse(resp)` (testo o blob JSON).
   Usa `services/logger.js` per gli errori.
2. `src/routes/mcp.js` — `POST /api/mcp` (requireAuth + check agent/api_token)
   StreamableHTTPServer transport; lingua da `res.locals.lang`/cookie; errore
   JSON-RPC; `GET/DELETE /api/mcp` → 405. Calco del CMS.
3. `test/mcp.test.js` — con `startTestApp()` (porta reale): user agent (API
   token) → POST /api/mcp tools/list → contiene `deploy_list`; tools/call su
   deploy_list → data. Se serve, mocka `fetch` per il loopback.

Verifica `node --check` + `npm test` per la sola suite mcp. Non toccare altri
file. Riepiloga in 4 righe (soprattutto il metodo loopback col cookie/Bearer).