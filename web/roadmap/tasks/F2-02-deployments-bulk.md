---
id: F2-02-deployments-bulk
fase: F2
dipende_da: [F1-01-deployments-list]
puo_parallelo_con: F2-01-deployment-crud, F2-03-profiles-write, F2-04-policy-edit, F2-05-policy-keys, F2-06-system-actions, F2-07-probe, F2-08-capabilities-write
---

# F2-02 — Operazioni multiple (bulk) sui deployment

## Obiettivo

Pagina `/deployments/bulk` per eseguire operazioni in blocco (create/update/
delete) in UNA chiamata atomica (`POST /admin/deployments/bulk`), con
multi-selezione, anteprima del payload e gestione dell'errore atomico
(all-or-nothing). Stesso scopo del TUI (`POST /admin/deployments/bulk`).

## File da creare/modificare

- `scrocco-web/src/routes/deployments-bulk.js` (crea)
- `scrocco-web/views/deployments/bulk.ejs` (crea)
- `scrocco-web/public/js/bulk.js` (crea)

## Contratto

- `GET /deployments/bulk` [requireAuth, authorize("deployments","bulk")] →
  render bulk.ejs con la lista deployment e un editor "riga modello".
- `POST /deployments/bulk` [requireAuth, authorize("deployments","bulk")]
  body zod: `{ operations: [{ action: 'create'|'update'|'delete', id?,
     ...fields }].min(1).max(50) }`.
  - Per `create` richiedi i campi (profile, modello, endpoint, key, context)
    **e `data`** (`_required_create` di admin.py pretende anche `data`
    obbligatoria nel create → una op create senza `data` rende l'intero batch
    400).
  - Per `update`/`delete` richiedi `id`.
  - Chiama `gateway.post('/admin/deployments/bulk', {json})` → 200 `{ok,
    applied, results}`; 400 (batch invalido) → mostra `results` con errori
    per op. `auditLog({entityType:'deployment', action:'bulk', newData
    {count, actions}})`.
  - Redirect/flash con esito.
- View: da sinistra lista con checkbox (id dei deployment), destra form per
  creare un "nuovo modello" (per create) + select operazione (create/update/
  delete). Costrutto operazioni in `public/js/bulk.js` dal DOM e submit via
  `scw.fetch`. Mostra anteprima JSON (text area readonly).

## Criterio di done

Test mock: bulk di 2 create + 1 delete → mock applica in ordine; bulk con 1 op
invalida (es. delete id inesistente) → 400 e mock NON applica nulla (verifica
stato invariato). `node --check`.

## Rischi / note

- Durata stimata: 60–90 min (JS client: builder DOM + anteprima).
- La bulk è ATOMICA: se almeno un'op fallisce, niente viene applicato. Il mock
  deve riflettere questo comportamento (F0-05 mock "bulk": valida tutto prima
  di applicare — incluse `data`/`key` non vuote sul create).
- **NON toccare `src/constants/permissions.js`**: l'azione `deployments:bulk`
  è già definita nella matrice COMPLETA di F0-07 (insieme a `probe`, `users`,
  `playground/use`, `system/*` ecc.). Qui usa soltanto `authorize("deployments",
  "bulk")`. Le uniche append a `permissions.js` avvengono in F0-07 e nel task
  di integrazione F2-09 (mai in parallelo sulla stessa wave).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa le operazioni bulk sui deployment.

1. (NO modifiche a `src/constants/permissions.js` — la matrice completa è di
   F0-07; usa `authorize("deployments","bulk")` così com'è.)
2. Crea `src/routes/deployments-bulk.js` — Router: `GET /deployments/bulk` e
   `POST /deployments/bulk` con requireAuth + `authorize("deployments",
   "bulk")`. POST valida con zod (operations min 1 max 50; action enum;
   dipendenze per action: sul create richiedi profile/modello/endpoint/
   **data**/key/context tutti non vuoti), chiama
   `gateway.post('/admin/deployments/bulk',
   {json})`, gestisce 400 con `results` (render bulk.ejs con `errorsResults`),
   auditLog (action "bulk"), flash/redirect. GET carica lista deployments +
   profili.
3. Crea `views/deployments/bulk.ejs` — layout due colonne: sinistra tabella
   con checkbox per ogni deployment (id); destra un form con select
   operazione (create/update/delete), campo id (per update/delete) e i campi
   per create (con `data` e `key` required per l'op create); bottone "Aggiungi
   all'anteprima"; `<pre>` anteprima JSON
   operazioni; submit "Esegui bulk". Se `errorsResults` → mostra errori per op.
4. Crea `public/js/bulk.js` — costruisce array `operations` dal DOM, li
   mostra in anteprima, POST via `scw.fetch('/deployments/bulk',
   {method:'POST', json})`, gestisce 400 body `{results}`.

Verifica `node --check` + test mock (aggiungi `test/f2-bulk.test.js` opzionale
ma benvenuto). Non toccare altri file. Riepiloga in 3 righe.