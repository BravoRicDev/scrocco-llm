---
id: F1-01-deployments-list
fase: F1
dipende_da: [F0-11-integration-smoke]
puo_parallelo_con: F1-02-profiles-view, F1-03-policy-view, F1-04-capabilities-view, F1-05-dashboard-state, F1-06-expiring-view, F1-07-history-view, F1-08-insights-view, F1-09-bootstrap-view, F1-10-guide-view
---

# F1-01 — Pagina lista deployments (sola lettura)

## Obiettivo

Pagina `/deployments` che lista i deployment del gateway (come TUI tabella) con
filtro per profilo e ricerca testuale, coprendo `GET /admin/deployments` e
`GET /admin/profiles` (per il select). SOLA LETTURA (le azioni sono F2).

## File da creare/modificare

- `scrocco-web/src/routes/deployments.js` (crea — GET /deployments)
- `scrocco-web/views/deployments/list.ejs` (crea)

NON toccare: `src/index.js` (è F1-11), altri `src/riutine`, sidebar.

## Contratto

- Route: `GET /deployments` [requireAuth, authorize("deployments","read")].
- Dati: `gateway.get('/admin/deployments', { params: { profile } })` →
  `{count, deployments:[...]}` (shape `_deployment_view`:
  id, profile, modello, provider, endpoint, data, category, context_k,
  max_input, priority, key_masked, group, capabilities, caps, cap_groups).
  `gateway.get('/admin/profiles')` → profili per il select (permette anche
  profilo "tutti").
- Query params: `?profile=` (filtro), `?q=` (ricerca testuale su modello/
  provider/endpoint/group/id). Il filtro testuale localmente.
- View: tabella con colonne come la TUI (modello, provider, data, ctx, prio,
  endpoint, chiave mascherata, gruppo, caps) + badge category (free/priority/
  zen/future/fallback) e capabilities. Form GET con select profilo + input q.
  Header con count. Stato vuoto se nessun deployment.

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
DATABASE_URL=postgres://... GATEWAY_MOCK=1 npm test   # se test F1 runner
# o manuale:
GATEWAY_MOCK=1 DATABASE_URL=... node -e "
  import('./src/index.js').then(async m=>{
    const app=await m.createApp(); const s=app.listen(0);
    const r=await fetch('http://127.0.0.1:'+s.address().port+'/deployments',{headers:{Cookie:'token=<SESS>'}});
    console.log(r.status, (await r.text()).length>0); s.close(); })"
```
Deve renderizzare HTML senza errori con fixture. Test node --test dedicato
opzionale (aggiungi `test/f1-deployments.test.js` se vuoi, non rompere altri).

## Rischi / note

- `authorize("deployments","read")` passa per viewer.
- Non usare script inline non-escapati (usi `escapeAttr` per il testo ricercato).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi `src/...`, `views/...`;
se `read` fallisce usa `cat`/`sed -n`).

Crea la pagina read-only della lista deployments per il pannello `scrocco-web`.
Stile route: `gestione-siti-riccardom/src/routes/users.js` (Router, requireAuth,
authorize, try/catch → next(err)). Le data vengono dal wrapper
`src/services/gateway.js` (import default, metodi `gateway.get(path,{params})`).

Crea ESATTAMENTE:
1. `src/routes/deployments.js` — default export Router con `GET /deployments`
   con `requireAuth` (da `src/middleware/auth.js`) e
   `authorize("deployments","read")` (da `src/middleware/authorize.js`);
   legge `?profile` e `?q`; chiama `gateway.get('/admin/deployments',
   {params:{profile}})` (se profile valorizzato) e
   `gateway.get('/admin/profiles')`; filtra localmente `q` (case-insensitive su
   modello/provider/endpoint/group/id); render
   `views/deployments/list.ejs` con `{deployments, profiles, count, q,
   currentProfile}`. Gestisci `GatewayError` → render error con messaggio
   sicuro.
2. `views/deployments/list.ejs` — form GET (select profilo + input ricerca +
   submit), tabella densa: modello, provider, data, ctx, prio, endpoint,
   chiave (mask), gruppo, caps (badge). Badge categoria colorati. Testo
   "nessun deployment" se count 0. Header con `<%= count %>` deployments.
   Usa `escapeAttr` e stile CSS esistente (classi `.btn`, `.badge`, `.table`).

Non toccare `src/index.js` (il mount è F1-11) né altri file. Verifica:
`node --check src/routes/deployments.js` e, se possibile, render smoke con
fixture mock. In 2 righe riepiloga.