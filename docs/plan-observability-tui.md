# Piano: 3 viste di osservabilità nella TUI (Textual)

**Scope**: solo progettazione + ricognizione. NON contiene codice applicativo.
**Target**: aggiungere alla TUI terminale (`tui/`, framework **Textual**) 3 viste:
1. **Log chiamate live** — scorrimento quasi-realtime di tutte le chiamate con instradamento visibile (gruppo/dim → deployment → provider, esito, latenza).
2. **Log errori tracciati-ma-non-bloccati** — vista filtrabile su `var/error-audit.log`.
3. **Classifica deployment** — tabella ordinabile + filtri: latenza media/p95, n. chiamate 7gg, tasso errori, ultimo uso, provider, gruppo/cap.

---

## A) Mappa TUI

### Framework & avvio
- **Textual** (`textual.app.App`, `textual.screen.Screen`/`ModalScreen`). Avvio via `./scrocco.sh` → `python -m tui.app` (fallback `./scrocco.sh --cli` → `tui/cli_lite.py`, menu numerico senza Textual).
- Entry point: `tui/app.py::GatewayTUI.main()` → `GatewayTUI().run()`. `on_mount` fa `push_screen(MainScreen())`.

### Come sono registrate/navigate le schermate
- Schermate figlie = sottoclassi di `ModalScreen` (es. `CapacitiesScreen`, `ClientKeysScreen`, `ExpiringScreen`, `AdvancedPolicyScreen` in `tui/extra_screens.py`, `tui/policy_screen.py`, `tui/modals.py`).
- Navigazione **push-based**: `self.app.push_screen(ScreenClass(...))` (non bloccante) o `self.app.push_screen_wait(...)` (ritorna un valore, usato da modali `ConfirmModal`/`TextInputModal`/`InfoModal`/`HelpScreen`/`TypedConfirmModal`).
- `MainScreen` è la schermata radice; le azioni la invocano e al ritorno richiamano `render_table()`/`render_profiles()` per ridisegnare.
- Pattern tipico di una schermata secondaria:
  - `compose()` costruisce i widget (spesso un `DataTable` + `Label`/`Input`).
  - `on_mount()` → `self.run_worker(self._load(), exclusive=True)` per il caricamento async iniziale.
  - `_load()` fa `await self.client.<metodo>()` e popola la `DataTable` (`t.clear()` + `t.add_row(...)`).
  - `BINDINGS = [Binding("escape","close","Chiudi"), ...]`.

### Come aggiungere una voce di menu + una nuova Screen
1. In `tui/app.py::MainScreen.BINDINGS` aggiungere es. `Binding("o", "observability", "Osserva")`.
2. In `MainScreen` aggiungere `def action_observability(self): self.app.push_screen(ObservabilityScreen(self.app.client))`.
3. Creare la nuova schermata (es. `tui/observability.py::ObservabilityScreen(ModalScreen)`) seguendo lo scheletro di `CapacitiesScreen`/`ExpiringScreen`.
4. Registrare la schermata nel `CSS` di `GatewayTUI` se servono stili dedicati (es. `#obs-box`).
5. Aggiornare `tui/modals.py::HelpScreen.HELP` per documentare il nuovo tasto.

### Come la TUI parla col gateway
- `tui/gateway_client.py::GatewayClient` è il wrapper HTTP (httpx async). **Nessun accesso a file/CSV**: "tutto passa dalle API (protocollo AGENT.md)". Autenticazione: header `Authorization: Bearer <GATEWAY_MASTER_KEY>` (`_headers()`).
- Metodi esistenti mappano 1:1 sugli endpoint admin: `state()`, `profiles()`, `deployments(profile)`, `create/update/delete_deployment`, `bulk`, `expiring(days)`, `policy_get/policy_patch`, `clear_cooldowns`, `release_sessions`, `reload`, `models`.
- Pattern HTTP generico: `get(path, params)` / `post(path, json)` / `put` / `patch` / `delete` → `_send` → `_parse` (solleva `GatewayError(status, message)` se `>=400` o se il gateway non è raggiungibile).
- **Nuovi endpoint** = nuovi metodi sul client (es. `async def calls(self, tail=500, since=None)`, `async def errors(self, filter=None, tail=500)`, `async def leaderboard(self, window="7d", sort="calls", order="desc")`).

### Pattern di polling/refresh già presenti
- Caricamento on-demand: `run_worker(coro, exclusive=True)` dentro `action_*` o `on_mount`. Nessun auto-poll oggi (refresh manuale con `r`/`R` su `MainScreen`, `r` nelle schermate secondarie).
- Per il **live** serve auto-poll: Textual espone `self.set_interval(2.0, self._poll)` (su `App` o `Screen`) oppure un `run_worker` con loop `while True: await self._poll(); await asyncio.sleep(1.5)`. Raccomandato `set_interval` con `since=` per fetching incrementale e `loading` guard per evitare sovrapposizioni (vedi pattern `ExpiringScreen._load` con `self._loading`).

---

## B) Formato ESATTO dei file dati

> Convenzioni: `dep` = unique router **`grp__<model>__<idx>`** (es. `scrocco-llm-collego-128k__openai-gpt-oss-120b__0`). `id` del catalogo admin (`drow_<md5>`) è **diverso** da `dep`: il join lato endpoint avviene tramite `gw.config.deployment_by_unique(dep)`.
> Nessun file contiene segreti: le chiavi sono già mascherate (`key_masked` tipo `gsk_…da`); i messaggi di errore non espongono chiavi. Esempi sotto già reali/mascherati.

### 1. `var/usage_ledger.jsonl`  (sorgente principale per live + classifica)
- **Formato**: JSONL **append-only**, 1 riga per richiesta servita. Scritto buffered da `app/ledger.py::Ledger` (flush periodico dal watcher + a shutdown). **Rotazione per dimensione** `>20MB` → `usage_ledger.jsonl.1` (mantiene `.1`, `.2`; `LEDGER_MAX_BYTES=20MB`, `LEDGER_KEEP=2`). `iter_rows()` legge tutti i segmenti.
- **Campi**: `ses, profile, req, grp, dep, model, tries, fb, dur_ms, stream, qc, wd, ttfb_ms, kind, status, usage{prompt_tokens,completion_tokens,total_tokens}, ts`.
- **Esempio reale**:
  `{"ses":"-","profile":"collego","req":"scrocco-llm-collego","grp":"scrocco-llm-collego-128k","dep":"scrocco-llm-collego-128k__openai-gpt-oss-120b__0","model":"openai/gpt-oss-120b","tries":2,"fb":1,"dur_ms":822,"stream":false,"qc":false,"wd":null,"ttfb_ms":null,"kind":"chat","status":null,"usage":{"prompt_tokens":113,"completion_tokens":120,"total_tokens":233},"ts":1787737357}`
- **Note**: `status` è `null` sui successi (gli errori HTTP non finiscono qui); `fb`=n° fallback, `qc`=scarti QC, `wd`=watchdog tier. `grp` codifica gruppo/dim (`<prefix><profilo>-<dim>k`). Provider **non** presente come campo esplicito (ricavabile da `model` o dal catalogo).

### 2. `var/gateway.log`  (sorgente "live" + catena di instradamento)
- **Formato**: logging Python `RotatingFileHandler` (soglia `GATEWAY_LOG_MB`). Riga: `YYYY-MM-DD HH:MM:SS,mmm LEVEL nx.<module> [<tag>] <msg>`.
- **Tag rilevanti**:
  - `[summary]` → JSON **identico al ledger**, 1 riga a fine richiesta. **Fonte ideale per il live** (ha `dep, grp, model, dur_ms, fb, qc, status, usage`).
  - `[route]` → `req -> group` con `ctx≈…, need=[…], session=…, stream=…`.
  - `[identity]` → `group -> dep (model)` (instradamento risolto).
  - `[fallback]` → `dep -<status> motivo=… -> next_dep :: <json error>` (fallback + errore attribuito al deployment).
  - `[cooldown]`, `[defer]`, `[erroraudit]` (le righe `erroraudit` finiscono ANCHE in `error-audit.log`).
  - Rumore: `httpx HTTP Request …`, `[auth] …`, `[policy] …`.
- **Rotazione**: sì (file `.1`/`.2` se supera la soglia; oggi assente perché sotto soglia).
- **Esempio `[summary]` reale**:
  `2026-08-27 01:09:24,494 INFO nx.main [summary] {"ses":"-","req":"scrocco-llm-mioaruba-1000k","grp":"scrocco-llm-mioaruba-1000k","dep":"scrocco-llm-mioaruba-1000k__nemotron-3-ultra-free__61","tries":1,"fb":0,"dur_ms":4471,"stream":true,"qc":false,"wd":null,"ttfb_ms":839,"usage":null}`
- **Esempio `[fallback]` reale**:
  `2026-08-27 01:09:09,872 WARNING nx.main [fallback] stream scrocco-llm-mioaruba-1000k__deepseek-v4-flash-free__31 -401 motivo=provider_error_body -> scrocco-llm-mioaruba-1000k__deepseek-v4-flash-free__32 :: {"type":"error","error":{"type":"AuthError","message":"Invalid API key."}}`

### 3. `var/error-audit.log`  (sorgente vista errori)
- **Formato**: stesso `RotatingFileHandler`, logger `nx.erroraudit` (`propagate=True` → le righe ci sono anche in `gateway.log`). 1 riga per errore **tracciato-ma-non-bloccati** (fallback esauriti / errori provider restituiti all'utente ma auditati). Forma: `TS WARNING nx.erroraudit status=<N> :: <json error body>`.
- **Campi utili**: `ts` (dalla riga), `status` (può essere negativo = errore downstream provider mappato, es. `-400`/`-401`; o positivo = generato dal gateway, es. `500`/`503`), `error.type`, `error.message`.
- **Nessun `dep` nella riga** → la vista errori è una lista piatta filtrabile (non attribuibile per deployment senza incrociare `gateway.log`).
- **Esempio reale**:
  `2026-08-27 01:09:09,863 WARNING nx.erroraudit status=-401 :: {"type":"error","error":{"type":"AuthError","message":"Invalid API key."}}`
- **Distribuzione rilevata** (campione): `429×197, 500×184, -400×95, -404×68, -401×22, 503×3`.

### 4. `var/adaptive_stats.json`  (sorgente "ultimo uso" + latenza EMA)
- **Formato**: JSON riscritto periodicamente dal watcher. Chiavi top: `stats` (dict `dep`→`{ema_latency_ms, last_used, fail_streak, success_ema}`), `cooldown`, `cap_strikes`, `saved_at`.
- **Campi per `dep`**: `ema_latency_ms` (ms), `last_used` (epoch float), `fail_streak`, `success_ema`.
- **Esempio reale**:
  `{"stats":{"scrocco-llm-collego-32k__llama-3-3-70b-free__3":{"ema_latency_ms":0.1568452868173772,"last_used":1787743388.898109,"fail_streak":30,"success_ema":5.039029953172211e-98}}, "cooldown":{…}, "cap_strikes":[…], "saved_at":1787743…}`

### 5. `var/probe_results.json`  (sorgente latenza probe)
- **Formato**: JSON dict `dep`→`{ok, latency_ms, ts, status, key_masked, probe_kind}`. Scritto dai probe (`POST /admin/deployments/probe`).
- **Esempio reale**:
  `{"scrocco-llm-collego-128k__openai-gpt-oss-120b__0":{"ok":true,"latency_ms":532,"ts":1787790974,"status":200,"key_masked":"gsk_…da","probe_kind":"chat"}}`

### 6. `var/key_health.json`  (sorgente colonna health)
- **Formato**: JSON dict `dep`→`{first_dead_ts, last_reason, streak_max, state}`. `state` ∈ `{dead_suspect, retired}`; **assente** = healthy.
- **Esempio reale**:
  `{"scrocco-llm-collego-32k__llama-3-3-70b-free__3":{"first_dead_ts":1787737339,"last_reason":null,"streak_max":31,"state":"dead_suspect"}}`

### 7. `var/operations.jsonl`  (contesto admin, non richiesto dalle 3 viste)
- **Formato**: JSONL append-only journal (`journal.py`). Campi: `ts, op, …dettagli`. Nessun segreto. `op`∈`{create,bulk,cap_auto_learn,purge,unretire,reload,probe,…}`. Già esposto via `GET /admin/history?limit=`.
- **Esempio reale**:
  `{"ts":1787662815.69,"op":"create","profile":"collego","modello":"Systran/faster-whisper-base","id":"drow_fe6577727c","key_rotated":true}`

---

## C) GAP lato gateway: endpoint vs lettura file diretta

### Endpoints già esistenti (riutilizzabili)
- `GET /admin/insights?days=&group_by=deployment` → per `dep`: `calls, avg_dur_ms, fallback_rate, qc_rate, tokens, cost` (base classifica, ma **mancano p95, error_rate vero, last_used, provider, group/cap**). `group_by` accetta `profile|model|deployment|day|kind|none`.
- `GET /admin/insights/summary` → compatto 24h.
- `GET /admin/history?limit=` → `operations.jsonl`.
- `GET /admin/state` → cooldowns, sticky, capabilities, health, adaptive (solo conteggi), budget, policy.
- `GET /admin/deployments?profile=` → catalogo con `id, profile, modello, provider, group, caps, cap_groups, category, context_k, priority, key_masked, endpoint` (sorgente provider/gruppo/cap per la classifica).
- `POST /admin/deployments/probe` / `/probe/bulk` → scrivono `probe_results.json`. `probe_results_view()` esiste ma **non ha ancora un GET admin** (usato solo da `/bootstrap/status`).

### Servono nuovi endpoint?
**SÌ, 3 nuovi endpoint in `app/admin.py` (router `/admin`).** Non basta leggere i file da TUI per i motivi sotto.

### La TUI gira sulla stessa macchina del gateway?
- **Caso comune (host)**: `run.sh` avvia uvicorn su `127.0.0.1:4001`; `scrocco.sh` lancia `tui.app` sulla **stessa macchina** → la TUI *potrebbe* tecnicamente leggere `var/` dal fs.
- **Caso docker** (`docker-compose.yml`): il gateway gira in container; `var/` è bind-mount (`/app/var`), esposto su host solo via porta `127.0.0.1:4001`. La TUI sull'host **non ha accesso al fs del container** → lettura file diretta impossibile.
- `GatewayClient.DEFAULT_BASE = http://127.0.0.1:{GATEWAY_PORT}` e variabile `GATEWAY_URL` ⇒ la TUI è pensata per parlare HTTP e può essere remota.

### Decisione: **TUI legge SOLO via nuovi endpoint HTTP** (NON legge `var/` direttamente)
1. **Coerenza architetturale**: `gateway_client.py` dichiara "La TUI NON tocca mai il CSV: tutto passa dalle API". I log/jsonl sono dati di verità allo stesso titolo.
2. **Isolamento docker**: la TUI non può vedere il fs del container.
3. **Dati strutturati > parsing log**: gli endpoint restituiscono JSON già filtrato/aggregato; la TUI resta "dumb render" e resta auth-through master key.
4. **Ottimizzazione futura (fuori piano)**: per il live si può evolvere in SSE/WebSocket; per ora polling HTTP con `set_interval` + `since=`.

### Endpoint nuovi proposti (tutti in `app/admin.py`, `admin_api`, protetti da `_require_master`)
- **`GET /admin/logs/calls?tail=500&since=<ts>`**
  - Legge in fondo a `var/gateway.log` (e `.1` se presente), filtra i tag `[summary]` (+ opzionale `[route]`,`[identity]`,`[fallback]`), ritorna `{"events":[{ts, level, tag, profile, grp, dep, model, dur_ms, tries, fb, qc, status, summary_json}]}`.
  - `since` = ultimo ts visto dalla TUI → ritorna solo le nuove righe (scroll incrementale, basso costo).
- **`GET /admin/logs/errors?filter=<status|type|testo>&tail=500&since=<ts>`**
  - Legge `var/error-audit.log`, ritorna `{"events":[{ts, status, error_type, error_message}]}`. Filtro server-side su `status` (es. `429`, `-401`), `error.type`, o substring messaggio.
- **`GET /admin/insights/leaderboard?window=7d&sort=calls|avg_dur_ms|p95_dur_ms|error_rate|last_used&order=asc|desc&profile=`**
  - Join **server-side** (usa `gw.LEDGER`, `gw.router._stats`, `gw.KEYHEALTH`, `gw.config.deployment_by_unique`, `probe_results.json`):
    - `calls`, lista `dur_ms` → `avg_dur_ms` + `p95_dur_ms` (da ledger, finestra `window`).
    - `error_rate` = proxy da ledger (`fallback_rate + qc_rate`) di default; upgrade a errore HTTP reale parsando `gateway.log` `[fallback]`/`[erroraudit]` per `dep` (vedi rischio U-GW3).
    - `last_used` da `adaptive_stats` (`last_used` per `dep`; "mai usato" se assente).
    - `provider, group, caps, profile, modello` da catalogo (`deployment_by_unique`).
    - `health` da `key_health.json`; `probe_ms, probe_ok` da `probe_results.json`.
  - Ritorna `{"window_days":7,"rows":[{dep, profile, group, provider, model, calls, avg_dur_ms, p95_dur_ms, error_rate, last_used, health, probe_ms}]}`.

### Gap/rischi sui dati
- `error_rate` vero richiede parsing di `gateway.log` (error-audit.log non ha `dep`). Alternativa pulita (future, **U-GW4**): instrumentare `forwarder.py`/`_emit_summary` per registrare `dep`+`status` anche su un ledger errori strutturato, così la classifica non dipende dal parse dei log.
- `provider` non è nel ledger: va dal catalogo (join per `dep`).
- Ledger cresce (rotazione 20MB/.1/.2): l'aggregazione su 7gg deve scansionare tutti i segmenti → mettere un piccolo cache/timeout sul endpoint.

---

## D) PIANO IN FASI — unità di lavoro indipendenti (parallele)

> Contratti endpoint definiti in §C (json di ritorno). Ogni unità è eseguibile da un agente separato. Wave = parallelismo possibile.

### Wave 1 — backend endpoint + client TUI + scaffold (PARALLELI)
**U-GW1 — Endpoint live calls**
- Descrizione: implementa `GET /admin/logs/calls?tail=&since=` in `app/admin.py`; helper di tail/parse di `var/gateway.log` (nuovo `app/logview.py` o funzioni locali). Filtra `[summary]` (+`[route]`/`[identity]`/`[fallback]`), normalizza in eventi.
- File: `app/admin.py`, `app/logview.py` (nuovo).
- Dipendenze: nessuna.
- Done: ritorna `events[]` strutturati; `since` restituisce solo le nuove; gestisce rotazione `.1`.
- Rischi: parsing robusto su righe troncate; performance tail (leggere da fondo, non tutto il file).
- Prompt: "In app/admin.py (router admin_api, prefix=/admin) aggiungi GET /admin/logs/calls?tail=500&since=float che legge da gw.VAR_DIR/gateway.log (e .1 se esiste) partendo dalla fine, filtra le righe i cui tag fra parentesi quadre siano in {summary,route,identity,fallback}, e ritorna JSON {events:[{ts(float epoch), level, tag, profile, grp, dep, model, dur_ms, tries, fb, qc, status, summary_json(dict)}]}. Usa since per ritornare solo righe con ts>since. Crea app/logview.py con funzione tail_parse(path, tags, tail, since). Gestisci righe troncate/json non valido saltandole. Nessun segreto nei log. Aggiungi test con un file fixture."

**U-GW2 — Endpoint errori**
- Descrizione: `GET /admin/logs/errors?filter=&tail=&since=` su `var/error-audit.log`; parse `status=<N> :: <json>`; filtro server-side.
- File: `app/admin.py`, `app/logview.py`.
- Dipendenze: nessuna (riusa `logview.py` di U-GW1 se pronto, altrimenti locale).
- Done: ritorna `events[{ts,status,error_type,error_message}]`; filtro per `status`/`error.type`/testo funzionante.
- Rischi: stesso parsing; `status` può essere negativo.
- Prompt: "In app/admin.py aggiungi GET /admin/logs/errors?filter=str&tail=500&since=float che legge gw.VAR_DIR/error-audit.log dalla fine, parse le righe 'TS LEVEL nx.erroraudit status=<N> :: <json>', e ritorna {events:[{ts(float), status(int, anche negativo), error_type(str), error_message(str)}]}. if filter: se numerico confronta status, altrimenti match su error.type o message (case-insensitive). since ritorna solo ts>since. Riutilizza app/logview.py. Test con fixture."

**U-GW3 — Endpoint classifica deployment**
- Descrizione: `GET /admin/insights/leaderboard?window=7d&sort=&order=&profile=`; join ledger+adaptive_stats+catalog+probe+key_health; calcola avg/p95 dur, calls, error_rate proxy, last_used, provider/group/caps, health, probe.
- File: `app/admin.py` (riusa `_insights_aggregate` per calls/avg/fb/qc).
- Dipendenze: nessuna (legge `gw.LEDGER`, `gw.router._stats`, `gw.KEYHEALTH`, `gw.config.deployment_by_unique`, `_load_probe_results()`).
- Done: ritorna `rows[]` ordinabili per `sort`; `window` filtra per `ts` su ledger.
- Rischi: `error_rate` vero non disponibile (usare proxy `fb+qc`); performace su 7gg → cache leggera; `last_used` assente per dep mai usati.
- Prompt: "In app/admin.py aggiungi GET /admin/insights/leaderboard?window=7d&sort=calls&order=desc&profile= che aggrega gw.LEDGER.iter_rows() filtrate per ts>=now-window*86400. Per ogni dep calcola calls, avg_dur_ms e p95_dur_ms (lista dur_ms), error_rate=fallback_rate+qc_rate (riusa _insights_aggregate), last_used da gw.router._stats[dep].last_used (None se assente), provider/group/caps da gw.config.deployment_by_unique(dep), health da gw.KEYHEALTH.data.get(dep,{}).get('state'), probe_ms da _load_probe_results().get(dep,{}).get('latency_ms'). Ritorna {window_days, rows:[{dep,profile,group,provider,model,calls,avg_dur_ms,p95_dur_ms,error_rate,last_used,health,probe_ms}]} ordinati per sort/order. Aggiungi profile come filtro opzionale. Test con fixture ledger."

**U-TUI5 — Metodi client TUI** (PARALLELO, dipende solo dal contratto)
- Descrizione: in `tui/gateway_client.py` aggiungere `calls(tail, since)`, `errors(filter, tail, since)`, `leaderboard(window, sort, order, profile)` che chiamano i 3 endpoint e ritornano dict/list.
- File: `tui/gateway_client.py`.
- Dipendenze: contratti endpoint (§C).
- Done: i 3 metodi ritornano la stessa forma JSON degli endpoint.
- Rischi: gestione `GatewayError` già presente; `since` come `float|None`.
- Prompt: "In tui/gateway_client.py aggiungi a GatewayClient: async def calls(self, tail=500, since=None) -> dict (GET /admin/logs/calls); async def errors(self, filter=None, tail=500, since=None) -> dict (GET /admin/logs/errors); async def leaderboard(self, window='7d', sort='calls', order='desc', profile=None) -> dict (GET /admin/insights/leaderboard). Usa i metodi get() esistenti con params. Nessun altro cambiamento."

**U-TUI1 — Scaffold menu + Screen osservabilità** (PARALLELO, dipende dal contratto dei nomi)
- Descrizione: in `tui/app.py::MainScreen.BINDINGS` aggiungere `Binding("o","observability","Osserva")` + `action_observability()` che fa `push_screen(ObservabilityScreen(self.app.client))`. Creare `tui/observability.py::ObservabilityScreen(ModalScreen)` con layout a tab/3 DataTable (o 3 sub-screen) e `BINDINGS` (escape=chiudi, 1/2/3 per commutare vista). Aggiornare `HelpScreen.HELP`.
- File: `tui/app.py`, `tui/observability.py` (nuovo), `tui/modals.py`.
- Dipendenze: nessuna (schermate populate poi da U-TUI2/3/4).
- Done: tasto `o` apre la schermata osservabilità con 3 placeholder DataTable popolabili.
- Rischi: coerenza stile CSS con `#modal-box`; navigazione tab.
- Prompt: "Aggiungi in tui/app.py MainScreen.BINDINGS Binding('o','observability','Osserva') e def action_observability(self): self.app.push_screen(ObservabilityScreen(self.app.client)). Crea tui/observability.py con ObservabilityScreen(ModalScreen) che ha 3 DataTable (#live-t, #err-t, #lb-t) e un selettore vista (binding 1/2/3 o Tab). compose() dichiara le 3 tabelle + un Label titolo. Aggiungi CSS minimo in GatewayTUI.CSS (#obs-box). Aggiorna tui/modals.py HelpScreen.HELP con la voce 'o Osserva'. Non implementare ancora il caricamento dati."

### Wave 2 — viste TUI (PARALLELE fra loro; dipendono da Wave 1)
**U-TUI2 — Vista Live chiamate**
- Descrizione: `LiveCallsScreen` (o tab in `ObservabilityScreen`): `DataTable` + `set_interval(1.5, self._poll)`; `client.calls(tail, since)`; mantiene `self._last_ts`; mostra colonne `ts, profile, grp, dep, model, tries, fb, dur_ms, status` con instradamento (evidenzia `[fallback]`/`[erroraudit]`).
- File: `tui/observability.py`, `tui/gateway_client.py` (metodo di U-TUI5).
- Dipendenze: U-GW1, U-TUI1, U-TUI5.
- Done: scorrimento quasi-realtime; nuove righe aggiunte in cima; `since` evita dup.
- Rischi: sovrapposizione poll → `self._loading` guard; formato ts leggibile.
- Prompt: "In tui/observability.py implementa la vista Live chiamate: DataTable con colonne ts,profile,grp,dep,model,tries,fb,dur_ms,status. Usa self.set_interval(1.5, self._poll); _poll chiama await self.client.calls(tail=300, since=self._last_ts) e aggiunge le nuove righe in cima (t.clear()+rebuild o add_row), aggiorna self._last_ts. Evidenzia righe con fb>0 o status non nullo. Gestisci GatewayError con notify. Usa il metodo calls() di U-TUI5."

**U-TUI3 — Vista Errori tracciati**
- Descrizione: `ErrorsScreen`: `DataTable` + `Input` filtro + `set_interval(3, _poll)`; `client.errors(filter, tail, since)`; colonne `ts, status, error_type, error_message`; filtro lato server + locale.
- File: `tui/observability.py`, `tui/gateway_client.py`.
- Dipendenze: U-GW2, U-TUI1, U-TUI5.
- Done: filtro per status/type funzionante; auto-refresh.
- Rischi: input filtro vs filtro server; escape pulisce.
- Prompt: "In tui/observability.py implementa la vista Errori: DataTable ts,status,error_type,error_message + Input filtro. set_interval(3,_poll) chiama self.client.errors(filter=valore_input or None, tail=500). Al cambio dell'Input ricarica. Evidenzia status>=500 o negativi. Usa metodo errors() di U-TUI5."

**U-TUI4 — Vista Classifica deployment**
- Descrizione: `LeaderboardScreen`: `DataTable` ordinabile (click header → `sort`) + `Input`/`Select` filtro profilo + `set_interval(10, _poll)`; `client.leaderboard(window, sort, order, profile)`; colonne `dep, profile, group, provider, model, calls, avg_dur_ms, p95_dur_ms, error_rate, last_used, health, probe_ms`.
- File: `tui/observability.py`, `tui/gateway_client.py`.
- Dipendenze: U-GW3, U-TUI1, U-TUI5.
- Done: ordinamento per colonna + filtro profilo + refresh periodico.
- Rischi: ordinamento numerico vs testo; `last_used` None → "mai".
- Prompt: "In tui/observability.py implementa la vista Classifica: DataTable con colonne dep,profile,group,provider,model,calls,avg_dur_ms,p95_dur_ms,error_rate,last_used,health,probe_ms. Click su header → toggla sort/order e chiama self.client.leaderboard(window='7d', sort=col, order=order, profile=filtro). set_interval(10,_poll) per refresh. Usa metodo leaderboard() di U-TUI5. Formatta last_used=None come 'mai'."

**U-GW4 (opzionale) — Errori strutturati nel ledger**
- Descrizione: in `app/main.py` (`_emit_summary`/percorso errore in `forwarder.py`) registrare `dep`+`status`+`err_type` su un ledger errori (o arricchire `usage_ledger` con `status`), così `error_rate` della classifica diventa quello HTTP reale senza parse di `gateway.log`.
- File: `app/main.py`, `app/forwarder.py`, `app/ledger.py`.
- Dipendenze: U-GW3 (definisce semantica `error_rate`).
- Done: `error_rate` da ledger; retrocompatibile.
- Rischi: cambio schema ledger; impatto performance.
- Prompt: "Arricchisci il ledger in app/ledger.py e _emit_summary in app/main.py per registrare, in caso di errore, anche {status, err_type, dep} (gia' presenti in f.get('status')/gateway.log [fallback]). Aggiorna _insights_aggregate in app/admin.py per esporre error_rate_http reale. Mantieni retrocompatibilita' con righe status=null."

### Wave 3 — Integrazione & test
**U-INT — Test end-to-end + unitari**
- Descrizione: avvia gateway (`run.sh`) + TUI (`scrocco.sh`); verifica le 3 viste con i dati reali in `var/`. Test unitari per i 3 endpoint con fixture (log/jsonl di esempio). Verifica auth master-key, gestione rotazione file, permessi in docker.
- File: `tests/` (nuovi), eventuali fix in `tui/observability.py`/`app/admin.py`.
- Dipendenze: U-GW1..4, U-TUI1..5.
- Done: le 3 viste mostrano dati coerenti; filtri/ordinamento funzionano; nessun crash su log vuoto/ruotato.
- Rischi: desync contratto endpoint↔client; permessi `var/` (oggi `rw-r--r--` per alcuni file, `rw-------` per ledger/key_health — il gateway li legge come stesso utente, ok).
- Prompt: "Esegui gateway (run.sh) e TUI (scrocco.sh) e verifica a mano le 3 viste osservabilita' con i dati in var/. Aggiungi test/ per GET /admin/logs/calls, /admin/logs/errors, /admin/insights/leaderboard usando fixture di gateway.log/error-audit.log/usage_ledger.jsonl. Verifica che filtri e ordinamento funzionino e che non ci siano crash con file vuoti o ruotati."

---

## Riepilogo
- **Unità totali**: 11 (U-GW1, U-GW2, U-GW3, U-GW4 opt, U-TUI5, U-TUI1, U-TUI2, U-TUI3, U-TUI4, U-INT) → 10 concrete + 1 opzionale.
- **Parallele**: Wave1 = {U-GW1, U-GW2, U-GW3, U-TUI5, U-TUI1} tutte in parallelo; Wave2 = {U-TUI2, U-TUI3, U-TUI4} in parallelo (dopo Wave1); U-GW4 opzionale parallelo a Wave2; Wave3 seriale finale.
- **Endpoint nuovi proposti**: `GET /admin/logs/calls`, `GET /admin/logs/errors`, `GET /admin/insights/leaderboard` (riusa `_insights_aggregate` + dati esistenti).
- **Decisione**: **TUI legge SOLO via nuovi endpoint HTTP** (no lettura diretta di `var/`): coerenza con `gateway_client.py`, isolamento docker, dati strutturati, auth master-key. Live = polling `set_interval`+`since`.
