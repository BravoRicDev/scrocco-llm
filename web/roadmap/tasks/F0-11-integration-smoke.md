---
id: F0-11-integration-smoke
fase: F0
dipende_da: [F0-01-repo-scaffold, F0-02-infra-docker-compose, F0-03-db-migrations, F0-04-core-services, F0-05-gateway-client, F0-06-api-tokens-service, F0-07-auth-rbac, F0-08-layout-theme, F0-09-health-openapi, F0-10-api-tokens-route]
puo_parallelo_con: []
---

# F0-11 — Integrazione F0: wiring index.js + test harness + smoke

## Obiettivo

Montare TUTTI i moduli F0 in `src/index.js` (stile CMS): helmet, cookie-parser,
request-id, csrf, ejs+layouts, static, mount auth/api-tokens/health, error
handler con messaggi sicuri, listener su PORT; creare il test harness
(`test/helpers.js`, `test/harness-load-mock.mjs`) e uno smoke test end-to-end
con `GATEWAY_MOCK` che prova: login→me, /health, 404, login redirect.

## File da creare/modificare

- `scrocco-web/src/index.js` (crea)
- `scrocco-web/test/helpers.js` (crea)
- `scrocco-web/test/auth.e2e.test.js` (crea)
- `scrocco-web/test/health.test.js` (crea)

NON toccare: altri `src/` (tranne append? NO: index.js li importa), `db/`,
`views/`, `roadmap/`.

## Contratto

- `src/index.js`: identico per struttura al CMS `src/index.js` ma con mount di:
  - middleware base (trust proxy, helmet con CSP off, cookieParser, requestId,
    express.urlencoded+json 50mb, csrf)
  - jwt user bootstrap da cookie (come CMS: `app.use((req,res,next)=>...)`)
  - ejs + layouts + static `public/`
  - `app.set('view engine','ejs')`, `views` da `../views`
  - res.locals (app, user, path, query, flash, t fallback da
    `services/i18n`, lang, escapeAttr)
  - rotte: `authRoutes`, `healthRoutes`, `apiTokensRoutes`
  - 404 handler: `/api` → JSON 404, altrove render/redirect
  - error handler finale: `err.code === "22P02"` → 404 (pattern CMS);
    `/api` → JSON; web → render `error` con messaggio sicuro (in production
    nascondi err.message)
  - start: `app.listen(config.port)` con log; unhandledRejection/uncaughtException
    come CMS
  - bootstrap check: se `config.jwtSecret` o `config.databaseUrl` mancanti →
    exit(1) con log FATAL; se `config.gatewayMasterKey` mancante → warning ma
    non fatale (in dev si può usare GATEWAY_MOCK)
  - in sviluppo, se `config.gatewayMock` → log "GATEWAY_MOCK=1 (nessuna
    chiamata reale al gateway)".
- `test/helpers.js`: come CMS (uniqueEmail, uniqueDomain, crea utente/token)
  + helper `mockApp()` che importa index ma senza listen? NO: meglio esportare
  `createApp()` da index.js (refactor logico: `index.js` esporta `createApp()`
  e chiama `listen` solo se `import.meta.url === process.argv[1]` var guard) →
  i test fanno `createApp()` + `app.listen(0)` . REQUISITO: `index.js` deve
  esportare `export async function createApp()` e avviare solo quando eseguito
  direttamente (pattern `if (isMain) start()`).
- `test/auth.e2e.test.js`: con GATEWAY_MOCK e DB test:
  - POST /api/auth/login con utente fixture (password bcrypt creata nel setUp)
    → 200 + cookie `token`; GET /api/auth/me con cookie → utente
  - POST login password errata → 401
  - GET /admin/profilo (ancora non esiste) → 404; GET /deployments → 404 fino a F1
  - GET / → redirect /login senza cookie
- `test/health.test.js`: GET /health con gateway mock e DB → 200 {ok:true}.
- Setup test: `npm test` esegue `db/migrate.js` su DATABASE_URL (test usa un
  DB dedicato `scrocco_web_test`); helpers puliscono tra file con prefissi
  unici (pattern CMS). Necessario: nel harness, prima di ogni suite creare
  utente admin fixture e set delle env (process.env.GATEWAY_MOCK='1',
  GATEWAY_URL='http://127.0.0.1:1', SMTP disattivato).

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
# con DB test dedicato e GATEWAY_MOCK=1:
DATABASE_URL=postgres://... npm test
# esce verde (almeno 2 file test: auth e2e + health)
node -e "import('./src/index.js').then(m=>console.log(typeof m.createApp))"  # function
docker build -t scrocco-web:test .     # spostato qui da F0-02: src/views/public
                                         # ora esistono e il build è deterministico
```

## Rischi / note

- `createApp()` separato dalla partenza è ESSENZIALE per i test: senza,
  `node --test` appenderebbe il server.
- Il gateway reale NON è richiesto: tutto passa da GATEWAY_MOCK delle fixture
  (F0-05).
- Il `docker build` assegnato a questo task (non a F0-02): F0-02 era nella
  stessa wave di F0-04/F0-08 (che creano src/views/public) → build lì sarebbe
  non-deterministico.
- Non iniziare i task F1 se F0-11 non è verde.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi `src/...`, `test/...`;
se `read` fallisce usa `cat`/`sed -n`).

Completa l'integrazione dello scaffold del pannello `scrocco-web`. Stile e
structure di riferimento: `gestione-siti-riccardom/src/index.js` e
`gestione-siti-riccardom/test/helpers.js`.

Crea ESATTAMENTE:
1. `src/index.js` — esporta `export async function createApp() { ... return
   app }`; avvia `app.listen` SOLO se eseguito direttamente (guard
   `isMain`). Dentro createApp: helmet({contentSecurityPolicy:false,
   crossOriginEmbedderPolicy:false}), trust proxy 1, cookieParser, requestId,
   express.urlencoded/json (limit 50mb), csrfProtection, bootstrap utente da
   cookie (jwt), view engine ejs + expressLayouts + express.static public,
   res.locals (app/user/path/query/flash/t/lang/escapeAttr), mount:
   `authRoutes` (da src/routes/auth.js), `healthRoutes` (src/routes/health.js),
   `apiTokensRoutes` (src/routes/api-tokens.js). Poi 404 handler
   (`/api` → JSON {error}, altrove redirect `/`), error handler finale (22P02
   → 404; /api → JSON; `config.nodeEnv==="production"` → messaggio sicuro
   senza err.message). FATAL se mancano jwtSecret/databaseUrl; warning se
   manca gatewayMasterKey e NODE_ENV=development; log `GATEWAY_MOCK=1` se
   attivo. (Aggiungi anche le rotte `/login` e `/verify` se non già in auth.js.)
2. `test/helpers.js` — pattern CMS: `uniqueEmail`, `uniqueDomain`, 
   `createTestUser(role='admin')` (password_hash via `src/services/password.js`),
   `createTestApiToken(userId)`, `closeDb()`, e helper `startTestApp()` che
   setta env (GATEWAY_MOCK="1", GATEWAY_URL="http://127.0.0.1:1", SMTP vuoto),
   importa `createApp()`, fa `app.listen(0)`, ritorna {app, port, base, server}.
   (Nota: `pool` è creato al primo import di `src/db.js`; in test ok.)
3. `test/health.test.js` — `GET /health` → 200 {ok:true}; con DB giù sarebbe
   503 ma nel test il DB è up.
4. `test/auth.e2e.test.js` — setup utente admin; login corretto → 200 + cookie
   `token`; `GET /api/auth/me` con cookie → user; login password errata → 401;
   `GET /` senza cookie → 302 a /login; `GET /api/nope` → 404 JSON.

Verifica `DATABASE_URL=postgres://... GATEWAY_MOCK=1 npm test` verde (crea il
DB di test prima, es. `createdb scrocco_web_test`). Se non hai Postgres,
almeno `node --check src/index.js test/helpers.js test/*.test.js` + annota.
Non toccare altri file di src/. In 3 righe riepiloga il wiring e i test.