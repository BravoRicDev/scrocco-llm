---
id: F1-10-guide-view
fase: F1
dipende_da: [F0-11-integration-smoke]
puo_parallelo_con: tutti gli altri F1 read
---

# F1-10 — Pagina guide agente (sola lettura)

## Obiettivo

Pagina `/guide` che mostra `/admin/guide` (AGENT.md del gateway, markdown) in
una scatola scrollabile con link di download. SOLA LETTURA.

## File da creare/modificare

- `scrocco-web/src/services/gateway.js` (modifica — aggiunge `gateway.rawGet`
  per body testo; è la risorsa di F0-05, qui SOLO append del metodo nuovo)
- `scrocco-web/src/routes/guide.js` (crea — GET /guide)
- `scrocco-web/views/guide/index.ejs` (crea)

## Contratto

- `GET /guide` [requireAuth, authorize("guide","read")].
- Dati: `gateway.get('/admin/guide')` — il gateway ritorna il markdown come
  body (`FileResponse` → testo). Il wrapper `gateway.get` ritorna il body
  testuale (stringa) per questo endpoint: se il tipo è markdown, non tentare
  di parsare JSON. (gestione: se `gateway.get('/admin/guide')` ecceziona per
  content-type, estendi il wrapper con `gateway.rawGet` per body text —
  dichiarato qui come VARIANTE CONTRATTUALE del wrapper; vedi nota.)
- View: `<pre>` con il markdown + bottone "scarica AGENT.md" (link a
  `/admin/guide` raw via href download) e riga "guida ufficiale del protocollo
  per agenti".

## Criterio di done

Render manuale con mock che fornisce una fixture di testo markdown per
`/admin/guide`, `node --check`.

## Rischi / note

- Il client esterno non deve mostrare un JSON parse error per il markdown:
  usa `gateway.rawGet` (text) se il PATH è `/admin/guide`. Aggiungi
  `gateway.rawGet(path, opts)` in `src/services/gateway.js` SOLO se non
  esiste ancora (F0-05 non lo definì: este simplemente la firma).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la pagina guide agente.

1. Se il wrapper `src/services/gateway.js` non ha ancora il metodo, aggiungi
   `gateway.rawGet(path, opts)` che ritorna il body come testo (senza parse
   JSON) — è l'unica eccezione al fetch JSON (l'endpoint a cui chiamiamo è
   `/admin/guide`, che ritorna markdown).
2. Crea `src/routes/guide.js` — Router; `GET /guide` requireAuth +
   `authorize("guide","read")`; `gateway.rawGet('/admin/guide')`; render
   `views/guide/index.ejs` con `{markdown}`.
3. Crea `views/guide/index.ejs` — `<pre>` scrollabile con il testo, bottone
   download (`<a href="/guide?download=1"` o direttamente il raw path) e nota
   che è la guida ufficiale del protocollo.

Verifica `node --check` (tutti e 3 i file, gateway incluso) + test rapido
mock (`GATEWAY_MOCK=1` con fixture testuale per /admin/guide). Non toccare
altri file. Riepiloga in 2 righe.