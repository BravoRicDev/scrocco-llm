---
id: F4-07-sticky-sessions
fase: F4
dipende_da: [F1-05-dashboard-state]
puo_parallelo_con: F4-05-key-health, F4-06-alerts, F4-08-ui-polish
---

# F4-07 — Sessioni sticky attive (dettaglio)

## Obiettivo

Pagina `/sticky` con le sessioni sticky attive dal gateway (`state.sticky_sessions`)
con dettaglio: group, età della sticky (derivata da timestamps interni non
esposti — usa ts del fetch), possibilità di release singola o multipla
(riuso delle azioni sistema). Read + azioni release (operator/admin).

## File da creare/modificare

- `scrocco-web/src/routes/sticky.js` (crea)
- `scrocco-web/views/sticky/index.ejs` (crea)
- `scrocco-web/public/js/sticky.js` (crea)

## Contratto

- `GET /sticky` [requireAuth, authorize("observability","read")] → 
  `gateway.get('/admin/state')` → `sticky_sessions:[{session_id, group}]`.
- `POST /sticky/release` [requireAuth, authorize("system","release")] body
  `{session_id?}` → `gateway.post('/admin/sessions/release', {json})`; audit;
  flash; redirect (o AJAX refresh).
- View: tabella session_id → group, badge count, bottone Release per riga +
  "release tutte" (opzioni), filter per group.

## Criterio di done

Render mock; release singola via AJAX aggiorna lista; autorizzazione: viewer
può leggere ma non release (403).

## Rischi / note

- `session_id` può contenere caratteri strani: guarda escaping.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la pagina sessioni sticky.

Crea:
1. `src/routes/sticky.js` — Router `GET /sticky` (requireAuth + authorize
   observability read) → carica state.sticky_sessions → render; `POST
   /sticky/release` (requireAuth + authorize system) zod {session_id?} →
   gateway.post('/admin/sessions/release',{json}); audit; flash; redirect.
2. `views/sticky/index.ejs` — header count + filter group + tabella
   session_id(group) con bottone release (data-destruct) e "Release tutte".
3. `public/js/sticky.js` — filtra e ricarica.

Verifica `node --check` + render mock. Non toccare altri file. Riepiloga in 2
righe.