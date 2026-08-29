---
id: F2-07-probe
fase: F2
dipende_da: [F1-01-deployments-list]
puo_parallelo_con: F2-01-deployment-crud, F2-02-deployments-bulk, F2-03-profiles-write, F2-04-policy-edit, F2-05-policy-keys, F2-06-system-actions, F2-08-capabilities-write
---

# F2-07 — Probe chiavi singolo e bulk

## Obiettivo

Pagina `/probe` per validare le chiavi: probe singolo su un deployment
(`POST /admin/deployments/probe {unique|id, force?}`) e bulk
(`/admin/deployments/probe/bulk {filter, force}`), con tabella esiti e
distinzione cached/ok/ko. Rispetta la regola "probe una volta, cached".

## File da creare/modificare

- `scrocco-web/src/routes/probe.js` (crea)
- `scrocco-web/views/probe/index.ejs` (crea)
- `scrocco-web/public/js/probe.js` (crea)

## Contratto

- `GET /probe` [requireAuth, authorize("deployments","probe")] → carica
  `gateway.get('/admin/deployments')` e `profiles`; render.
- `POST /probe/:id` [probe] → `gateway.post('/admin/deployments/probe',
  {json:{id:req.params.id, force: !!body.force}})`. Mostra esito. Il mock
  tiene `probe_results` (mock o reali): cached quando ok e key uguale.
- `POST /probe/bulk` [probe] body `{filter: 'all'|'cap:x'|<profile>,
  force?:bool}` → `gateway.post('/admin/deployments/probe/bulk', {json})` →
  `{filter, count, results:[{unique, ok, error_class?, latency_ms?}]}`.
- View: tabella deployment con colonna "Probe" (bottone per riga + checkbox
  "force"), sezione "Probe bulk" (select filter + force + run), tabella
  risultati (unique, esito, latency, errore).

## Criterio di done

Test mock: probe singolo su id presente → mock ritorna ok (fixture);
probe bulk 'all' → count = numero deployment. `node --check`.

## Rischi / note

- I probe burnano quota su tier a pagamento: il mock e l'UI devono
  sottolineare "cached" ed evitare doppie run senza force.
- **NON toccare `src/constants/permissions.js`**: l'azione `deployments:probe`
  è già definita nella matrice COMPLETA di F0-07. Qui usa soltanto
  `authorize("deployments","probe")`. Le append a `permissions.js` vivono solo
  in F0-07 / F2-09 (mai in parallelo sulla stessa wave 2A).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa i probe chiave.

1. (NO modifiche a `src/constants/permissions.js` — matrice completa in F0-07;
   usa `authorize("deployments","probe")`.)
2. Crea `src/routes/probe.js` — Router: `GET /probe` (requireAuth +
   authorize deployments probe, carica deployments+profiles, render);
   `POST /probe/:id` (zod body {force?:bool}; gateway.post probe con id;
   render esito singolo sulla stessa pagina fra flash oppure torna a GET con
   `?result=...` semplice: usa query param `probeResult`); `POST /probe/bulk`
   (zod {filter, force?}; gateway.post probe/bulk; passare results a render).
   auditLog soltanto se serve (probe è informativo: audit 'probe' è FACOLTATIVO,
   non bloccante).
3. `views/probe/index.ejs` — tabella deployment (id, modello, provider,
   endpoint, key_masked, gruppo) con bottone "Probe" per riga e checkbox
   force; bottone "Probe tutti (cached)" e "Probe tutti (FORCE)"; sezione
   risultati: tabella esito (unique, ok/cached/ko badge, latency_ms,
   error_class). Messaggio "⚠ probe = consumo quota sui free-tier: usa
   cached".
4. `public/js/probe.js` — conferma nel bulk force (double confirm), submit.

Verifica `node --check` + test mock. Non toccare altri file. Riepiloga in 3
righe.