# Configuration

Two files drive the gateway, both under `var/` and both hot-reloaded:

- `var/keys_rotation.csv` — **what** can serve (deployments, keys, metadata).
- `var/gateway.yaml` — **how** it behaves (routing policy).

Start from the `.example` files:
`cp var/keys_rotation.csv.example var/keys_rotation.csv` and
`cp var/gateway.yaml.example var/gateway.yaml`.

## Environment variables

Gateway (read from `.env.gateway` via compose):

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY_MASTER_KEY` | — (required in prod) | master/admin key; weak values are rejected when `GATEWAY_ENV=production` |
| `GATEWAY_ENV` | `development` | `production`/`prod` disables deterministic `sk-<profile>` keys and enables fail-fast startup checks |
| `GATEWAY_HOST` / `GATEWAY_PORT` | `127.0.0.1` / `4001` | bind address/port (Docker sets host `0.0.0.0`) |
| `GATEWAY_CSV` | `var/keys_rotation.csv` | deployments CSV path |
| `GATEWAY_POLICY` | `var/gateway.yaml` | policy path |
| `GATEWAY_WATCH_SECONDS` | `5` | hot-reload poll interval |
| `GATEWAY_PERSIST_STATS` / `GATEWAY_PERSIST_ROUTING` | `1` | persist stats / routing state |
| `GATEWAY_MAX_FALLBACK_TRIES` | `128` | hard cap on attempts per request |
| `GATEWAY_LOG_MAX_MB` / `_BACKUPS` / `_FILE` / `_ERROR_LOG_FILE` | `20` / `5` / `var/gateway.log` / `var/gateway_error.log` | log rotation |
| `GATEWAY_OBSERVABILITY` / `GATEWAY_JSON_LOGGING` | `1` / `0` | trace IDs + `/metrics`; JSON logs |
| `SNIFF_HEADERS` | — | log upstream/client headers (`[sniff]`) |
| `GATEWAY_DEBUG_SNIFF` | — | dump full request/response to `var/debug-sniff.log` |
| `OPENCODE_SPOOF_HEADERS` | — | allow `opencode.ai` upstreams for non-opencode clients |
| `OPENCODE_CAUTIOUS` | `= spoof` | demote zen to a separate block; warm-owner rule |
| `OPENCODE_GO` | on | enable the paid `opencode-go` bucket |
| `BACKGROUND_CAUTIOUS` | — | disable automatic probes/health/nightly for all providers |
| `OPENROUTER_APP_REFERER` / `_TITLE` | `https://opencode.ai` / `opencode` | OpenRouter attribution for `:free` models |
| `THOUGHT_SIG_FILE` | `var/thought_sigs.json` | Gemini thought-signature cache |
| `LEDGER_MAX_BYTES` / `_KEEP` / `_SUMMARY_MIN_ROWS` | `20M` / `2` / `50000` | ledger rotation |
| `REPAIR_LEDGER_MAX_BYTES` / `_KEEP` | `4M` / `2` | repair ledger rotation |
| `METRICS_LATENCY_MAX` | `512` | latency histogram buckets |

Operator scripts:

| Variable | Default | Used by |
|---|---|---|
| `SCROCCO_CSV` | `var/keys_rotation.csv` | `scripts/assign_intelligence_scores.py` |
| `SCROCCO_PROFILE_COL` | `scrocco-llm-example` | `scripts/cf_catalog.py`, `integrate_providers.py`, `probe_opencode.py` |
| `SCROCCO_PROFILE` | `example` | `scripts/probe_opencode.py` |

Web panel (`web/.env`, Node/Express): `DATABASE_URL`, `JWT_SECRET`,
`GATEWAY_MASTER_KEY`, `GATEWAY_URL` (default `http://scrocco-llm:4001`), `PORT`,
`NODE_ENV`, `LOG_LEVEL`, `APP_NAME`, `COOKIE_SECURE`, `SESSION_COOKIE_NAME`,
`JWT_EXPIRES_IN`, `BOOTSTRAP_ADMIN_*`, `SMTP_*`/`EMAIL_FROM`,
`MAGIC_LINK_BASE_URL`, `GATEWAY_MOCK`, `TELEGRAM_BOT_TOKEN`,
`ALERT_POLLER_DISABLED`.

## `var/keys_rotation.csv`

One row per deployment. Columns (header in the `.example`):

| Column | Meaning |
|---|---|
| `commento` | free-text note (often the account email) |
| `modello` | upstream model id |
| `provider` | provider key (used for grouping/bias/quirks) |
| `endpoint` | base URL (without `/chat/completions`) |
| `data` | `free`/`priority`, `fallback`/`paid`, or a day-of-month (renewal) → bucket |
| `context` | context window in **k** tokens (drives `-Nk` dims) |
| `max_input` | usable input tokens cap |
| `priority` | integer preference |
| `scrocco-llm-<profile>` | the API key for that profile (one column per host/profile) |
| `caps` | capabilities: `text,vision,audio,video,...` |
| `alias` | comma-separated callable names (e.g. `gemini,fast`): `model=<alias>` routes to the `-free → -go → -fallback` chain built from the rows carrying that alias |
| `tool_repair` | `aggressive`/`safe`/`off` |
| `model_preference` | bias `-100..100` |
| `media_defer` | defer media to another deployment |
| `multimodal_last_resort` | allow as last resort for multimodal |
| `order` | integer tier (lower = earlier); zen is demoted at runtime under caution |
| `enabled` | enable/disable the row |
| `hold_until_finish` | never deliver a truncated answer from this dep |
| `api_style` | `chat` / `responses` / `messages` / `google` (protocol adapter) |
| `thinking_replay` | replay Gemini thinking signatures |

Bucket derivation (`config._classify`): `data` → `priority`/`free`/`fallback`;
a day number means "future renewal" and becomes `zen` (provider contains `zen`)
or `go` (everything else). Free rows form the `-Nk` dimension groups; the others
form the `-go` and `-fallback` buckets.

## `var/gateway.yaml` (policy)

Every field is optional; defaults live in `app/policy.py`. Groups:

- **Routing**: `adaptive_pick`, `recency_halflife`, `latency_ref`,
  `latency_rotate_threshold_ms`, `soft_slow_*`, `ctx_bucket_edges`,
  `ttft_rate_*`, `slow_*`, `effort_*`, `scoring_weights`,
  `provider_bias_normalization`.
- **Warm / sessions**: `sticky_ttl`, `session_dep_guard_*`, `warm_pool_*`,
  `warm_refill_*`, `warm_borrow_*`, `warm_pick_fastest`,
  `anon_session_fingerprint`, `cache_aware_*`.
- **Canary / hedge**: `canary_warm_last`, `go_preferred_models`,
  `stream_hedge_*`, `slow_race_*`, `slow_canary_after_ms`, `hunt_*`,
  `cold_spread_pct`. Il canary lento ha un timing **proprio**
  (`slow_canary_after_ms`, default 15s), separato dal **flag lento** che
  marca il deployment "lento per la sessione" (demozione warm + grant `-go`)
  a `stream_slow_race_after_ms` / `nonstream_slow_race_after_ms` (default
  45s); `0` = accoppiato (storico: canary e flag insieme alla soglia gara).
- **Cooldown / ladder**: `cooldown_sec`, `mode`, `base`/`linear_mult`,
  `jitter_*`, `probe_*`, `autoprobe_*`, `stale_retry`, `ladder_*`,
  `chronic_*`, `model_circuit_*`.
- **Budget / rate hints**: `budget_guard`, `retry_after_min_sec`,
  `retry_after_floor_by_provider`, `rate_hint_*`, `key_soft_429`, `quota_*`.
- **Stream / hold**: `stream_stall_sec`, `stall_ttft_*`, `qc_json.*`,
  `hold_until_finish`, `parachute` (sotto hold il paracadute `-go`/`-fallback`
  consegna il buffer, mai byte live).
- **Capabilities / media**: `capability_routing_enabled`, `model_capabilities`
  (token: `text,vision,video,audio,image_gen,image_edit,image_multi_ref,tools,tts,stt,video_gen`),
  `capabilities_default`, `image_token_estimate`, `images_chat_fallback`,
  `image_refs_hard_max`, `cap_auto_learn`,
  `cap_groups_enabled`, `multimodal_last_resort`, `gen_same_model`.
- **Images store**: blocco `images.*` — `store_enabled`, `store_ttl_sec`,
  `store_max_items`, `store_max_bytes`, `url_base` (base URL pubblico del
  download; vuoto = derivata dalla request), `mirror_remote`,
  `remote_timeout_sec`, `remote_max_bytes`. Ogni immagine di
  `/v1/images/*` torna con `url` (del gateway) **e** `b64_json`; l'endpoint
  pubblico `GET /v1/images/files/{id}` serve i byte.
- **Rimborso latenza**: blocco `go_refund.*` — `enabled` (kill-switch),
  `pct` (% dei turni totali della sessione, default 20), `min_turns` (default
  1), `max_turns` (default 5), `trigger_ms` (soglia assoluta di regalo in ms,
  default 20000; `<= 0` disabilita il trigger). Quando una risposta supera
  **`trigger_ms`** la sessione riceve
  `clamp(pct%·turni_totali, min_turns, max_turns)` turni serviti sul bucket
  `-go` del profilo, come se il client avesse chiamato
  `scrocco-llm-<profilo>-go`. La soglia è **indipendente** dal floor "lento"
  della warm (`slow_latency_abs_floor_ms`, default 45s): un deployment che
  serve in ~32s regala turni `-go` ma **non** viene demoto, quindi resta
  warm/holder e viene ritrovato al ritorno dai turni regalo (cache calda).
  Vale solo per richieste di **testo** che atterrano su un dim (`-Nk`);
  media/cap, `-go`/`-fallback` e i unique espliciti restano invariati. Il
  conteggio dei turni e la deviazione avvengono **all'atterraggio** (scelta del
  gruppo); ladder e selezione sono invariati.
  **Secondo trigger (fallback)**: `fb_enabled` (default `true`) è il kill-switch
  del regalo per i fallback attraversati dalla richiesta; l'importo è
  `turns = clamp(floor(fb_per_fallback · fb), fb_min_turns, fb_max_turns)` con
  `fb_per_fallback` (default 0.5), `fb_min_turns` (1), `fb_max_turns` (3). `fb`
  è il numero già tracciato in `[summary]`/ledger (`tentativi - 1`), quindi
  conta ogni cambio di deployment (chiave gemella dello stesso gruppo inclusa,
  non solo i salti free→`-go`→`-fallback`). Con i default: `fb` 1–3 → 1 turno,
  4–5 → 2, ≥6 → 3. È valutato a risposta **consegnata** sulla sola
  chat/completions servita (esclusi i 503 e immagini/audio/STT/video) e
  accredita solo `go_until`, come il rimborso latenza; `go_refund.enabled` è il
  kill-switch master di entrambi.
- **Bilanciamento `-go`**: blocco `go_balance.*` + scalar `go_stick_ttl_sec`.
  Nei bucket rinnovo (`-go`/`-fallback`) il pick **a freddo** usa come metrica i
  **token di output** consumati nella finestra rolling `window_sec` (default
  18000 = 5h) invece del prefill-24h: `enabled: false` ripristina la metrica
  storica. Con `flat_pool: true` (default) solo il rinnovo di **oggi**
  (`sort_key == 0`) resta tier assoluto e tutti gli altri rinnovi formano un
  **unico pool** bilanciato per consumo reale (i vari abbonamenti si spartiscono
  il carico invece di esaurire un tier alla volta); `flat_pool: false`
  ripristina i tier stretti per `sort_key`. La stickiness sul `-go` (`last_go` e
  cache-holder) è limitata a `go_stick_ttl_sec` (default 600 = 10 min): scaduta,
  la sessione ripesca bilanciando. Tie-break del pool: `model_preference`, poi
  scelta casuale.
- **Fair-share chiavi (cap group)**: blocco `cap_fair_share.*` — `enabled`
  (default `false`), `caps` (lista di capacità, default `[stt]`), `window_sec`
  (default 60). Per le capacità elencate, nei **gruppi primary** (`-C`) la
  chiave non è più scelta dalla reputation (che premia sempre la stessa chiave
  fino al 429, winner-take-all) ma è la chiave col **minor numero di richieste
  nella finestra rolling** `window_sec` (tie-break: richieste in volo,
  `model_preference`, latenza EMA). Serve a distribuire uniformemente il RPM su
  chiavi gemelle dello stesso modello (es. 5 chiavi groq whisper) evitando
  cooldown e latenza inutili. Non tocca `-C-go`/`-C-fallback` né il mondo testo.
- **Other**: `estimate_divisor`, adaptive tuning, `provider_models_ttl`,
  `strip_client_fields` (denylist di campi client-only non standard rimossi dal
  body prima dell'invio, default `["fallback_models"]`),
  `shutdown_drain`, `reputation_decay`, `adaptive_timeout_*`,
  `http_keepalive_pool`, `quirks`, `coalesce_cache_max`, `tool_repair_*`,
  `history_normalize_*`.

Policy can be edited live via `GET/PUT /admin/policy/raw` or
`PATCH /admin/policy` (hot-reload; a backup is written to `var/backups/`).

## Deployment profiles (compose)

| Profile | Command | Services |
|---|---|---|
| Minimal | `docker compose up -d` | `scrocco-llm` |
| STT (local speech) | `-f docker-compose.yml -f docker-compose.stt.yml up -d` | + `speaches`, `speaches-init` |
| Web panel | `-f docker-compose.yml -f docker-compose.web.yml up -d` | + `scrocco-web`, `db` |
| Full | `-f docker-compose.full.yml up -d` | all of the above |

The web profile needs `web/.env`, `DB_PASSWORD` in the project `.env`, an
external volume (`docker volume create scrocco-web_pgdata`) and optionally
`WEB_BIND` for the published address (default `127.0.0.1:9120`).
