---
id: F2-08-capabilities-write
fase: F2
dipende_da: [F1-04-capabilities-view]
puo_parallelo_con: F2-01-deployment-crud, F2-02-deployments-bulk, F2-03-profiles-write, F2-04-policy-edit, F2-05-policy-keys, F2-06-system-actions, F2-07-probe
---

# F2-08 — Capacità write: seed-from-map + audit

## Obiettivo

Sulla pagina `/capabilities` aggiungere le due azioni: `seed-from-map`
(propone/applica la colonna `caps` da `capability_routing.model_capabilities`,
dry-run o applica) e `audit` (verifica server-side che i modelli esistano),
con proposta→conferma applicazione e report.

## File da creare/modificare

- `scrocco-web/src/routes/capabilities.js` (modifica)
- `scrocco-web/views/capabilities/index.ejs` (modifica — aggiungi form dry-run,
  tabella proposte, bottoni)
- `scrocco-web/views/capabilities/audit.ejs` (crea)

## Contratto

- `POST /capabilities/seed` [authorize("capabilities","seed")] body
  `{dry_run: bool}`. Prima chat: `gateway.post(
  '/admin/capabilities/seed-from-map', {json:{dry_run:true}})` → `{dry_run,
  count, total, proposals:[{id,profile,modello,current,proposed}]}`. Se
  count>0: render form conferma con `proposals`; USER clicca "Applica" →
  stessa route con `dry_run:false` → `{ok, applied, skipped, errors}`.
  auditLog (action "cap_seed", count). Il se escalato `dry_run` default true.
- `GET /capabilities/audit` [authorize("capabilities","audit")] →
  `gateway.post('/admin/capabilities/audit', {json:{}})` → `{checked_at,
  accounts, accounts_checked, missing_models:[...], cap_suggestions,
  errors}` → render `audit.ejs`.
- `POST /capabilities/audit` facoltativo (refresha). L'audit è read-informe, il
  POST è per ripeterlo.
- View: sezione seed con tabella proposte (id, modello, current → proposed,
  diffs evidenziati) e bottone "Applica N modifiche"; sezione audit con report
  e tasto "Riesegui audit".

## Criterio di done

Test mock: seed dry_run → mock ritorna 1 proposta per la riga con caps
diversa; applica → mock aggiorna la caps della riga. audit → mock ritorna
fixture con `missing_models` popolato. `node --check`.

## Rischi / note

- `authorize("capabilities","audit")` = lettura per tutti (viewer incluso), il
  seed = admin/operator.
- Righe `text/tools` sono escluse dal seed (il gateway esclude già):
  mostra solo il delta.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Aggiungi seed + audit capacità.

Modifica `src/routes/capabilities.js`, `views/capabilities/index.ejs` esistenti
e crea `views/capabilities/audit.ejs`:
1. Route nuove in `src/routes/capabilities.js`:
   - `POST /capabilities/seed` (requireAuth + authorize capabilities seed)
     body {dry_run:bool}; con dry_run:true → chiama
     `gateway.post('/admin/capabilities/seed-from-map',{json:{dry_run:true}})`,
     render index con `seedProposals` (tabella + form "Applica" con hidden
     dry_run false); con dry_run:false → chiama con `{dry_run:false}` e flash
     esito; auditLog.
   - `GET /capabilities/audit` (requireAuth + authorize capabilities audit):
     `gateway.post('/admin/capabilities/audit',{json:{}})` → render audit.ejs.
2. `views/capabilities/index.ejs`: sezione seed (bottone "Calcola proposte
   (dry-run)" → POST dry_run true); quando `seedProposals` presente →
   tabella id/modello/current/proposto con diff bold + form conferma Applica;
   link a `/capabilities/audit`.
3. `views/capabilities/audit.ejs` — header checked_at, cards accounts checked,
   tabella missing_models (model, endpoint, key_masked, candidates), sezione
   cap_suggestions (modello → caps), errors list. Bottone "Riesegui audit"
   (GET → refresh).

Verifica `node --check` + test mock. Non toccare altri file. Riepiloga in 3
righe.