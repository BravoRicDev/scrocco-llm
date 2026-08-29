---
id: F3-04-charts-uplot
fase: F3
dipende_da: [F1-08-insights-view, F1-11-integration]
puo_parallelo_con: F3-01-live-calls, F3-02-errors-view, F3-03-leaderboard
---

# F3-04 — Grafici insights con uPlot

## Obiettivo

Aggiungere a `/insights` i grafici a linea/stack con **uPlot** (dipendenza già
in package.json): p95/giorno, chiamate/giorno, costo/giorno, err% per gruppo,
dai dati `GET /admin/insights?days=&group_by=day` (+ leaderboard per i p95).
Il resto della pagina resta F1-08; qui si aggiunge la sezione chart.

## File da creare/modificare

- `scrocco-web/package.json` (verifica che `uplot` sia tra le dipendenze; se
  non c'è AGGIUNGILO con npm)
- `scrocco-web/src/routes/charts.js` (crea — GET /api/charts/data)
- `scrocco-web/views/insights/index.ejs` (modifica — aggiungi contenitore
  chart + select gruppo)
- `scrocco-web/views/observability/charts.ejs` (crea — pagina dedicata con i 4
  grafici; rotta `GET /observability/charts`)
- `scrocco-web/public/js/charts.js` (crea)

## Contratto

- `GET /observability/charts?days=&group_by=` [requireAuth, authorize
  observability read] → render pagina chart.
- `GET /api/charts/data?days=30&group_by=day` (requireAuth) → JSON:
  `{days, series: {labels:[...], p95:[...], calls:[...], cost:[...],
  err_rate:[...]}}` costruito da:
  - `gateway.get('/admin/insights', {params:{days, group_by:'day'}})` →
    `by_day` con keys "YYYY-MM-DD", e per ogni entry `{calls,
    cost_reported_usd, cost_estimated_usd, avg_dur_ms, bad_rate}`;
  - p95: `gateway.get('/admin/insights/leaderboard', {params:{window:days+'d',
    sort:'p95_dur_ms', order:'asc'}})` — se i p95 del leaderboard sono
    per-deployment e non per giorno, costruisci un fallback usando
    avg_dur_ms per giorno. (DECISIONE: se il gateway non ha serie per-day dei
    p95, il grafico p95 usa la media mobile di avg_dur_ms per giorno e lo
    annota in una nota.)
  - err% = bad_rate*100.
- uPlot: link `/js/uplot.min.js` (uplot espone dist/uPlot.iife.min.js nel
  pacchetto) + css. Grafici: p95 (linea), chiamate (barre/linea), costo usd
  (linea), err% (linea con soglia rossa 20).
- Group select su `group_by` ma SERIE richiedono `day` per asse X: se si sceglie
  altro group_by, il grafico non è disponibile → nota.

## Criterio di done

`node --check`; con un mock che fornisce `by_day` popolato, la pagina charts
renderizza 4 canvas/contenitori; selezionare days cambia l'URL. Niente CDN
(niente internet in runtime): uPlot servito da `node_modules/uplot/dist`.

## Rischi / note

- NIENTE CDN: serve `/node_modules/...` esplicito NON funziona; quindi copia
  `uplot.min.js`+css in `public/vendor/` e renderizza da lì (committato).
  AGGIUNGI in questo task la copia in `scrocco-web/public/vendor/uplot/`.
- Null safety: dati con `null` → 0 per uPlot.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Aggiungi i grafici insights con uPlot (nessuna CDN).

Passi:
1. Controlla `package.json`: se `uplot` NON è tra le dipendenze, `npm install
   uplot` (aggiorna package.json+lock). Poi COPIA i file dist in pubblico:
   `mkdir -p public/vendor/uplot && cp node_modules/uplot/dist/uPlot.iife.min.js
   public/vendor/uplot/uplot.min.js && cp node_modules/uplot/dist/uPlot.min.css
   public/vendor/uplot/`.
2. Crea `src/routes/charts.js` — `GET /observability/charts` (requireAuth +
   authorize observability read; params days default 30 clamp 1..90) → render
   `views/observability/charts.ejs`; `GET /api/charts/data` (requireAuth;
   params days, group_by='day') → costruisci `{labels, p95, calls, cost,
   err_rate}` come da contratto (due chiamate gateway: insights day + 
   leaderboard). Gestione errori → JSON con `up:false`.
3. Modifica `views/insights/index.ejs` — aggiungi un link/card "Grafici" verso
   `/observability/charts`.
4. Crea `views/observability/charts.ejs` — nave filtro (days select) + 4
   `<div id=chart-p95>|<chart-calls>|<chart-cost>|<chart-err>` + script uplot
   + `/js/charts.js`.
5. Crea `public/js/charts.js` — carica dati da `/api/charts/data`, costruisce
   i 4 uPlot (line per p95/cost, barre per calls, linea err% con soglia),
   formatta azis date; aggiorna su cambio days (ricarica).

Verifica `node --check` su src e che i file vendor esistano. Non toccare altri
file. Riepiloga in 3 righe.