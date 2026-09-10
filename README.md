# scrocco-llm

[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-brightgreen.svg)](https://unlicense.org/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-compose%20up-blue.svg)](#quickstart)
[![Tests](https://img.shields.io/badge/tests-421%20passing-brightgreen.svg)](#development)

> 🇮🇹 **Leggi in italiano** — [README.it.md](README.it.md)

**scrocco-llm** is a self-hosted, OpenAI-compatible LLM gateway that pools
many free-tier and paid provider accounts (Groq, OpenRouter, Mistral,
Google AI Studio, NVIDIA NIM, Cloudflare Workers AI...) into one resilient
endpoint with context-aware routing, capability groups and key rotation.
Zero database. One container. Port `4001`.

> **Give this repo to any AI agent** and it can set the service up alone:
> `git clone` → `docker compose up -d` → `curl localhost:4001/bootstrap`.
> See [Agent setup](#agent-setup-self-bootstrap).

---

## Why

A single free API key is fragile: tiny context windows, rate limits, models
that disappear. scrocco-llm routes every request to the **smallest model that
fits** the estimated context across *all* your keys, then fails over along an
escalating ladder with proportional cooldowns — so unattended agents survive
individual account limits instead of dying on the first 429.

## Features

- **Context-aware dims ladder**: estimates prompt tokens (chars / 3) and
  routes to the smallest context group that fits (`-24k` … `-1000k`). The
  dims are *discovered from the CSV* (`context` column), so adding a 256k
  model creates the `-256k` rung automatically. Explicit requests
  (`-200k`, `-1000k`, a full unique) are **floors**: rotation never goes down.
- **Structural capability groups**: `-vision`, `-audio`, `-image_gen`,
  `-video_gen`, `-tts`, `-stt` are separate worlds. A fallback for an image
  request never lands on a text-only model, and vice versa.
- **Chained failover**: free dims → renewal bucket (`-go`) → paid fallback
  (`-fallback`). Cooldown escalation is **linear** (30 min base + 30 min per
  failure in the last 24h, capped at 5h). **Timeouts are penalised 10×**
  because a hung upstream costs real wall-clock time.
- **Resilient ladder**: at most `ladder_skip_after` attempts per context
  group before climbing, stale-cooldown revival, a *chronic parachute* for
  high-failure keys before spending on the paid `-fallback`, and a final
  last-resort pass. A dead bucket never blocks the whole chain for minutes.
- **Escalation winner + pre-pin probe**: when a request climbs out of a dead
  bucket and a higher group serves it, that winner is remembered *per
  requested bucket*. The next request still tries its own bucket, makes one
  extra attempt there, samples up to two random **intermediate** context
  groups (only live candidates), and only then jumps to the remembered
  winner. This catches a group that recovered in the meantime instead of
  blindly re-walking a dead ladder or blindly sticking to the shortcut.
- **Budget guard (no-waste)**: learns per-key limits from observed 429s and
  deprioritises exhausted keys *before* they burn more calls; probe results
  are cached forever (some free tiers count calls, not tokens).
- **Key lifecycle**: persistent disk evidence (`var/key_health.json`); keys
  dead for >7 days are retired from routing — **never deleted** — until a
  successful probe revives them.
- **Adaptive rotation + sticky sessions**: latency EMA + freshness + inflight
  scoring; free and `-go` buckets can pin a session to the same key for
  prompt-cache warmth.
- **Streaming anti-stall**: the stream toward the client starts only after
  *real* content arrives. A per-deployment first-content deadline (default
  4 min) and a total per-request deadline (default 16 min) rotate
  transparently when an upstream hangs; on the final `-go`/`-fallback` rung
  the timeout becomes a parachute and the partial stream is delivered.
- **Media endpoints**: images, async video jobs (submit / poll / content),
  TTS/STT including the local `speaches` sidecar.
- **QC + watchdog**: broken-JSON retry with annotated last-response delivery,
  empty-content sanity, passive stream watchdog, length-truncation aware
  (reasoning tokens eating `max_tokens`).
- **Usage & cost insights**: persistent ledger + `GET /admin/insights`
  (per profile/model/day burn; provider-reported vs estimated costs).
- **Three-tier auth**: master key / deterministic `sk-<profile>` client keys
  / custom overrides.
- **Hot-reload everything**: credentials CSV + policy YAML are re-read
  atomically (~5s). No restarts, ever.
- **Terminal UI** (`./scrocco.sh`) + Prometheus `/metrics`, plus an optional
  web panel under `web/`.

## How routing works (short)

1. **Resolve** the requested model/alias to a group (`-vision`, `-200k`, …).
2. **Estimate** tokens and choose the target rung: the requested one, or the
   smallest dim that fits the estimate (`dims_ladder_floor`).
3. **Pick** a deployment inside the group (adaptive score: latency EMA,
   recency, inflight, sticky affinity; skips cooled/retired keys).
4. **On failure** mark a cooldown and walk the ladder: remaining live
   candidates of the rung → next dims (ascending) → `-go` → stale revival →
   chronic parachute → `-fallback` → last resort.
5. **On success** record an escalation winner if the request was served
   uphill, clear any cooldown, and emit one `[summary]` log line.

## Quickstart

```bash
git clone https://github.com/BravoRicDev/scrocco-llm && cd scrocco-llm
cp var/keys_rotation.csv.example var/keys_rotation.csv   # if missing
docker compose up -d
curl -s localhost:4001/healthz | head -c 60              # -> {"status":"ok"...
```

Then follow the built-in playbook:

```bash
curl -s localhost:4001/bootstrap     # phased setup guide (public, EN)
```

Run without Docker: `./run.sh` (venv + `127.0.0.1:4001`).

### Minimal configuration

`var/keys_rotation.csv` — one row per *(model x key x endpoint)*:

```csv
commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-myteam,caps
you@example.com,openai/gpt-oss-120b,groq,https://api.groq.com/openai/v1,free,128,8000,0,gsk_XXXXXXXXXXXXXXXX,
```

- `data`: `free`/`priority` = first-choice buckets, `paid`/`fallback` = last
  resort, day number 1–31 = monthly renewal ordering inside `-go`
- `context`: kilo-units (128 = 128k window); drives the dims ladder
- `max_input`: soft prompt-token guard for that deployment (0 = no guard)
- `caps`: comma-separated subset of `text,vision,image_gen,video_gen,tts,stt`

Clients call it like OpenAI:

```bash
curl localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-myteam" \
  -H "Content-Type: application/json" \
  -d '{"model":"scrocco-llm-myteam","messages":[{"role":"user","content":"hi"}]}'
```

## Agent setup (self-bootstrap)

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /bootstrap` | none | phased zero-to-running playbook: research providers online → insert keys via admin API → validate once → smoke test |
| `GET /bootstrap/providers` | none | stable facts only: signup URLs, api_base shapes, data-category semantics |
| `GET /bootstrap/status` | none | live gap analysis: missing deployments/caps, dead keys, default master key warning |
| `POST /admin/deployments/probe/bulk` | master | one real call per key (max_tokens=1), success cached persistently |

Day-2 operations live in `GET /admin/guide` (master key) and
[docs/AGENT.md](docs/AGENT.md). First-run recipe: [docs/BOOTSTRAP.md](docs/BOOTSTRAP.md).

## Architecture

```
Client (agents, curl, TUI, web panel...)
        |  Bearer sk-<profile>
        v
+--------------------------------------+
| FastAPI :4001                        |
| auth -> alias -> ctx estimate ->     |
| group resolve -> adaptive pick ->    |
| ladder failover + escalation pin     |
+--------------------------------------+
        |                    ^
        v                    | httpx + fallback chain
+----------------+  +--------+--------+
| var/keys_      |  | Providers       |
| rotation.csv   |  | Groq Mistral    |
| var/gateway.   |  | NVIDIA OpenRouter|
| yaml           |  | Cloudflare Google|
+----------------+  +-----------------+
```

Every module starts with a **bilingual IT/EN docstring** explaining WHAT /
HOW / WHY decisions were made. Read it before changing code.

| File | Role |
|---|---|
| `app/main.py` | HTTP endpoints + request pipeline; streaming anti-stall peek; per-request `[summary]` logs |
| `app/config.py` | credential CSV → dims/capability groups; atomic hot-reload |
| `app/router.py` | adaptive pick, dims ladder, cooldown escalation, chronic parachute, sticky sessions, escalation-winner pin + pre-pin probe, budget guard scoring |
| `app/forwarder.py` | all upstream HTTP; precise error taxonomy (incl. timeout) → correct rotation; probe with persistent cache |
| `app/qc.py` | JSON QC / sanity / watchdog; D3 annotation in reasoning_content |
| `app/policy.py` | validated hot-reloadable behaviour knobs (`var/gateway.yaml`) |
| `app/auth.py` | three-tier bearer auth |
| `app/csv_store.py` | stable `drow_*` ids, validate-before-swap writes, key masking |
| `app/admin.py` | management API: deployment CRUD/bulk, policy PATCH, state/history, insights, audit, probe, playground |
| `app/bootstrap.py` | agent self-setup playbook endpoints |
| `app/keyhealth.py` | persistent dead-key evidence, retirement lifecycle |
| `app/ledger.py` | usage/cost ledger feeding `/admin/insights` |

## Tuning (policy)

All behaviour knobs live in `var/gateway.yaml`; see
[`var/gateway.yaml.example`](var/gateway.yaml.example) for the annotated
template. The ones that matter most:

| Knob | Default | Meaning |
|---|---|---|
| `cooldown_mode` / `cooldown_base_min` / `cooldown_linear_mult_min` | `linear` / 30 / 30 | linear cooldown: 30 min + 30 min per failure/24h |
| `max_cooldown_sec` | 18000 | cooldown ceiling (5 h) |
| `timeout_cooldown_mult` | 10 | multiplier applied to a *timeout* failure |
| `ladder_skip_after` / `ladder_stale_max` | 4 / 3 | attempts per dim before climbing / stale revivals |
| `cooldown_retry_max_fail_24h` / `chronic_fail_cooldown_sec` | 10 / 7200 | chronic threshold / mandatory pause after re-failure |
| `escalation_pin` / `escalation_pin_probe_dims` | true / 2 | escalation-winner shortcut and pre-pin probe count |
| `qc_json.stream_first_content_ms` / `stream_total_deadline_ms` | 240000 / 960000 | first-content deadline per deployment / total request deadline |

## Security model

- Secrets live in `var/keys_rotation.csv` and `.env.gateway`: bind-mounted,
  **never in the image, never in git history**
- Admin surface (`/admin/*`) requires the master key and is invisible to
  client keys
- Client keys are deterministic (`sk-<profile>`) or custom overrides; keys
  are always masked in admin responses
- The service binds to `127.0.0.1` by default: put a reverse proxy in front
  before exposing it, and change `GATEWAY_MASTER_KEY`
- `/bootstrap*` endpoints are public read-only and contain no secrets

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests/ -q          # full suite (421 passing)
```

CI runs the suite and builds the image on push
(`.github/workflows/ci.yml`, GHCR).

### Operator scripts (`scripts/`)

Offline helpers that read `var/keys_rotation.csv` and hit each provider's
`GET /models` once per (endpoint, key). They are read-only unless `--fix`:

- `scripts/audit_models.py` — checks every CSV deployment resolves to a model
  the provider actually serves (post `infer_model_prefix`); prints a report.
  `--fix` corrects the unambiguous mismatches via the admin API.
- `scripts/discover_capabilities.py [--profile <name>]` — infers input/output
  modalities per model and prints a suggested
  `capability_routing.model_capabilities` block. `--fix` writes it into
  `var/gateway.yaml`.
- `scripts/add_openrouter_key.py KEY [--profile <name>]` — appends an
  OpenRouter key to the CSV for every catalogued model.

## License

[Unlicense](LICENSE) — public domain. Use it, fork it, sell it.
