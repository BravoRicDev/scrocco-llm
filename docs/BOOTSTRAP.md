# scrocco-llm · Zero-to-running bootstrap

The gateway ships a **live version of this playbook**:
`curl -s localhost:4001/bootstrap` (public, no auth).
This file is the static reference.

## Phase 1 · Start

```bash
git clone <repo> && cd scrocco-llm
cp .env.gateway.example .env.gateway                     # required
cp var/keys_rotation.csv.example var/keys_rotation.csv   # if absent
docker compose up -d                                     # gateway only (minimal profile)
curl -s localhost:4001/healthz          # -> {"status":"ok",...}
```

Set a real master key first: edit `.env.gateway`
(`GATEWAY_MASTER_KEY=sk-master-...`) — never ship the default.
Optional profiles: add local STT with
`docker compose -f docker-compose.yml -f docker-compose.stt.yml up -d`, or the
full stack with `docker compose -f docker-compose.full.yml up -d`.

## Phase 2 · Insert keys

Sign up at the providers listed by `GET /bootstrap/providers`,
then add rows via the admin API. `POST /admin/deployments/bulk` is
**atomic**: one invalid operation and the whole batch is rejected with
HTTP 400 writing **zero** rows.

Required fields of a `create` operation:
`profile`, `modello`, `endpoint`, `data`, `key` (plus an integer
`context` >= 0).

- `profile` — **required**: the tenant/namespace name. It creates the
  deterministic client key `sk-<profile>` used in Phase 4.
- `modello` — the exact upstream model id (**not** `model`; `model` is
  silently ignored and the request is rejected as missing `modello`).
- `key` — a **real** provider key of **at least 8 characters**. Short
  placeholders (`gsk_XXXX`) are rejected by the CSV store validation.

```bash
curl -X POST localhost:4001/admin/deployments/bulk \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"operations":[{"action":"create","profile":"myprofile","modello":"openai/gpt-oss-120b","provider":"groq",
        "endpoint":"https://api.groq.com/openai/v1","data":"free",
        "context":128,"max_input":8000,"priority":0,
        "key":"gsk_XXXX","caps":"text"}]}'
```

CSV hot-reloads atomically within ~5s. No restarts, ever.

## Phase 3 · Validate once

One real call per key (`max_tokens=1`), cached forever:

```bash
curl -X POST localhost:4001/admin/deployments/probe/bulk \
  -H "Authorization: Bearer $MASTER_KEY"
```

## Phase 4 · Smoke test

```bash
curl localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-myprofile" \
  -H "Content-Type: application/json" \
  -d '{"model":"scrocco-llm-myprofile","messages":[{"role":"user","content":"ping"}]}'
```

Live gap analysis at any time: `GET /bootstrap/status`.
Day-2 protocol: [AGENT.md](AGENT.md) or `GET /admin/guide`.
