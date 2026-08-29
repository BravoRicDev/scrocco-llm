---
id: F4-05-key-health
fase: F4
dipende_da: [F2-06-system-actions]
puo_parallelo_con: F4-06-alerts, F4-07-sticky-sessions, F4-08-ui-polish
---

# F4-05 — Timeline salute chiavi (dead_suspect → retired → recuperata)

## Obiettivo

Pagina `/key-health` con la timeline di salute di ogni chiave/deployment:
ricostruita da `/admin/deployments/expiring`, `/admin/state` (cooldowns) e
`/admin/bootstrap/status` (retired_keys con `dead_since_days` e
`last_reason`). Mostra stati dead_suspect/retired/recovered e azioni
(unretire — link a sistema). SOLO lettura + azione unretire.

## File da creare/modificare

- `scrocco-web/src/routes/key-health.js` (crea)
- `scrocco-web/views/key-health/index.ejs` (crea)
- `scrocco-web/public/js/key-health.js` (crea)

## Contratto

- `GET /key-health` [requireAuth, authorize("deployments","read")] → raccoglie:
  - `gateway.get('/admin/deployments')` (lista deployment con unique/group/
    modello/key_masked)
  - `gateway.get('/admin/state')` → cooldowns_active + health del router
  - `gateway.get('/bootstrap/status')` → issues con code retired_keys
    (items: {deployment, dead_since_days, last_reason}), suspicious_keys
    (cooldown/fail_streak/health), never_probed
  - `gateway.get('/admin/insights/leaderboard', {params:{window:'30d',
    sort:'error_rate', order:'desc'}})` → righe con health/probe_ms → merge
    per `dep`.
  Costruisce la timeline: ogni row → {unique, modello, group, stato
  derivato: retired (da status.issues), dead_suspect (cooldown attivo o
  fail_streak alto), recovered (health ok e probe ok), ok, mai_probed}.
- "Recuperata" = unretire eseguito / probe ok recente.
- View: filtri (stato), tabella: depl, modello, stato badge, ultimo motivo,
  giorni dead, probe_ms, azioni (se retired → bottone "Unretire" → POST
  /system/unretire riuso: link; oppure AJAX qui con confirm + refresh).

## Criterio di done

Render con mock; le 4 categorie compaiono quando la fixture le contiene.
Unretire via mock funziona.

## Rischi / note

- Non inventare stati: derivare ESATTAMENTE dalle tre sorgenti.
- `dead_suspect` = in cooldown adesso oppure fail_streak>=3 (come status).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa la timeline salute chiavi.

Crea:
1. `src/routes/key-health.js` — Router `GET /key-health` (requireAuth +
   authorize deployments read): 4 fetch come da contratto (deployments, state,
   bootstrap/status, leaderboard 30d) e costruzione `timeline`:
   per ogni deployment derivato stato `retired` (da issues retired_keys per
   unique) | `dead_suspect` (cooldown o fail_streak≥3) | `recovered` (probe
   ok nel leaderboard e non in cooldown) | `never_probed` | `ok`. Passa alla
   view `{timeline, state}`. `POST /key-health/:unique/unretire` (requireAuth +
   authorize deployments unretire) → `gateway.post('/admin/deployments/
   unretire', {json:{unique}})` → flash + redirect; audit.
2. `views/key-health/index.ejs` — filtri stato (select), tabella: unique
   (short), modello, group, stato (badge colorato), last_reason, dead_since_days,
   fail_streak/cooldown_remaining, probe_ms, bottone Unretire (se retired,
   data-destruct).
3. `public/js/key-health.js` — filtro client e submit unretire AJAX
   (`scw.fetch`) con refresh.

Verifica `node --check`. Non toccare altri file. Riepiloga in 2 righe.