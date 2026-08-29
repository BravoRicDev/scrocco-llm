---
id: F0-07-auth-rbac
fase: F0
dipende_da: [F0-04-core-services, F0-06-api-tokens-service]
puo_parallelo_con: F0-09-health-openapi
---

# F0-07 — Auth: login password + magic-link + JWT + RBAC

## Obiettivo

Implementare l'autenticazione stile CMS: `requireAuth` (JWT in cookie httpOnly
oppure API token da header), `authorize(resource, action)` con i ruoli
admin/operator/viewer, rotte `/login`, `/api/auth/*` con login a password
(bcryptjs) e magic-link/OTP opzionale, logout (bump token_version), rate-limit
per-IP e per-account, e i file costanti ruolo/permessi.

## File da creare/modificare

- `scrocco-web/src/constants/roles.js` (crea)
- `scrocco-web/src/constants/permissions.js` (crea)
- `scrocco-web/src/middleware/auth.js` (crea)
- `scrocco-web/src/middleware/authorize.js` (crea)
- `scrocco-web/src/middleware/csrf.js` (crea)
- `scrocco-web/src/services/password.js` (crea)
- `scrocco-web/src/services/magic-link.js` (crea)
- `scrocco-web/src/routes/auth.js` (crea; crea la dir `routes/`)

NON toccare: `views/`, `db/`, `roadmap/`, `package.json`, `src/index.js`
(integrazione F0-11), altri file `src/` (in particolare `src/services/api-tokens.js`
è di F0-06, va solo importato).

## Contratto

- `constants/roles.js`: `export const ROLES = { ADMIN: "admin", OPERATOR:
  "operator", VIEWER: "viewer" }`.
- `constants/permissions.js`: matrice risorsa→azione→ruoli. Risorse:
  `deployments` (read: tutti; create/update/delete/bulk/probe/unretire:
  admin,operator), `profiles` (read tutti; create/purge admin,operator),
  `policy` (read tutti; update admin SOLO), `capabilities` (read tutti;
  seed admin,operator; audit tutti), `state` read tutti, `expiring` read tutti,
  `history` read tutti, `insights` read tutti, `observability` read tutti,
  `bootstrap` read tutti, `guide` read tutti, `system` (reload/cooldowns/
  sessions/release: admin,operator — **azioni atomiche** `system/reload`,
  `system/cooldowns`, `system/sessions`, `system/release`), `keys` (read
  tutti; rotate admin), `users` (admin SOLO — tutte le azioni `users/*`),
  `api_tokens` (self: admin,operator,viewer sul proprio), `csv` (read
  admin,operator; write admin), `playground` (azione **`playground/use`**:
  admin,operator), `config_snapshots` (read admin,operator; restore admin;
  azione **`config/restore`**), `alerts` (admin SOLO — tutte le azioni
  `alerts/*`: read/create/update/delete), `audit` (**`audit/read`: TUTTI**),
  `ui_settings` (tutti). `authorize` legge da qui.
- Nomi azione COERENTI (usati in F2-06, F4-07, F5-03):
  - `authorize("system","reload")`, `authorize("system","cooldowns")`,
    `authorize("system","sessions")`, `authorize("system","release")`
    (per le release sessioni sticky).
  - `authorize("users", ...)` per il CRUD utenti (F2-10).
  - `authorize("playground","use")` per il run del playground (F4-02).
  - `authorize("config_snapshots","restore")` (F4-04) e
    `authorize("alerts", ...)` (F4-06) per gli admin.
- `middleware/auth.js`: identico pattern CMS. `requireAuth(req,res,next)`:
  token da cookie `req.cookies[config.sessionCookieName]` o da
  `Authorization: Bearer`. Se token API (via `isApiTokenFormat`) →
  `verifyApiToken`. Altrimenti `jwt.verify` + query utente per `token_version`
  e status. Su `/api` ritorna JSON 401; altrimenti redirect `/login`.
  Valorizza `req.user`, `res.locals.user`.
- `middleware/authorize.js`: `authorize(resource, action)` → se ruolo admin
  bypass; altrimenti guarda `PERMISSIONS[role][resource][action]`; 401/403 JSON
  su `/api`, render `error` sul web.
- `middleware/csrf.js`: identico a CMS (Origin/Referer check su POST/PUT/
  PATCH/DELETE autenticati via cookie; esenzione richieste con solo Bearer
  header e niente cookie).
- `services/password.js`: `hashPassword(pw)` → bcryptjs hash; `verifyPassword`.
  Forza minima 8 caratteri (zod).
- `services/magic-link.js`: copia CMS (`generateAndSend`, `verify`) usando le
  colonne della tabella magic_links; se SMTP non configurato ritorna
  `{sent:false, reason}` senza inviare.
- `routes/auth.js`:
  - `GET /login` render `views/auth/login`
  - `POST /api/auth/login` {email,password} → verifica password (se utente ha
    password_hash) → JWT in cookie `token` httpOnly. Se password errata →
    401 uniforme. Se l'utente è `disabled` → 403.
  - `POST /api/auth/login/magic` {email} → genera magic-link (solo se SMTP).
  - `POST /api/auth/verify` {token, otp} → verifica OTP → JWT cookie.
  - `GET /api/auth/me` requireAuth → `{user}`.
  - `POST /api/auth/logout` requireAuth → bump token_version + clear cookie.
  - rate-limit: `loginLimiter` e `loginAccountLimiter` per-IP e per-email
    (10/15min), applicati alle rotte /api/auth/*
  - jwt sign con `{sub,email,name,role,token_version,agent}` per l'agent
    flow e `api/auth/verify-otp` (per `scripts/agent-login.mjs`, 7d token agent).

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
node --check src/routes/auth.js && node --check src/middleware/auth.js \
  && node --check src/middleware/authorize.js && node --check src/middleware/csrf.js
```
Con DB: crea utente (bcrypt), login → cookie, /api/auth/me OK; `authorize` su
operazione "policy update" con ruolo operator → 403; con viewer → 403;
logout invalida. Il criterio funzionale pieno è però nel task F0-11
(integrazione): qui basta che le funzioni e le route compilino e i check
passino (i test e2e arrivano in F0-11).

## Rischi / note

- Password hash: usa SOLO bcryptjs (pure JS, funziona su Alpine senza build
  native). NCripta mai in chiaro.
- Non loggare mai email+password; uniforma i tempi di risposta tra email
  esistente/non esistente (pattern anti-enumeration del CMS).
- La sessione è JWT stateless; `token_version` è la revoca.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi `src/...`; se `read`
fallisce usa `cat`/`sed -n`).

Implementa l'autenticazione e l'RBAC del pannello, copiando il pattern del CMS
gemello `gestione-siti-riccardom`: leggi `src/middleware/auth.js`,
`src/middleware/authorize.js`, `src/middleware/csrf.js` (identici per
meccanica) e `src/services/magic-link.js`, `src/routes/auth.js` (per la parte
magic-link e i rate-limit). Il login qui è a PASSWORD per default (bcryptjs),
con magic-link opzionale solo se SMTP configurato, e ruoli custom
admin/operator/viewer.

Crea ESATTAMENTE:
1. `src/constants/roles.js` — ROLES {ADMIN,OPERATOR,VIEWER}.
2. `src/constants/permissions.js` — matrice risorse/azioni/ruoli come da
   contratto (COMPLETA fin da subito: questa è l'UNICA fonte degli append a
   questo file; nessun task F2-xx la modifica): deployments(read:admin,
   operator,viewer; write:admin,operator; bulk/probe/unretire:admin,operator),
   profiles(read tutti; create/purge admin,operator), policy(read tutti;
   update admin SOLO), capabilities(read tutti; seed admin,operator; audit
   tutti), state/expiring/history/insights/observability/bootstrap/guide
   (read tutti), system(admin,operator — azioni `system/reload`,
   `system/cooldowns`, `system/sessions`, `system/release`),
   keys(read tutti; rotate admin), users (admin SOLO, `users/*`),
   api_tokens (proprio token: tutti), csv(read admin,operator;
   write admin), playground (**`playground/use`: admin,operator**),
   config_snapshots(read admin,operator; **restore admin =
   `config_snapshots:restore`/`config/restore`**), alerts(admin SOLO,
   `alerts/*`), **audit (`audit/read`: TUTTI)**, ui_settings(tutti).
3. `src/middleware/auth.js` — requireAuth (cookie token | Bearer; api-token
   path via F0-06 `isApiTokenFormat`+`verifyApiToken`; jwt.verify; check
   token_version+status su users; JSON 401 per /api, redirect /login altrove).
4. `src/middleware/authorize.js` — `authorize(resource, action)` da
   PERMISSIONS; superadmin→all; 401/403.
5. `src/middleware/csrf.js` — Origin/Referer check (UNSAFE_METHODS), esenzione
   Bearer-senza-cookie.
6. `src/services/password.js` — `hashPassword`, `verifyPassword` con bcryptjs.
7. `src/services/magic-link.js` — generateAndSend/verify come CMS, ritorna
   `{sent:false, reason}` se SMTP non configurato (usa `config.smtpHost`).
8. `src/routes/auth.js` — `/login` GET, `POST /api/auth/login` {email,
   password} → jwt cookie (httpOnly, sameSite lax, maxAge da jwtExpiresIn),
   `POST /api/auth/login/magic`, `POST /api/auth/verify` {token, otp},
   `GET /api/auth/me` (requireAuth), `POST /api/auth/logout` (bump
   token_version + clear cookie), `POST /api/agent/verify-otp` {email, token,
   otp} per l'agent-login (ritorna {token} jwt 7d con claim agent:true).
   Rate-limit per-IP e per-account (10 per 15 minuti) sulle rotte login/verify.

Non toccare `src/index.js` (lo monta F0-11) né `src/services/api-tokens.js`,
né `views/` (le viste auth esistono già da F0-08). Verifica `node --check` su
tutti i file. In 3 righe riepiloga ciò che hai implementato.