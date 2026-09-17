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
  `stream_hedge_*`, `slow_race_*`, `hunt_*`, `cold_spread_pct`.
- **Cooldown / ladder**: `cooldown_sec`, `mode`, `base`/`linear_mult`,
  `jitter_*`, `probe_*`, `autoprobe_*`, `stale_retry`, `ladder_*`,
  `chronic_*`, `model_circuit_*`.
- **Budget / rate hints**: `budget_guard`, `retry_after_min_sec`,
  `retry_after_floor_by_provider`, `rate_hint_*`, `key_soft_429`, `quota_*`.
- **Stream / hold**: `stream_stall_sec`, `stall_ttft_*`, `qc_json.*`,
  `hold_until_finish`, `parachute`.
- **Capabilities / media**: `capability_routing_enabled`, `model_capabilities`,
  `capabilities_default`, `image_token_estimate`, `cap_auto_learn`,
  `cap_groups_enabled`, `multimodal_last_resort`, `gen_same_model`.
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
