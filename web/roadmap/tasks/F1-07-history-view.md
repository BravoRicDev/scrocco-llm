---
id: F1-07-history-view
fase: F1
dipende_da: [F0-11-integration-smoke]
puo_parallelo_con: tutti gli altri F1 read
---

# F1-07 — Pagina history/journal (sola lettura)

## Obiettivo

Pagina `/history` che mostra il journal delle operazioni admin del gateway
(`GET /admin/history?limit=`): journal dell'ultimo operatore. SOLA LETTURA.

## File da creare/modificare

- `scrocco-web/src/routes/history.js` (crea — GET /history)
- `scrocco-web/views/history/index.ejs` (crea)

## Contratto

- `GET /history?limit=` [requireAuth, authorize("history","read")].
- Dati: `gateway.get('/admin/history', {params:{limit}})` → `{total,
  entries:[{ts, op, id, profile, ...}]}`. **Il journal del gateway scrive
  `op` (non `action`): usa `entry.op` per badge/dettagli.** `limit` default 50,
  **max 100** (il gateway tronca a 100: `journal.history` fa `[:100]`).
- View: tabella ts (formattato data/ora), action (badge colorato per
  create/update/delete/bulk/probe/…), dettagli JSON (pretty, troncato a 300
  char per riga). Nessun valore segreto atteso dal gateway (già sanitarizzato).

## Criterio di done

Render manuale con mock, `node --check`.

## Rischi / note

- Il journal è append-only: nessuna scrittura qui.
- Mostra anche il nostro `audit_log`? NO: questo task guarda SOLO l'history del
  gateway (il nostro audit_log sarà esposto altrove, come /admin/audit — se
  vuoi, aggiungilo in F5-INTEGRATION).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la pagina read-only del journal del gateway.

Crea:
1. `src/routes/history.js` — Router; `GET /history` requireAuth +
   `authorize("history","read")`; `?limit` (default 50, **max 100**);
   `gateway.get('/admin/history',{params:{limit}})` → render
   `views/history/index.ejs` con `{total, entries, limit}`.
2. `views/history/index.ejs` — header "Journal operazioni gateway (totale X)",
   form limit; tabella: timestamp (locale), **op** (badge, NON action), dettaglio
   `<pre>`/`<code>` JSON truncate a 300 char; stato vuoto. Usa `entry.op`.

Non toccare altri file. Verifica `node --check` + render smoke mock. Riepiloga
in 2 righe.