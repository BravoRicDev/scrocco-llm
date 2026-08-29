---
id: F0-02-infra-docker-compose
fase: F0
dipende_da: []
puo_parallelo_con: F0-01-repo-scaffold, F0-03-db-migrations, F0-04-core-services, F0-08-layout-theme
---

# F0-02 — Infra: Dockerfile, docker-compose, start.sh, Caddy vhost

## Obiettivo

Confezionare `scrocco-web` per Docker (deploy su VPN/locale, niente internet
in runtime) copiando la struttura del CMS `gestione-siti-riccardom`:
Dockerfile multi-stage node:22-alpine, compose con app+postgres 16, script di
avvio che esegue le migrazioni, e snippet Caddy per il nuovo vhost su :4002.

## File da creare/modificare

- `scrocco-web/Dockerfile` (crea)
- `scrocco-web/docker-compose.yml` (crea)
- `scrocco-web/scripts/start.sh` (crea; crea anche la dir `scripts/` se serve)
- `scrocco-web/.dockerignore` (crea)
- `scrocco-web/caddy/scrocco-web.Caddyfile` (crea — snippet da aggiungere al
  Caddy esistente, NON va modificato `gestione-siti-riccardom`)

NON toccare: `src/`, `db/`, `views/`, `roadmap/`, `package.json`.

## Contratto

- Utente non-root nel container (come il CMS); `EXPOSE 3000`;
  `CMD ["sh","scripts/start.sh"]`.
- `scripts/start.sh`: `set -e`; `node db/migrate.js`; poi `exec node src/index.js`.
- `docker-compose.yml` (calco di quello del CMS):
  - servizio `scrocco-web`: build `.`, `restart: unless-stopped`, `env_file: .env`,
    `port 127.0.0.1:3000:3000` SOLO per debug locale (il pubblico passa da Caddy),
    `depends_on: db (service_healthy)`, reti `edge_net` (external, aliases
    `scrocco-web`) + `internal` + rete del gateway `scrocco-llm_default`
    (external, così risolve `scrocco-llm:4001`); healthcheck
    `wget ... http://localhost:3000/health`.
  - servizio `db`: `postgres:16-alpine`, volume `pgdata`, env
    `POSTGRES_DB/USER/PASSWORD` da `.env` (`${POSTGRES_DB:-scrocco_web}` ecc.),
    healthcheck `pg_isready`.
  - `volumes: pgdata`, `networks` come da CMS (edge_net external, internal
    bridge, scrocco_web external, scrocco-llm_default external).
- `scrocco-web/caddy/scrocco-web.Caddyfile`: vhost esterno `:4002` con TLS
  interno (self-signed, `tls internal`), header di sicurezza, `reverse_proxy
  scrocco-web:3000`. Con commenti su come aggiungerlo al Caddy esistente
  (`gestione-siti-riccardom/caddy-gestito/`) senza modificarlo qui.

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
docker compose config -q          # compose valido
sh -n scripts/start.sh            # sintassi OK
```
Nota: il container non parte ancora (manca `src/index.js`: atteso, è task
F0-11). **Il `docker build` NON va eseguito qui**: `src/`, `views/`, `public/`
sono creati da F0-04/F0-08 nella STESSA wave (il build sarebbe
non-deterministico). Il build effettivo (`docker build -t scrocco-web:test .`)
è spostato nel done di F0-11.

## Rischi / note

- La rete `scrocco-llm_default` potrebbe non esistere o avere nome diverso se
  il gateway è stato lanciato con un progetto compose custom: documenta
  entrambi i default (`http://scrocco-llm:4001` oppure `http://127.0.0.1:4001`)
  nel README del vhost.
- `GATEWAY_URL` resta in `.env`; il container NON deve contenere la master key
  (solo env_file).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora nella directory `~/Serverino/scrocco-web` (MAI path assoluti: usa solo
path relativi `scrocco-web/...`; se `read` fallisce usa `cat`/`sed -n`).

Creare l'infrastruttura Docker per l'app Express `scrocco-web` replicando
fedelmente le convenzioni del CMS gemello `gestione-siti-riccardom` (leggi
`gestione-siti-riccardom/Dockerfile`, `gestione-siti-riccardom/docker-compose.yml`,
`gestione-siti-riccardom/scripts/start.sh` come riferimento di stile). Stile:
ESM, commenti in italiano, image `node:22-alpine`, POSTGRES `16-alpine`.

Crea ESATTAMENTE:
1. `Dockerfile` — multi-stage (builder con `npm ci --omit=dev`, poi runtime
   Alpine con `apk add wget` per l'healthcheck), utente non-root, `EXPOSE 3000`,
   copia `db/`, `src/`, `views/`, `public/`, `scripts/`, `package.json`,
   `chmod +x scripts/start.sh`, `CMD ["sh","scripts/start.sh"]`.
2. `docker-compose.yml` — servizio `scrocco-web` (build ., restart
   unless-stopped, env_file `.env`, ports `127.0.0.1:3000:3000`, depends_on
   `db` condition service_healthy, networks edge_net (external, alias
   scrocco-web) + `internal` + `scrocco_web` (external, alias scrocco-web) +
   `scrocco-llm_default` (external) così GATEWAY_URL=http://scrocco-llm:4001
   risolve, healthcheck `wget --no-verbose --tries=1 --spider
   http://localhost:3000/health`); servizio `db` (postgres:16-alpine, volume
   `pgdata`, env da `.env` con default POSTGRES_DB=scrocco_web /
   POSTGRES_USER=scwebuser, healthcheck `pg_isready`); `volumes: pgdata`;
   networks edge_net (external:true), internal (bridge), scrocco_web
   (external:true), scrocco-llm_default (external:true).
3. `scripts/start.sh` — `#!/bin/sh`, `set -e`, `node db/migrate.js`, poi
   `exec node src/index.js`.
4. `.dockerignore` — node_modules, .git, .env, *.log, var, backups, test/tmp,
   roadmap.
5. `caddy/scrocco-web.Caddyfile` — vhost `:4002` con `tls internal`,
   `reverse_proxy scrocco-web:3000`, header di sicurezza; come commento,
   istruzioni (4-6 righe) su come importare lo snippet nel Caddy già esistente
   di `gestione-siti-riccardom` senza modificarlo.

Verifica: `sh -n scripts/start.sh`, `docker compose config -q`. **NON
lanciare `docker build`** (src/views/public non sono ancora completi in questa
wave): il build è di competenza del done di F0-11. Non toccare altri file, non
toccare `gestione-siti-riccardom`.