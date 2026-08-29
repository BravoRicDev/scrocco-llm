---
id: F3-02-errors-view
fase: F3
dipende_da: [F1-11-integration]
puo_parallelo_con: F3-01-live-calls, F3-03-leaderboard, F3-04-charts-uplot
---

# F3-02 — Errori filtrabili

## Obiettivo

Pagina `/observability/errors` che legge `GET /admin/logs/errors?tail&since&filter`
con filtro testuale/numerico, refresh manuale + auto 5s, e dettaglio a riga.
Fedele alla schermata TUI "ERRORI TRACCIATI".

## File da creare/modificare

- `scrocco-web/src/routes/errors.js` (crea)
- `scrocco-web/views/observability/errors.ejs` (crea)
- `scrocco-web/public/js/errors.js` (crea)

## Contratto

- `GET /observability/errors?filter=&tail=` [requireAuth, authorize
  observability read] → render.
- API `GET /api/errors/events?...` (requireAuth) JSON `{events}` da
  `gateway.get('/admin/logs/errors',{params:{tail, filter, since}})`.
- View: input filtro live (trigger su input), tabella: ora, status (badge rossi
  4xx/5xx), tipo, messaggio. Auto-refresh 5s.
- `errors.js`: polling; quando il filtro cambia → richiesta. Numero max eventi
  visibili 500, newest in alto.

## Criterio di done

`node --check`; render mock. L'endpoint deve restituire `{events:[]}` senza
errore quando il gateway è giù (stessa gestione di F3-01).

## Rischi / note

- `filter` può essere un numero (status) o substring (error_type/message):
  passa il valore grezzo come da contratto.
- Auto-refresh 5s per non dare 429 al gateway (tail contenuto).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la vista errori.

Crea:
1. `src/routes/errors.js` — Router: `GET /observability/errors` (requireAuth +
   observability read) render; `GET /api/errors/events` (requireAuth) con
   `?filter=&tail=&since=` → `gateway.get('/admin/logs/errors',{params})`,
   sempre JSON `{events}` (mai 5xx sul client anche se il gateway è giù →
   `{events:[]}` + campo `up:false`).
2. `views/observability/errors.ejs` — input filtro (placeholder
   "status o testo"), badge auto-refresh 5s, `<table>` con colonne ora/status/
   tipo/messaggio.
3. `public/js/errors.js` — debounce filtro (300ms) + poll 5s, prepend.

Verifica `node --check`. Non toccare altri file. Riepiloga in 2 righe.