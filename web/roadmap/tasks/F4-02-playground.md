---
id: F4-02-playground
fase: F4
dipende_da: [GP-01-playground, F2-09-integration]
puo_parallelo_con: F4-01-csv-editor, F4-03-policy-raw-editor, F4-04-config-history
---

# F4-02 — Playground con trace di routing/fallback

## Obiettivo

Pagina `/playground` che invia una chat di prova dal pannello ATTRAVERSO il
gateway (endpoint `POST /admin/playground`, prerequisito GP-01) e mostra il
**trace di routing/fallback**: deployment provati, motivo scarto, verdetto
finale. Solo admin/operator.

## File da creare/modificare

- `scrocco-web/src/routes/playground.js` (crea)
- `scrocco-web/views/playground/index.ejs` (crea)
- `scrocco-web/public/js/playground.js` (crea)

## Contratto

- `GET /playground` [requireAuth, authorize("playground","use")] → render con
  select modello/alias e textarea prompt (usa `gateway.get('/admin/state')`
  o `/admin/insights/leaderboard`? meglio: elenco modelli da
  `gateway.get('/v1/models')` — il wrapper può chiamare /v1/models — più
  profili da `/admin/profiles`).
- `POST /playground/run` [requireAuth, playground use] body zod `{model,
  messages:[{role:'user',content}], stream?}` → `gateway.post(
  '/admin/playground', {json})` a timeout più alto (30s) → **GP-01 risponde:
  `{model, deployment, content, usage?, trace:[{step, unique?, group?,
  reason, verdict}], attempts, fallbacks}` — il testo risposta è in `content`
  (NON `choices`)**. Render con l'output e il trace.
- View: form (model/alias, prompt, bottone Esegui), area risposta (pre/JSON o
  testo), tabella TRACE: step, deployment/gruppo, motivo scarto, verdetto;
  badge colorati per tentativo riuscito/fallito.
- auditLog (entityType 'playground', action 'run', newData {model}).

## Criterio di done

Con mock GP-01: run → risposta con trace di 2 step (fallito→scarto motivo,
poi riuscito). Test `node --check`; verifica che il client NON mostri mai la
chiave (usa solo i campi di trace del gateway, che sono sanitizzati).

## Rischi / note

- Il playground manda chiamate reali (consume quota): AGGIUNGI un warning e
  rate-limit per-IP/per-utente generoso (es. 20/min) montato in route.
- Timeout lungo (30s) perché il gateway può fare fallback multipli.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa il playground con trace.

PREREQUISITO: endpoint `POST /admin/playground` (GP-01) che gira una chat e
ritorna il trace di routing/fallback (mock in fixtures se GP-01 non ancora
mergiato).

Crea:
1. `src/routes/playground.js` — Router: `GET /playground` (requireAuth +
   authorize playground use) → carica modelli (`gateway.get('/v1/models')`) e
   profili (`/admin/profiles`) → render; `POST /playground/run` (playground
   use; zod {model, prompt:string, stream?:bool}) → `gateway.post(
   '/admin/playground', {json:{model, messages:[{role:'user',
   content:prompt}], stream:false}, timeout:30000})` → la risposta GP-01 ha
   `content` (NON `choices`): render con `result.content` (output) +
   `result.trace`. Gestisci GatewayError (es. modello non instradabile)
   mostrando il messaggio. auditLog (solo {model}). Rate-limit 20/min per user
   (usa express-rate-limit, keyGenerator = req.user?.sub).
2. `views/playground/index.ejs` — form model (o alias), textarea prompt,
   bottone "Esegui" (label loading); se `result` → blocco `<pre>` con
   `result.content` + tabella trace (passo, unique, group, motivo, verdetto
   badge; campi `attempts`/`fallbacks`).
3. `public/js/playground.js` — submit con `scw.fetch`, spinner, layout
   risposta.

Verifica `node --check` + render mock. Non toccare altri file. Riepiloga in 3
righe.