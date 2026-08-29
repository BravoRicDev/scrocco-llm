# GATEWAY PREREQUISITI — task lato `scrocco-llm` (Python/FastAPI)

> Questi 4 task (GP-01..GP-04) sono i SOLI interventi consentiti sul gateway.
> Repo: `~/Serverino/scrocco-llm` (path relativi `scrocco-llm/...`). Sono
> **sequenziali tra loro** (tutti toccano `scrocco-web/../scrocco-llm/app/admin.py`
> in regioni diverse dello stesso file): NON lanciarli in parallelo tra loro.
> Sono invece **paralleli** rispetto alle fasi web F0..F3 e alla wave 4A di F4
> (che non li richiedono); la wave 4B di F4 (csv-editor, playground,
> policy-raw, config-history) li richiede.
>
> Modello subagente: `scrocco-llm/scrocco-llm-mioaruba-200k`, cwd
> `~/Serverino/scrocco-llm`, regole path relative (`scrocco-llm/app/...`,
> `scrocco-llm/app/admin.py`, `scrocco-llm/tests/...`); se `read` fallisce usa
> `cat` / `sed -n '1,200p' <file>`.

---

## GP-01 · `POST /admin/playground` (chat di prova + trace)

`id: GP-01-playground`
`fase: GP`
`dipende_da: []`
`puo_parallelo_con: nessun altro GP in parallelo`

### Obiettivo
Esporre un endpoint che rigira UNA chat come una vera richiesta di routing
(non-streaming) raccogliendo il **trace di routing/fallback**: ogni deployment
provato, motivo di scarto, verdetto finale. Serve a `scrocco-web` per il
Playground (F4-02). **Non deve alterare lo stato reale del router** (niente
percussioni su cooldown/health/EMA).

### File da creare/modificare
- `scrocco-llm/app/admin.py` — nuovo endpoint + helper
- `scrocco-llm/tests/test_admin_playground.py` (crea)
(NON toccare altri file; in particolare store il trace in memoria del handler,
usando i metodi PUBBLICI esistenti di `router` e `forwarder` — vedi contratto.)

### Contratto
```
POST /admin/playground
Auth: Bearer GATEWAY_MASTER_KEY (stessa _require_master di admin.py)
Body: {
  "model": "string" (alias o modello o gruppo SICURO: verrà risolto come una
          richiesta reale; NON consentire payload con "stream": true),
  "messages": [{"role":"user","content":"..."}],   // come /v1/chat/completions
  "profile": "string|optional",
  "max_tokens": int|optional (default 256)
}
OK 200 → {
  "model": "...",                 // modello richiesto
  "deployment": "unique_finale",  // quello che ha risposto (o null)
  "content": "testo della risposta",
  "usage": {...} | null,
  "trace": [                       // ORDINATO cronologico
    {"step": 1, "unique": "...", "group": "...", "profile": "...",
     "attempted": true, "ok": false, "scartato_a": "deployment_unico_120kk|None",
     "reason": "http_429|stream_empty|network|model_missing|ok",
     "verdict": "tentativo|riuscito|esaurito"},
    ...
  ],
  "attempts": 2, "fallbacks": 1
}
Errore 4xx/5xx → { "error": {"message": "..."} } (stessa shape)
```
Regole d'implementazione:
- Riusa `_gw()` per accedere a `config`, `router`, `forwarder`, `authn`,
  `policy`. Il resolver è UGUALE a una richiesta reale: `canonicalize(model)`,
  `resolve_group_for_request(model, [], sid, need)`, `initial_pick`,
  `fallback_next(profile, dep, need, scope)`.
- L'**unica** chiamata upstream va fatta con `forwarder.call(dep, payload)`
  (non-streaming) come fate per `/v1/chat/completions` non-streaming.
- **No side-effects**: NON chiamare `router.mark_failed / note_start /
  note_result / note_end / _strike_hook`; NON toccare `_cooldown`, `_stats`,
  EMA, `keyhealth`, metrics (o al più usa metriche conteggio `nx_playground_*`
  nuove). Se un tentativo fallisce, il trace riporta la ragione e si passa al
  fallback successivo come farebbe la produzione, ma SENZA punire.
- `need` da `policy.caps_for(model)` come nel path reale (mantieni
  capability_routing attivo).
- Timeout upstream: 20s (colpa del provider da mettere nel reason).
- Rate: niente rate-limit speciale (chiama comunque _require_master).
- Audit: `journal.record(VAR_DIR, "playground", {"model":..., "attempts":N})`.

### Criterio di done
- `cd ~/Serverino/scrocco-llm && .venv/bin/python -m pytest tests/ -x -q` verde
  (i test esistenti non devono rompersi) + nuovo
  `tests/test_admin_playground.py` con:
  - `model` inesistente → 4xx con message chiaro;
  - `messages` mancanti → 400;
  - con un router mockato in dry (patch di `forwarder.call`) che fallisce la
    1ª chiave e risponde alla 2ª → trace con 2 step (1 scartato + 1 riuscito),
    `attempts=2`, `fallbacks=1`; stato del router NON sporcato (cooldown vuoto).
- Manuale col gateway vivo (se gira): `curl -s -H "Authorization: Bearer
  $GATEWAY_MASTER_KEY" -H 'Content-Type: application/json' -d
  '{"model":"<modello reale>","messages":[{"role":"user","content":"Ciao"}]}'
  http://127.0.0.1:4001/admin/playground` → JSON con trace.

### Rischi / note
- Il rischio grosso è il refactor del path `_chat` (main.py): NON rifattorizzare
  la produzione. Reimplementa il piccolo loop di fallback DENTRO il handler del
  playground col trace (30-60 righe), riusando i metodi di `router`. Se il
  codice ti porta a toccare `main.py`, fermati e riprogetta dentro admin.py.
- Nessuna scrittura sulla config: il playground NON salva nulla.

---

## PROMPT PRONTO PER IL SUBAGENTE (GP-01)

Lavora nella repo `~/Serverino/scrocco-llm` (fork ammesso dei path relativi
`scrocco-llm/app/...`; MAI path assoluti; se `read` fallisce usa
`cat`/`sed -n '1,200p'`).

Repo: gateway LLM FastAPI. Aggiungi un endpoint admin di "playground" che
rigira una chat di prova e restituisce il trace di routing/fallback, SENZA
toccare lo stato reale del router (niente cooldown/health/EMA modificati).
Guarda PRIMA come sono fatti gli endpoint esistenti in `app/admin.py` (pattern
`_require_master`, `_gw()`, `_json_body`, `journal.record`) e come il router
fa il fallback negli handler di chat in `app/main.py` (NON modificare main.py).

Implementa in `app/admin.py`:
1. `POST /admin/playground` (montato sull'`admin_api` esistente, prefix
   `/admin`): body `{model, messages:[{role,content}], profile?, max_tokens?
   }`; risolve come una richiesta reale (canonicalize + resolve_group_for_request
   con `need=caps_for(model)` se routing_active + initial_pick + loop di
   fallback via `router.fallback_next(profile, dep, need, scope)`); chiama
   `forwarder.call(dep, payload)` per il tentativo (timeout 20s); su errore
   registra reason e prova il successivo fino a esaurimento. Raccoglie `trace`
   (step, unique, group, profile, reason, verdict). NON chiamare mai
   mark_failed/note_start/note_end; nessuna scrittura su `_cooldown/_stats/
   keyhealth`. Resolve della risposta: `content` dal dict di `forwarder.call`.
   Risposta 200 con lo shape del contratto. Journal con
   `journal.record(gw.VAR_DIR, "playground", {...})`.
2. Test `tests/test_admin_playground.py` (pytest, stile degli altri test della
   repo: guarda `tests/`): mock di `forwarder.call` per 1÷2 tentativi +
   asserzioni su trace/attemps/fallbacks e su stato router pulito. Esempi
   basati su `app.router` reale con config minimale (vedi fixture nei test
   esistenti).

Criterio di done: `python -m pytest tests/ -x -q` verde con i test esistenti,
il nuovo test incluso; nessuna modifica a `app/main.py`, `app/router.py`,
`app/forwarder.py`, `app/qc.py`, `app/policy.py`. Non toccare altri file.
Riepiloga in 4 righe il contratto di risposta e la strategia no-side-effect.

---

## GP-02 · `GET|PUT /admin/csv` (lettura/validazione+backup+reload del CSV)

`id: GP-02-csv-read-write`
`fase: GP`
`dipende_da: []`
`puo_parallelo_con: nessun altro GP in parallelo`

### Obiettivo
Esporre il contenuto di `var/keys_rotation.csv` in forma testuale (raw) e
parsata, e permetterne la sostituzione con validate-before-write + backup
automatico + reload della config (riusando `csv_store.save_table`,
`journal.backup_csv`, `config.reload()`).

### File da creare/modificare
- `scrocco-llm/app/admin.py` — `GET /admin/csv`, `PUT /admin/csv`
- `scrocco-llm/tests/test_admin_csv.py` (crea)

### Contratto
```
GET /admin/csv  (master)
200 → {
  "path": "/app/var/keys_rotation.csv",
  "raw": "commento,modello,provider,...\n...",   // CSV testuale (utf-8-sig, CRLF
                                                  // come salvato) — raramente
                                                  // le chiavi ci sono: raw può
                                                  // contenerle (master-only)
  "parsed": { "header": [...], "rows": [ {...chiavi MASCHERATE...} ] },
  "count": 12,
  "backups": [ {"filename": "keys_rotation-20240826-103000.csv",
                "size": 812, "mtime": 1724679000} ]   // ultimi 5, da
                                                       // var/backups/
}

PUT /admin/csv  (master)
Body: { "raw": "<csv testuale>" }
200 → { "ok": true, "backup": "keys_rotation-<ts>.csv", "rows": 12 }
400 → { "error": {"message": "riga 5: campo 'endpoint' mancante ..."} }
```
- `GET` deve usare `csv_store.load_table` per `parsed` (masking chiavi con
  `csv_store.mask_key`), leggere il raw come stringa dal file, e listare i
  backup da `var/backups/` (glob `keys_rotation-*.csv`, ordinati per mtime desc).
- `PUT` deve: prendere `raw`, salvare su tmp, validare (istanziando
  `GatewayConfig` sul tmp come fa `csv_store.save_table`), su errore → 400 con
  messaggio; su ok: `csv_store.save_table`, `config.reload()`, risposta con
  backup e rows. (Stessa manovra di `_commit_csv` di admin.py — riusarla SENZA
  duplicarla: se `_commit_csv` già fa backup+save+reload, chiamala.)
  **NON fare backup manuale PRIMA di chiamare `_commit_csv`: essa esegue già
  `journal.backup_csv` internamente → un backup esplicito PRIMA produrrebbe un
  DOPPIO backup.**
- Idempotente: PUT con raw identico → ok.

### Criterio di done
`python -m pytest tests/ -x -q` verde; `tests/test_admin_csv.py` con: GET
restituisce raw+parsed+count coerenti con un CSV fixture; PUT con CSV rotto →
400 e file invariato; PUT valido → 200, backup creato in var/backups/, file
aggiornato e `config.reload` ok (load successivo senza errori).

### Rischi / note
- MAI duplicare la logica di validazione: riusa `csv_store.save_table`.
- `raw` può essere grande (50kb ok), body limit ampi.
- Recovery: `_commit_csv` già gestisce backup+save+reload con errore → usa
  quella (così il diff con la CRUD deployment è minimo).

---

## PROMPT PRONTO PER IL SUBAGENTE (GP-02)

Lavora in `~/Serverino/scrocco-llm` (solo path relativi `scrocco-llm/...`; se
`read` fallisce usa `cat`/`sed -n`).

Repo gateway FastAPI. Aggiungi `GET /admin/csv` e `PUT /admin/csv` in
`app/admin.py` per letto/scrittura raw del file CSV con validazione+backup+
reload. Studia `app/csv_store.py` (save_table valida su tmp e os.replace),
`app/journal.py` (`backup_csv`, `_paths`) e `app/admin.py` (`_commit_csv`,
`_require_master`, `csv_store.mask_key`). Riusa questi helper; NON duplicarli.

Dettagli di implementazione:
1. `GET /admin/csv` → leggi il file `gw.CSV_PATH` come testo; `parsed` via
   `csv_store.load_table` con chiavi mascherate; lista backup da
   `var/backups/` (glob `keys_rotation-*.csv`, 5 più recenti, con size/mtime);
   shape come da contratto. Se il file non esiste (CSV vuoto) → 200 con
   `raw: ""`, `parsed: {header: [], rows: []}`, `count: 0`.
2. `PUT /admin/csv` → body `{raw}`; scrivi su tmp, valida con `csv_store`
   (istanziando GatewayConfig sul tmp), su CsvStoreError → 400 con message;
   su ok chiama la sequenza save→reload (riusa `_commit_csv`, che fa GIÀ il
   backup internamente — NON fare un backup manuale PRIMA, sarebbe doppio),
   passandogli header/rows derivati da un load del raw
   proposto — salvando il RAW testuale: scrivi il raw così com'è ma passando
   dal `save_table` per la validazione). Risposta 200 {ok, backup, rows} o 400.
3. `tests/test_admin_csv.py` — pytest con joint fixture: crea CSV temp in
   `var/` test, valida GET, PUT valido/invalido/identico.

Criterio: `python -m pytest tests/ -x -q` verde. NON toccare altri file.
Riepiloga in 3 righe.

---

## GP-03 · `GET|PUT /admin/policy/raw` (yaml raw + diff + validazione + reload)

`id: GP-03-policy-raw`
`fase: GP`
`dipende_da: []`
`puo_parallelo_con: nessun altro GP in parallelo`

### Obiettivo
Esporre il `gateway.yaml` come testo (raw) per l'editor web, con PUT che
valida (Policy.load su tmp), fa backup, `os.replace` atomico e swap runtime —
stessa meccanica di `_persist_policy_merged` ma sul documento intero.

### File da creare/modificare
- `scrocco-llm/app/admin.py` — `GET /admin/policy/raw`, `PUT /admin/policy/raw`
- `scrocco-llm/tests/test_admin_policy_raw.py` (crea)

### Contratto
```
GET /admin/policy/raw  (master)
200 → { "path": "/app/var/gateway.yaml", "raw": "<yaml testuale>" }

PUT /admin/policy/raw  (master)
Body: { "raw": "<yaml testuale>" }
200 → { "ok": true, "validated": true,
        "reloaded": true,
        "effective": { "step_up_pct": 22, "aliases": 3, ... } }
400 → { "error": {"message": "policy non valida: riga 12 ..."} }
```
- `GET`: `raw` senza alcun masking (master-only); se il file manca → 200 con
  `raw: ""`.
- **NOTA IMPORTANTE: il PUT sostituisce l'INTERO `gateway.yaml` con il testo
  fornito — NON fa merge con la policy corrente.** Questo NON cozza con il
  PATCH parziale `/admin/policy` (F2-04/F2-05, che merge scalari/profili/
  liste): uno è un editor raw "full-replace", l'altro è un patch strutturato.
  Chi salva dal web-editor lo sa: invia SEMPRE un documento completo.
- `PUT`: scrive su tmp con `yaml.safe_dump`? NO — il raw è il testo fornito:
  scrivi la stringa su tmp, `Policy.load(tmp)` per validare, su errore 400 con
  message e file intatto; altrimenti backup del file corrente in
  `var/backups/` (nome `gateway.yaml-<ts>.yaml`, best-effort), `os.replace`,
  swap dei riferimenti (`gw.router.policy`, `global policy`, `gw.policy`) come
  `_persist_policy_merged` di admin.py (riusa/quello o replica 10 righe).
- Journal: `journal.record(gw.VAR_DIR, "policy_raw", {...})`.

### Criterio di done
`python -m pytest tests/ -x -q` verde; `tests/test_admin_policy_raw.py`: GET
raw matcha il file; PUT con yaml sintatticamente invalido → 400 e file
intatto; PUT valido → 200, file aggiornato, `policy.step_up_pct` riflette; il
reload runtime funziona (chiamata successiva a `/admin/policy` mostra il
valore nuovo).

### Rischi / note
- La validazione va fatta su un file TMP (mai scrivere il live prima di
  validare). Il backup best-effort (come `backup_csv`).
- Attenzione: `Policy.load(tmp)` deve essere robusto a yaml senza `policy:`
  top-level (considera il default).

---

## PROMPT PRONTO PER IL SUBAGENTE (GP-03)

Lavora in `~/Serverino/scrocco-llm` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Aggiungi `GET /admin/policy/raw` e `PUT /admin/policy/raw` in
`app/admin.py`: lettura/scrittura del `gateway.yaml` come testo con
valida-sul-tmp + backup + os.replace + swap runtime. Studia:
`app/admin.py` (`_persist_policy_merged`, `_persist_policy_merged` swap di
`globals()["policy"]`), `app/policy.py` (`Policy.load`), `app/journal.py`.

1. `GET /admin/policy/raw` → `{path, raw}` (file o `""`).
2. `PUT /admin/policy/raw` → scrivi body.raw su tmp, `Policy.load(tmp)` →
   errore → 400 {error:{message}}, ok → backup del file attuale
   (`var/backups/gateway.yaml-<ts>.yaml`, best effort), os.replace(tmp, live),
   swap riferimenti (come `_persist_policy_merged`), journal. response 200
   {ok, validated, reloaded, effective:{...}}.
3. `tests/test_admin_policy_raw.py` — pytest: valido/invalido/identità e
   cambio effective.

Criterio: `python -m pytest tests/ -x -q` verde. NON toccare altri file.
Riepiloga in 3 righe.

---

## GP-04 · `GET /admin/backups` + `POST /admin/backups/restore`

`id: GP-04-backups-restore`
`fase: GP`
`dipende_da: []`
`puo_parallelo_con: nessun altro GP in parallelo`

### Obiettivo
Listare i backup di CSV e `gateway.yaml` presenti in `var/backups/` e
ripristinare uno di essi sul file live con reload (config per CSV, policy per
yaml). Serve per la Config History/F4-04 (che poi mantiene anche snapshot
propri in Postgres).

### File da creare/modificare
- `scrocco-llm/app/admin.py` — `GET /admin/backups`, `POST /admin/backups/restore`
- `scrocco-llm/tests/test_admin_backups.py` (crea)

### Contratto
```
GET /admin/backups  (master)
200 → {
  "dir": "/app/var/backups",
  "csv":  [ {"filename": "keys_rotation-<ts>.csv",   "size": 812, "mtime": 1724679000} ],
  "yaml": [ {"filename": "gateway.yaml-<ts>.yaml",   "size": 2310, "mtime": ...} ]
}

POST /admin/backups/restore  (master)
Body: { "filename": "keys_rotation-<ts>.csv" | "gateway.yaml-<ts>.yaml" }
200 → { "ok": true, "restored": "<filename>", "rows"?: 12,
        "effective"?: {...} }
400 → { "error": {"message": "backup '...' non trovato"} }
404 → backup inesistente o nome non sicuro.
```
Regole:
- `filename` deve combaciare esattamente `^(keys_rotation-|gateway.yaml-).*`
  e risolversi DENTRO `var/backups/` (niente `../`, niente subdir: usa
  `os.path.basename` e verifica esistenza). Mai path di rete.
- Restore CSV: copy backup → CSV_PATH (backup dell'attuale PRIMA),
  `config.reload()`; risposta con rows. Restore yaml: copy → POLICY_PATH,
  swap runtime come in GP-03; risposta con effective.
- Audit: `journal.record(VAR_DIR, "restore", {"filename": ...})`.
- Nota: ripristino = sostituzione completa; consigliare (nel messaggio)
  di rifare anche il backup dell'attuale prima.

### Criterio di done
`python -m pytest tests/ -x -q` verde; `tests/test_admin_backups.py`: GET lista
i backup creati in fixture; restore con TS valido ripristina il file e il
reload funziona; restore con filename tipo `../etc/passwd` → 400/404 con
message sicuro. Nessun path traversal.

### Rischi / note
- Sicurezza: whitelist regex + basename + esistenza; per la 404 usa un message
  che non rifletta il filesystem.
- Mantenere i backup esistenti (rotazione a `journal.backup_csv(keep=20)`).

---

## PROMPT PRONTO PER IL SUBAGENTE (GP-04)

Lavora in `~/Serverino/scrocco-llm` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Aggiungi `GET /admin/backups` e `POST /admin/backups/restore`
in `app/admin.py`. Riusa `journal._paths(var_dir)` per la dir backup e
`csv_store`/`policy` per il reload.

1. `GET /admin/backups` → lista `keys_rotation-*.csv` e `gateway.yaml-*.yaml`
   da `var/backups/` (size/mtime, ordine desc) → `{dir, csv:[...], yaml:[...]}`.
2. `POST /admin/backups/restore` → valida filename (regex + basename +
   existence dentro backups); backup dell'attuale; copia sul live; reload
   corretto per tipo (CSV → `config.reload()`; yaml → `Policy.load`+swap
   runtime come GP-03); journal; risposta 200/400/404 come contratto.
3. `tests/test_admin_backups.py` — pytest: lista, restore valido, path
   traversal rifiutato.

Criterio: `python -m pytest tests/ -x -q` verde. NON toccare altri file.
Riepiloga in 3 righe.