---
id: F3-03-leaderboard
fase: F3
dipende_da: [F1-11-integration]
puo_parallelo_con: F3-01-live-calls, F3-02-errors-view, F3-04-charts-uplot
---

# F3-03 — Leaderboard deployment (ordinabile)

## Obiettivo

Pagina `/observability/leaderboard` con la classifica deployment da
`GET /admin/insights/leaderboard?window&sort&order&profile` — ordinabile per
colonna (server-side), filtro profilo, aggiornamento 15s, colonne health e
err%. Fedele al TUI `obs_leaderboard`.

## File da creare/modificare

- `scrocco-web/src/routes/leaderboard.js` (crea)
- `scrocco-web/views/observability/leaderboard.ejs` (crea)
- `scrocco-web/public/js/leaderboard.js` (crea)

## Contratto

- `GET /observability/leaderboard` [requireAuth, authorize observability read].
- Dati: `gateway.get('/admin/insights/leaderboard', {params:{window, sort,
  order, profile}})` → `{window_days, count, rows:[{dep, profile, group,
  provider, model, calls, avg_dur_ms, p95_dur_ms, error_rate, fb_rate,
  qc_rate, wd_rate, last_used, health, probe_ms}]}`.
- Sort: valida `sort` contro i campi server validi (dep, profile, group,
  provider, model, calls, avg_dur_ms, p95_dur_ms, error_rate, last_used,
  probe_ms); toggle order su click header. Filtro profilo (input) + window
  (select 24h/7d/30d/90d).
- API `GET /api/leaderboard/data?...` (requireAuth) JSON per refresh AJAX.
- View: tabella con header cliccabili (▼▲), colori err% (rosso ≥20%),
  health badge (dead_suspect/retired/healthy), probe_ms.

## Criterio di done

`node --check`; render mock; sort server ok (il mock ordina). Test: il click
sul header → fetch con sort/order attivi.

## Rischi / note

- `p95_dur_ms` può essere null → mostra "—".
- L'ordine `asc/desc` passa al gateway; il mock replica.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la leaderboard deployment.

Crea:
1. `src/routes/leaderboard.js` — Router: `GET /observability/leaderboard`
   (requireAuth + authorize observability read; params window/sort/order/
   profile; valida sort contro lista permessa; render); `GET
   /api/leaderboard/data` (requireAuth) JSON `{rows, window_days, count}`
   per il refresh client.
2. `views/observability/leaderboard.ejs` — form: select window (24h/7d/30d/
   90d), input profilo, "Refresh 15s" toggle; tabella con `<th data-key>` per
   ogni colonna ordinabile; footer con titolo finestra (es. "7 giorni").
3. `public/js/leaderboard.js` — event delegation su `th` → cambia sort/order →
   `scw.fetch('/api/leaderboard/data?'+qs)` → rebuild righe; poll 15s.

Verifica `node --check`. Non toccare altri file. Riepiloga in 2 righe.