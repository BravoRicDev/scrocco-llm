---
id: F0-10-api-tokens-route
fase: F0
dipende_da: [F0-06-api-tokens-service, F0-07-auth-rbac]
puo_parallelo_con: []
---

# F0-10 — Route UI API tokens + script agent-login

## Obiettivo

Creare la pagina utente per gli API token (crea/lista/revoca, token mostrato
una volta sola, stile CMS `routes/api-tokens.js`) e `scripts/agent-login.mjs`,
uno strumento CLI che gli agenti useranno per autenticarsi e ottenere un API
token di lunga durata.

**Decisioni di progetto (UNICHE, non ci sono alternative):**
1. `routes/auth.js` (di F0-07) riceve la **append** di `POST /api/agent/login`
   — endpoint stateless che valida email+password e ritorna un **JWT agent
   (claim `agent:true`, 7 giorni)**. È un'append in coda al file, nessuna
   modifica alle parti esistenti. F0-10 non gira in parallelo a F0-07.
2. `routes/api-tokens.js` espone sia l'UI `/admin/api-tokens` sia le rotte
   JSON `/api/agent/api-tokens` (GET/POST/DELETE) per il CLI, riusando le
   stesse funzioni del service `services/api-tokens.js` (F0-06).
3. `scripts/agent-login.mjs` fa: (a) `POST /api/agent/login` → JWT agent;
   (b) `POST /api/agent/api-tokens` con quel Bearer → API token `agtok_...`
   mostrato UNA volta.

## File da creare/modificare

- `scrocco-web/src/routes/api-tokens.js` (crea)
- `scrocco-web/src/routes/auth.js` (modifica — SOLO append di
  `POST /api/agent/login`)
- `scrocco-web/scripts/agent-login.mjs` (crea)
- `scrocco-web/views/admin/api-tokens/index.ejs` (crea)

NON toccare: `src/index.js` (monta F0-11), `db/`, `roadmap/`, altri file.

## Contratto

- `routes/api-tokens.js` (router):
  - `GET /admin/api-tokens` [requireAuth] → render
    `views/admin/api-tokens/index` con `{tokens, newToken:null, baseUrl}`.
  - `POST /admin/api-tokens` [requireAuth] → crea per `req.user.sub`, `name`
    richiesto, `expires_days` tra i giorni consentiti [30,60,90,120,180,365]
    (default 120), render con `newToken` (il token in chiaro SOLO qui).
  - `POST /admin/api-tokens/:id/revoke` [requireAuth] → revoca del
    proprietario, redirect.
  - `GET /api/agent/api-tokens` + `POST /api/agent/api-tokens` +
    `DELETE /api/agent/api-tokens/:id` [requireAuth] → stesse funzioni in JSON
    `{tokens:[...]}` / `{token, prefix, expires_at}` (per il CLI;
    `requireAuth` accetta anche il Bearer JWT agent).
- `routes/auth.js` — APPEND in coda:
  - `POST /api/agent/login` — body zod `{email, password}`; verifica con
    `services/password.js` (bcryptjs); se ok → `jwt.sign` con
    `{sub, email, name, role, token_version, agent:true}`, `expiresIn: 7d`,
    risposta `{token, user:{id,email,name,role}}`. Errori → 401 generico
    (non rivelare quale campo). Stateless (nessun cookie richiesto).
- `scripts/agent-login.mjs` (ESM, self-contained, `npm run agent-login`):
  - CLI: `--email`, `--password` (login a password) o `--magic-link` (stampa
    istruzioni), `--base-url` default `SCROCCO_WEB_URL` o
    `http://127.0.0.1:3000`.
  - Flusso password: `POST {base}/api/agent/login` {email,password} → `token`
    (JWT agent) → salva in `~/.scrocco-web/agent.token` (chmod 600) → crea API
    token via `POST {base}/api/agent/api-tokens` con Bearer `token` → stampa
    una sola volta `agtok_...`, `prefix`, `expires_at` e il path del file.
  - Solo `fetch` globals, nessuna dipendenza extra.

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
node --check scripts/agent-login.mjs && node --check src/routes/api-tokens.js \
  && node --check src/routes/auth.js
```
Se DB+app in esecuzione (o GATEWAY_MOCK): `node scripts/agent-login.mjs
--email admin@x --password 'secret'` → stampa un token `agtok_...` che poi
`/api/auth/me` accetta via Bearer.

## Rischi / note

- Il token in chiaro si legge UNA volta: se lo perdi vai su /admin/api-tokens
  e revochi.
- CSRF (F0-07) esenta le chiamate con SOLO Bearer e senza cookie → gli agenti
  vanno bene.
- `agent-login.mjs` self-contained, nessuna dipendenza extra.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi `src/...`,
`scripts/...`, `views/...`; se `read` fallisce usa `cat`/`sed -n`).

Implementa la gestione API token e il CLI di login per agenti del pannello
`scrocco-web`. Stile: CMS gemello `gestione-siti-riccardom/src/routes/api-tokens.js`.

NON esistono alternative da valutare: segui ESATTAMENTE questa decisione.
PASSI (niente modifiche a `src/index.js`, che monta tutto in F0-11):
1. Crea `src/routes/api-tokens.js` — router che riusa
   `src/services/api-tokens.js` (F0-06):
   - `GET /admin/api-tokens` (requireAuth) → render
     `views/admin/api-tokens/index.ejs` con `{tokens, newToken:null, baseUrl}`.
   - `POST /admin/api-tokens` (requireAuth) → crea per l'utente corrente,
     `expires_days` in [30,60,90,120,180,365] default 120; render con
     `newToken` {token grezzo} una sola volta.
   - `POST /admin/api-tokens/:id/revoke` (requireAuth) → revoca del
     proprietario, redirect `/admin/api-tokens`.
   - `GET /api/agent/api-tokens` + `POST /api/agent/api-tokens` +
     `DELETE /api/agent/api-tokens/:id` (requireAuth, accetta Bearer agent) →
     risposte JSON `{tokens:[...]}` / `{token, prefix, expires_at}` per il CLI.
2. Crea `views/admin/api-tokens/index.ejs` — tabella token (prefix, name,
   expires_at, last_used_at, revoked), form crea, e se `newToken` → box verde
   "Salva subito" con il token in chiaro in input readonly (max-width, copy).
3. APPEND (aggiungi in coda, NON modificare le parti esistenti) in
   `src/routes/auth.js`: endpoint `POST /api/agent/login` {email,password} →
   valida con `services/password.js` (bcryptjs); se ok → `jwt.sign` con
   `{sub,email,name,role,token_version,agent:true}` expiresIn 7d e
   `res.json({token, user:{id,email,name,role}})`. Risposta 401 generica se
   credenziali errate (non rivelare quale campo); stateless.
4. Crea `scripts/agent-login.mjs` — ESM, self-contained:
   - Args: `--email`, `--password` o `--magic-link` (stampa istruzioni),
     `--base-url` default `SCROCCO_WEB_URL` o `http://127.0.0.1:3000`.
   - Flusso password: `POST {base}/api/agent/login` {email,password} → JWT
     agent in `{token}` → salva in `~/.scrocco-web/agent.token` (chmod 600) →
     `POST {base}/api/agent/api-tokens` con validità 120d e Bearer `token` →
     stampa `agtok_...`, `prefix`, `expires_at` una sola volta + path del file.

Verifica: `node --check` sui 3 file. Non toccare altre parti di
`routes/auth.js` se non l'append. Non toccare altri file. In 3 righe riepiloga
i file e il flusso.