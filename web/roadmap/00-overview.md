# `scrocco-web` — ROADMAP (pannello web + API + MCP per `scrocco-llm`)

> Tutti i path in QUESTO documento e nei task sono RELATIVI a `~/Serverino`
> (es. `scrocco-web/src/...`, `gestione-siti-riccardom/src/...`). Mai path
> assoluti. Questa roadmap è **solo documentazione**: non genera codice
> applicativo, produce i file Markdown che i subagenti eseguiranno.

## 1. Architettura

```
                     browser (VPN/locale)
                            │  :4002  HTTPS (self-signed)  via Caddy edge_net
                            ▼
                 ┌───────────────────────────┐
                 │         Caddy (esistente) │  ← si aggiunge SOLO un vhost
                 │  gestione-siti-riccardom  │     (nessuna modifica al resto)
                 └────────────┬──────────────┘
                              │ 127.0.0.1:3000 (non esposta fuori da Caddy)
                              ▼
   ┌───────────────────────────────────────────────┐
   │              scrocco-web  (monolite Express)   │   ESM · node:22-alpine
   │ ┌─────────┐ ┌───────────┐ ┌──────────────────┐ │
   │ │ UI  SSR │ │  /api/*   │ │   MCP server     │ │
   │ │  (EJS)  │ │   JSON    │ │  /api/mcp tools  │ │
   │ └────┬────┘ └─────┬─────┘ └───────┬──────────┘ │
   │      │            │               │            │
   │      └────────────┴───────────────┤            │
   │   requireAuth (JWT cookie httpOnly │            │
   │     + API token di lunga durata)   │            │
   │                     │              │            │
   │            ┌────────▼───────────┐  │            │
   │            │ services/gateway.js│  │  (UNICO    │
   │            │  wrapper s2s verso │  │   punto    │
   │            │  GATEWAY_URL/admin*│  │   d'uscita)│
   │            └────────┬───────────┘  │            │
   │   Postgres 16 ┌─────▼──────────────┼─────┐      │
   │   (vol. pgdata)│     scrocco-web → │ → GATEWAY_MASTER_KEY (SOLO env) │
   └───────────────┴─────────────────┬──┴────┘      │
                                     │              │
              http://scrocco-llm:4001              │
                                     ▼              │
                    ┌───────────────────────────┐   │
                    │  scrocco-llm (FastAPI)    │   │  repo SEPARATO, inviolato
                    │  /admin/*  Bearer master  │   │  (solo prerequisiti GP per
                    │  var/keys_rotation.csv +  │   │   playground/csv/raw/backups)
                    │  var/gateway.yaml         │   │
                    └───────────────────────────┘   │
```

Regole chiave:
- **La master-key vive SOLO in `scrocco-web/.env`** (`GATEWAY_MASTER_KEY`) e viene
  usata server-to-server da `services/gateway.js`. Il browser NON la vede mai:
  si autentica con la sessione JWT di `scrocco-web`.
- Il gateway resta l'engine; `scrocco-web` è un client admin+. La TUI resta
  il riferimento "di parità" (`scrocco-llm/tui/`).
- Eventuali scritture su `scrocco-llm` sono SOLO i prerequisiti GP (v. 
  `GATEWAY-PREREQS.md`) e sono in repo separato.

## 2. Stack (identico a `gestione-siti-riccardom/`)

- `node:22-alpine`, ESM (`"type":"module"`).
- Express 4 + `express-ejs-layouts` + EJS; `helmet`, `cookie-parser`,
  `express-rate-limit` (per-IP **e** per-account), CSRF via Origin/Referer,
  `request-id`, `winston`, `zod`, `jsonwebtoken`, `pg` (Pool), `dotenv`,
  `nodemailer` (opzionale), `@modelcontextprotocol/sdk`.
- Aggiunte per scrocco-web: `bcryptjs` (no compilazione nativa su Alpine),
  `uplot` (grafici), `diff` (diff CSV/yaml), `js-yaml` (editor raw).
- Migrazioni `db/*.sql` ordinate + `db/migrate.js`, eseguite da
  `scripts/start.sh` e prima dei test (`npm test`).

## 3. Decisioni già prese (riepilogo)

1. **Topologia**: monolite `scrocco-web` davanti a `scrocco-llm`.
2. **Login umani**: password bcrypt di default; magic-link/OTP se SMTP configurato.
   JWT `httpOnly` in cookie; logout → bump `token_version`.
3. **RBAC 3 ruoli** (vedi `src/constants/roles.js` + `permissions.js`,
   modello del CMS ma ruoli custom):
   - `admin` — tutto.
   - `operator` — deployment CRUD+bulk, cooldown clear, probe, reload, release
     sessioni, unretire, playground. MAI: policy PATCH, editor CSV/gateway.yaml,
     gestione utenti, alert rules, config history restore.
   - `viewer` — sola lettura.
4. **Postgres 16**: container dedicato nel compose, volume `pgdata`, healthcheck,
   `depends_on`.
5. **Deploy**: porta interna 3000, pubblicata SOLO via Caddy `edge_net` su :4002
   TLS interno self-signed (snippet pronto in `scrocco-web/caddy/`).
6. `.env` separato (v. `.env.example`).
7. **Log del gateway** letti via polling (`/admin/logs/calls?since=`,
   `/admin/logs/errors`) — nessun volume condiviso.
8. **Bootstrap del codice**: copiare/sfoltire lo scaffold del CMS
   (`src/db.js`, middleware, services, `db/migrate.js`, layout, Dockerfile,
   compose, openapi, agent-login). Non reinventare.

## 4. Endpoint admin del gateway esistenti (contratto reale verificato)

Da `scrocco-llm/app/admin.py`, `app/bootstrap.py`, `app/main.py`
(prefix `/admin`, auth `Authorization: Bearer <GATEWAY_MASTER_KEY>`):

| Endpoint | Metodo | Uso | RBAC scrocco-web |
|---|---|---|---|
| `/admin/deployments?profile=` | GET | lista deployments | read: tutti |
| `/admin/deployments` | POST | crea (campi: profile, modello, endpoint, data, key, context, caps…) | write: admin/operator |
| `/admin/deployments/{row_hash}` | PUT / DELETE | update / delete | write: admin/operator |
| `/admin/deployments/bulk` | POST | `{operations:[{action:create,update,delete, id?, ...}]}` atomico | write: admin/operator |
| `/admin/deployments/expiring?days=` | GET | `{days, expiring:[{id,modello,in_days,data_raw}]}` | read: tutti |
| `/admin/deployments/probe` | POST | `{unique|id, force?}` → `{unique,ok,latency_ms,cached?,error_class,detail?}` | write: admin/operator |
| `/admin/deployments/probe/bulk` | POST | `{filter:all|cap:x|<profile>, force?}` → `{filter,count,results}` | write: admin/operator |
| `/admin/deployments/unretire` | POST | `{unique}` → `{ok,unique,state}` | write: admin/operator |
| `/admin/profiles` | GET | `{count, profiles:[{name,base_model,dims_k,groups,deployments,step_up_pct,…}]}` | read: tutti |
| `/admin/profiles/purge` | POST | `{profile}` (solo se 0 righe usano la colonna) | write: admin/operator |
| `/admin/state` | GET | stato completo (cooldowns, sticky, budget, capabilities, health, adaptive, policy) | read: tutti |
| `/admin/history?limit=` | GET | `{total, entries:[...journal]}` | read: tutti |
| `/admin/policy` | GET | `{file, configured, effective:{...}}` (alias/client keys mascherate) | read: tutti |
| `/admin/policy` | PATCH | patch parziale yaml (scalari; profiles merget; alias_keys/client_keys add/del; liste intere) | write: admin SOLO |
| `/admin/cooldowns/clear` | POST | `{unique?}` → `{ok, cleared}` | write: admin/operator |
| `/admin/sessions/release` | POST | `{session_id?}` → `{ok, released}` | write: admin/operator |
| `/admin/reload` | POST | `{reloaded, profiles, deployments, policy:{...}}` | write: admin/operator |
| `/admin/capabilities/seed-from-map` | POST | `{dry_run?}` → dry run `{dry_run,count,total,proposals}` o `{ok,applied,skipped,errors}` | write: admin/operator |
| `/admin/capabilities/audit` | POST | report `{accounts, missing_models[], cap_suggestions, errors}` | write/test: tutti (solo lettura logica) |
| `/admin/logs/calls?tail&since&tags=` | GET | `{events:[...summary/route…]}` | read: tutti |
| `/admin/logs/errors?tail&since&filter=` | GET | `{events:[...]}` | read: tutti |
| `/admin/insights?days&group_by=` | GET | `{total, by_<gb>}` / `{total, aggregate}` | read: tutti |
| `/admin/insights/summary` | GET | 24h compatto | read: tutti |
| `/admin/insights/leaderboard?window&sort&order&profile=` | GET | `{window_days, count, rows:[{dep,profile,group,provider,model,calls,avg_dur_ms,p95_dur_ms,error_rate,fb_rate,qc_rate,wd_rate,last_used,health,probe_ms}]}` | read: tutti |
| `/admin/guide` | GET | markdown AGENT.md | read: tutti |
| `/bootstrap` `/bootstrap/status` `/bootstrap/providers` | GET | playbook / gap-analysis / provider registry (pubblici) | read: tutti |
| `/healthz` | GET | stato sanitario compatto | — |
| `/admin/reload` (in `main.py`) | POST | come sopra | write |

Prerequisiti lato gateway (NON esistenti oggi) → `GATEWAY-PREREQS.md`:
GP-01 `POST /admin/playground`, GP-02 `GET|PUT /admin/csv`,
GP-03 `GET|PUT /admin/policy/raw`, GP-04 `GET /admin/backups` + `POST /admin/backups/restore`.

## 5. Fasi e task

Tutte le sigle sono `task`, ognuno in `roadmap/tasks/FN-NN-<slug>.md`. Ogni
fase termina con un task di **integrazione/smoke** che monta le route in
`src/index.js`, aggiorna la sidebar e scrive i test `node --test`.

- **F0 — Scaffold** (11 task): repo, infra Docker+Caddy, migrazioni Postgres,
  core services, wrapper gateway + mock COMPLETO delle fixture, auth+RBAC
  (matrice permessi completa), api-tokens, layout/theme, health+
  openapi base, route api-tokens+agent-login, **integrazione F0**.
- **F1 — Parità READ** (11 task): deployments list, profili, policy view, capacità,
  dashboard/state, scadenze, history, insights, bootstrap, guide, **integrazione F1**.
- **F2 — Parità WRITE** (10 task): CRUD deployment, bulk, profili write, policy
  editor, alias/client keys, azioni di sistema (cooldown/release/reload/unretire),
  probe, capacità write (seed/audit), **users admin (CRUD utenti)**, **integrazione F2**.
- **F3 — Osservabilità** (5 task): live calls (polling+auto-refresh), errori,
  leaderboard, grafici uPlot, **integrazione F3**.
- **F4 — "E anche di più"** (9 task): editor CSV, playground+trace, editor
  gateway.yaml raw, config history/rollback, timeline salute chiavi, alert rules,
  sessioni sticky, temi/ricerca/shortcut, **integrazione F4**.
- **F5 — Agenti** (7 task): `/api/*` read deployments, `/api/*` read core,
  `/api/*` write, MCP server, openapi completo, AGENT.md bilingue,
  **integrazione F5**.
- **Gateway prerequisiti** (4 task Python, in `GATEWAY-PREREQS.md`): GP-01..GP-04.

Totale: 53 task web + 4 task gateway.

## 6. Grafo delle dipendenze (chi blocca chi)

```
F0-01..F0-08 (base)
   └─► [0B] F0-05 (wrapper gateway + mock completo), F0-06 (api-tokens svc)
            └─► [0C] F0-07 (auth+RBAC, matrice completa), F0-09 (health)
                     └─► [0C-bis] F0-10 (route api-tokens + agent-login)   ← dopo F0-07
                              └─► F0-11 (integrazione F0)  ← BARRIERA F0
                                     ├─► F1-01..F1-10 (read, in parallelo)
                                     │        └─► F1-11 (integrazione F1)  ← BARRIERA F1
                                     │              ├─► F2-01..F2-08, F2-10 (write, in parallelo)
                                     │              │        └─► F2-09 (integrazione F2)
                                     │              ├─► F3-01..F3-04 (osservabilità, IN PARALLELO a F2)
                                     │              │        └─► F3-05 (integrazione F3)
                                     │              ├─► F4-05..F4-08 (parallelo, no GP)
                                     │              │        └─► F4-09 (integrazione F4, wave 4A)
                                     │              ├─► GP-01..GP-04 (gateway, SEQUENZIALI)
                                     │              │        └─► F4-01..F4-04 (parallelo, dip. GP)
                                     │              │              └─► F4-09 (integrazione F4, wave 4B)
                                     │              └─► F5-01, F5-02, F5-03 (API read/write)
                                     │                    └─► F5-06 (AGENT.md, dopo 5A)
                                     │                          ├─► F5-04, F5-05 (parallelo)
                                     │                          │      └─► F5-07 (integrazione F5)
```

Regole di blocco:
- Niente task F1+ prima di **F0-11** (index.js deve girare).
- I task **di una stessa wave** non condividono file (verificato per
  costruzione: ogni wave tocca file disgiunti).
- **F3-xx dipende da F1-11 e corre IN PARALLELO con F2** (dichiarato da
  `dipende_da: [F1-11-integration]` nei task F3-xx) — NON dopo F2-09.
- `src/constants/permissions.js` è creato UNA volta da F0-07 con la matrice
  COMPLETA; i task F2-02/F2-07 NON lo toccano (eventuali append solo in F2-09).
- F2-xx estende file nati in F1-xx (stesso file, fasi sequenziali: OK).
- F4-01/02/03/04 richiedono i GP-01/02/03/04 **merging schierati in lato
  gateway** (uno alla volta: `app/admin.py` è condiviso).
- F5-04 (MCP) e F5-05 (openapi) richiedono che le route `/api/v1/*` esistano.

## 7. Convenzioni obbligatorie per i SUBAGENTI

1. **Path relativi**: cwd = `~/Serverino/scrocco-web` per i task web (path
   `src/...`, `views/...`). Per i task gateway cwd = `~/Serverino/scrocco-llm`.
   MAI path assoluti `/home/...` o `/workspace/...`. Se lo strumento `read`
   fallisce, usa `cat` / `sed -n '1,200p' <file>`.
2. **Stile**: copia dal CMS `gestione-siti-riccardom/` (stesso stack, stesso
   stile ESM: import in testa, commenti in italiano, `Router` da express,
   `try/catch` + `next(err)` nelle route, warning/error con `logger`). Leggi
   almeno `src/routes/users.js`, `src/middleware/auth.js`, `src/services/audit.js`,
   `views/layouts/admin.ejs`.
3. **Un task = pochi file**: modifica SOLO i file elencati nella sezione
   "File da creare/modificare". Non toccare altri file (le wave parallele
   dipendono da questa regola).
4. **`src/index.js` e la sidebar sono di proprietà dei task di integrazione**
   (`*-11-integration.md` dei rispettivi task). Nessun altro task li modifica.
5. **Ogni mutazione** passa per `services/gateway.js` (mai `fetch` diretto) e
   scrive `auditLog(...)` in `src/services/audit.js`.
6. **Ogni route** valida il body con `zod`; **ogni route** applica
   `requireAuth` e `authorize(resource, action)`.
7. **RBAC**: `admin`, `operator`, `viewer` (vedi `src/constants/roles.js`).
   `viewer` = solo GET; `operator` = azioni operatore (mai policy PATCH / csv /
   yaml / utenti / alert / config restore); `admin` = tutto.
8. Lingua UI: italiano (non serve i18n per l'UI; `res.locals.t` resta come
   convenzione con fallback = la chiave stessa).
9. Verifica SEMPRE alla fine: `npm test` (se il task tocca codice eseguibile)
   o almeno `node --check <file>`. Non inventare comandi lint: il progetto non
   definisce lint, solo `npm test`.

## 8. Criteri trasversali di done (ogni fase)

- App parte con `npm start` o `docker compose up` e risponde su :3000.
- `/health` OK con DB e gateway raggiungibili (o mock).
- Tutti i test `node --test --test-force-exit --test-concurrency=1` verdi
  (i test usano `GATEWAY_MOCK=1` quando non c'è il live gateway).
- Ogni mutazione loggata in `audit_log`; ogni richiesta scrive `request-id`.