# scrocco-web

Pannello web + API JSON + server MCP per il gateway LLM **scrocco-llm**.
Tiene la master key del gateway lato server; browser e agenti si autenticano a
`scrocco-web` (sessione JWT o API token) e non vedono mai la master key.

## Stack

- Node.js ≥ 22 (ESM), Express 4 + `express-ejs-layouts` + EJS
- PostgreSQL (`pg`), migrazioni in `db/*.sql` via `db/migrate.js`
- `helmet`, `cookie-parser`, `express-rate-limit`, `jsonwebtoken`, `bcryptjs`
- `zod` (validazione), `winston` (log), `diff` / `js-yaml` (editor config)
- `@modelcontextprotocol/sdk` (server MCP), `nodemailer` (magic-link opzionale)

## Avvio (dev)

```sh
cp .env.example .env      # DATABASE_URL, JWT_SECRET, GATEWAY_URL, GATEWAY_MASTER_KEY
npm install
npm run migrate           # schema + admin di bootstrap (BOOTSTRAP_ADMIN_EMAIL/PASSWORD)
npm start                 # http://localhost:3000
```

`GATEWAY_MOCK=1` fa girare tutto senza il gateway reale (fixture in
`test/fixtures/gateway.json`).

## Test

```sh
DATABASE_URL=postgres://postgres:pwd@localhost:5432/scrocco_web_test \
  JWT_SECRET=x GATEWAY_MOCK=1 npm test
```

`npm test` esegue `db/migrate.js` poi `node --test`. Serve un Postgres dedicato
(es. `docker run -d -e POSTGRES_PASSWORD=pwd -e POSTGRES_DB=scrocco_web_test -p 5432:5432 postgres:16-alpine`).

## Struttura

```
src/
  index.js            createApp() + mount + poller (isMain)
  config.js  db.js
  middleware/         auth (JWT|agtok_), authorize (RBAC), csrf, request-id, snapshot-hook
  services/           gateway.js (unico wrapper verso /admin/*), audit, api-tokens,
                      password, magic-link, alert-poller, config-snapshots, mcp-tools
  routes/            pagine SSR + /api/v1/* + /api/mcp + /admin/audit + /agent-guide
  openapi.js         SPEC OpenAPI 3.0 (paths introspezionati dai router /api/v1)
views/               EJS (layout layouts/admin, partials sidebar/topbar/search)
db/                  migrate.js + 00x_*.sql
docs/AGENT.md  locales/{it,en}/AGENT.md
scripts/agent-login.mjs
caddy/scrocco-web.Caddyfile
```

## RBAC

`admin` (tutto), `operator` (CRUD/bulk/probe deployment, azioni sistema, seed
capacità, playground), `viewer` (sola lettura). `PATCH policy`, scrittura CSV,
utenti, alert e il pannello `/admin/audit` sono **solo admin**. Matrice unica in
`src/constants/permissions.js`.

## Agenti

- **Login**: `node scripts/agent-login.mjs --email a@b --password '…'` → salva il
  JWT agente in `~/.scrocco-web/agent.token` (mode 600) e stampa un API token
  `agtok_…` (mostrato una sola volta).
- **API**: `Authorization: Bearer <token>` su `/api/v1/*`. Spec:
  `GET /api/v1/openapi.json`, pagina: `/api/v1/docs`. Dettagli ed esempi in
  [`docs/AGENT.md`](docs/AGENT.md) (anche `/agent-guide` nel pannello).
- **MCP**: `POST /api/mcp` (Streamable HTTP) espone `/api/v1` come tool; ogni
  tool call è proxy in loopback verso la rotta REST con lo stesso
  `Authorization` (RBAC invariato). Richiede token agente.

## Deploy

Repo separato dal gateway. Container su porta interna 3000, esposto **solo in
VPN/locale** su `:4002` via Caddy (`caddy/scrocco-web.Caddyfile`, `tls internal`,
rete `edge_net`). `docker compose up -d` avvia app + Postgres dedicato
(`docker-compose.yml`). `GATEWAY_URL=http://scrocco-llm:4001` risolve via la rete
`scrocco-llm_default`.
