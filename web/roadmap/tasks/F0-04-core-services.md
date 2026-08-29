---
id: F0-04-core-services
fase: F0
dipende_da: []
puo_parallelo_con: F0-01-repo-scaffold, F0-02-infra-docker-compose, F0-03-db-migrations, F0-08-layout-theme
---

# F0-04 — Core services: config, db, logger, request-id

## Obiettivo

Copiare/sfoltire dal CMS `gestione-siti-riccardom` i quattro mattoncini base:
`src/config.js` (con le env di scrocco-web), `src/db.js` (Pool pg), 
`src/services/logger.js` (winston) e `src/middleware/request-id.js`.

## File da creare/modificare

- `scrocco-web/src/config.js` (crea)
- `scrocco-web/src/db.js` (crea)
- `scrocco-web/src/services/logger.js` (crea; crea la dir `services/`)
- `scrocco-web/src/middleware/request-id.js` (crea; crea la dir `middleware/`)

NON toccare: `views/`, `db/`, `roadmap/`, `package.json`, altri file `src/`.

## Contratto

- `config.js`: identico nel pattern al CMS (default export, `dotenv.config()`,
  valori da `process.env` con fallback). Variabili:
  `port` (3000), `nodeEnv`, `databaseUrl`, `jwtSecret`, `jwtExpiresIn` ("24h"),
  `sessionCookieName` ("token"), `gatewayUrl` (da `GATEWAY_URL`, default
  `http://scrocco-llm:4001`), `gatewayMasterKey` (da `GATEWAY_MASTER_KEY`),
  `gatewayTimeoutMs` (default 10000, sovrascrivibile con
  `GATEWAY_TIMEOUT_MS`), `gatewayMock` (bool da `GATEWAY_MOCK`),
  `logLevel`, `appName` (default "scrocco-web — Gateway LLM"),
  `bootstrapAdminEmail`/`bootstrapAdminPassword` (nullable),
  `smtp{host,port,user,pass,from}` (vuoti se non configurati),
  `magicLinkBaseUrl` (default `http://localhost:3000`), `magicLinkExpiryMs` (15m).
- `db.js`: uguale al CMS (`pg.Pool` con connection string da config, max 20,
  idle 30000, connectionTimeout 5000, `pool.on('error')` loggato; export
  `query(text, params)`, `getClient()`, default pool).
- `logger.js`: identico al CMS (winston, level da config, timestamp, meta
  `{service: "scrocco-web"}`).
- `request-id.js`: identico al CMS (`crypto.randomUUID` o
  `x-request-id` header).

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
node --check src/config.js && node --check src/db.js \
  && node --check src/services/logger.js && node --check src/middleware/request-id.js
```
E un mini-test manuale se DATABASE_URL non è disponibile deve fallire in modo
chiaro (`throw new Error("DATABASE_URL non configurata")`) come nel CMS.

## Rischi / note

- `db.js` lancia all'import se manca `DATABASE_URL`: è voluto (stesso del CMS),
  quindi NON avviare l'app in test senza DB o senza GATEWAY_MOCK.
- Mantieni TUTTE le env già documentate in `.env.example` (F0-01) e NON
  aggiungerne di nuove qui senza aggiornare anche `.env.example` (se lo fai,
  nota la modifica nel criterio di done — ma è meglio non toccare package.json).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi `src/...`; se `read`
fallisce usa `cat`/`sed -n`).

Copia lo scaffold base dal CMS gemello `gestione-siti-riccardom`: leggi
`src/config.js`, `src/db.js`, `src/services/logger.js`,
`src/middleware/request-id.js` e replica il pattern ES identico (stesso stile:
import in testa, commenti in italiano, default export, `process.env` con
fallback). Poi adatta le variabili alla nuova app `scrocco-web` (pannello web
per il gateway LLM `scrocco-llm`).

Crea ESATTAMENTE questi file:
1. `src/config.js` — export default con: port(3000), nodeEnv, databaseUrl,
   jwtSecret, jwtExpiresIn("24h"), sessionCookieName("token"), gatewayUrl
   (GATEWAY_URL default "http://scrocco-llm:4001"), gatewayMasterKey
   (GATEWAY_MASTER_KEY), gatewayTimeoutMs (GATEWAY_TIMEOUT_MS default 10000),
   gatewayMock (GATEWAY_MOCK === "1"), logLevel, appName (default
   "scrocco-web — Gateway LLM"), bootstrapAdminEmail/PASSWORD,
   smtp{host,port,user,pass,from}, magicLinkBaseUrl, magicLinkExpiryMs(15min).
2. `src/db.js` — Pool pg (max 20, idleTimeoutMillis 30000,
   connectionTimeoutMillis 5000), error handler su pool con logger, export
   `query(text, params)`, `getClient()`, default pool; se manca DATABASE_URL
   lancia errore chiaro all'import.
3. `src/services/logger.js` — winston con level da config, timestamp, meta
   service "scrocco-web", console transport umanizzato.
4. `src/middleware/request-id.js` — `req.requestId =
   req.headers["x-request-id"] || crypto.randomUUID()`.

Verifica con `node --check` su tutti e quattro. Non creare altro e non toccare
altri file. Alla fine elenca cosa hai creato in 2 righe.