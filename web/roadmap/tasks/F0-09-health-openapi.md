---
id: F0-09-health-openapi
fase: F0
dipende_da: [F0-04-core-services, F0-05-gateway-client]
puo_parallelo_con: F0-07-auth-rbac
---

# F0-09 — Endpoint health + base openapi.js

## Obiettivo

Creare `/health` (usato da Docker healthcheck), `/api/health` con stato di
DB+gateway, e lo scheletro di `src/openapi.js` (oggetto SPEC costruito a
runtime, stesso pattern del CMS) con voci per la surface `/api/v1` che verrà
riempita in F5.

## File da creare/modificare

- `scrocco-web/src/routes/health.js` (crea)
- `scrocco-web/src/openapi.js` (crea — scheletro con struttura, security
  scheme, tags; paths saranno arricchiti in F5)
- `scrocco-web/test/fixtures/gateway.json` (NON crearlo qui: è di F0-05; qui
  solo assicurati che `health.js` non lo richieda)

NON toccare: `src/index.js` (task F0-11), altri file `src/`.

## Contratto

- `routes/health.js`:
  - `GET /health` → 200 `{ok: true, status: "ok"}` se DB `SELECT 1` ok e
    gateway (`gateway.health()`) ok o `gatewayMock`; altrimenti 503
    `{ok: false, db, gateway}`. Sempre JSON. Questo endpoint NON richiede
    auth (usato da healthcheck Docker).
  - `GET /api/health` → stessa shape ma include `version`, `uptime`,
    `gatewayError` (se il gateway è giù) — richiede `requireAuth`? NO: utile
    agli agenti, quindi pubblico come il CMS /v1/openapi.json (documentazione).
- `openapi.js`: default export dello SPEC OpenAPI 3.0 (info con title
  "scrocco-web API", version, servers [{url:"/api"}], securitySchemes
  `BearerAuth` http bearer + `CookieAuth` apiKey cookie, tags per le risorse
  (deployments, profiles, policy, capabilities, state, expiring, history,
  insights, observability, bootstrap, guide, csv, playground, config, alerts),
  `paths: {}` vuoto (verrà riempito con `buildPaths()` in F5-05). Export anche
  `registerOpenApiRoutes(app)` opzionale? NO: le route docs sono montate in
  F0-11 (integration) per non toccare openapi oltre lo scheletro.
- Stile: identico a `gestione-siti-riccardom/src/openapi.js` (pattern oggetto
  JS, commenti che spiegano come aggiungere una voce in `paths`).

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
node --check src/routes/health.js && node --check src/openapi.js
node -e "const s=require('./src/openapi.js').default; console.log(s.openapi, s.info.title)"
```
`node --check` non basta: import dello schema e print dei campi base.

## Rischi / note

- `requireAuth` per `/api/health` NON richiesto: resta pubblico (shape
  innocua), come `/v1/openapi.json` del CMS.
- Non inventare `fetch` verso il gateway: chiama `gateway.health()` da F0-05.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi `src/...`; se `read`
fallisce usa `cat`/`sed -n`).

Crea due file per il pannello `scrocco-web`:
1. `src/routes/health.js` — router con `GET /health` (Pubblico, 200
   `{ok:true,status:"ok"}` se `SELECT 1` sul DB ok e `gateway.health()` ok o
   `config.gatewayMock`; altrimenti 503 con `{ok:false, db, gateway}`) e
   `GET /api/health` (stessa shape + version/uptime/gatewayError, pubblico).
   Usa `src/db.js` (query `SELECT 1`), `src/services/gateway.js`
   (`gateway.health()`), `src/config.js`. Timeout contenuto (2s) per non
   bloccare l'healthcheck.
2. `src/openapi.js` — default export SPEC OpenAPI 3.0: info
   {title:"scrocco-web API", version:"0.1.0"}, servers [{url:"/api"}],
   securitySchemes BearerAuth (http bearer) + CookieAuth (apiKey in cookie,
   name "token"), tags per le risorse della roadmap (deployments, profiles,
   policy, capabilities, state, expiring, history, insights, observability,
   bootstrap, guide, csv, playground, config, alerts), `paths: {}` (verrà
   riempito da F5-05). Guarda lo stile di
   `gestione-siti-riccardom/src/openapi.js` per i commenti e la struttura.

Verifica: `node --check` su entrambi e un import smoke che stampa
`openapi` + `info.title`. Não toccare `src/index.js` né altri file. Riepiloga
in 2 righe.