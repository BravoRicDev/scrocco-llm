---
id: F1-11-integration
fase: F1
dipende_da: [F1-01-deployments-list, F1-02-profiles-view, F1-03-policy-view, F1-04-capabilities-view, F1-05-dashboard-state, F1-06-expiring-view, F1-07-history-view, F1-08-insights-view, F1-09-bootstrap-view, F1-10-guide-view]
puo_parallelo_con: []
---

# F1-11 — Integrazione F1: mount route + sidebar + smoke read

## Obiettivo

Montare in `src/index.js` TUTTE le route F1, abilitare i link nella sidebar,
aggiungere un middleware di contesto per default profile (per dashboard),
e scrivere il test di smoke che verifica che TUTTE le pagine read renderizzino
200 con `GATEWAY_MOCK`.

## File da creare/modificare

- `scrocco-web/src/index.js` (modifica — aggiungi mount + default locals)
- `scrocco-web/views/partials/sidebar.ejs` (modifica — abilita link F1)
- `scrocco-web/test/f1-read-pages.test.js` (crea)

## Contratto

- In `src/index.js` (dentro `createApp`): importa e monta `deploymentsRoutes`
  (F1-01), `profilesRoutes` (F1-02), `policyRoutes` (F1-03),
  `capabilitiesRoutes` (F1-04), `dashboardRoutes` (F1-05), `expiringRoutes`
  (F1-06), `historyRoutes` (F1-07), `insightsRoutes` (F1-08),
  `bootstrapRoutes` (F1-09), `guideRoutes` (F1-10). Aggiungi
  `res.locals.gatewayUp` se serve (opzionale). Niente altri cambi alle rotte.
- Sidebar: abilita/aggiungi link Dashboard `/`, Deployments `/deployments`,
  Profili `/profiles`, Policy `/policy`, Capacità `/capabilities`,
  Scadenze `/expiring`, Insights `/insights`, History `/history`,
  Bootstrap `/bootstrap`, Guide `/guide`, Osservabilità (ancora F3: link
  disabilitati o assenti).
- `test/f1-read-pages.test.js`: con `GATEWAY_MOCK=1` e utente viewer fixture,
  per ogni path `/`, `/deployments`, `/deployments?profile=x`,
  `/profiles`, `/policy`, `/capabilities`, `/expiring?days=14`,
  `/history`, `/insights?days=30&group_by=day`, `/bootstrap`, `/guide`
  verifica status 200 + body non vuoto. (Login come viewer: usa il cookie via
  step login test helper o pass `res.locals.user` via superuser? Più semplice:
  crea utente viewer, login con password, riusa cookie.)

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
DATABASE_URL=postgres://<test> GATEWAY_MOCK=1 npm test   # tutti verdi incl. f1-read-pages
```

## Rischi / note

- `src/index.js` è "proprietà" dei task di integrazione: questo è l'unico task
  F1 che lo tocca.
- Se una vista F1-xx ha un bug di render, questo smoke lo cattura: corri e
  sistemale (ma il fix va fatto nel file della vista, non qui).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Completa l'integrazione della fase READ del pannello.

Modifica SOLO questi 3 file:
1. `src/index.js` — dentro `createApp()`, importa e monta i router F1
   (`routes/deployments.js`, `routes/profiles.js`, `routes/policy.js`,
   `routes/capabilities.js`, `routes/dashboard.js`, `routes/expiring.js`,
   `routes/history.js`, `routes/insights.js`, `routes/bootstrap.js`,
   `routes/guide.js`) come `app.use(...)`. Non cambiare il resto del wiring.
2. `views/partials/sidebar.ejs` — abilita (spacchetta da commenti/disabled) i
   link: Dashboard `/`, Deployments `/deployments`, Profili `/profiles`,
   Policy `/policy`, Capacità `/capabilities`, Scadenze `/expiring`,
   Insights `/insights`, History `/history`, Bootstrap `/bootstrap`,
   Guide `/guide`.
3. Crea `test/f1-read-pages.test.js` — test node:test che, usando
   `startTestApp()` di `test/helpers.js` (GATEWAY_MOCK=1), crea un utente
   viewer con password, fa login (`POST /api/auth/login` → cookie), poi per
   ogni pagina della lista fa `GET` con cookie e asserisce status 200 e body
   contiene un marker atteso (es. "scrocco" o il title). In chiusura se
   `closeDb()`.

Verifica `DATABASE_URL=<test-db> GATEWAY_MOCK=1 npm test` verde per l'intera
suite. Non toccare altri file. Riepiloga in 3 righe i mount e i test.