---
id: F4-09-integration
fase: F4
dipende_da: [F4-01-csv-editor, F4-02-playground, F4-03-policy-raw-editor, F4-04-config-history, F4-05-key-health, F4-06-alerts, F4-07-sticky-sessions, F4-08-ui-polish, F3-05-integration]
puo_parallelo_con: []
---

# F4-09 — Integrazione F4: snapshot-hook, poller start, mount, smoke

## Obiettivo

Chiudere la fase "di più": montare le nuove route (csv-editor, playground,
policy-raw, config-history, key-health, alerts, sticky) e i relativi
`/api/*`; avviare il poller alert in `src/index.js`; creare lo
**snapshot-hook** `src/middleware/snapshot-hook.js` che crea uno snapshot in
`config_snapshots` dopo ogni save di CSV/gateway.yaml riuscito; aggiornare la
sidebar e scrivere smoke test che verificano render+azioni principali.

## File da creare/modificare

- `scrocco-web/src/index.js` (modifica — mount + avvio poller + snapshot hook)
- `scrocco-web/src/middleware/snapshot-hook.js` (crea)
- `scrocco-web/src/config.js` (modifica — append `alertPollerDisabled` da
  `ALERT_POLLER_DISABLED==="1"`, default false)
- `scrocco-web/views/partials/sidebar.ejs` (modifica)
- `scrocco-web/test/f4-extra.test.js` (crea)
- `scrocco-web/.env.example` (modifica — aggiungi `TELEGRAM_BOT_TOKEN=` e
  `ALERT_POLLER_DISABLED=0` se non già presenti da F4-06)

## Contratto

- `snapshot-hook.js`: `export function withSnapshot(kind)` → middleware che
  passa; alla risposta `res.status < 400` E path matching (POST
  `/api/csv-editor/save` → kind 'csv'; POST `/api/policy-raw/save` → kind
  'yaml') legge il body che la route ha messo su `res.locals.lastSavedRaw`
  (il contratto: le route F4-01/F4-03 impostano `res.locals.lastSavedRaw` =
  raw salvato e `res.locals.lastSavedSourceSha`). Il hook crea
  `createSnapshot({kind, source: raw, sha, userId: req.user?.sub})`
  coba in background (fire-and-forget con catch).
  → MODIFICA LEGGERA a F4-01/F4-03? Non richiesta: se le route non settano
  `res.locals.lastSavedRaw`, il hook logga e passa. Le route di F4 settano già
  `res.locals` solo se lo avevano scritto: per evitare di rifare artefatti,
  AGGIUNGI come append surgelato: il hook legge `res.locals` guardando se
  esiste `lastSavedRaw`; se vuoto, prova a rileggere da `GET /admin/csv` o
  `/admin/policy/raw` (2 chiamate extra, accettabile). Fai così: fallback
  live-read.
- In `src/index.js`:
  - mount `csvEditorRoutes`, `playgroundRoutes`, `policyRawRoutes`,
    `configHistoryRoutes`, `keyHealthRoutes`, `alertsRoutes`,
    `stickyRoutes` + relativi `/api/...`.
  - `const poller = createPoller(); poller.start();` — stop su SIGTERM.
  - `res.locals.telegramHint` = config.telegramBotToken ? true : false.
- Sidebar: sezione "Strumenti": Playground, CSV editor, Config history,
  gateway.yaml (raw) (admin), Key health, Alerts (admin), Sticky; aggiungi
  `data-nav-item` ai link esistenti e l'include del componente ricerca
  `<%- include('partials/search') %>` (creato da F4-08) in testa alla nav.
- `test/f4-extra.test.js`: con mock e utente operator: 200 su
  `/playground`, `/csv-editor`, `/key-health`, `/sticky`, `/alerts` (admin
  solo: crea admin). POST playground/run → 200. POST save csv con raw valido
  → ok e `config_snapshots` ha una riga nuova (hook). Admin runtime:

## Criterio di done

`DATABASE_URL=<test> GATEWAY_MOCK=1 npm test` verde (incluso F4).
`node --check` su snapshot-hook.

## Rischi / note

- Il poller va avviato SOLO se `!gatewayMock` false? NO: con mock fa niente
  (fetch fallisce → log). Per i test, `createPoller({disabled:true})` via env
  `ALERT_POLLER_DISABLED=1` (set in helpers).
- `oauth` non c'è: nessuna dipendenza extra.
- Sei l'ultimo task che tocca `index.js` prima di F5 — dopo di te F5 lo
  tocca ancora (F5-07).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Chiudi la fase "extra".

Crea/modifica:
1. `src/middleware/snapshot-hook.js` — `withSnapshot(kind)` middleware:
   `const raw = res.locals.lastSavedRaw || await fetchFallback()` (per kind
   csv → `gateway.get('/admin/csv')` e usa `.raw`; per yaml →
   `/admin/policy/raw` `.raw`) POI se `res.statusCode < 400` chiama
   `createSnapshot({kind, source:raw, sha:sha256(raw).slice(0,16), userId:
   req.user?.sub})` con catch log. Export anche la create accessibile.
2. `src/routes/csv-editor.js` + `src/routes/policy-raw.js`: NON devono essere
   modificate qui — se il hook fallback è ok, non serve. (Solo nel caso, in
   README della task è documentato.)
3. `src/index.js` — monta i 7 router; `createPoller()` avviato se
   `!config.alertPollerDisabled` (importa env `ALERT_POLLER_DISABLED` nel
   config? aggiungi a `src/config.js`: `alertPollerDisabled` da
   `ALERT_POLLER_DISABLED==="1"` + la voce in `.env.example` se non c'è) con
   `poller.start()` e `process.on('exit',
   ()=>poller.stop())`.
4. `views/partials/sidebar.ejs` — sezione Strumenti con link (Playground,
   CSV editor, Config history, gateway.yaml, Key health, Sticky, Alerts) +
   `<%- include('partials/search') %>` e `data-nav-item` sui link (per la
   ricerca di F4-08).
5. `test/f4-extra.test.js` — smoke come da contratto + verifica snapshot
   hook con create via POST save (raw valido) e query a config_snapshots.

Verifica suite verde. Non toccare altri file. Riepiloga in 3 righe.