---
id: F2-03-profiles-write
fase: F2
dipende_da: [F1-02-profiles-view]
puo_parallelo_con: F2-01-deployment-crud, F2-02-deployments-bulk, F2-04-policy-edit, F2-05-policy-keys, F2-06-system-actions, F2-07-probe, F2-08-capabilities-write
---

# F2-03 — Gestione profili (nuovo profilo + purge)

## Obiettivo

Sulla pagina `/profiles` aggiungere: creazione di un nuovo profilo (che crea
il primo deployment tramite `POST /admin/deployments`, pattern TUI "n nuovo
profilo") e purge di un profilo (solo se senza deployment) via
`POST /admin/profiles/purge`, con audit e conferme.

## File da creare/modificare

- `scrocco-web/src/routes/profiles.js` (modifica)
- `scrocco-web/views/profiles/index.ejs` (modifica — aggiungi form nuovo
  profilo + bottone purge per profilo con count 0)
- `scrocco-web/views/profiles/new.ejs` (crea — il "primo deployment" come
  wizard: nome profilo → prefill form deployment)

## Contratto

- `GET /profiles/new` [requireAuth, authorize("profiles","create")] → form
  minimale (nome profilo).
- `POST /profiles` [create] — body zod `{name: string min1}`. Chiama
  `gateway.post('/admin/deployments', {json:{profile:name, modello:'',
  endpoint:'', data:'', key:'', context:0}})` con i campi minimi OBBI­GATORI
  (il gateway li richiede). Il risultato può fallire con 400 → flash errore
  "il profilo vuoto richiede almeno un endpoint valido": in tal caso, invece
  di fallire, reindirizza al form deployment F2-01 prefill profile. Quindi:
  prova a creare un deployment segnaposto; se fallisce (endpoint vuoto), apri
  `/deployments/new?profile=<name>` per il wizard completo. auditLog
  (action 'profile_create').
- `POST /profiles/:name/purge` [authorize("profiles","purge")] —
  `gateway.post('/admin/profiles/purge', {json:{profile:name}})`; se la
  colonna ha ancora deployment → 400 → flash; altrimenti ok. auditLog
  'profile_purge'. Conferma client.
- Il purge compare solo per profili con `deployments === 0`.

## Criterio di done

Test mock: `POST /profiles` con nome → una riga deployment creata nel mock (o
redirect a /deployments/new?profile=); purge di profilo con 0 deployment → mock
rimuove colonna; purge con deployment → 400. `node --check`.

## Rischi / note

- Il gateway richiede `_required_create` (profile, modello, endpoint, data,
  key, context≥0): il warp "profilo vuoto" deve gestire il fallimento
  con dignità.
- Non modificare `F1-02` route: questa è la stessa file (sequenziale).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Aggiungi la gestione profili (nuovo + purge).

Modifica `src/routes/profiles.js` e `views/profiles/index.ejs` esistenti e
crea `views/profiles/new.ejs`:
1. `GET /profiles/new` + `POST /profiles` (requireAuth, authorize profiles
   create): per POST, prova `gateway.post('/admin/deployments', {json:
   {profile:name, modello:'', endpoint:'', data:'', key:'', context:0}})`;
   se ok → flash "profilo creato" e redirect `/deployments?profile=name`; se 400
   (endpoint vuoto) → redirect `/deployments/new?profile=name` (wizard
   completo). Audit `profile_create`.
2. `POST /profiles/:name/purge` (requireAuth, authorize profiles purge):
   `gateway.post('/admin/profiles/purge', {json:{profile:name}})`; su 400 →
   flash errore; ok → flash + redirect. Confirm client-side. Audit
   `profile_purge` (name).
3. `views/profiles/index.ejs`: header + bottone "Nuovo profilo" → `/profiles/new`;
   per ogni riga con deployments===0 → bottone "Purga" (data-destruct);
   flash esiti.
4. `views/profiles/new.ejs`: form nome + nota "definisci poi il primo
   deployment".

Verifica `node --check` (le route e le viste si rendersing con l'integrazione
F1 già presente; se non è montata non importa: render smoke nel test).
Non toccare altri file. Riepiloga in 3 righe.