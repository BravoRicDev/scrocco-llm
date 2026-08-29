# REVIEW della roadmap `scrocco-web` — 2026-08-28

> Revisione di 52 task web + 4 task gateway (GP-01..04) incrociata con
> `scrocco-llm/app/admin.py`, `app/main.py`, `app/router.py`,
> `app/forwarder.py`, `app/csv_store.py`, `app/journal.py`, `app/config.py`.
> NESSUN file della roadmap è stato modificato: è stato SOLO scritto questo
> report.

---

## Sommario — 🟠 GIALLO/ROSSO (rosso-chiaro)

La struttura (fasi → barriere di integrazione → wave parallele) è solida e il
grafo non ha cicli né id inesistenti gravi (solo un `GP-00` fantasma). MA ci
sono **3 problemi bloccanti** se si esegue così com'è:

1. **Mock gateway incompleto** (F0-05): mancano fixtures/endpoint mock per
   probe, bulk, policy PATCH, reload, cooldowns, sessions, unretire,
   seed-from-map, csv, playground, policy-raw, backups, healthz, guide,
   `/v1/models`, `/bootstrap/providers` → **F2-09 (barriera F2) e tutta la
   fase F4/F5 falliscono sui test**.
2. **Contratto create deployment errato**: `data` e `key` dichiarati opzionali
   da F2-01/F2-02, ma `_required_create` (admin.py) li pretende obbligatori →
   ogni create va in 400.
3. **Same-wave dependency**: F0-09↔F0-05 (wave 0B), F0-10↔F0-07 (wave 0C),
   F5-06↔F5-01 (wave 5A) e stesso-file `permissions.js` toccato da F2-02+F2-07
   nella stessa wave 2A.

Tutto il resto (contratto GP, completezza funzionale, ownership di index.js
e sidebar) regge; le correzioni sono modifiche di documento, non di codice.

---

## 1. GRAFO DIPENDENZE

Nessun ciclo rilevato; tutti gli id citati in `dipende_da:` esistono tranne uno
(il fantasma `GP-00`). Problemi di **stessa-wave che in realtà dipende da un
fratello** (il "un task in puo_parallelo_con che in realtà tocca lo STESSO
file" invece è in sezione 2):

- [ ] `GP-01` `GATEWAY-PREREQS.md` — `puo_parallelo_con: GP-00 (…)` → `GP-00`
      non esiste (id fantasma). — Fix: sostituire con "nessun altro GP in
      parallelo".
- [ ] `F0-09` `tasks/F0-09-health-openapi.md` · `PARALLEL.md` wave **0B** —
      `dipende_da: [F0-05]` ma è nella stessa wave 0B di F0-05 (3 in parallelo:
      F0-05, F0-06, F0-09). `health.js` importa `services/gateway.js`: se
      parte prima che F0-05 finisca, `node --check` passa ma l'import è rotto.
      — Fix: wave 0B = (F0-05, F0-06) poi F0-09 (o spostarlo in 0C).
- [ ] `F0-10` `tasks/F0-10-api-tokens-route.md` · wave **0C** —
      `dipende_da: [F0-06, F0-07]` ma F0-07 è nella STESSA wave 0C (appende a
      `routes/auth.js` creato da F0-07). PARALLEL.md lo ammette in nota ma la
      tabella 0C(2) non lo riflette. — Fix: wave 0C = F0-07 da solo, poi
      F0-10.
- [ ] `F5-06` `tasks/F5-06-agent-docs.md` · wave **5A** —
      `dipende_da: [F5-01]` ma è in parallelo con F5-01 (legge i file route che
      F5-01 deve ancora creare). Il prompt ha un fallback ("se esistono già")
      ma la dipendenza dichiarata è violata. — Fix: spostare F5-06 in una
      sotto-wave dopo F5-01..03 oppure toglierla da `dipende_da`.
- [ ] `00-overview.md` §6 — il grafo mostra F3-01..04 **dopo F2-09**, ma i
      task F3-xx dichiarano `dipende_da: [F1-11-integration]` e PARALLEL.md
      mette 3A subito dopo F1-11. Il grafo di overview e le dipendenze dei
      task si contraddicono. — Fix: allineare il grafo ai task (F3 dopo F1-11,
      in parallelo con F2).
- [ ] `00-overview.md` §6 — F5-05 è posto dopo "F5-01/02/03/06"; ok, ma
      F5-04 e F5-05 sono nella stessa wave 5B e F5-04 legge i file route mentre
      F5-05 li legge anch'essi (solo lettura): nessun conflitto, solo churn.
      — Fix: nessuno (informazione).

## 2. PROPRIETÀ FILE CONDIVISI

Porzioni sane: `src/index.js` è toccato SOLO dai task di integrazione
(F0-11, F1-11, F2-09, F3-05, F4-09, F5-07) ✓; `db/migrate.js` solo F0-03 ✓;
estensioni sequenziali (F2-xx estende route/viste F1-xx) corrette ✓.
Violazioni:

- [ ] `F2-02` + `F2-07` `src/constants/permissions.js` — entrambi Append
      (`bulk:` e `probe:`) sulla STESSA matrice **nella stessa wave 2A** →
      lost-update (l'ultimo che scrive perde l'append dell'altro). — Fix:
      centralizzare i due append in un solo task (es. F2-09) o in wave
      sequenziali; sprite in questo file fuori da F2-02/F2-07.
- [ ] `F4-08` `views/partials/sidebar.ejs` — task NON-integration che modifica
      la sidebar, violando la "regola d'oro" di PARALLEL.md e 00-overview §7.4.
      Non confligge con F4-09 (sequenziali) ma rompe la regola dichiarata. —
      Fix: aggiornare la regola (la sidebar si tocca anche in F4-08) oppure far
      includere il componente ricerca via `partials/search.ejs` dalla sidebar
      senza modificare `sidebar.ejs`.
- [ ] `F1-10` `src/services/gateway.js` — modifica risorsa di F0-05 (aggiunta
      `rawGet`) NON elencata nella sezione "File da creare/modificare" (vi
      compaiono solo route+vista). — Fix: dichiarare gateway.js tra i file.
- [ ] `F4-06` `src/config.js` + `.env.example` — aggiunge `telegramBotToken`
      ma non è elencato nei file del task (compare solo nel prompt); F4-09
      dichiara la modifica a `.env.example`. Allineare le sezioni file tra
      F4-06 e F4-09.
- [ ] `F4-09` `src/config.js` — aggiunge `alertPollerDisabled` ma non elenca
      `src/config.js` nei file. — Fix: aggiungerlo alla lista.
- [ ] `F0-05` `test/fixtures/gateway.json` — creato "qui" (contratto) ma non
      elencato nei file da creare: è lettura da tutti i task F1..F5. — Fix:
      aggiungerlo (tracciabilità).

## 3. CONTRATTO GATEWAY (verificato su admin.py / main.py / journal.py)

Endpoint citati tutti esistenti (sono quelli reali in admin.py:164-1584, +
`/admin/reload` in main.py:354, `/healthz` main.py:308, `/v1/models`
main.py:370, `/bootstrap*` in bootstrap.py — questi ultimi pubblici senza
auth, coerente con l'overview). Discrepanze concrete:

- [ ] `F2-01` `src/routes/deployments.js` — body zod: `data?` e `key`
      opzionali, e nota "key vuota = ok". **MA** `_required_create`
      (admin.py:150-160) pretende `profile, modello, endpoint, data, key`
      tutti non-vuoti + `context>=0`. Ogni create senza `data`/`key` → 400. —
      Fix: rendere `data` e `key` obbligatori nello zod (richiederli nel form;
      la regola "key vuota ok" vale SOLO per PUT, non per POST).
- [ ] `F2-02` `src/routes/deployments-bulk.js` — op `create` richiede
      profile/modello/endpoint/key/context ma NON `data` (obbligatorio per il
      gateway). — Fix: aggiungere `data` ai campi richiesti del create bulk.
- [ ] `F1-07` `views/history/index.ejs` · `F1-11` — il journal del gateway
      (journal.py `record`) scrive `{"ts", "op", …}`: i task leggono
      `entry.action` → badge/dettagli vuoti. — Fix: usare `entry.op` (il resto
      del detail è `{id, profile, …}`).
- [ ] `F1-07` `src/routes/history.js` — dichiara `limit` max 200 ma
      `journal.history` tronca a 100 voci (`[:100]`). — Fix: dichiarare max
      100 o estendere il limite lato gateway.
- [ ] `F4-01` `src/routes/csv-editor.js` — dichiara shape dati
      `{path, headers, rows, raw, backups}`; il contratto GP-02 è
      `{path, raw, parsed:{header,rows}, count, backups}` (header/rows
      annidati in `parsed`). — Fix: unwrap di `parsed` nella route o allineare
      il contratto.
- [ ] `F4-02` `src/routes/playground.js` — atteso `{deployment, choices,
      usage, trace}`; GP-01 restituisce `{model, deployment, content, usage,
      trace, attempts, fallbacks}` (**`content`, non `choices`**). La frase
      "o shape simile" evita il bug ma è rischiosa. — Fix: usare `content`.
- [ ] `F2-06` `src/routes/system-actions.js` / `F4-07` `src/routes/sticky.js` —
      azioni authorize incoerenti: `system/reload`, `system`, `system/release`,
      `observability/release`…; la matrice F0-07 definisce `system` con
      (reload/cooldowns/sessions, admin+operator). — Fix: fissare i nomi azione
      (es. `system/release`) nella matrice F0-07 e nell'uso.
- [ ] `F4-02` `src/routes/playground.js` — `authorize("playground","use")`: la
      matrice F0-07 elenca `playground (admin,operator)` senza azioni. — Fix:
      definire l'azione `use` (o `run`) in F0-07.
- [ ] `F1-03` `views/policy/index.ejs` — `_mask_configured` (admin.py:588)
      maschera SOLO `alias_keys` nel blocco `configured`; se il yaml conterrà
      `client_keys` in chiaro verrebbero echeggiati. La vista usa `*_masked`
      quindi l'UI è al sicuro, ma `configured` grezzo finisce anche in F5-02
      API. — Fix: (option) estendere `_mask_configured` ai `client_keys`
      (riga gateway) o documentare il vincolo.

RBAC: assegnazioni coerenti con le regole overview (§3): policy PATCH = admin
solo; csv/yaml = admin scrivono; probe/audit seed/operator; unretire =
admin/operator; report `capabilities/audit` POST = tutti (scelta "read-only
logica" esplicita) ✓.

## 4. COMPLETEZZA vs SPEC

Coperta TUTTA la parità TUI: deployments CRUD+bulk, profili, policy completa
(view/editor/keys), capacità (view/seed/audit), scadenze, cooldown/sessioni/
reload/unretire, probe, osservabilità (live/errori/leaderboard/chart), csv
editor, playground+trace, gateway.yaml editor, config history/rollback,
key-health timeline, alert rules, sticky, temi/ricerca/shortcut. Coperti gli
agenti: API `/api/v1` READ+WRITE, api_tokens, agent-login, MCP, openapi,
AGENT.md bilingue. Mancano:

- [ ] **Gestione utenti admin** — nessun task crea `/users` (list/create/
      disable/role change), pur essendo in sidebar (F0-08: "Utenti (admin)"),
      nella matrice permessi F0-07 (`users: admin`) e come esclusiva admin in
      00-overview §3. — Fix: aggiungere task (o sotto-task di F0-10) per user
      CRUD admin-only.
- [ ] **UI audit_log proprio** — F1-07 rimanda il nostro `audit_log` a
      F5-INTEGRATION ma F5-07 non lo monta. — Fix: piccola rotta read-only
      (es. `GET /admin/audit`) in F5-07 o sotto-task.
- [ ] `F0-05` **mock incompleto** — il contratto fetches copre deployments/
      profiles/state/policy/expiring/history/insights/summary/leaderboard/
      logs/bootstrap/status/audit, ma NON `probe`, `probe/bulk`, `unretire`,
      `cooldowns/clear`, `sessions/release`, `reload`, `capabilities/
      seed-from-map`, PATCH policy, `csv`, `playground`, `policy/raw`,
      `backups`, `healthz`, `guide` (text), `/v1/models`,
      `/bootstrap/providers`. I task F2-01/02/06/07/08, F2-09 (smoke
      create→read→update→delete→bulk→probe→reload) e F4/F5 presuppongono il
      mock su TUTTI questi, ma nessun task li implementa (e "Non toccare altri
      file" lo vieta). — Fix **bloccante**: estendere F0-05 (o F0-05-bis) col
      contratto completo del mock.

## 5. TASK MAL DIMENSIONATI

- [ ] `F0-10` `tasks/F0-10-api-tokens-route.md` — **troppo vago/non
      autoconsistente**: il "Contratto" è un flusso di coscienza con 5
      soluzioni scartate inline (`…NO: …`, `PROBLEMA`, `RISOLUZIONE`). Un
      subagente può bloccarsi. — Fix: riscrivere lasciando SOLO la decisione
      finale (append `POST /api/agent/login` in auth.js + route api-tokens +
      agent-login.mjs).
- [ ] `F5-04` `tasks/F5-04-mcp-server.md` — **rischio tecnico massimo**:
      l'invocazione in-process delle route via `matchRouter`+`createReqStub`+
      proxy di `res` (30-60 righe) è fragile e sottospecificata (middleware,
      async, error handler, route guide con FileResponse/rawGet, BodyParser
      nelle route). Il prompt stesso ammette 4 alternative abbandonate. — Fix:
      preferire fetch loopback con cookie (come CMS, skip rate-limit) oppure
      estrarre un service-layer condiviso dalle route API.
- [ ] `F5-05` `tasks/F5-05-openapi-full.md` — grande (tutte le route + schemi +
      docs página); crea `views/api-docs.ejs` non elencato nei file. — Fix:
      dichiarare la vista nei file; ok dimensionato.
- [ ] `F0-07` — 8 file (matrice + 3 middleware + 2 service + routes/auth):
      borderline ma accettabile (copia CMS). — Fix: nessuno.
- [ ] `F2-02` / `F4-01` — JS client articolato (builder DOM + anteprima /
      contenteditable): ok, ma alzare a 60-90 min.

## 6. GATEWAY PREREQUISITI (GP-01..04)

Realistici: tutti gli helper citati esistono e hanno firme compatibili —
`config.reload()` (config.py:474), `csv_store.{load_table,save_table,
mask_key,endpoint_of,apply_payload,row_id,find_row,ensure_*_column,expiring}`,
`journal.{backup_csv,_paths,record,history}`, `Policy.load` (policy.py:711),
globali `gw.{config,router,forwarder,policy,authn,VAR_DIR,CSV_PATH,
POLICY_PATH,LEDGER,KEYHEALTH}` (main.py:67-152). `forwarder.call(dep,payload)`
ritorna `resp.json()` (dict OpenAI) → il trace può estrarre `content`.

- [ ] `GP-01` — il loop con `resolve_group_for_request`/`initial_pick`/
      `fallback_next` esiste (router.py:760/1086/1172) e i metodi "no
      side-effect" (niente `mark_failed/note_start/note_end/_strike_hook`)
      sono realistici: il fallback loop di `_stream_with_fallback`
      (main.py:1113) NON va rifattorizzato, reimplementare in admin.py è
      corretto. Nota: `fallback_next` ora ha `scope`/`ctx` — firme compatibili
      (scope default "chain"). OK.
- [ ] `GP-02` — contraddizione interna: "journal.backup_csv PRIMA" + "riusa
      `_commit_csv`" (che GIÀ chiama backup_csv internamente, admin.py:125) →
      doppio backup. — Fix: togliere "backup PRIMA", affidarsi a `_commit_csv`.
- [ ] `GP-03` — il PUT sostituisce l'INTERO yaml (non merge): va scritto
      esplicitamente per non cozzare concettualmente con PATCH `/policy` di
      F2-04/F2-05 (che poi lavora sul file nuovo: ok, sequenziali a runtime
      gateway). - Fix: solo nota esplicita nel contratto.
- [ ] `GP-01..04` **sequenziali tra loro: corretto** — tutti toccano
      `app/admin.py` in regioni diverse; i test sono file distinti
      (`tests/test_admin_{playground,csv,policy_raw,backups}.py`); la wave 4B
      li attende tutti e 4. ✓ Wave 4A e F0..F3 in parallelo ai GP ✓.

## 7. RISCHI DI ESECUZIONE PARALLELA

- [ ] **Wave 2A**: F2-02 e F2-07 scrivono entrambi `permissions.js` (sez. 2).
      Con 8 subagenti in parallelo, import mancanti/doppioni sono la norma:
      i task dichiarano "non toccare altri file", ma il permissions.js li
      obbliga a toccarlo TUTTI E DUE.
- [ ] **Wave 0A**: F0-02 ha nel done `docker build` che fa COPY di `src/`,
      `views/`, `public/` — creati da F0-04/F0-08 nella STESSA wave: il build è
      non-deterministico (il task lo nota "accettabile", ma rende la wave
      flaky). — Fix: posticipare `docker build` di F0-02 a F0-11.
- [ ] **Wave 0B**: F0-09 importa `services/gateway.js` (F0-05) nella stessa
      wave → `node --check` non lo rileva, lo scopre F0-11 con churn.
- [ ] **Wave 0C**: F0-10 appende a `routes/auth.js` (F0-07) nella stessa wave.
- [ ] **Wave 5A**: F5-06 legge i file route di F5-01 (stessa wave).
- [ ] `test/helpers.js` e `startTestApp()` condivisi da tutte le suite: ok se
      le wave sono separare (un agente per wave lancia `npm test`); con
      `--test-concurrency=1` e DB `scrocco_web_test` dedicato non c'è race di
      migrazione se NON si lancia `npm test` da due subagenti contemporanei.
      — Fix: regola esplicita "solo l'agente integrazione lancia npm test".
- [ ] Mock in-memory di F0-05 (stato create/update/delete) è per-processo: i
      test F2 lo riusano in-process (ok). Col mock incompleto (sez. 4) i test
      F2-09 falliranno "ma" renderanno subito evidente il gap.

---

## AZIONI PRIMA DI ESEGUIRE (in ordine — correzioni bloccanti)

1. **Estendere F0-05** con il contratto mock COMPLETO (endpoint read + tutte
   le mutazioni write + guide text + `/v1/models` + `healthz`) e relativa
   fixture in `test/fixtures/gateway.json`. Senza, la barriera F2-09 e le fasi
   F4/F5 falliscono sui test.
2. **Correggere F2-01 e F2-02**: `data` e `key` diventano obbligatori nel
   create (allineamento a `_required_create` di admin.py). Inserire
   `permissions.js` nel perimetro di UN solo task (o centralizzare in F2-09).
3. **Sequenziare le wave**: 0B senza F0-09; 0C = F0-07 poi F0-10; 5A senza
   F5-06 (sotto-wave) — e allineare il grafo di 00-overview §6 (F3 dopo F1-11,
   parallelo a F2).
4. **Allineare il contratto**: history usa `op` (non `action`) e max 100;
   F4-01 unwrap di `parsed`; F4-02 usa `content` (non `choices`); definire le
   azioni `playground/use` e `system/release` nella matrice F0-07.
5. **Riscrivere F0-10** (prompt autoconsistente) e semplificare F5-04 (fetch
   loopback col cookie come CMS, NON reimplementare il routing in-process).
6. **Chiudere i gap di completezza**: user CRUD admin-only (+ allineare la
   sidebar) e piccolo pannello `audit_log`; chiarire GP-02 (niente doppio
   backup) e la sezione file di F4-06/F4-09 (`src/config.js`,
   `.env.example`).

Dopo queste 6 correzioni (tutte di documento) la roadmap è eseguibile con le
wave parallele dichiarate.

---

## APPLICATO IL 2026-08-28

Checklist delle 6 azioni + follow-up dei `[ ]` puntuali delle sezioni 1–7. Solo
file Markdown della roadmap; nessun codice applicativo.

- [x] **1. F0-05 mock COMPLETO** — riscritto `tasks/F0-05-gateway-client.md`:
      mock/fixture che copre TUTTI gli endpoint read + write/mutazione
      (in-memory) + v1/GP (playground, csv, policy/raw, backups), shape reali
      da `admin.py`/`main.py`/`journal.py`; `test/fixtures/gateway.json`
      esplicitato tra i file; promemoria shape (`_deployment_view`,
      `_required_create`, `/admin/state`, `/admin/policy`).
- [x] **2. Contratto create** — `F2-01` e `F2-02`: `data` e `key` OBBLIGATORI
      nello zod del POST (allineato a `_required_create`); "key vuota ok" resta
      SOLO per PUT/update; form/vista e prompt aggiornati.
- [x] **3. Wave / dipendenze** — `PARALLEL.md`: 0B = (F0-05, F0-06); F0-09 in
      0C; 0C = F0-07+F0-09 poi 0C-bis F0-10; 5A senza F5-06 (5A-bis);
      `permissions.js` fuori da F2-02/F2-07 (centralizzato in F0-07, append
      solo in F2-09); grafo `00-overview.md` §6 allineato (F3 dopo F1-11,
      parallelo a F2); `GATEWAY-PREREQS.md`: rimosso `GP-00` da GP-01.
- [x] **4. Allineamento contratto** — F1-07: `entry.op` e `limit` max 100;
      F4-01: unwrap di `parsed` (`{path, raw, parsed:{header,rows}, count,
      backups}`); F4-02: `content` (non `choices`) e shape GP-01;
      F0-07: azioni RBAC aggiunte (`playground/use`, `system/release`/
      `cooldowns`/`sessions`/`reload`, `users/*`, `alerts/*`,
      `config/restore`/`config_snapshots:restore`, `audit/read`); authorize
      coerenti in F2-06 (system/release, deployments/unretire) e F4-07
      (system/release); F4-06/F4-09: `src/config.js` e `.env.example` nei
      file; F1-10: `src/services/gateway.js` nei file (rawGet).
- [x] **5. Riscritture** — `F0-10` riscritto autoconsistente (append
      `POST /api/agent/login` in auth.js, route api-tokens list/create/revoke,
      `scripts/agent-login.mjs`), nessun flusso di coscienza; `F5-04`
      riscritto: tool call via fetch loopback su 127.0.0.1 con header
      Authorization/cookie del chiamante (pattern CMS `mcp-tools.js`), nessuna
      reimplementazione del routing express in-process, alternative abbandonate
      rimosse.
- [x] **6. Gap completezza** — creato `tasks/F2-10-users-admin.md` (CRUD utenti
      admin-only, route `src/routes/users.js`, viste `views/users/*`,
      `authorize("users", ...)`); sidebar allineata in F0-08 e mount in F2-09;
      pannello read-only `audit_log` aggiunto in `F5-07` (`GET /admin/audit`,
      admin-only, `views/admin/audit/index.ejs`); GP-02: rimosso "backup
      PRIMA" (`_commit_csv` già fa backup, niente doppio backup); GP-03: nota
      "il PUT sostituisce l'INTERO gateway.yaml, non fa merge"; PARALLEL.md:
      regola "solo l'agente -integration lancia `npm test`, gli altri
      `node --check`"; F0-02: `docker build` spostato nel done di F0-11.
- [x] Conteggi: `00-overview.md` §5 (53 task web + 4 GP) e `PARALLEL.md`
      (57 totale) aggiornati.

File modificati: `REVIEW.md`, `00-overview.md`, `PARALLEL.md`,
`GATEWAY-PREREQS.md`, `tasks/F0-05-gateway-client.md`, `tasks/F0-02-*`,
`tasks/F0-06-*`, `tasks/F0-07-*`, `tasks/F0-08-*`, `tasks/F0-09-*`,
`tasks/F0-10-*`, `tasks/F0-11-*`, `tasks/F1-03-*`, `tasks/F1-07-*`,
`tasks/F1-10-*`, `tasks/F2-01-*`, `tasks/F2-02-*`, `tasks/F2-06-*`,
`tasks/F2-07-*`, `tasks/F2-09-*`, **`tasks/F2-10-users-admin.md` (nuovo)**,
`tasks/F4-01-*`, `tasks/F4-02-*`, `tasks/F4-06-*`, `tasks/F4-08-*`,
`tasks/F4-09-*`, `tasks/F5-02-*`, `tasks/F5-04-*`, `tasks/F5-05-*`,
`tasks/F5-06-*`, `tasks/F5-07-*`.