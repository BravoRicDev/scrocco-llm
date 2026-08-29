# scrocco-web — Agent guide

`scrocco-web` is a web panel + JSON API + MCP server that sits in front of the
`scrocco-llm` gateway. It holds the gateway master key server-side; agents
authenticate to `scrocco-web` with their own token and never see the master key.

## 1. Authentication

Send `Authorization: Bearer <token>` on every request. `<token>` is one of:

- **Agent JWT** — obtained with `scripts/agent-login.mjs` (email + password),
  valid 7 days, carries `agent: true`. Also available via
  `POST /api/agent/login` `{email, password}` → `{token, user}`.
- **API token** (`agtok_…`) — long-lived, created in the UI at
  `/admin/api-tokens` or via `scripts/agent-login.mjs`. Stored as SHA-256 only;
  the clear value is shown **once**. Revoke individually at `/admin/api-tokens`
  or `DELETE /api/agent/api-tokens/:id`. Allowed lifetimes: 30/60/90/120/180/365
  days (default 120). Only the prefix (`agtok_xxxxxx…`) is ever displayed.

Missing/invalid token → `401 {"error":{"message":"…"}}`.

## 2. RBAC

Three roles: `admin` (everything), `operator` (deployment CRUD/bulk/probe,
system actions, capabilities seed, playground), `viewer` (read only).
`PATCH /api/v1/policy`, CSV write, users and alerts are **admin only**.
A forbidden action → `403`.

## 3. Endpoints — `/api/v1`

### Read

| Method | Path | Query | RBAC |
|---|---|---|---|
| GET | `/api/v1/deployments` | `profile`, `q` | read |
| GET | `/api/v1/deployments/:id` | — | read |
| GET | `/api/v1/deployments/expiring` | `days` | read |
| GET | `/api/v1/profiles` | — | read |
| GET | `/api/v1/policy` | — | read (masked keys, no raw `configured`) |
| GET | `/api/v1/state` | — | read |
| GET | `/api/v1/history` | `limit` (≤100) | read |
| GET | `/api/v1/insights` | `days`, `group_by` | read |
| GET | `/api/v1/insights/summary` | — | read |
| GET | `/api/v1/insights/leaderboard` | `window`, `sort`, `order`, `profile` | read |
| GET | `/api/v1/bootstrap` | — | read (text → `{text}`) |
| GET | `/api/v1/bootstrap/status` | — | read |
| GET | `/api/v1/bootstrap/providers` | — | read |
| GET | `/api/v1/guide` | — | read (markdown → `{text}`) |

### Write

| Method | Path | Body | RBAC |
|---|---|---|---|
| POST | `/api/v1/deployments` | `profile, modello, endpoint, data, key, context` (+ `provider, priority, caps`) | create |
| PUT | `/api/v1/deployments/:id` | any subset (empty `key` = keep) | update |
| DELETE | `/api/v1/deployments/:id` | — | delete |
| POST | `/api/v1/deployments/bulk` | `{operations:[{action:create|update|delete, …}]}` (1..50, atomic) | bulk |
| POST | `/api/v1/deployments/probe` | `{id|unique, force?}` | probe |
| POST | `/api/v1/deployments/probe/bulk` | `{filter, force?}` | probe |
| POST | `/api/v1/deployments/unretire` | `{unique}` | unretire |
| POST | `/api/v1/system/reload` | — | system |
| POST | `/api/v1/system/cooldowns/clear` | `{unique?}` | system |
| POST | `/api/v1/system/sessions/release` | `{session_id?}` | system |
| POST | `/api/v1/capabilities/seed` | `{dry_run?}` | seed |
| POST | `/api/v1/capabilities/audit` | — | audit |
| PATCH | `/api/v1/policy` | partial policy object | **admin** |

Full machine-readable spec: `GET /api/v1/openapi.json` · human page: `/api/v1/docs`.

## 4. Examples

```bash
BASE=http://127.0.0.1:3000
TOK=agtok_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# list
curl -s -H "Authorization: Bearer $TOK" "$BASE/api/v1/deployments?profile=mioaruba"

# create
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"profile":"mioaruba","modello":"deepseek/deepseek-chat","endpoint":"https://openrouter.ai/api/v1","data":"free","key":"sk-or-…","context":200}' \
  "$BASE/api/v1/deployments"

# bulk
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"operations":[{"action":"delete","id":"…"},{"action":"update","id":"…","priority":9}]}' \
  "$BASE/api/v1/deployments/bulk"

# probe one (uses upstream quota — one shot, never in a loop)
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"unique":"…"}' "$BASE/api/v1/deployments/probe"

# policy patch (admin only)
curl -s -X PATCH -H "Authorization: Bearer $ADMIN_TOK" -H 'Content-Type: application/json' \
  -d '{"step_up_pct":25}' "$BASE/api/v1/policy"
```

## 5. Errors

All errors are `{"error":{"message":"…"}}` with the HTTP status:
`400` validation, `401` no/invalid token, `403` RBAC, `404` not found,
`503` gateway unreachable.

## 6. MCP

`POST /api/mcp` — Streamable HTTP MCP server exposing the `/api/v1` surface as
tools (`deploy_list`, `deploy_create`, `policy_get`, `system_reload`,
`playground_run`, …). Requires an agent token (JWT-agent or `agtok_`). The
server proxies each tool call to the corresponding `/api/v1` route over
loopback, forwarding your `Authorization` header (so RBAC still applies).
`GET`/`DELETE /api/mcp` → `405`.

## 7. Rate limits & good practice

- `/api/mcp` and login are rate-limited per IP (loopback exempt).
- **Probe is expensive**: it burns free-tier quota. Probe once, read the
  cached result afterwards; never poll it in a loop.
- Prefer `/api/v1/state` and `/api/v1/insights` for monitoring; they are cheap.
- Bulk over many single writes.
- Treat `agtok_` like a password: store it in `~/.scrocco-web/agent.token`
  (mode 600), rotate/revoke if leaked.
