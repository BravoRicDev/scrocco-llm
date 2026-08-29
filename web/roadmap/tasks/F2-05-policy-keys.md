---
id: F2-05-policy-keys
fase: F2
dipende_da: [F1-03-policy-view]
puo_parallelo_con: F2-01-deployment-crud, F2-02-deployments-bulk, F2-03-profiles-write, F2-04-policy-edit, F2-06-system-actions, F2-07-probe, F2-08-capabilities-write
---

# F2-05 — Editor alias + client keys (PATCH policy keys)

## Obiettivo

Gestione alias (mappa alias→modello) e **client keys** custom per profilo
(`alias_keys`, `client_keys` in gateway.yaml) e `aliases`: aggiunta, modifica,
eliminazione (valore vuoto/null = cancella). Stesso pattern della schermata
TUI `e`/`a`/`D`/`k`. Solo admin (sono chiavi). Le chiavi sono SEMPRE
mascherate in output.

## File da creare/modificare

- `scrocco-web/src/routes/policy-keys.js` (crea)
- `scrocco-web/views/policy/keys.ejs` (crea)
- `scrocco-web/public/js/policy-keys.js` (crea)

## Contratto

- `GET /policy/keys` [requireAuth, authorize("keys","rotate")] → render
  con `effective.aliases`, `effective.alias_keys_masked`,
  `effective.client_keys_masked`.
- `POST /policy/keys` [rotate] body zod `{kind:'aliases', object: {...}}`
  oppure `{kind:'alias_key', alias, key}` oppure `{kind:'client_key',
  profile, key}`; ricostruisce l'oggetto intero (come TUI `_add_alias_flow`)
  e chiama `gateway.patch('/admin/policy', {json})`. La key nuova È in
  chiaro nel body verso il gateway (è ACCETTATO: il gateway la salva nel
  yaml); MAI loggarla. `auditLog({entityType:'policy', action:'update',
  newData:{kind}})`.
- `POST /policy/keys/delete` [rotate] body `{kind, name}` → patch con valore
  vuoto/null per la voce (il gateway la rimuove).
- MBAC: solo admin (`keys.rotate`).

## Criterio di done

Test mock: set alias={cam: 'gpt-5'} → mock effective.aliases aggiornato (come
gemello); delete alias → sparisce; set client_key per profilo → effective
client_keys_masked mostra il nuovo (masked). `node --check`.

## Rischi / note

- NB: il GATEWAY non restituisce mai le chiavi in chiaro (solo masked): il
  form di edit NON può precompilare una chiave. Ogni modifica = nuova chiave.
- Non toccare altri file di policy (F2-04 lavora in parallelo sul suo editor).
- L'audit log NON deve contenere mai il valore della chiave.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa la gestione alias + client keys.

Crea 3 file:
1. `src/routes/policy-keys.js` — Router: `GET /policy/keys` (requireAuth +
   authorize("keys","rotate"), carica `gateway.get('/admin/policy')` con i
   campi effective.aliases/alias_keys_masked/client_keys_masked) e
   `POST /policy/keys` (zod kind enum aliases|alias_key|client_key + campi;
   ricostruisce patch: kind 'aliases' → {aliases: obj}; 'alias_key' →
   {alias_keys:{alias:key}}; 'client_key' → {client_keys:{profile:key}};
   `gateway.patch('/admin/policy',{json:patch})` — il valore key resta in
   transito verso il gateway ma NON va mai loggato), `POST /policy/keys/delete`
   (zod {kind:'alias_key'|'client_key', name} → patch con valore null per la
   voce). auditLog solo con kind/nome, mai valori; flash; redirect `/policy/keys`.
2. `views/policy/keys.ejs` — sezioni: Alias (tabella alias → modello, bottoni
   elimina), Alias keys (tabella alias → key mascherata, form nuovo/sostituisci
   key), Client keys (tabella profilo → key mascherata, form nuovo). Tutti i
   campi con `_masked`.
3. `public/js/policy-keys.js` — submit AJAX con `scw.fetch`, conferma delete.

Verifica `node --check` + test mock opzionale (`test/f2-policy-keys.test.js`).
Non toccare altri file. Riepiloga in 3 righe.