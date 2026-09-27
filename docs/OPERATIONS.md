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

- **One process, one event loop — on purpose.** Cooldowns, reputation
  scores, the coalescing cache, usage windows, in-flight counters and probes
  live in memory (`app/state.py`). N uvicorn/gunicorn workers would mean N
  independent copies of that routing state (a key cooled down by worker 1
  picked again by worker 2): adding workers is a correctness problem until
  that state is externalised. Scale by keeping the loop free instead:
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
