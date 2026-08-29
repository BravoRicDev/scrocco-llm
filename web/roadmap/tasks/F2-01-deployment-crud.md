---
id: F2-01-deployment-crud
fase: F2
dipende_da: [F1-01-deployments-list]
puo_parallelo_con: F2-02-deployments-bulk, F2-03-profiles-write, F2-04-policy-edit, F2-05-policy-keys, F2-06-system-actions, F2-07-probe, F2-08-capabilities-write
---

# F2-01 — Deployment CRUD (create/edit/delete via form)

## Obiettivo

Aggiungere alla pagina deployments la creazione, la modifica e la cancellazione
di un deployment singolo, con form dedicato, conferma per le azioni distruttive
e `auditLog` su ogni mutazione. Usa `POST/PUT/DELETE /admin/deployments`.

## File da creare/modificare

- `scrocco-web/src/routes/deployments.js` (modifica — aggiungi GET
  /deployments/new, GET /deployments/:id/edit, POST /deployments, PUT
  /deployments/:id, POST /deployments/:id/delete)
- `scrocco-web/src/routes/deployments.js` → NON ripete il file; usa questo
- `scrocco-web/views/deployments/form.ejs` (crea)
- `scrocco-web/views/deployments/list.ejs` (modifica — colonna azioni con
  link edit/delete, bottoni confirm)

## Contratto

- `GET /deployments/new` [requireAuth, authorize("deployments","create")] →
  render form vuoto con profili pre-scaricati.
- `GET /deployments/:id/edit` [requireAuth, authorize("deployments","update")]
  → trova il deployment dalla lista (`/admin/deployments`) e precompila.
- `POST /deployments` [create] — body zod:
  `{ profile: string, modello: string, provider?: string, endpoint: string,
     data: string, key: string, context: number≥0, priority?: string,
     caps?: string, commento?: string }`. **`data` e `key` sono OBBLIGATORI
  (il gateway li pretende nel create: `_required_create` di admin.py richiede
  profile, modello, endpoint, data, key non-vuoti e context≥0 → senza `data`/
  `key` il POST va in 400).** La regola "key vuota ok" NON vale qui: vale SOLO
  per il PUT/update. Chiama
  `gateway.post('/admin/deployments', {json})` → `{ok, id}` → redirect
  `/deployments` con flash. `auditLog({entityType:'deployment',
  action:'create', newData, ipAddress})`.
- `PUT /deployments/:id` [update] — body è un PATCH con `{id: :id}` nel json
  (gateway accetta id nel body per find); chiama
  `gateway.put(`/admin/deployments/${id}`, {json})` → redirect + flash.
  auditLog 'update'.
- `POST /deployments/:id/delete` [delete] — confirm client-side; chiamo
  `gateway.del(`/admin/deployments/${id}`)`; auditLog 'delete'; flash; redirect.
- `key` nel form di CREATE: campo OBBLIGATORIO da valorizzare (il gateway
  richiede una key non vuota nel POST). Nel form di EDIT (PUT): si può lasciare
  vuota per NON ruotare la chiave (PUT senza key = niente key_rotated); se
  valorizzata, la ruota (non recuperabile dopo).
- RBAC: `create/update/delete` = admin/operator.

## Criterio di done

Test con mock: `POST /deployments` (valido) → 302 e `gateway` mock aggiorna
lista; nuovo deployment compare su GET /deployments; edit cambia; delete lo
toglie. `audit_log` contiene una riga per ogni azione (verifica via query nel
test). `node --check`.

## Rischi / note

- `row_hash` (id) del gateway NON è riassegnabile: il PUT deve mandare lo
  stesso id nel body e nel path.
- La creazione di un NUOVO PROFILO è separato (F2-03); qui il form usa profili
  esistenti.
- Convalida con zod → 400 con messaggio chiaro, non 500.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa il CRUD singolo dei deployment del gateway.

Parti da `src/routes/deployments.js` e `views/deployments/list.ejs` esistenti
(leggi prima). Modifica SOLO questi 2 + crea la vista form:
1. In `src/routes/deployments.js` aggiungi: `GET /deployments/new`,
   `GET /deployments/:id/edit` (precarica da `gateway.get('/admin/deployments')`),
   `POST /deployments`, `PUT /deployments/:id`, `POST /deployments/:id/delete`.
   - `POST /deployments`: schema zod come da contratto con **`data` e `key`
     OBBLIGATORI** (il gateway li richiede nel create: validali come non-vuoti,
     altrimenti 400 lato client); `gateway.post('/admin/deployments',
     {json})`; `auditLog` (da `src/services/audit.js` con entityType
     "deployment", action create, newData = body sanitized senza key in
     chiaro, ipAddress = req.ip); flash; redirect 302 `/deployments`.
   - `PUT /deployments/:id`: PATCH zen con id; `gateway.put(`/admin/deployments
     /${id}, {json})`; qui `key` è OPZIONALE/vuota = non ruotare (non includere
     `key` nel stored audit, mask); flash.
   - `DELETE`: conferma lato client (form POST con onclick confirm via
     `scw.confirm`), `gateway.del(`/admin/deployments/${id}`)`, auditLog,
     redirect.
   - Gestisci `GatewayError` → render error/flash error (mai crash).
   - Flash pattern: usa `req.flash`? NON esiste come middleware di default:
     implementa un piccolo helper `res.flash(type,msg)` in `src/index.js`? NO —
     qui NON tocchi index.js. Soluzione: le viste leggono `locals.flash` che
     l'integrazione F2-09 dovrà popolare da query param `?flash=...`. Quindi
     in queste route imposta `res.redirect('/deployments?flash=msg')` e
     aggiungi alla lista se `?flash` è presente. Lascia a F2-09 il parsing.
2. `views/deployments/form.ejs` — form per crea/edita: campi
   profile/commento/modello/provider/endpoint/data/key/context/priority/caps/
   data col; prefill quando `deployment` passato. In CREATE: `data` e `key`
   sono REQUIRED (attributo `required` obbligatorio). In EDIT: badge "lascia
   key vuota per non ruotare"; bottone secondario "Elimina" (solo in edit) che
   apre il confirm (data-destruct).
3. `views/deployments/list.ejs` — aggiungi colonna Azioni: 'Modifica' (link) e
   'Elimina' (form POST con `scw.confirm`). Header della form in alto.

Verifica: `node --check src/routes/deployments.js` e, se puoi, un test rapido
mock (POST/PUT/DELETE e verifica audit). Non toccare altri file. Riepiloga in
3 righe.