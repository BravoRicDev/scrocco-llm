# Architecture

`scrocco-llm` is a FastAPI app (`app.main:app`, default `:4001`) — one
process by default, or N worker processes behind `python -m app.serve` with
`GATEWAY_WORKERS` (session affinity + replicated routing state, see
OPERATIONS.md *Multi-worker*) — that exposes an OpenAI-compatible surface and forwards each request to one of
many upstream deployments, chosen at runtime by `app/router.py`.

The design goal is **one endpoint, many free/paid providers, no client changes**:
clients send `model = "<group>"` (e.g. `scrocco-llm-<profile>-200k`) and the
gateway decides which concrete deployment (provider + key + endpoint + model)
serves the request, retries across alternatives, keeps hot sessions warm and
cools down failing ones.

## Components

| Module | Responsibility |
|---|---|
| `app/main.py` | App assembly only: logging, shared runtime state, lifespan (load/save state, cluster join, background tasks), middleware, error handlers, route registration |
| `app/chat_completions.py` | `POST /v1/chat/completions`: method object `_ChatCompletion`, whose `run()` reads as the pipeline (read payload → auth → session/needs → context estimate → group → first deployment → preprocessing/compaction → stream or non-stream) |
| `app/chat_stream.py`, `chat_relay.py`, `chat_hedge.py`, `chat_media.py` | Streaming engine with fallback (`_StreamFallback`), client SSE relay (`_ClientRelay`), first-content hedge race, image cap + STT bridge |
| `app/logsetup.py`, `runtime_persistence.py` | Process logging (console + files); state persistence, watcher/hot-reload and `apply_policy` (policy → modules, one list for startup, reload and admin writes) |
| `app/state.py` | Process-wide runtime state (config, policy, router, forwarder, auth, ledger, keyhealth, state-file paths, coalescing/video/probe registries). Created by `app/main.py` at import; every other module reads it as `gw_state.<name>` and tests patch it there |
| `app/router.py` | Core routing engine: `initial_pick`, `pick_deployment`, `_walk_ladder_resilient`, `_walk_chain`, cooldowns, sticky sessions, reputation, `fallback_after` |
| `app/routing/*.py` | Router mixins: warm pool/borrows, canary/hedge, sticky sessions, circuit breakers, usage windows, cooldowns, failure handling, per-key leases, connection draining |
| `app/forwarder.py` | Upstream HTTP: `call`, `stream_response`, `call_with_fallback`, token clamping, session/opencode headers, cooldown classification |
| `app/admin.py` | `/admin/*` API: deployments, policy, CSV, backups, probes, stats, sessions |
| `app/admin_mcp.py` | MCP config tools over the admin API (`/admin/mcp/config/*`) |
| `app/policy_store.py`, `caplearn.py`, `config_writes.py` | Atomic validated `gateway.yaml` writes; capability auto-learn; single writer lock for CSV/policy |
| `app/policy.py` | Runtime policy model (`var/gateway.yaml`), hot-reloadable |
| `app/config.py` | Loads `var/keys_rotation.csv` into deployment groups; validation and hot-reload |
| `app/auth.py` | Three-level auth: master (admin) → explicit client keys → deterministic `sk-<profile>` (dev only) |
| `app/opencode_gate.py` | Per-client gating of `opencode.ai` upstreams (zen/go), spoof and caution semantics |
| `app/caution.py` | *Generic* caution: disables background probes/health/nightly |
| `app/protocols.py` | Protocol adapters: OpenAI Chat ↔ Responses / Anthropic Messages / Google Generative |
| `app/capabilities.py` | Required capabilities detection and capability groups (vision/audio/...) |
| `app/histnorm.py`, `ctxcompact.py`, `sampling.py`, `schemaout.py`, `texttoolparse.py`, `toolrepair.py`, `fakecall.py`, `qc.py` | Response/request hygiene: history normalisation, context compaction, sampling defaults, structured output, text→tool-call parsing and repair, quality-control/sanity |
| `app/thought_sig.py` | Gemini thought-signature sidecar cache |
| `app/health.py`, `autoprobe.py`, `keyhealth.py` | Proactive health, autoprobe, dead-key lifecycle |
| `app/metrics.py`, `observability.py`, `sniff.py`, `ledger.py`, `journal.py`, `repairlog.py`, `logview.py` | Prometheus metrics, trace IDs, debug sniffing, usage ledger, CSV journal/backups, repair audit, log parsing |
| `app/atomic_store.py`, `csv_store.py`, `csvlearn.py` | Atomic persistence; stable CSV row ids; per-deployment flag learning |
| `app/serve.py`, `cluster.py`, `affinity.py` | Entry point (single process or supervisor + N workers), replication of global routing observations between workers, per-session routing to the owner worker |
| `app/admission.py`, `liveness.py`, `offload.py` | Admission gate, event-loop heartbeat for the Docker healthcheck, CPU-bound work on threads |
| `app/effort.py`, `session_ctx.py`, `constants.py`, `errors.py`, `provider_models.py`, `logboot.py` | Per-request effort, async-safe session context, shared constants/errors, provider model cache, rolling-window bootstrap |

## Request lifecycle — `POST /v1/chat/completions`

### Streaming

1. **Ingress & auth** — `chat_completions._ChatCompletion` (`app/chat_completions.py`) parses the body,
   sets per-request effort/flags, authenticates (`auth.AuthManager.authenticate`,
   `authorize_model`), and derives the session id (`_session_id`).
2. **Client gating** — `chat_helpers._set_opencode_gate` calls
   `opencode_gate.set_allow_opencode_zen` / `set_spoofing_request`; generic
   caution comes from `caution.background_cautious_enabled`.
3. **Group/profile resolution** — `policy.canonicalize`, capability need
   (`capabilities.required_caps | {"text"}`), token estimate
   (`router.estimate_tokens`), then `router.resolve_group_for_request`
   (may climb context dims; fails fast on context overflow).
4. **Deployment selection** — `router.initial_pick`:
   warm pool first (`routing/warm._warm_pool`), sticky deployment, then
   `pick_deployment` / `_walk_ladder_resilient` (dims ladder) or `_walk_chain`
   (capability chains). Every candidate passes `opencode_gate.dep_usable`,
   cooldown and circuit-breaker checks.
5. **Forwarding with fallback** — `chat_stream._stream_with_fallback` (delegates
   to the method object `chat_stream._StreamFallback`, whose `run()` reads as the
   attempt loop; the client-side SSE relay is `chat_relay._ClientRelay`):
   `_peek_stream` streams one attempt upstream via `forwarder.stream_response`;
   speculative helpers (warm refill, slow-race, hedge canaries) may run in
   parallel when enabled. On failure it asks the router for the next candidate
   (`fallback_after` / `fallback_next`) and repeats, marking failures with
   `router.mark_failed` (linear cooldown + jitter, quota-aware).
6. **Persistence/telemetry** — on completion `_emit_summary` writes the ledger
   (`var/usage_ledger.jsonl`) and the `[summary]` log line; metrics
   (`/metrics`), debug sniff (`sniff.py`), thought signatures, adaptive stats,
   cooldown/routing state and key health are updated by the watcher/lifespan.

### Non-streaming

Same stages 1–4, then `main._forward_coalesced` (in-flight coalescing + short
response cache) → `forwarder.call_with_fallback` (method object
`forwarder._NonStreamFallback`) → per-attempt `forwarder.call`
(with `clamp_max_tokens` and adaptive timeout) → `router.fallback_after` on
failure → 503 with `Retry-After` only once the ladder is exhausted.

## Persistence map (`var/`, bind-mounted)

| File | Writer | Content |
|---|---|---|
| `gateway.log`, `error-audit.log` | `logsetup.py` (RotatingFileHandler) | runtime logs, `[summary]` lines, error audit |
| `keys_rotation.csv` | `csv_store.py` / admin / `csvlearn.py` | source of truth for deployments/keys |
| `gateway.yaml` | admin policy API (backups in `var/backups/`) | hot-reloaded policy |
| `adaptive_stats.json` | `runtime_persistence.py` (atomic) | per-deployment EMA/latency stats |
| `cooldown_state.json` | `router` | active cooldowns |
| `routing_state.json` | `router` | sticky sessions / warm owners |
| `usage_ledger.jsonl` | `ledger.py` | usage & estimated cost |
| `journal.jsonl` | `journal.py` | CSV writes + backups |
| `keyhealth.json` | `keyhealth.py` | dead-key lifecycle |
| `thought_sigs.json` | `thought_sig.py` | Gemini thought-signature cache |
| `debug-sniff.log` | `sniff.py` | request/response dumps when enabled |

## Extension points

- **Add a provider/model**: append a row to `var/keys_rotation.csv` (see
  CONFIGURATION.md) — no restart, the CSV watcher hot-reloads; or use
  `POST /admin/deployments`.
- **Tune behaviour**: `var/gateway.yaml` (hot-reloaded) — see CONFIGURATION.md;
  or `PATCH /admin/policy`.
- **Add a protocol**: extend `app/protocols.py` (translate request/response and
  `apply_auth`) and set `api_style` on the CSV row.
- **New routing stage**: add a small mixin under `app/routing/` and append it to
  the `Router(...)` bases, keeping the interface explicit (see DEVELOPMENT.md).
