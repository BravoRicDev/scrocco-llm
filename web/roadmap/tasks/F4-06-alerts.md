---
id: F4-06-alerts
fase: F4
dipende_da: [F1-05-dashboard-state]
puo_parallelo_con: F4-05-key-health, F4-07-sticky-sessions, F4-08-ui-polish
---

# F4-06 — Alert rule pool santé (+ poller + notifica webhook/telegram)

## Obiettivo

Regole di alert stile "se la salute `-Nk` (pool/gruppo) scende sotto X% →
notifica": CRUD delle regole in Postgres (tabella `alert_rules` da F0-03) in
pagina `/alerts`, più un **poller** (`services/alert-poller.js`) che ogni N
secondi calcola la salute del pool (dal `state` del gateway) e notifica
webhook POST o telegram (se configurati), rispettando `notify_min_interval_sec`.
Solo admin per la gestione.

## File da creare/modificare

- `scrocco-web/src/services/alert-poller.js` (crea)
- `scrocco-web/src/routes/alerts.js` (crea)
- `scrocco-web/views/alerts/index.ejs` (crea)
- `scrocco-web/src/config.js` (modifica — append `telegramBotToken` da
  `TELEGRAM_BOT_TOKEN`, default "")
- `scrocco-web/.env.example` (modifica — aggiungi `TELEGRAM_BOT_TOKEN=` vuoto)
- Test: `test/alert-poller.test.js` (crea, opzionale ma consigliato)

NON toccare `src/index.js` (il start del poller è F4-09-integration).

## Contratto

- `alert_rules` (esistente): id, name, enabled, pool_filter (glob su
  gruppo/unique, es 'scrocco-llm-*-128k' o profilo/group), health_threshold_pct
  int, check_every_sec, webhook_url, telegram_chat_id, notify_min_interval_sec,
  last_notified_at, created_by, created_at.
- `services/alert-poller.js`:
  - export `createPoller({ run }?)` → `{ start(opts), stop() }` con un
    `setInterval` per regola (usa `check_every_sec`):
  - `checkAndNotify(rule)`:
    1. legge `gateway.get('/admin/state')` → costruisce la santé per gruppo:
       `groups` da state (via config? state non espone gruppi con salute...).
       → Approccio: usa il leaderboard `GET /admin/insights/leaderboard?
       window=1h&sort=error_rate&order=desc` → per ogni row (dep) abbiamo
       `error_rate` (0..1) e health. Calcola per pool (group prefix) la
       percentuale sana = 100 * (1 - media error_rate del pool) / 1.
    2. se `health_pct < threshold` e ora-last_notified >= interval →
       notifica: webhook POST (fetch, JSON {rule, pool, health_pct, rows,
       timestamp}), telegram via `https://api.telegram.org/bot<TOKEN>/sendMessage`? NIENTE token: la notifica telegram è un POST text/plain a un webhook telegram (chat_id + message) su `TELEGRAM_BOT_TOKEN`? piu semplice: invia come webhook: body alla webhook_url; per telegram se telegram_chat_id presente e `ALERT_TELEGRAM_BOT_TOKEN` configurato in env → `https://api.telegram.org/bot${token}/sendMessage` con chat_id e text. In env .env aggiungi `TELEGRAM_BOT_TOKEN`.
    3. aggiorna `last_notified_at`.
  - il poller NON blocca: try/catch; log errori; opzione `dryRun`.
- `routes/alerts.js`:
  - `GET /alerts` [requireAuth, authorize("alerts","read")] → CRUD view +
    "stato poller" (from service).
  - `POST /alerts` [create] zod {name, pool_filter, health_threshold_pct,
    check_every_sec, webhook_url?, telegram_chat_id?, notify_min_interval_sec}
    → insert, audit.
  - `POST /alerts/:id/toggle` [update] → enabled flip.
  - `POST /alerts/:id/delete` [delete] → delete, audit.
- View: lista regole con toggle + delete, form aggiunta.

## Criterio di done

`node --check`; unit-test del poller con gateway mock: una regola con soglia
alta e leaderboard err% alto → `notify` invocata (mock fetch). Test con
`notify_min_interval_sec` non scaduto → non notifica. 

## Rischi / note

- Il poller parte in F4-09 (index.js): qui solo service + route + test.
- NESSUN volume di stato: last_notified_at nel DB.
- `pool_filter` glob (semplice `substring` o prefix match su group) — evita
  regex full.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa gli alert di salute pool.

Crea:
1. `src/services/alert-poller.js` — export `createPoller()`:
   - `start()`: legge regole enabled da DB (`SELECT * FROM alert_rules WHERE
     enabled`), per ognuna `setInterval(interval check_every_sec*1000)` che
     chiamo `checkAndNotify(rule)`; ritorna `{stop}`.
   - `checkAndNotify(rule)`: fetch `gateway.get('/admin/insights/leaderboard',
     {params:{window:'1h', sort:'error_rate', order:'desc'}})`; filtra righe
     per `rule.pool_filter` (match se `row.group` o `row.profile` o `row.dep`
     contiene la stringa); health_pct = 100 * (1 - avg(error_rate)) ; se
     `< threshold` e `cooldown` (last_notified_at null o > interval) →
     `notify(rule, healthPct, rows)`.
- `notify`: webhook (POST JSON a `rule.webhook_url` via fetch con timeout
      5s, body {rule, health_pct, timestamp, rows:[{dep,error_rate,group}]});
      se `rule.telegram_chat_id` e `config.telegramBotToken` → fetch
      `https://api.telegram.org/bot<TOKEN>/sendMessage` form/chien JSON
      {chat_id, text}. Poi UPDATE last_notified_at.
    - try/catch ovunque + log (non crasha mai).
    - `status()` → {started, activeRules, lastRunAt}.
    - Aggiungi `telegramBotToken` a `src/config.js` e `.env.example`
      (da `TELEGRAM_BOT_TOKEN`, default "") — append, minore.
2. `src/routes/alerts.js` — Router CRUD (requireAuth + authorize alerts),
   con lista e form; ogni mutazione auditLog; toggle/delete.
3. `views/alerts/index.ejs` — tabella regole (enabled toggle, pool_filter,
   soglia, interval, webhook, last_notified) + form nuova regola + stato
   poller (badge started).

Non toccare `src/index.js`. Verifica `node --check` + `test/alert-poller.test.js`
(usa le fixture mock: leaderboard con error_rate alto). Non toccare altri
file. Riepiloga in 3 righe.