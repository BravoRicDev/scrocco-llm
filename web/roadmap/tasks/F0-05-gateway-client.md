---
id: F0-05-gateway-client
fase: F0
dipende_da: [F0-04-core-services]
puo_parallelo_con: F0-06-api-tokens-service
---

# F0-05 — `services/gateway.js`: unico wrapper verso l'admin API + mock COMPLETO

## Obiettivo

Creare l'UNICO punto d'uscita verso `GATEWAY_URL`: `src/services/gateway.js`
con metodi generici `get/post/put/patch/del/rawGet` (Bearer master-key,
timeout, normalizzazione errori, retry leggero solo su GET idempotenti) e la
**modalità mock completa** con stato in memoria che copre TUTTI gli endpoint
usati dalle fasi F0..F5. Nessuna route del progetto dovrà mai fare `fetch`
diretto.

Il mock deve coprire ogni endpoint letto/scritto dalle fasi F1..F5: senza
questo la barriera F2, le fasi F4/F5 e lo smoke e2e falliscono sui test.

## File da creare/modificare

- `scrocco-web/src/services/gateway.js` (crea)
- **`scrocco-web/test/fixtures/gateway.json` (crea)** — fixture COMPLETA con
  le shape reali degli endpoint (vedi Contratto qui sotto). È il contratto
  letto da tutti i task F1..F5 in modalità mock.

NON toccare altri file (in particolare NON aggiungere qui helper endpoint
specifici: ogni fase li costruisce passando i path; se proprio serve un helper
nuovo, va aggiunto SOLO dal rispettivo task di fase e segnalato in `puo_parallelo`).

## Contratto

API pubblica:
```js
gateway.request(method, path, { params, json, timeout } = {})
gateway.get(path, { params, timeout })
gateway.post(path, { json, timeout })
gateway.put(path, { json, timeout })
gateway.patch(path, { json, timeout })
gateway.del(path)                       // -> 204/obj
gateway.rawGet(path, opts)              // -> body come testo (per /admin/guide, markdown)
```
Comportamento:
- Header: `Authorization: Bearer <config.gatewayMasterKey>`,
  `Content-Type: application/json`, `X-Request-Id` (passato da req.requestId
  quando il caller lo fornisce).
- Timeout: `config.gatewayTimeoutMs` default, override per chiamata (probe,
  playground e logs useranno timeout più lunghi).
- Retry leggero: su errore di rete (ECONNREFUSED/ECONNRESET/timeout) ritenta
  UNA volta SOLO i metodi idempotenti (GET). Mai retry su POST/PUT/PATCH/DELETE.
- `rawGet(path, opts)` ritorna il body come testo (senza parse JSON): serve
  per `/admin/guide` (markdown/FileResponse). Usato da F1-10.
- Errori: classe `GatewayError extends Error` con `status` (http) e `message`
  (estrae `{error:{message}}` dal body JSON, altrimenti testo troncato a 300
  caratteri). Errori 4xx/5xx vengono lanciati; chi chiama decide.
- `GATEWAY_MOCK=1` (o `config.gatewayMock`): NON fa rete. Carica la fixture
  da `test/fixtures/gateway.json` e risponde coerentemente con **stato in
  memoria** (array interno `deployments`, `history`, cooldowns, sticky,
  backups, ecc.) così i test girano senza il gateway. Il mock implementa fino
  in fondo il ciclo di vita delle mutazioni (create/update/delete/bulk/probe/
  unretire/cooldown/sessions/reload/seed) come fatto dal gateway reale.
- `gateway.health()` → usa `GET /healthz` e ritorna booleano + payload.

### Shape reali di riferimento

Per le shape vere leggere `scrocco-llm/app/admin.py` (`_deployment_view`,
`_required_create`, `/admin/state`, `/admin/policy`, ecc.), `scrocco-llm/app/main.py`
(`/admin/reload`, `/healthz`, `/v1/models`), `scrocco-llm/app/journal.py`
(history: `{ts, op, ...}`), `scrocco-llm/app/bootstrap.py` (`/bootstrap*`).

### Endpoint mock REQUIRED — READ (GET)

| Path | Shape fixture |
|---|---|
| `/admin/deployments` (con `?profile=`) | `[ _deployment_view ... ]` (array) |
| `/admin/profiles` | `{count, profiles:[{name,base_model,dims_k,groups,deployments,step_up_pct,…}]}` |
| `/admin/state` | `{cooldowns_active, sticky_sessions, budget, capabilities:{...}, health, adaptive, policy:{...}}` |
| `/admin/policy` (GET) | `{file, configured, effective:{...}}` (alias/client keys MASCHERATE) |
| `/admin/history?limit=` | `{total, entries:[{ts, op, id, profile, ...}]}` (max 100 voci) |
| `/admin/deployments/expiring?days=` | `{days, expiring:[{id,modello,in_days,data_raw}]}` |
| `/admin/insights?days=&group_by=` | `{total, by_<gb>}` / `{total, aggregate}` |
| `/admin/insights/summary` | 24h compatto |
| `/admin/insights/leaderboard?window=` | `{window_days, count, rows:[{dep,profile,group,provider,model,calls,...,probe_ms}]}` |
| `/admin/logs/calls?tail&since&tags=` | `{events:[...]}` |
| `/admin/logs/errors?tail&since&filter=` | `{events:[...]}` |
| `/admin/guide` | testo markdown (raw, via `rawGet`) |
| `/healthz` | `{ok:true, ...}` |
| `/v1/models` | elenco modelli disponibili |
| `/bootstrap` | playbook (testo o shape) |
| `/bootstrap/status` | gap-analysis |
| `/bootstrap/providers` | provider registry |
| `/admin/capabilities/audit` (POST) | report `{accounts, missing_models[], cap_suggestions, errors}` |

### Endpoint mock REQUIRED — WRITE/MUTAZIONE (POST/PUT/DELETE, stato in-memory)

| Path | Comportamento mock |
|---|---|
| `POST /admin/deployments` | valida `_required_create` (profile, modello, endpoint, **data, key** obbligatori, context≥0) → 400 se mancanti; crea e appende a `deployments`; append a `history` |
| `PUT /admin/deployments/{hash}` | update per `row_hash` (id nel body e path); key vuota = NON ruoto |
| `DELETE /admin/deployments/{hash}` | rimuove il deployment |
| `POST /admin/deployments/bulk` | atomico (all-or-nothing): `{operations:[{action:create,update,delete,id?,...}]}`; se una op invalida → 400 e NESSUNA applicata |
| `POST /admin/deployments/probe` | `{unique\|id, force?}` → `{unique, ok, latency_ms, cached?, error_class, detail?}` (cached se già ok e key uguale) |
| `POST /admin/deployments/probe/bulk` | `{filter:all|cap:x|<profile>, force?}` → `{filter, count, results:[{unique,ok,...}]}` |
| `POST /admin/deployments/unretire` | `{unique}` → `{ok, unique, state}` |
| `POST /admin/profiles/purge` | `{profile}` → 200 se 0 righe lo usano, altrimenti 400/409 |
| `PATCH /admin/policy` | patch parziale yaml; aggiorna `effective` nel mock |
| `POST /admin/cooldowns/clear` | `{unique?}` → `{ok, cleared}` (svuota cooldown dal `state`) |
| `POST /admin/sessions/release` | `{session_id?}` → `{ok, released}` (svuota sticky_sessions dal `state`) |
| `POST /admin/reload` | → `{reloaded, profiles, deployments, policy:{...}}` |
| `POST /admin/capabilities/seed-from-map` | `{dry_run?}` → dry `{dry_run,count,total,proposals}` o `{ok,applied,skipped,errors}` |

### Endpoint mock REQUIRED — v1/GP (dopo i prerequisiti gateway; il mock li copre FIN DA SUBITO)

Per F4/F5 il mock risponde a questi (anche prima che il gateway reale sia
esteso, così i test girano in dry-run):

| Path | Shape fixture |
|---|---|
| `POST /admin/playground` | `{model, deployment, content, usage, trace:[{step,unique,group,profile,attempted,ok,scartato_a,reason,verdict}], attempts, fallbacks}` |
| `GET /admin/csv` | `{path, raw, parsed:{header, rows}, count, backups}` (chiavi MASCHERATE in `parsed`) |
| `PUT /admin/csv` | `{raw}` → `{ok, backup, rows}` o 400 (valida prima, backup prima di scrivere) |
| `GET /admin/policy/raw` | `{path, raw}` |
| `PUT /admin/policy/raw` | `{raw}` → `{ok, validated, reloaded, effective}` (sostituisce l'INTERO yaml, non merge) |
| `GET /admin/backups` | `{dir, csv:[...], yaml:[...]}` |
| `POST /admin/backups/restore` | `{filename}` → `{ok, restored, rows?/effective?}` |

Il mock mantiene internamente: array `deployments`, lista `history` (append a
ogni mutazione), `cooldowns`, `sticky_sessions`, lista `backups` (per csv/
policy-raw PUT), e risponde `content`/`trace` per il playground.

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
node --check src/services/gateway.js
GATEWAY_MOCK=1 node --import ./test/harness-load-mock.mjs ...   # se presente
```
E un piccolo test manuale: con `GATEWAY_MOCK=1`, un node one-liner esegue
`gateway.get("/admin/profiles")` e stampa `count` senza errori di rete; e uno
che esegue `gateway.post("/admin/deployments", {json: {...}})`, `gateway.put`,
`gateway.del` e poi `gateway.get("/admin/deployments")` verificando che lo
stato in-memory si aggiorni. Col gateway vivo su 127.0.0.1:4001 (se c'è):
`gateway.get("/admin/state")` con master key valida ritorna lo stato; con
chiave sbagliata lancia `GatewayError` con status 401.

## Rischi / note

- Il wrapper NON deve mai loggare la master key né le chiavi dei deployment.
- Il mock è il contratto per le fasi F1..F5: se manca un endpoint lì, i test
  delle fasi successive falliscono. Mantienilo COMPLETO e allineato alle shape
  reali di `admin.py`/`main.py`/`journal.py`.
- `rawGet` è l'unica eccezione al fetch JSON (per `/admin/guide`).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi `src/...`,
`test/fixtures/...`; se `read` fallisce usa `cat`/`sed -n`).

Crea il wrapper HTTP server-to-server verso l'admin API del gateway LLM
`scrocco-llm` (FastAPI su `GATEWAY_URL` default `http://scrocco-llm:4001`,
auth `Authorization: Bearer <GATEWAY_MASTER_KEY>`). È il SOLO punto d'uscita
di tutte le richieste: nessuna route potrà fare `fetch` diretto. Node 21+/22
(fetch globale), ESM.

PRIMA di scrivere il mock, leggi le shape reali: `scrocco-llm/app/admin.py`
(`_deployment_view`, `_required_create`, `/admin/state`, `/admin/policy`,
`/admin/capabilities/audit`, `_commit_csv`), `scrocco-llm/app/main.py`
(`/admin/reload`, `/healthz`, `/v1/models`), `scrocco-llm/app/journal.py`
(history: `{ts, op, ...}`, max 100), `scrocco-llm/app/bootstrap.py`
(`/bootstrap`, `/bootstrap/status`, `/bootstrap/providers` pubblici).

Crea ESATTAMENTE:
1. `src/services/gateway.js` (usa `src/config.js`, `src/services/logger.js`
   esistenti) con:
   - classe `GatewayError extends Error` con `status` e `message`.
   - metodi `request(method, path, {params, json, timeout})`,
     `get/post/put/patch/del/rawGet` come da convenzione.
   - header Bearer, `Content-Type: application/json`, `X-Request-Id` se passato
     come opzione `requestId`.
   - timeout da `config.gatewayTimeoutMs` con override; retry leggero (1
     tentativo) SOLO su GET per errori di rete.
   - normalizzazione errori: estrai `{error:{message}}` dal body, altrimenti
     testo; mai loggare la master key.
   - `gateway.health()` = `GET /healthz` → `{ok, payload}`.
   - `gateway.rawGet(path, opts)` = body come testo (per `/admin/guide`).
   - modalità mock se `config.gatewayMock`: nessuna rete; carica/scrive la
     fixture `test/fixtures/gateway.json` e implementa TUTTI gli endpoint della
     tabella sotto con stato in-memory.
2. `test/fixtures/gateway.json` — fixture con le shape REALI (dai file sopra)
   per TUTTI questi endpoint:
   - READ: `/admin/deployments` (array di `_deployment_view`), `/admin/profiles`,
     `/admin/state`, `/admin/policy` (GET, chiavi mascherate), `/admin/history`
     (`{ts,op,...}`, max 100), `/admin/deployments/expiring`, `/admin/insights`,
     `/admin/insights/summary`, `/admin/insights/leaderboard`,
     `/admin/logs/calls`, `/admin/logs/errors`, `/admin/guide` (testo markdown),
     `/healthz`, `/v1/models`, `/bootstrap`, `/bootstrap/status`,
     `/bootstrap/providers`.
   - WRITE (mock con stato in-memory: valida e muta l'array `deployments` e
     append a `history`): `POST /admin/deployments` (richiede profile, modello,
     endpoint, **data, key**, context≥0 → 400 se mancano), `PUT/DELETE
     /admin/deployments/{hash}`, `POST /admin/deployments/bulk` (ATOMICO:
     se una op invalida → 400 e niente applicato), `POST
     /admin/deployments/probe` (+`/bulk`), `POST /admin/deployments/unretire`,
     `POST /admin/profiles/purge`, `PATCH /admin/policy`, `POST
     /admin/cooldowns/clear`, `POST /admin/sessions/release`, `POST
     /admin/reload`, `POST /admin/capabilities/seed-from-map`.
   - V1/gateway-prereq (mock F4/F5, fin da subito): `POST /admin/playground`
     (shape `{model, deployment, content, usage, trace, attempts, fallbacks}`),
     `GET/PUT /admin/csv` (`{path, raw, parsed:{header,rows}, count, backups}`),
     `GET/PUT /admin/policy/raw`, `GET /admin/backups` + `POST
     /admin/backups/restore`.

Verifica: `node --check src/services/gateway.js` e, con `GATEWAY_MOCK=1`, un
mini script che chiama `gateway.get("/admin/deployments")` e poi fa una
create/update/delete verificando che lo stato in-memory rifletta le mutazioni.
Non creare altri file né toccare `src/index.js`. Non toccare altri file. Alla
fine descrivi la firma in 3 righe e l'elenco endpoint coperti dal mock.
