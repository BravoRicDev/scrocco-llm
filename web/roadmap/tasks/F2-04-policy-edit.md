---
id: F2-04-policy-edit
fase: F2
dipende_da: [F1-03-policy-view]
puo_parallelo_con: F2-01-deployment-crud, F2-02-deployments-bulk, F2-03-profiles-write, F2-05-policy-keys, F2-06-system-actions, F2-07-probe, F2-08-capabilities-write
---

# F2-04 — Editor policy campo per campo (PATCH /admin/policy)

## Obiettivo

Trasformare la vista policy in un **editor campo per campo** (fedele alla
schermata TUI `Y`): scalari, liste, mappe e `profiles` per-profilo, con
dispatch sul tipo e PATCH parziale (`PATCH /admin/policy`). Solo admin.

## File da creare/modificare

- `scrocco-web/src/routes/policy.js` (modifica — aggiungi POST /policy/field)
- `scrocco-web/views/policy/edit.ejs` (crea — editor; la profit index.ejs
  resta come "legacy view" o viene sostituita: tieni `index.ejs` = visualizza e
  `edit.ejs` = modifica via tab)
- `scrocco-web/views/policy/index.ejs` (modifica — aggiungi tab/bottone "Modifica")
- `scrocco-web/public/js/policy-edit.js` (crea)

## Contratto

- `GET /policy/edit` [requireAuth, authorize("policy","update")] → render
  editor con i valori `effective` e metadati tipo per campo (tabella campo →
  tipo come TUI SCALAR_ROWS/LIST_ROWS/MAP_ROWS).
- `POST /policy/field` [update] body zod: `{field: string, value: any}` —
  dispatch:
  - `profiles.<name>.step_up_pct` → `{profiles:{name:{step_up_pct}}}`
  - `alias_keys.<alias>` → `{alias_keys:{alias:value}}` (v. F2-05)
  - liste (legacy_prefixes, hotwords, speed_hotwords, model_capabilities):
    value stringa separate da virgola → array
  - mappe `capability_routing.model_capabilities` → oggetto glob→caps parse
  - scalari bool → true/false; numerici → parseFloat; stringhe → trim
  - Chiama `gateway.patch('/admin/policy', {json: patch})` → `{ok, effective}`
    → flash + redirect `/policy/edit`. `auditLog({entityType:'policy',
    action:'update', newData:{field}})` — MAI loggare key.
- RBAC solo admin.

## Criterio di done

Test mock: PATCH con `{profiles:{test:{step_up_pct:42}}}` → mock aggiorna; con
`{qc_sanity:{min_chars:120}}` → ok; con un campo invalido → 400 dal mock
(policy non valida) e nessuna modifica. `node --check`.

## Rischi / note

- Il gateway rifiuta yaml invalido con 400 e file intatto: il mock deve
  replicarlo.
- Le chiavi di tipo `alias_keys` e i valori `client_keys` son gestiti in
  F2-05; qui NON toccarli (il dispatcher può però preparare l'oggetto e
  chiamare il gateway — in F2-05 ci sono i campi dedicati).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa l'editor campo-per-campo della policy.

Parti da `routes/policy.js`, `views/policy/index.ejs` esistenti (leggi). Crea:
1. `src/routes/policy.js` — aggiungi `GET /policy/edit` (requireAuth +
   authorize("policy","update"), carica `gateway.get('/admin/policy')` e
   render `views/policy/edit.ejs` con la mappa metadati) e `POST /policy/field`
   (zod {field,value}; dispatch tipo → patch oggetto annidato corretto; lista
   virgola→array; models_capabilities glob→caps map; scalari cast; mai
   loggare chiavi; `gateway.patch('/admin/policy',{json:patch})`; auditLog
   entityType policy action update newData {field}; flash; redirect
   `/policy/edit`).
2. `views/policy/edit.ejs` — tabella dei campi (sezione/parametro/valore/tipo)
   con, su click, form inline per modificare (input appropriato per bool/int/
   num/str/list/map). Bottoni "Salva" → `POST /policy/field`. Sempre possibili
   i campi scalari, le liste e la mappa `capability_routing.model_capabilities`.
3. `views/policy/index.ejs` — aggiungi un bottone "Modifica policy" (solo se
   il ruolo lo consente: passa `canEdit` locale da res.locals.user.role)
4. `public/js/policy-edit.js` — gestisce il click sulla cella, apre il form
   inline, invia `scw.fetch('/policy/field',{method:'POST',json:{field,value}})`
   e ricarica.

Non toccare i campi `alias_keys`/`client_keys` (F2-05); se `index.ejs` ha
quella sezione, lasciala come read-only. Verifica `node --check`. Non toccare
altri file. Riepiloga in 3 righe.