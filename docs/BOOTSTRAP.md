# scrocco-llm · Zero-to-running bootstrap

The gateway ships a **live version of this playbook**:
`curl -s localhost:4001/bootstrap` (public, no auth).
This file is the static reference.

## Phase 1 · Start

```bash
git clone <repo> && cd scrocco-llm
cp var/keys_rotation.csv.example var/keys_rotation.csv   # if absent
docker compose up -d
curl -s localhost:4001/healthz          # -> {"status":"ok",...}
```

Set a real master key first: edit `.env.gateway`
(`GATEWAY_MASTER_KEY=sk-master-...`) — never ship the default.

## Phase 2 · Insert keys

Sign up at the providers listed by `GET /bootstrap/providers`,
then add rows via the admin API:

```bash
curl -X POST localhost:4001/admin/deployments/bulk \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"rows":[{"modello":"openai/gpt-oss-120b","provider":"groq",
        "endpoint":"https://api.groq.com/openai/v1","data":"free",
        "context":128,"max_input":8000,"priority":0,
        "chiave":"gsk_XXXX","caps":"text"}]}'
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
