# Operations

## Deploy

The gateway is a single container built from this repo; only `var/` is
bind-mounted, so **code changes require a rebuild** (not just a restart):

```bash
git fetch origin && git merge --ff-only origin/master
docker compose build scrocco-llm
docker compose up -d --no-build --force-recreate scrocco-llm
```

Compose profiles (see CONFIGURATION.md): minimal, `-f docker-compose.stt.yml`,
`-f docker-compose.web.yml`, or `-f docker-compose.full.yml`. Pick the profile
that matches what the host was running before; `var/` content (CSV, policy,
state) is preserved across rebuilds.

Health is `GET /healthz` (200 when the process is up and serving); `GET
/health/liveliness` is a trivial liveness probe. `/metrics` exposes Prometheus
text metrics.

## Capacity & overload

- **One process by default, N workers on demand** (`GATEWAY_WORKERS`, see
  *Multi-worker* below). Each process keeps its event loop free:
- **Liveness healthcheck** (`python -m app.liveness`): an event-loop task
  touches a heartbeat file every 5s; the Docker `HEALTHCHECK` only reads its
  age (no HTTP), so a busy-but-alive gateway stays *healthy* and is not
  restarted cold under load. It turns unhealthy only if the loop has been
  stuck for `GATEWAY_HEARTBEAT_MAX_AGE` (120s). `GATEWAY_HEARTBEAT_FILE`
  overrides the path. `/healthz` is unchanged for external monitoring.
- **Admission gate** (`app/admission.py`, policy `admission_*`): at most
  `admission_max_inflight` LLM requests (`POST /v1/*`) in flight, streams
  counted until they end, and at most `admission_max_streams` streams.
  Excess requests **wait** in a queue; only after
  `admission_queue_timeout_sec` do they get `503 gateway_busy` +
  `Retry-After: 2`. Health, metrics and admin never queue. `0` = no cap.
- **CPU-bound work off the loop** (`app/offload.py`): base64 of images and
  audio above 256 KiB, the image store writes and the audio transcoding of
  STT-in-chat run on threads.
- **Metrics to watch**: `nx_event_loop_lag_ms` (loop responsiveness),
  `nx_admission_inflight` / `nx_admission_streams` / `nx_admission_waiting`,
  `nx_admission_total{outcome="rejected"}`.

## Multi-worker

`GATEWAY_WORKERS` (default `1`) sets the number of gateway processes. The
container starts through `python -m app.serve`:

- **`1`** — `exec python -m uvicorn app.main:app --host … --port …`: exactly
  the historical single process. Nothing below applies.
- **`N` or `auto`** (`auto` = available cores, capped at 16) — `app.serve`
  becomes a **supervisor**: it binds the port once and shares it with N
  worker processes (the kernel spreads connections), runs the replication
  bus, restarts a worker that exits (backoff 1→30s when crash-looping) or
  whose event loop is stuck (per-worker heartbeat older than
  `GATEWAY_HEARTBEAT_MAX_AGE`), and keeps the container heartbeat alive
  while at least one worker is. `SIGTERM` is forwarded to every worker
  (each drains its own requests); the supervisor waits up to
  `GATEWAY_SHUTDOWN_TIMEOUT` (55s, below the compose `stop_grace_period: 60s`) and then kills stragglers.

The routing rules and the policy are the same as with one process. How the
state stays single-process-equivalent:

| State | How it is kept |
|---|---|
| **Per-session** (sticky, cache holder, warm owner, ctx frontier, per-session slow/go-refund, thought signatures) | every request is served by the **owner worker of its session** (`crc32(session) % N`; session = the same id the gateway already uses: `x-opencode-session` / `x-session-affinity` / `x-session-id`, then `user` / `metadata.session_id`, then the anonymous fingerprint). A worker that receives another worker's session forwards the request over the owner's private unix socket and relays the response byte for byte (streams included, client disconnect propagated). Requests without a session are served where they land. |
| **Global routing** (cooldowns, fail streaks, EMA latency/success, usage and budget windows, in-flight counters, learned rate limits, per-key leases, quarantines, circuit breakers, wake budgets, escalation pins, the session-dep guard "which session used this deployment last", operator commands) | **replicated**: the router's observation/command methods (`app/cluster.py::ROUTER_METHODS`) run on the worker that served the request and are re-run with the same arguments on every other worker over a local bus (sub-millisecond, eventually consistent). Replays are silent (no duplicate logs or metric increments). Time-dependent cooldowns are copied verbatim. If a worker dies, the others drop its in-flight requests and leases. |
| **Joining workers** | a worker that starts or restarts loads the files, then asks the oldest running worker for a **live snapshot** of the global state (including in-flight requests and leases per process). Replication messages that arrive meanwhile are queued and applied after it, skipping the ones the snapshot already contains (per-process sequence numbers). With no other worker up, it starts from the files. |
| **Deployment identities** | unique names (`group__model__<n>`) come from a shuffle of the CSV seeded by the CSV **content**: every worker, and every restart, gets the same unique → key mapping (persisted stats and cooldowns stay attached to the right deployment); editing the CSV reshuffles. This also applies to the single process. |
| **CSV / policy writes** | admin writes (`/admin/deployments*`, `/admin/csv`, `/admin/policy*`, `/admin/backups/restore`, `/admin/profiles/purge`, `/admin/capabilities/seed-from-map`), learned CSV flags and capability auto-learn do read-modify-write under one inter-process lock (`var/.config-write.lock`), then tell the other workers to reload **now** instead of at the next watcher tick. |
| **Unique jobs** (health loop, nightly autoprobe, retired/hot-reload probes, request-triggered autoprobe, key-health retirement) | **worker 0** only. |
| **Files** | `adaptive_stats.json`, `cooldown_state.json`, `key_health.json`: written by worker 0 (the others read `key_health.json` when it changes). `routing_state.wN.json` and `thought_sigs.wN.json`: one per worker (first start falls back to the single-process file). Ledger, repair ledger, journal and learned CSV flags: appended under an inter-process `flock`. `gateway.log` / `error-audit.log`: rotated by worker 0, the others reopen after rotation. |
| **Admission gate** | `admission_max_inflight` / `admission_max_streams` stay **gateway-wide**: each worker enforces `ceil(limit / N)`. Raise them when you add workers. |
| **`/metrics`** | the worker that receives the scrape merges every worker's series with a `worker="i"` label (sum by without `worker` for gateway totals). |

Inspecting a cluster: `GET /admin/cluster` (master) shows the answering
worker, leader flag, replication counters (`published`, `replayed`,
`skipped`, `errors`, `dropped`, `reconnects`) and its own in-flight
requests; `/metrics` exposes the same counters as `nx_cluster_*` gauges per
worker, plus `nx_affinity_fallback_total{owner}` (requests served locally
because the session owner was restarting). A worker that loses the bus and
reconnects asks for a fresh snapshot, keeping its own in-flight requests.
On `SIGTERM` each worker's private socket stops accepting together with
the public one: requests already forwarded finish, new ones fall back to
the worker that received them.

Caveats: `/admin` session views (`?session_id=` or a `session_id` in the
body) are routed to the owner; list views of per-session state show the
receiving worker only. A CSV/policy edited by hand on disk reaches the
workers through the normal hot-reload (`GATEWAY_WATCH_SECONDS`). Changing `GATEWAY_WORKERS`
remaps sessions once (like a restart: sticky/warm state rebuilds).

## Admin API (`/admin/*`, master key required)

The admin API manages everything without editing files or restarting:

| Area | Endpoints |
|---|---|
| Deployments | `GET/POST /admin/deployments`, `PUT/DELETE /admin/deployments/{row_hash}`, `POST /admin/deployments/bulk`, `GET /admin/deployments/expiring`, `GET /admin/deployments/stats`, `POST /admin/deployments/probe`, `POST /admin/deployments/probe/bulk`, `POST /admin/deployments/unretire` |
| Policy | `GET/PATCH /admin/policy`, `GET/PUT /admin/policy/raw` |
| CSV | `GET/PUT /admin/csv` |
| Backups | `GET /admin/backups`, `POST /admin/backups/restore` |
| State / pressure | `GET /admin/state`, `POST /admin/cooldowns/clear`, `POST /admin/pressure/clear`, `POST /admin/pressure/inspect`, `POST /admin/sessions/release`, `GET /admin/sessions`, `GET /admin/sessions/{id}` |
| Warm / drain (F4) | `GET /admin/warm`, `POST /admin/warm/wake`, `POST /admin/hosts/drain`, `POST /admin/hosts/undrain` |
| Diag / reset (F2-4) | `GET /admin/policy/schema`, `GET /admin/keys/soft`, `GET /admin/circuits`, `POST /admin/metrics/reset`, `POST /admin/scores/reset`, `POST /admin/sessions/purge`, `POST /admin/keys/leases/clear` |
| Profiles | `GET /admin/profiles`, `POST /admin/profiles/purge` |
| Insights / stats | `GET /admin/insights`, `/admin/insights/summary`, `/admin/insights/leaderboard`, `/admin/stats/{summary,models,tokens,cache,sessions,deployments,providers}`, `GET /admin/providers/health` |
| Diagnostics | `GET /admin/logs/calls`, `/admin/logs/errors`, `GET /admin/repairs`, `GET /admin/history`, `GET /admin/tuning`, `GET /admin/guide` |
| Misc | `POST /admin/reload`, `POST /admin/playground`, `POST /admin/capabilities/audit`, `POST /admin/capabilities/seed-from-map`, `GET`/`DELETE /admin/replay` (master-only, not in OpenAPI) |
| MCP config | `GET /admin/mcp/config/tools`, `POST /admin/mcp/config/execute`, `POST /admin/mcp/config/call` |

> Admin `POST` endpoints expect a JSON body: send `-d '{}'` with
> `Content-Type: application/json`, otherwise you get "invalid JSON body".

Public compat endpoints: `GET /v1/models`, `/v1/models/{id}`, `/api/tags`,
`/api/show`, `/api/version`. Chat is `POST /v1/chat/completions`; media are
`/v1/images/generations`, `/v1/images/edits` (+ `GET /v1/images/files/{id}`,
download pubblico delle immagini con `url` del gateway),
`/v1/audio/{speech,transcriptions,translations}`,
`/v1/videos/generations` (+ `/{job_id}` and `/{job_id}/content`).

## Runbooks

**Add a provider/model** — append a CSV row (see CONFIGURATION.md); the watcher
hot-reloads within `GATEWAY_WATCH_SECONDS`. Or `POST /admin/deployments`.

**Rotate client keys** — `python scripts/gen_client_keys.py --profiles a,b
--write`, then update clients; or set `client_keys` manually and reload.

**Clear cooldowns** — `POST /admin/cooldowns/clear` (`{}` for all, or
`{"unique": "..."}`). Pressure (cooldowns + penalties + failure windows):
`POST /admin/pressure/clear`.

**Release sticky sessions** — `POST /admin/sessions/release` (`{}` or
`{"session_id": "..."}`).

**Un-retire a key** — `POST /admin/deployments/unretire` `{"unique": "..."}`
(clears key health + streak), or a successful probe.

**Restore config** — `GET /admin/backups` then
`POST /admin/backups/restore`; policy backups are also written automatically
under `var/backups/`.

**Full reload** — `POST /admin/reload` re-reads CSV + policy.

## Observability & TUI

- Logs: `var/gateway.log` (rotating) with one `[summary]` line per request
  (`tries`, `fb`, `dur_ms`, `ttfb_ms`, `via`, …) and `var/error-audit.log`.
- Per-request debug: set `GATEWAY_DEBUG_SNIFF=1` to dump input/output to
  `var/debug-sniff.log`; `SNIFF_HEADERS=1` logs the headers actually sent
  upstream (opencode identity, spoof, session).
- Metrics: `/metrics` serves BOTH the observability HTTP collector
  (`http_*`/`router_*` request metrics) AND the `nx_*` counters/gauges
  (cooldowns active, canary, hedges, opencode headers, content-string, …) —
  unified in Phase 1 (no shadowed route, single owner).
- TUI: `requirements-tui.txt` provides a textual dashboard/console
  (`tui/`). It reads `GATEWAY_URL` (default `http://127.0.0.1:4001`) and the
  master key.

## Troubleshooting

| Symptom | Likely cause / check |
|---|---|
| `401` | missing/invalid key. In production, `sk-<profile>` is rejected by design — use an explicit `client_keys` entry. |
| `403` on `opencode.ai` | client is not opencode and `OPENCODE_SPOOF_HEADERS` is off; or the spoofed session is not in the native `ses_...` format (the gateway normalises it, but check `SNIFF_HEADERS=1`). |
| `503` `gateway_busy` | the admission queue timed out: the process is saturated. Check `nx_admission_*` and `nx_event_loop_lag_ms`; raise `admission_*` only if the loop lag stays low. |
| `503` with `Retry-After` | the ladder is exhausted (all candidates cooled/failed). Inspect `/admin/state` (`cooldowns_active`) and `/admin/deployments/stats`. |
| Timeouts / high TTFB | a cold pool or slow free provider; check `[summary]` `ttfb_ms`/`via` and `/admin/providers/health`. |
| All requests go to one provider | a warm/sticky session: check `/admin/sessions` and the `[warm]`/`[cache]` log lines. |
| Zen used too early (or not at all) | review the opencode switches in CONFIGURATION.md and the zen block in ROUTING.md. |
| High memory / leak suspicion | check `coalesce_cache_max`, ledger rotation (`LEDGER_*`), and restart the container; state is on disk. |
