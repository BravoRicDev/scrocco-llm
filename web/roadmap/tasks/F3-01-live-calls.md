---
id: F3-01-live-calls
fase: F3
dipende_da: [F1-11-integration]
puo_parallelo_con: F3-02-errors-view, F3-03-leaderboard, F3-04-charts-uplot
---

# F3-01 — Live calls (polling + auto-refresh)

## Obiettivo

Pagina `/observability/live` con le ultime chiamate del gateway in tempo reale:
polling a `GET /admin/logs/calls?tail=&since=` ogni 2s (fragment auto-refresh
via JS — si può usare la stessa tecnica di htmx ma in JS puro come il resto del
progetto), filtri tag e pausa. Fedele alla schermata TUI "CHIAMATE LIVE".

## File da creare/modificare

- `scrocco-web/src/routes/live.js` (crea)
- `scrocco-web/views/observability/live.ejs` (crea)
- `scrocco-web/public/js/live.js` (crea)

## Contratto

- `GET /observability/live` [requireAuth, authorize("observability","read")].
- Dati via `gateway.get('/admin/logs/calls', { params: { tail, since, tags } })`
  → `{events:[{ts, tag, ...}]}`. `tag="summary"` sono le chamate; mostra le
  colonne: ora, profilo, gruppo, deployment, modello, try, fb, ms, status.
- API `GET /api/live/events?since=` (requireAuth osservability) JSON con i
  nuovi eventi (fragment per il poller): wrapper applica il filtro `since` e
  ritorna gli eventi tail 500 nell'ordine. (Si può anche fare direttamente la
  chiamata pubblica del gateway, ma meglio passare dal wrapper per coerenza e
  rate-limit.)
- View: tabella + stato "in pausa/running"; bottone pausa; select tag; input
  since. `live.js` fa polling ogni 2s con `scw.fetch('/api/live/events?since=
  <lastTs>')` e aggiunge righe in cima (più recente in alto); cap a 500 righe.

## Criterio di done

`node --check`; render mock. Test opzionale: con fixture logs, GET
/api/live/events?since=... ritorna solo eventi nuovi. Il polling NON deve
dare 500 se il gateway è giù: mostra "errore gateway" e riprova.

## Rischi / note

- `since` come epoch secondi o ms float: il mock/parse deve essere coerente
  (fixture usa `ts` secondi). Normalizza a secondi nel client.
- Nessuna libreria: solo ciò che è già in dipendenze.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la vista "live calls".

Crea:
1. `src/routes/live.js` — Router: `GET /observability/live` (requireAuth +
   authorize observability read) → render live.ejs (carica anche i primi
   eventi con tail=200); `GET /api/live/events` (requireAuth observability
   read) `?since=` → JSON `{events}` da `gateway.get('/admin/logs/calls',
   {params:{tail:500, since, tags:'summary'}})`; gestione errori (mock o
   giù) → `{events:[]}`.
2. `views/observability/live.ejs` — header con badge stato (live/pausa) e
   bottone pausa; riga filtri (tag select: summary, route, identity,
   fallback; input "min since"); `<table id=live>`; script `/js/live.js`.
3. `public/js/live.js` — polling ogni 2s; `let paused=false`; `let lastTs`
   dall'ultimo evento visto; `scw.fetch('/api/live/events?since='+lastTs)`;
   prepende righe; mantiene max 500; se errore → mostra banner ma continua.

Verifica `node --check`. Non toccare altri file. Riepiloga in 2-3 righe.