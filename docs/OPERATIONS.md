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

## Admin API (`/admin/*`, master key required)

The admin API manages everything without editing files or restarting:

| Area | Endpoints |
|---|---|
| Deployments | `GET/POST /admin/deployments`, `PUT/DELETE /admin/deployments/{row_hash}`, `POST /admin/deployments/bulk`, `GET /admin/deployments/expiring`, `GET /admin/deployments/stats`, `POST /admin/deployments/probe`, `POST /admin/deployments/probe/bulk`, `POST /admin/deployments/unretire` |
| Policy | `GET/PATCH /admin/policy`, `GET/PUT /admin/policy/raw` |
| CSV | `GET/PUT /admin/csv` |
| Backups | `GET /admin/backups`, `POST /admin/backups/restore` |
| State / pressure | `GET /admin/state`, `POST /admin/cooldowns/clear`, `POST /admin/pressure/clear`, `POST /admin/pressure/inspect`, `POST /admin/sessions/release`, `GET /admin/sessions`, `GET /admin/sessions/{id}` |
| Profiles | `GET /admin/profiles`, `POST /admin/profiles/purge` |
| Insights / stats | `GET /admin/insights`, `/admin/insights/summary`, `/admin/insights/leaderboard`, `/admin/stats/{summary,models,tokens,cache,sessions,deployments,providers}`, `GET /admin/providers/health` |
| Diagnostics | `GET /admin/logs/calls`, `/admin/logs/errors`, `GET /admin/repairs`, `GET /admin/history`, `GET /admin/tuning`, `GET /admin/guide` |
| Misc | `POST /admin/reload`, `POST /admin/playground`, `POST /admin/capabilities/audit`, `POST /admin/capabilities/seed-from-map` |
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
- Metrics: `/metrics` (`nx_*` counters/gauges: cooldowns active, canary,
  hedges, opencode headers, content-string, …).
- TUI: `requirements-tui.txt` provides a textual dashboard/console
  (`tui/`). It reads `GATEWAY_URL` (default `http://127.0.0.1:4001`) and the
  master key.

## Troubleshooting

| Symptom | Likely cause / check |
|---|---|
| `401` | missing/invalid key. In production, `sk-<profile>` is rejected by design — use an explicit `client_keys` entry. |
| `403` on `opencode.ai` | client is not opencode and `OPENCODE_SPOOF_HEADERS` is off; or the spoofed session is not in the native `ses_...` format (the gateway normalises it, but check `SNIFF_HEADERS=1`). |
| `503` with `Retry-After` | the ladder is exhausted (all candidates cooled/failed). Inspect `/admin/state` (`cooldowns_active`) and `/admin/deployments/stats`. |
| Timeouts / high TTFB | a cold pool or slow free provider; check `[summary]` `ttfb_ms`/`via` and `/admin/providers/health`. |
| All requests go to one provider | a warm/sticky session: check `/admin/sessions` and the `[warm]`/`[cache]` log lines. |
| Zen used too early (or not at all) | review the opencode switches in CONFIGURATION.md and the zen block in ROUTING.md. |
| High memory / leak suspicion | check `coalesce_cache_max`, ledger rotation (`LEDGER_*`), and restart the container; state is on disk. |
