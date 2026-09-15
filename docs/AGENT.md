# scrocco-llm · AGENT.md

Day-2 operational protocol for AI agents and humans.
Served live by the gateway at `GET /admin/guide` (master key required).
Zero-to-running setup: see [BOOTSTRAP.md](BOOTSTRAP.md) or `GET /bootstrap`.

---

## What this is

A self-hosted, OpenAI-compatible LLM gateway that pools many provider
accounts (Groq, OpenRouter, Mistral, Google AI Studio, NVIDIA NIM,
Cloudflare Workers AI...) into one resilient endpoint on port `4001`.
No database: state lives entirely under `var/` (bind-mounted, never in
the image).

## Auth model (three tiers)

| Tier | Shape | Can do |
|---|---|---|
| Master key | `GATEWAY_MASTER_KEY` env | everything, incl. `/admin/*` |
| Client key | deterministic `sk-<profile>` | OpenAI-compatible calls only |
| Override | custom key mapped to a profile | same as client key |

Admin surface is invisible to client keys. Keys are always masked in
admin responses.

## Endpoint map

### OpenAI-compatible (client keys)

| Endpoint | Purpose |
|---|---|
| `POST /v1/chat/completions` | chat; streaming supported |
| `GET /v1/models` | models visible to your profile |
| `POST /v1/images/generations` | image gen |
| `POST /v1/audio/speech` | TTS |
| `POST /v1/audio/transcriptions` `/translations` | STT (local speaches sidecar or cloud) |
| `POST /v1/videos/generations` (+ `/{job_id}`, `/{job_id}/content`) | async video jobs |

### Ops & observability

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz`, `GET /health/liveliness` | none | liveness |
| `GET /metrics` | none | Prometheus |
| `GET /bootstrap` `/bootstrap/providers` | none | phased setup playbook (read-only) |

### Admin API (`/admin/*`, master key)

| Endpoint | Purpose |
|---|---|
| `GET /admin/guide` | this document |
| `GET/POST/PUT/DELETE /admin/deployments[...]` | CRUD over CSV rows (stable `drow_*` ids) |
| `POST /admin/deployments/bulk` | add many rows at once |
| `POST /admin/deployments/probe[/bulk]` | one real max_tokens=1 call per key; result cached forever |
| `GET /admin/deployments/expiring` | renewals window |
| `POST /admin/deployments/unretire` | revive keys dead >7d after fixing them |
| `GET /admin/state` · `GET /admin/history` | live routing state / operations journal |
| `POST /admin/cooldowns/clear` · `POST /admin/sessions/release` | reset transient state |
| `GET/PATCH /admin/policy` | validated hot-reload of behaviour knobs (`var/gateway.yaml`) |
| `GET /admin/profiles` · `POST /admin/profiles/purge` | list / remove profile + its rows |
| `GET /admin/insights[/summary]` | per profile/model/day usage and cost burn |
| `POST /admin/reload` | force re-read of CSV + policy |
| `POST /admin/capabilities/seed-from-map` · `/audit` | capability metadata upkeep |

## Core recipes

**Validate all unvalidated keys** (never run automatically — some free
tiers count calls, not tokens):

```
curl -X POST localhost:4001/admin/deployments/probe/bulk \
  -H "Authorization: Bearer $MASTER_KEY"
```

**Check what's broken right now:** `GET /bootstrap/status`
(missing caps, dead keys, master-key warning) — public.

**Add a key:** `POST /admin/deployments/bulk` with rows shaped like
`var/keys_rotation.csv.example`; hot-reloaded atomically ~5s later,
no restart.

**Where did my request go?** every request logs one `[summary]` line;
routing state per deployment: `GET /admin/state` (includes
`adaptive.session_dep_guard` and `adaptive.warm_pool`). Useful tags:
`[prelast]` (shared deployment tier), `[warm]` (own warm-pool tier; never a
dim below the requested `-Nk`; drops deps whose latency EMA exceeds 90s or
that were slow for this session from warm/sticky/cache-holder and from the
cheap selections — same session only, re-fished at `-fallback`),
`[maxtok]` (`max_tokens` clamped to the window), `[autoprobe]`
(cooldown probe / backoff), `[cache]` (session holder),
`[fallback]` (failure + next hop), `[pick-final]` (go/fallback pick; with an
*explicit* `-go`/`-fallback` request the session cache-holder also beats the
renewal tier — paid cache, never for auto routing), `[ctxcompact]` (cold-cache
context trim: old large tool outputs → deterministic head+tail stub with
`[tool] N char, M righe[, exit X]` summary, duplicates → `[rimando: ...]`
(content-hash refs, `(già compresso)` when the target is itself stubbed;
per-session frontier watermark: a stub never un-stubs on window rotation);
old oversized `tool_calls` arguments get a JSON-aware trim
(`tool_args_max_chars`, never breaks JSON validity); error outputs are NEVER
rewritten (incl. line-anchored FAILED/ERROR/fatal:); tail protected while it
fits `keep_tail_pct`% of the window; `X-Ctxcompact-Saved` header +
`nx_ctxcompact_tool_total` counter; reasoning-only (effort_capable) deps get
the earlier `reasoning_headroom_ratio` trigger), `[effort]` (`reasoning_effort` injected/
removed per the row's `effort_capable`). Thinking crosses the translator
boundary: upstream reasoning (Responses summaries, Anthropic `thinking`
blocks, Gemini `thought` parts) is delivered to the client as
`message.reasoning_content` / `delta.reasoning_content`, exactly like the
pass-through `chat` style — non-stream final answers included),
`[warmstart]` (routing state — holders, stickies, warm ownership, slow
demotes, esc pins, ctxcompact frontiers — restored at boot from
`var/routing_state.json`, TTLs revalidated), `[cache-audit]` (why a session's
conversation prefix CHANGED between requests: `identity`/`prefix` verdicts;
the same verdict also labels `nx_chain_503_total`, the breadcrumb telling
how many retryable 503 were a prefix-mutation cache miss rather than a dead
provider),
`[hedge]` (cold-chain first-content race: one canary, pre-commit only, the
loser is cancelled unpunished, never toward paid buckets), `[key-soft]`
(per-api-key 429 blackout for Retry-After seconds: soft skip of every row
sharing the key — no strikes, no reputation loss; near-exhausted rate
headers do the same, TTL `rate_hint_ttl_sec`).

## Invariants (do not break)

1. Secrets live only in `var/keys_rotation.csv` and `.env.gateway`
   (host-side bind mount) — never in the image, never in git.
2. Writes go through the admin API: validate-before-swap atomic rename;
   never hand-edit the CSV while the gateway is running unless you also
   trigger `POST /admin/reload`.
3. Probe results are cached forever on purpose: do not re-probe healthy
   keys on a schedule.
4. The service binds loopback by default. Exposing it publicly requires
   a reverse proxy in front and a non-default master key.
