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
| `GET /admin/sessions` | active sessions: sticky, dep-sticky, cache holder, dep-guard, slow demote |
| `GET /admin/sessions/{id}` | ONE session: deployment ranking (successful), preferred model, tokens |
| `GET/PATCH /admin/policy` | validated hot-reload of behaviour knobs (`var/gateway.yaml`) |
| `GET/PUT /admin/policy/raw` | raw YAML view / full validated replace |
| `GET /admin/profiles` · `POST /admin/profiles/purge` | list / remove profile + its rows |
| `GET /admin/insights[/summary]` | per profile/model/day usage and cost burn |
| `GET /admin/stats/summary` | tokens (1h/24h), cache, success rate, model ranking, preferred model |
| `GET /admin/stats/tokens?window=24h` | tokens by model/profile/day |
| `GET /admin/stats/cache` | cache hit rate, coalescing, holders |
| `GET /admin/stats/models?window=7d` | model leaderboard (success/latency/usage) |
| `GET /admin/stats/deployments?sort=&order=` | per-deployment stats |
| `GET /admin/stats/providers` | per-provider aggregate |
| `GET /admin/stats/sessions?window=7d&limit=` | session leaderboard (tokens, ok/fail, preferred model) |
| `GET /admin/tuning` | effective runtime tuning params (router/forwarder/admin/storage/misc) |
| `GET/PUT /admin/csv` | raw CSV view / full validated replace |
| `GET /admin/backups` · `POST /admin/backups/restore` | list / restore CSV+YAML snapshots |
| `GET /admin/providers/health` · `GET /admin/pressure/inspect` | provider health / why deployments are skipped |
| `POST /admin/reload` | force re-read of CSV + policy |
| `POST /admin/capabilities/seed-from-map` · `/audit` | capability metadata upkeep |
| `GET /admin/mcp/config/tools` | MCP tool catalogue (name/description/inputSchema) |
| `POST /admin/mcp/config/execute` | execute an MCP tool: `{tool, arguments}` → MCP envelope |
| `POST /admin/mcp/config/call` | JSON-RPC 2.0 (`initialize`, `tools/list`, `tools/call`) |

### MCP configuration protocol

Every configuration operation is exposed as a **Model Context Protocol**
tool so agents can drive the whole gateway over JSON-RPC 2.0:

```
POST /admin/mcp/config/call
{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"deploy_list","arguments":{"profile":"example"}}}
```

Canonical tool names (48): `policy_get`, `policy_patch`, `policy_raw_get`,
`policy_raw_put`, `deploy_list|get|create|update|delete|bulk|expiring|probe|
probe_bulk|unretire`, `profile_list|purge`, `csv_get|put`, `backup_list|
restore`, `capabilities_seed|audit`, `state_get`, `history_get`,
`reload_gateway`, `cooldowns_clear`, `pressure_clear|inspect`,
`sessions_list|release|detail`, `stats_summary|tokens|cache|models|
deployments|providers|sessions`, `tuning_get`, `deployments_stats`,
`providers_health`, `guide_get`, `insights_get|summary`,
`leaderboard_get`, `logs_calls|errors`, `playground`.
Legacy aliases (`get_stats_summary`, `list_deployments`, …) are accepted too.

## Terminal UI (`tui/`, Textual)

`./scrocco.sh` launches the TUI, which drives the admin API only (never the
CSV). Requires the optional Textual dependency
(`pip install -r requirements-tui.txt`); without it the launcher prints the
install hint and can fall back to `./scrocco.sh --cli`. Every backend setting
is reachable from the terminal:

| Key | View |
|---|---|
| `r` / `R` | reload local data / force remote CSV+policy reload |
| `C` / `S` | clear all cooldowns / release all sticky sessions |
| `n` `e` `d` | new / edit / delete deployment · `p` `x` new / delete profile |
| `Y` | advanced policy editor · `m` capacities · `E` expirations · `k` client keys |
| `O` | observability: live, errors, leaderboard, sessions, statistics |
| `t` `l` `u` | statistics · deployment leaderboard · sessions (ENTER → detail) |
| `M` | MCP config browser/executor (all 48 tools) |
| `T` | effective runtime tuning · `P` persisted scores · `H` provider health |
| `V` / `G` | raw policy YAML / raw deployment CSV editor |
| `Z` | operations hub: probe, unretire, purge, capabilities, pressure, backups, insights, history, guide, playground |

TUI runtime knobs are configurable via `TUI_*` env vars (defaults = the
historical constants): `TUI_REFRESH_LIVE_SEC`, `TUI_REFRESH_ERRORS_SEC`,
`TUI_REFRESH_SESSIONS_SEC`, `TUI_REFRESH_LEADERBOARD_SEC`,
`TUI_REFRESH_STATS_SEC`, `TUI_LIVE_MAX_ROWS`, `TUI_MODEL_RANKING_MAX`,
`TUI_OPS_ROWS_MAX`, `TUI_OPS_HISTORY_MAX`, `TUI_OPS_PRESSURE_LIMIT`,
`TUI_RESULT_MAX_CHARS`, `TUI_MCP_RESULT_MAX_CHARS`,
`TUI_ERROR_MSG_MAX_CHARS`, `TUI_HTTP_ERR_SNIPPET_CHARS`.

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
`[prelast]` (shared deployment tier, tried BEFORE cooldown wakeups),
`[warm]` (own warm-pool tier; never a dim below the requested `-Nk`; with
`warm_pool.allow_slow=false` deps slower than the size-aware threshold leave
warm/sticky/cache-holder and are re-fished only at `-fallback`; by default
slow deps STAY in warm and the race hunts them a NEW substitute),
`[ladder] dims cooldown-wakeup 429` (revives only dims whose last failure
was quota, budget 20/window per dep, before the paid `-go`),
`[estimate] auto-adaptive ON` / `[latency-penalty]` (slow = worse than
`max(slow_latency_abs_floor_ms, slow_latency_rel_mult x fleet median/rate
estimate)` — a 128k in 100s is NOT slow), `[hedge]`/`[hunt]` backoff
(a race that found nothing better suspends hunting for `hunt_backoff_sec`),
`[maxtok]` (`max_tokens` clamped to the window: minus a 5% safety margin and,
for `effort_capable` deps, minus a reasoning reserve — `nx_max_tokens_clamped`
counts the clamps), `[autoprobe]`
(cooldown probe / backoff), `[cache]` (session holder),
`[fallback]` (failure + next hop), `[pick-final]` (go/fallback pick; with an
*explicit* `-go`/`-fallback` request the session cache-holder also beats the
renewal tier — paid cache, never for auto routing), `[ctxcompact]` (cold-cache
context trim: old large tool outputs → deterministic head+tail stub with
`[tool] N char, M righe[, exit X]` summary, duplicates → `[rimando: ...]`
(content-hash refs, `(già compresso)` when the target is itself stubbed;
per-session frontier watermark: a stub never un-stubs on window rotation);
old oversized `tool_calls` arguments get a JSON-aware trim
(`tool_args_max_chars`, never breaks JSON validity; a non-JSON argument string
is char-trimmed head/tail instead of being left huge); error outputs are NEVER
rewritten (incl. `exit != 0`, line-anchored FAILED/ERROR/fatal: **and** the
real agent patterns: command not found, permission denied, no such file,
timeout, ENOENT/EACCES/EPERM/ECONNREFUSED…, case-insensitive; outputs < 20
chars get a bare stub and are never inflated); tail protected while it fits
`keep_tail_pct`% of the window; JSON list/dict outputs with too many elements
are cut structurally (valid JSON + `totale` marker) — also inside ```json
fences and on a dict's dominant value — and a cited output is never stubbed
(`cite_retention`, path-like tokens ≥ `cite_min_freq`, dynamic cap); dedup
runs on the NORMALIZED content (dates/clock/ms/hex stripped) and its
back-reference carries a `head:` of the first 200 chars;
`X-Ctxcompact-Saved` header +
`nx_ctxcompact_tool_total` counter; reasoning-only (effort_capable) deps get
the earlier `reasoning_headroom_ratio` trigger **and** a
`reasoning_reserve_ratio` window reserve added to `ctx_est` in the
compaction gate; the frontier walk and the saved-token estimate use the
deployment's learned divisor (`[latency]`/`estimate_correction`)), `[effort]` (`reasoning_effort` injected/
removed per the row's `effort_capable`), `[key-soft]` (a 429 puts the whole
API KEY in soft-skip for the Retry-After window: no reputation damage; the
autoprobe treats a saturated key the same way and never spends another model's
probe on it), `[autoprobe]` (conservative probes: per-key 24 h budget, hourly
per-key gap, skip when real traffic proved the key alive, daily sweep of
retired keys after midnight), `[ladder] ... ULTIMA SPIAGGIA ESTREMA` (the
strictest rung may use retired NON-permanent keys; a success clears the
lifecycle). Thinking crosses the translator
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
`[hedge]` (first-content race when warm cannot help — cold chain, slow warm
holder, or tried holder: up to `stream_hedge_tiers` canaries on NEW candidates
in ascending other tiers (never paid, never below the requested dim, never
`json_fallback>=2`; when replacing the slow warm holder the warm list itself
is excluded and least-used-24h are preferred), pre-commit only, losers
cancelled unpunished; a loser that produced content still becomes
warm-ownership evidence), `[key-soft]`
(per-api-key 429 blackout for Retry-After seconds: soft skip of every row
sharing the key — no strikes, no reputation loss; near-exhausted rate
headers do the same, TTL `rate_hint_ttl_sec`), `[cooldown-class]` (which
error class decided the pause: `transient` → short deployment-only cooldown,
`quota` → Retry-After, key reputation untouched), `[rep-fail]`
(`classe=quota: nessuna penale` when a 429 deliberately does NOT touch the
scores), `[model-cb]` (proactive per-`provider|model` breaker:
`N` distinct keys failing 5xx inside the window ⇒ the whole model is
soft-skipped for the open period, no reputation damage), `[ctx-overflow]`/`[dim]`
(the payload estimate exceeds the `max_input` of every deployment of the
requested group: FIRST climb to the smallest dim that fits — `[dim]` log,
never down, never paid buckets — else one forced compaction then a synthetic
400 `context_length_exceeded` — no upstream call, `nx_ctx_overflow_total` /
`nx_ctx_compacted_forced`). Retry-After is read from the header **or** the
error body JSON
(`"retry_after"`, `"retryAfter"`, `"retryDelay"`, "retry in 58s",
"try again in 45s"), header first, then a global/per-provider floor.

## Invariants (do not break)

1. Secrets live only in `var/keys_rotation.csv` and `.env.gateway`
   (host-side bind mount) — never in the image, never in git.
2. Writes go through the admin API: validate-before-swap atomic rename;
   never hand-edit the CSV while the gateway is running unless you also
   trigger `POST /admin/reload`.
3. Probe results are cached forever on purpose: do not re-probe healthy
   keys on a schedule. The same **api key** (even on different row
   deployments/twins) is never re-probed within
   `cooldown_autoprobe_key_gap_sec` (default 300 s).
4. The service binds loopback by default. Exposing it publicly requires
   a reverse proxy in front and a non-default master key.
5. Per-request thought-signature flags (`set_avoid_gemini`/`set_dummy_fill`)
   are reset at the top of every handler (`reset_request_flags()`): an early
   `return` (400/413/401) or an exception must never leave the ContextVar
   active for the next request reusing the same event loop.
6. Durability/observability never lose data or explode: `ledger.flush()`
   requeues the extracted rows in order when the write fails (retried on the
   next tick), and the latency series are bounded to 512 uniques with LRU
   eviction so `nx_upstream_latency_ms{unique=...}` cannot grow unbounded.
7. The token-estimate rollout is self-closing: the shadow counters
   (legacy/adaptive) persist in `adaptive_stats.json` (so deploys/restarts do
   not reset the evidence) and the adaptive estimator turns itself on once at
   least `estimate_adaptive_auto_min_n` samples show a mean divergence within
   `estimate_adaptive_auto_max_delta_pct`; the explicit
   `estimate_adaptive_enabled` master switch still wins. `[estimate]
   auto-adaptive ON` marks the flip, and `/admin/policy` exposes
   `estimate_adaptive_effective` plus the shadow `auto` state.
