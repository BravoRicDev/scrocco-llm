---
id: F0-03-db-migrations
fase: F0
dipende_da: []
puo_parallelo_con: F0-01-repo-scaffold, F0-02-infra-docker-compose, F0-04-core-services, F0-08-layout-theme
---

# F0-03 — Migrazioni Postgres + db/migrate.js

## Obiettivo

Creare `db/migrate.js` (identico nella meccanica a quello del CMS) e le prime
migrazioni SQL: utenti (con password bcrypt + token_version + ruoli booleani),
api_tokens, audit_log, sessione (cookie statless, ma tabella `sessions` per
revoca) e le tabelle di servizio richieste dal spec (`config_snapshots` per
F4, `alert_rules` per F4). Il bootstrap admin viene creato dalla migrate SOLO
se la tabella users è vuota.

## File da creare/modificare

- `scrocco-web/db/migrate.js` (crea; copia la meccanica da
  `gestione-siti-riccardom/db/migrate.js`)
- `scrocco-web/db/001_schema.sql` (crea: users, api_tokens, audit_log,
  sessions, magic_links)
- `scrocco-web/db/002_services.sql` (crea: config_snapshots, alert_rules)
- `scrocco-web/db/003_bootstrap_admin.sql` (crea: bootstrap admin se users vuoto)

NON toccare: `src/`, `views/`, `roadmap/`, `package.json`.

## Contratto (schema)

`001_schema.sql`:
- `users` — id serial PK, email citext univoca, name, password_hash (bcrypt),
  role `CHECK (role IN ('admin','operator','viewer'))`, status
  `CHECK (status IN ('active','disabled'))`, token_version int default 0,
  mfa_enabled bool, created_at/updated_at.
- `api_tokens` — come `026_api_tokens.sql` del CMS (token_hash sha256 univoco,
  token_prefix, expires_at, last_used_at, revoked_at, user_id FK).
- `audit_log` — come `001_schema.sql` del CMS + colonna `route_method` e
  `gateway_path` (eventuale), `old_data/new_data JSONB`, `ip_address`.
- `sessions` — id, user_id FK, token_jti, expires_at, revoked_at, created_at
  (per revoca attiva opzionale; il JWT resta in cookie httpOnly).
- `magic_links` — come CMS (user_id FK, token, otp, expires_at, used_at,
  failed_attempts).

`002_services.sql`:
- `config_snapshots` — id, kind (`csv`|`yaml`), content TEXT (yaml o csv
  testuale), source_sha256, created_by (user_id), note, created_at.
- `alert_rules` — id, name, enabled, pool_filter (pattern gruppo),
  health_threshold_pct int, check_every_sec int, webhook_url, telegram_chat_id,
  notify_min_interval_sec, last_notified_at, created_by, created_at.
- Indici su FK e su `config_snapshots(kind, created_at)`.

`003_bootstrap_admin.sql` — blocco PL/pgSQL: se `SELECT count(*) FROM users`
è 0 e `current_setting('app.bootstrap_admin_email', true)` e
`current_setting('app.bootstrap_admin_password', true)` sono valorizzate,
inserisce un utente `role='admin'`, password bcrypt della variabile
`bootstrap_admin_password` (può essere settata da db/migrate.js inline alla
stringa di connessione con `options=-capp.bootstrap_admin_email=...`). Se i
parametri mancano, non fare nulla (log su console da migrate.js).

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
# (serve un Postgres raggiungibile; in locale usa GATEWAY_MOCK e un DB di test)
DATABASE_URL=postgres://... npm run migrate     # migra senza errori
DATABASE_URL=postgres://... npm run migrate     # idempotente (2° giro OK)
```
Il 2° giro non deve cambiare nulla (migrazioni idempotenti, `ON CONFLICT DO NOTHING`).

## Rischi / note

- `citext` richiede l'estensione `citext` disponibile in postgres:16-alpine
  (default: sì). Se preferisci evitarne la dipendenza, usa `email VARCHAR(255)
  COLLATE "C"` + unique lower() btree (il CMS ha già pattern simili).
- Il bootstrap admin via `current_setting` è il modo pulito per iniettare i
  secret da migrate.js senza scriverli nei file .sql (ne `01_schema.sql`).
- Le tabelle F4 (`config_snapshots`, `alert_rules`) esistono già in F0 per
  evitare conflitti di migrazione in parallelo più avanti.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi `db/...`; se `read`
fallisce usa `cat`/`sed -n`).

Copia la meccanica del runner di migrazioni dal CMS gemello
(`gestione-siti-riccardom/db/migrate.js`: tabella `schema_migrations`,
ordine alfabetico dei file `.sql`, ogni file idempotente, errore → exit 1) e
crea le PRIME migrazioni per il pannello `scrocco-web`. Stile: SQL con
`IF NOT EXISTS`, commenti in italiano, `ON CONFLICT DO NOTHING`.

Crea ESATTAMENTE:
1. `db/migrate.js` — runner come sopra; che legge `DATABASE_URL` e, prima del
   giro, passa `current_setting('app.bootstrap_admin_email'...)` impostando le
   variabili GUC `app.bootstrap_admin_email` e `app.bootstrap_admin_password`
   dai rispettivi env `BOOTSTRAP_ADMIN_EMAIL`/`BOOTSTRAP_ADMIN_PASSWORD`
   (così `003_bootstrap_admin.sql` può leggerle senza hardcodare secret).
2. `db/001_schema.sql` — users (email univoca, name, password_hash bcrypt
   `VARCHAR(255)`, role CHECK in ('admin','operator','viewer'), status
   'active'|'disabled', token_version int default 0, mfa_enabled bool,
   timestamps), api_tokens (modello `gestione-siti-riccardom/db/026_api_tokens.sql`),
   audit_log (modello CMS + `route_method`, `gateway_path`), sessions (user_id
   FK, token_jti, expires_at, revoked_at, created_at), magic_links (modello CMS,
   con failed_attempts int default 0). Indici su FK e token.
3. `db/002_services.sql` — config_snapshots (id, kind 'csv'|'yaml', content
   TEXT, source_sha256, created_by FK users nullable, note, created_at; indice
   (kind, created_at)), alert_rules (id, name, enabled bool, pool_filter,
   health_threshold_pct int, check_every_sec int, webhook_url, telegram_chat_id,
   notify_min_interval_sec, last_notified_at, created_by, created_at).
4. `db/003_bootstrap_admin.sql` — blocco DO $$ ... $$ PL/pgSQL che, solo se
   users è vuota e `current_setting('app.bootstrap_admin_email')` non è null,
   inserisce l'utente admin con password bcrypt (bcryptjs) di
   `app.bootstrap_admin_password`; logga via RAISE NOTICE. Non deve fallire se
   i parametri mancano (usa try/exception silenzioso).

Verifica: se hai un Postgres locale, esegui `DATABASE_URL=... npm run migrate`
due volte di fila (la seconda idempotente). Se NON hai Postgres, almeno
`node --check db/migrate.js` e la sintassi SQL è accettata (psql non
disponibile → annota). NON creare `src/` né `views/`. Non toccare altri file.