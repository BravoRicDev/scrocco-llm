---
id: F3-05-integration
fase: F3
dipende_da: [F3-01-live-calls, F3-02-errors-view, F3-03-leaderboard, F3-04-charts-uplot]
puo_parallelo_con: []
---

# F3-05 — Integrazione F3: mount + sidebar + smoke osservabilità

## Obiettivo

Montare le route di osservabilità in `src/index.js`, abilitare i link sidebar,
e scrivere smoke test che verificano render e JSON list dell'osservabilità.

## File da creare/modificare

- `scrocco-web/src/index.js` (modifica — mount live/errors/leaderboard/charts
  + api data)
- `scrocco-web/views/partials/sidebar.ejs` (modifica — sezione Osservabilità)
- `scrocco-web/test/f3-observability.test.js` (crea)

## Contratto

- Mount: `liveRoutes`, `errorsRoutes`, `leaderboardRoutes`, `chartsRoutes`.
- Sidebar: sezione "Osservabilità" con Live `/observability/live`, Errori
  `/observability/errors`, Classifica `/observability/leaderboard`, Grafici
  `/observability/charts`.
- `test/f3-observability.test.js`: utente viewer; verifica 200 su
  `/observability/live` `errors` `leaderboard` `charts` e che
  `/api/live/events`, `/api/errors/events`, `/api/leaderboard/data`,
  `/api/charts/data` rispondano JSON con la shape attesa (events/rows/series)
  con GATEWAY_MOCK.

## Criterio di done

`DATABASE_URL=<test> GATEWAY_MOCK=1 npm test` verde (incluso F3).

## Rischi / note

- Il mount degli `api/*/data` deve essere PRIMA del 404 handler ma dopo
  opportuni rate-limit (non stretti).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Chiudi la fase osservabilità.

Modifica:
1. `src/index.js` — monta `liveRoutes`, `errorsRoutes`, `leaderboardRoutes`,
   `chartsRoutes` (import + app.use) PRIMA del 404 handler.
2. `views/partials/sidebar.ejs` — gruppo "Osservabilità" con i 4 link.
3. Crea `test/f3-observability.test.js` — utente viewer; GET (con cookie) le
   4 pagine → 200; GET i 4 `/api/...` data → 200 e body JSON con chiavi
   attese; verifica che siano JSON (Content-Type application/json).

Verifica suite verde. Non toccare altri file. Riepiloga in 3 righe.