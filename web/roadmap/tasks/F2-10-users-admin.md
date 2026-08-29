---
id: F2-10-users-admin
fase: F2
dipende_da: [F0-07-auth-rbac]
puo_parallelo_con: F2-01-deployment-crud, F2-02-deployments-bulk, F2-03-profiles-write, F2-04-policy-edit, F2-05-policy-keys, F2-06-system-actions, F2-07-probe, F2-08-capabilities-write
---

# F2-10 — Users admin: CRUD utenti (list/create/disable/role/reset password)

## Obiettivo

Pagina `/users` di amministrazione utenti (admin SOLO): lista, creazione,
disable/enable, cambio ruolo (admin/operator/viewer) e reset password. Nessun
utente può gestire se stesso (no self-disable/self-demote). Ogni mutazione
scritta in `audit_log`. Usa le tabelle `users` (F0-03) e la matrice permessi
`users` di F0-07.

## File da creare/modificare

- `scrocco-web/src/routes/users.js` (crea)
- `scrocco-web/views/users/index.ejs` (crea)
- `scrocco-web/views/users/form.ejs` (crea — crea/modifica/role/reset)

NON toccare: `src/index.js` (mount in F2-09), `views/partials/sidebar.ejs`
(link già previsto da F0-08, abilitato in F2-09), altri file.

## Contratto

- `GET /users` [requireAuth, authorize("users","list")] → elenco utenti
  (id, email, name, role, status, last login/token) → render index.
- `GET /users/new` [authorize("users","create")] → form vuoto.
- `POST /users` [create] body zod `{email, name, password (min 8), role in
  (admin,operator,viewer)}` → hash con `services/password.js` (bcryptjs),
  insert; audit; flash; redirect `/users`.
- `POST /users/:id/toggle` [update] → `status` active ↔ disabled;
  **vietato su se stesso**; audit; redirect.
- `POST /users/:id/role` [update] body `{role}` → cambia ruolo;
  **vietato su se stesso** (anti self-demote); audit; redirect.
- `POST /users/:id/password` [update] body `{password}` → nuovo hash +
  **bump `token_version`** (revoca sessione attiva); audit; redirect.
- TUTTE le mutate: `authorize("users", ...)`; solo `admin`.
- Logica anti-self: negli handler controlla `req.user.sub !== :id` per
  toggle/role (403); il reset password può essere anche su se stessi.

## Criterio di done

Test con mock e utente admin: GET /users → 200; POST /users crea e compare in
lista; toggle disable → login negato (`requireAuth` 403/redirect); change role
di un utente diverso → applicato e l'utente NON può più fare la vecchia
azione; change role su se stesso → 403; reset password → vecchio token
invalido. Viewer/operator → 403 su tutte le rotte `/users`. `node --check`.

## Rischi / note

- La tabella `users` (F0-03) ha già `password_hash`, `token_version`,
  `role CHECK`, `status CHECK`: nessuna migrazione nuova qui.
- Bootstrap: l'admin iniziale arriva da `db/003_bootstrap_admin.sql`.
- Il nome file `users.js` NON è in conflitto con il CMS (repo separato);
  stile route come `gestione-siti-riccardom/src/routes/users.js`.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa la gestione utenti (admin-only).

Modella su `gestione-siti-riccardom/src/routes/users.js` (pattern CRUD + audit;
qui ruolo/status custom admin/operator/viewer). NON toccare `src/index.js` né
la sidebar (li editano F2-09). Crea:
1. `src/routes/users.js` — Router con requireAuth + `authorize("users", ...)`:
   - `GET /users` (list) → render con `{users}`.
   - `GET /users/new` (create) → form.
   - `POST /users` (create) zod {email, name, password min8, role enum} →
     `hashPassword` da `src/services/password.js`, `INSERT`, `auditLog`, flash,
     redirect.
   - `POST /users/:id/toggle` (update): flip status; 403 se `:id ===
     req.user.sub`.
   - `POST /users/:id/role` (update): zod {role}; 403 se self; `auditLog`.
   - `POST /users/:id/password` (update): zod {password min8}; nuovo hash +
     `token_version = token_version + 1`; `auditLog`.
   - Ogni route gestisce `GatewayError`/DB error → flash errore, mai crash.
2. `views/users/index.ejs` — tabella (email, name, role badge, status badge,
   bottoni toggle/role/reset) + form crea (o link `/users/new`).
3. `views/users/form.ejs` — form crea utente (email, name, password, role) e,
   in modifica, i form role/reset.

Verifica `node --check` su tutti i file. Non toccare altri file. Riepiloga in
3 righe.