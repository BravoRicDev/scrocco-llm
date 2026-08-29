---
id: F5-05-openapi-full
fase: F5
dipende_da: [F5-01-api-read-deployments, F5-02-api-read-core, F5-03-api-write, F0-09-health-openapi]
puo_parallelo_con: F5-04-mcp-server
---

# F5-05 — Spec OpenAPI completa per `/api/v1`

## Obiettivo

Riempire `src/openapi.js` (scheletro da F0-09) con le route reali create
in F5-01/02/03 + schemi; esporre `GET /api/v1/openapi.json` e una pagina
`/api/v1/docs` (HTML statico minimale, niente CDN). Serve anche come doc per
agenti e per l'MCP.

## File da creare/modificare

- `scrocco-web/src/openapi.js` (modifica — buildPaths con tutte le route)
- `scrocco-web/src/routes/api-docs.js` (crea — documentazione router)
- `scrocco-web/views/api-docs.ejs` (crea — pagina docs minimale offline,
  referenzia i vendor sotto)
- `scrocco-web/public/vendor/openapi/swagger-ui.min.js` + `.css` (crea —
  copia da node_modules? NON c'è swagger-ui in dipendenze: NON inserirli.
  Invece crea una pagina docs custom vanilla che usa lo spec JSON)
- `scrocco-web/public/vendor/openapi/simple-render.js` (crea)
- `scrocco-web/test/openapi.test.js` (crea)

## Contratto

- `src/openapi.js`: SPEC openapi 3.0, info {title: "scrocco-web API"},
  servers [{url:"/api/v1"}], securitySchemes (BearerAuth http bearer;
  CookieAuth apiKey cookie "token"), tags (deployments, profiles, policy,
  state, history, insights, observability, bootstrap, guide, system,
  capabilities, csv, playground, alerts); `paths` con TUTTE le route /api/v1
  documentate (method, summary, parameters, requestBody per write, responses
  200/400/401/403/404) con component/schemas.
- `routes/api-docs.js`:
  - `GET /api/v1/openapi.json` → res.json(spec) [publico, no auth].
  - `GET /api/v1/docs` → render HTML minimale (vista custom) che carica
    lo spec e lista endpoint; NO CDN (offline).
- Il JS renderer legge `/api/v1/openapi.json` e costruisce una lista
  accordeon (tag → paths → method badge) con forms per try (GET only, con
  Bearer input).

## Criterio di done

`node --check`; test: GET /api/v1/openapi.json → 200 JSON, e per ogni path in
`paths` esiste la nuova rotta (spot check deploy_list)/`paths` include le 3
aree. Nessuna CDN nel HTML.

## Rischi / note

- Ogni route scritta in F5-xx va DOCUMENTATA qui: se te ne manca una, apri il
  file della route e aggiungi (non duplicare logica).
- `/api/v1/docs` è pubblico (come CMS openapi docs).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Completa la spec OpenAPI.

1. `src/openapi.js` — aggiungi in fondo (o modifica lo scheletro) `buildPaths()`
   che ritorna le paths per TUTTE le route; aggiungi `components.schemas`.
   (Leggi i file delle route per parametri reali; per i path con `:id`
   aggiungi `parameters`.)
2. `src/routes/api-docs.js` — Router: `GET /api/v1/openapi.json` →
   `res.json(spec)`; `GET /api/v1/docs` → render una vista `views/api-docs.ejs`
   (crea anche quella) semplice che referenzia `/vendor/openapi/simple-render.js`
   e `/js/openapi-render.js`; `GET /js/openapi-render.js` → express.static è
   già su public → metti lì il file.
3. `public/vendor/openapi/simple-render.js` e `public/js/openapi-render.js` —
   il secondo carica spec e disegna (forms GET con Bearer input; POST/DELETE
   mostrano body template). Nessuna libreria esterna.
4. `test/openapi.test.js` — GET /api/v1/openapi.json → 200; `paths` ha
   `/api/v1/deployments`; `schemas` non vuoti.

Verifica `node --check` + npm test. Non toccare altri file. Riepiloga in 3
righe.