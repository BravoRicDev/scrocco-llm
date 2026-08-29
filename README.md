# scrocco-llm

[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-brightgreen.svg)](https://unlicense.org/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-compose%20up-blue.svg)](#quickstart)
[![Tests](https://img.shields.io/badge/tests-176%20passing-brightgreen.svg)](#development)

> 🇮🇹 **Leggi in italiano** — [README.it.md](README.it.md)

**scrocco-llm** is a self-hosted, OpenAI-compatible LLM gateway that pools
many free-tier and paid provider accounts (Groq, OpenRouter, Mistral,
Google AI Studio, NVIDIA NIM, Cloudflare Workers AI...) into one resilient
endpoint with adaptive routing, capability groups and key rotation.
Zero database. One container. Port `4001`.

> **Give this repo to any AI agent** and it can set the service up alone:
> `git clone` → `docker compose up -d` → `curl localhost:4001/bootstrap`.
> See [Agent setup](#agent-setup-self-bootstrap).

---

## Why

A single free API key is fragile: tiny context windows, rate limits, models
that disappear. scrocco-llm routes every request to the **minimum model
that fits** the estimated context across *all* your keys, failing over with
exponential cooldowns — so workloads survive individual account limits.

## Features

- **Context-aware routing**: token estimate -> smallest sufficient group
  (`-32k` ... `-1000k`); explicit requests are floors that escalate upward
- **Structural capability groups**: `-vision`, `-tts`, `-stt`,
  `-image_gen`, `-video_gen`; purpose-aware fallback never lands on a
  model lacking the requested modality
- **Chained failover**: free -> renewal (`-go`) -> fallback buckets;
  exponential cooldown (capped at 5h); same-model-first rotation for media
- **Budget guard (no-waste)**: learns per-key limits from observed 429s and
  deprioritises exhausted keys BEFORE they burn more calls; probe results
  are cached forever (some free tiers count calls, not tokens)
- **Key lifecycle**: persistent health evidence; keys dead for >7 days get
  retired from routing (never deleted) until a successful probe revives them
- **Adaptive rotation**: latency EMA + freshness + inflight scoring
- **Media endpoints**: images, async video jobs (submit/poll/content),
  TTS/STT incl. local speaches sidecar
- **QC + watchdog**: broken-JSON retry with annotated last-response
  delivery, empty-content sanity, passive stream watchdog,
  length-truncation aware (reasoning tokens eating max_tokens)
- **Usage & cost insights**: persistent ledger + `GET /admin/insights`
  (per profile/model/day burn; provider-reported vs estimated costs)
- **Three-tier auth**: master key / deterministic `sk-<profile>` client
  keys / custom overrides
- **Hot-reload everything**: credentials CSV + policy YAML re-read
  atomically (~5s). No restarts, ever.
- **Terminal UI** (`./scrocco.sh`) + Prometheus `/metrics`

## Quickstart

```bash
git clone https://github.com/BravoRicDev/scrocco-llm && cd scrocco-llm
cp var/keys_rotation.csv.example var/keys_rotation.csv   # se assente
docker compose up -d
curl -s localhost:4001/healthz | head -c 60              # -> {"status":"ok"...
```

Then follow the built-in playbook:

```bash
curl -s localhost:4001/bootstrap     # phased setup guide (public, EN)
```

Run without Docker: `./run.sh` (venv + `127.0.0.1:4001`).

### Minimal configuration

`var/keys_rotation.csv` -- one row per *(model x key x endpoint)*:

```csv
commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-myteam,caps
you@example.com,openai/gpt-oss-120b,groq,https://api.groq.com/openai/v1,free,128,8000,0,gsk_XXXXXXXXXXXXXXXX,
```

- `data`: `free`/`priority` = first-choice buckets, `paid`/`fallback` =
  last resort, day number 1-31 = monthly renewal ordering inside `-go`
- `context`: kilo-units (128 = 128k window)
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
| `GET /bootstrap` | none | phased zero-to-running playbook: research providers online -> insert keys via admin API -> validate once -> smoke test |
| `GET /bootstrap/providers` | none | stable facts only: signup URLs, api_base shapes, data-category semantics |
| `GET /bootstrap/status` | none | live gap analysis: missing deployments/caps, dead keys, default master key warning |
| `POST /admin/deployments/probe/bulk` | master | one real call per key (max_tokens=1), success cached persistently |

Day-2 operations live in `GET /admin/guide` (master key) and
[docs/AGENT.md](docs/AGENT.md). First-run recipe: [docs/BOOTSTRAP.md](docs/BOOTSTRAP.md).

## Architecture

```
Client (agents, curl, TUI...)
        |  Bearer sk-<profile>
        v
+--------------------------------------+
| FastAPI :4001                        |
| auth -> alias -> ctx estimate ->     |
| capability group -> adaptive pick    |
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
| `app/main.py` | HTTP endpoints + request pipeline; stream watchdog; per-request `[summary]` logs |
| `app/config.py` | credential CSV -> dims/capability groups; `*_gen` quarantine; atomic hot-reload |
| `app/router.py` | adaptive pick (EMA/freshness/inflight), fallback, cooldown escalation, sticky sessions, ladder, media defer, budget guard scoring |
| `app/forwarder.py` | all upstream HTTP; precise error taxonomy -> correct rotation; probe with persistent cache |
| `app/qc.py` | JSON QC / sanity / watchdog; D3 annotation in reasoning_content |
| `app/policy.py` | validated hot-reloadable behaviour knobs (`var/gateway.yaml`) |
| `app/auth.py` | three-tier bearer auth |
| `app/csv_store.py` | stable `drow_*` ids, validate-before-swap writes, key masking |
| `app/admin.py` | management API: deployment CRUD/bulk, policy PATCH, state/history, insights, audit, probe |
| `app/bootstrap.py` | agent self-setup playbook endpoints |
| `app/keyhealth.py` | persistent dead-key evidence, retirement lifecycle |
| `app/ledger.py` | usage/cost ledger feeding `/admin/insights` |

## Security model

- Secrets live in `var/keys_rotation.csv` and `.env.gateway`: bind-mounted,
  **never in the image, never in git history**
- Admin surface (`/admin/*`) requires the master key and is invisible to
  client keys
- Client keys are deterministic (`sk-<profile>`) or custom overrides;
  keys are always masked in admin responses
- The service binds to `127.0.0.1` by default: put a reverse proxy in
  front before exposing it, and change `GATEWAY_MASTER_KEY`
- `/bootstrap*` endpoints are public read-only and contain no secrets

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python3 -m pytest tests/ -q          # full suite
```

CI runs the suite and builds the image on push
(`.github/workflows/ci.yml`, GHCR).

### Operator scripts (`scripts/`)

Offline helpers that read `var/keys_rotation.csv` and hit each provider's
`GET /models` once per (endpoint, key). Both are read-only unless `--fix`:

- `scripts/audit_models.py` — checks every CSV deployment resolves to a
  model the provider actually serves (post `infer_model_prefix`); prints a
  report. `--fix` corrects the unambiguous mismatches via the admin API.
- `scripts/discover_capabilities.py [--profile <name>]` — infers input/
  output modalities per model and prints a suggested
  `capability_routing.model_capabilities` block. `--fix` writes it into
  `var/gateway.yaml`.

## Documentazione italiana

Il progetto nasce in italiano: i docstring dei moduli sono bilingue IT/EN e
spiegano COSA fa / COME funziona / PERCHE' sono state prese le decisioni.
Riferimenti rapidi:

- **Setup zero-to-running per agenti**: [docs/BOOTSTRAP.md](docs/BOOTSTRAP.md) (EN)
  e playbook live `GET /bootstrap`
- **Protocollo operativo day-2**: [docs/AGENT.md](docs/AGENT.md), servito dal
  gateway su `GET /admin/guide` — endpoint admin, ricette per caso d'uso,
  mappa completa dei tag di log
- **Filosofia di routing**: minimo modello che ci sta nel contesto stimato;
  fallback consapevole delle capacita'; niente sprechi di chiamate su
  free-tier che contano le chiamate

## License

[Unlicense](LICENSE) — public domain. Use it, fork it, sell it.
