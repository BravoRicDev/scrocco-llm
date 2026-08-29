---
id: F0-01-repo-scaffold
fase: F0
dipende_da: []
puo_parallelo_con: F0-02-infra-docker-compose, F0-03-db-migrations, F0-04-core-services, F0-08-layout-theme
---

# F0-01 — Scaffold repo

## Obiettivo

Creare la base del progetto `scrocco-web/` (che esiste già come directory vuota
con dentro solo `roadmap/`): `package.json` completo (ESM, tutte le dipendenze),
`.gitignore`, `.env.example` e `README.md` scheletro. Installare le dipendenze
con `npm install` così i task successivi possono scrivere test subito.

## File da creare/modificare

- `scrocco-web/package.json` (crea)
- `scrocco-web/.gitignore` (crea)
- `scrocco-web/.env.example` (crea)
- `scrocco-web/README.md` (crea, scheletro)
- `scrocco-web/package-lock.json` (generato da `npm install`)

NON toccare: `roadmap/`, `src/`, `db/`, `views/`, `scripts/`.

## Contratto

- `"type": "module"`, `engines.node >= 22`.
- Script npm: `dev` = `node --watch src/index.js`, `start` = `node src/index.js`,
  `migrate` = `node db/migrate.js`, `agent-login` = `node scripts/agent-login.mjs`,
  `test` = `node db/migrate.js && node --test --test-force-exit --test-concurrency=1`.
- Dipendenze (con versioni coerenti con quelle già usate dal CMS
  `gestione-siti-riccardom/package.json` per i pacchetti comuni):
  - `dotenv`, `express@^4.21`, `express-ejs-layouts@^2.5`, `ejs@^3.1`,
    `helmet@^8`, `cookie-parser`, `express-rate-limit@^7.4`, `jsonwebtoken@^9`,
    `pg@^8.21`, `winston@^3.17`, `zod@^3.24`, `nodemailer@^6.9`,
    `@modelcontextprotocol/sdk@^1.30`, `diff@^9`
  - `bcryptjs@^2.4` (login password senza compilazione nativa)
  - `uplot@^1.6` (grafici F3), `js-yaml@^4` (editor gateway.yaml F4)
- `.env.example` con TUTTE le variabili del spec (commentate, default):
  `DATABASE_URL`, `JWT_SECRET`, `GATEWAY_URL` (default
  `http://scrocco-llm:4001`), `GATEWAY_MASTER_KEY`, `PORT=3000`, `NODE_ENV`,
  `LOG_LEVEL`, `APP_NAME`, `SESSION_COOKIE_NAME=token`,
  `BOOTSTRAP_ADMIN_EMAIL`, `BOOTSTRAP_ADMIN_PASSWORD`, `JWT_EXPIRES_IN`,
  `SMTP_HOST/PORT/USER/PASS`, `EMAIL_FROM`, `MAGIC_LINK_BASE_URL`,
  `GATEWAY_MOCK` (default vuoto).
- `.gitignore`: `.env`, `node_modules/`, `*.log`, `.DS_Store`, `var/`,
  `backups/`, `test/tmp/`.

## Criterio di done

```bash
cd ~/Serverino/scrocco-web && npm install   # esce 0, crea package-lock.json
npm pkg get type            # => "module"
```
`node --check src/index.js` NON deve girare ancora (src/ non esiste): il
criterio è solo che `npm install` completi e i file sopra esistano.

## Rischi / note

- `npm install` richiede rete npm: nel deploy Docker è eseguito al build; qui
  serve solo per lo sviluppo locale e i test. Se la rete manca, annota nel
  README che `npm ci` deve girare in un ambiente con accesso al registry.
- Non aggiungere dipendenze non elencate (le wave parallele contano sul fatto
  che package.json non cambi dopo questo task).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora nella directory `~/Serverino/scrocco-web` (MAI path assoluti `/home` o
`/workspace`: usa solo path relativi `scrocco-web/...`; se lo strumento `read`
fallisce usa `cat` o `sed -n '1,200p' <file>`).

Scaffold di base per un'app Node/Express **ESM** chiamata `scrocco-web`: un
pannello web che in fasi successive parlerà con il gateway LLM `scrocco-llm`
via API. Lo stile va copiato dal CMS gemello: guarda
`gestione-siti-riccardom/package.json` (path relativo da `~/Serverino`) per
versioni già usate, e non reinventare convenzioni.

Crea ESATTAMENTE questi file, solo questi:
1. `package.json` — `"type":"module"`, engines node>=22, script: `dev`
   (`node --watch src/index.js`), `start` (`node src/index.js`),
   `migrate` (`node db/migrate.js`), `agent-login`
   (`node scripts/agent-login.mjs`), `test`
   (`node db/migrate.js && node --test --test-force-exit --test-concurrency=1`).
   Dipendenze: dotenv, express@^4.21, express-ejs-layouts@^2.5, ejs@^3.1,
   helmet@^8, cookie-parser, express-rate-limit@^7.4, jsonwebtoken@^9,
   pg@^8.21, winston@^3.17, zod@^3.24, nodemailer@^6.9,
   @modelcontextprotocol/sdk@^1.30, diff@^9, bcryptjs@^2.4, uplot@^1.6,
   js-yaml@^4.
2. `.env.example` — commentato, con: DATABASE_URL, JWT_SECRET, GATEWAY_URL
   (default `http://scrocco-llm:4001`), GATEWAY_MASTER_KEY, PORT=3000,
   NODE_ENV, LOG_LEVEL, APP_NAME, SESSION_COOKIE_NAME=token,
   BOOTSTRAP_ADMIN_EMAIL, BOOTSTRAP_ADMIN_PASSWORD, JWT_EXPIRES_IN,
   SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM,
   MAGIC_LINK_BASE_URL, GATEWAY_MOCK.
3. `.gitignore` — `.env`, `node_modules/`, `*.log`, `.DS_Store`, `var/`,
   `backups/`, `test/tmp/`.
4. `README.md` — scheletro: titolo, una riga di descrizione ("pannello web +
   API + MCP per il gateway LLM scrocco-llm"), sezione Stack, sezione "Come
   avviare" con `cp .env.example .env`, `npm install`, `npm run migrate`,
   `npm start`.

Poi esegui `npm install` (crea `package-lock.json`). Non creare altro. Non
toccare `roadmap/`. Alla fine descrivi in 2 righe cosa hai creato e se
`npm install` è andato a buon fine.