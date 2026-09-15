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
  group before climbing, up to `ladder_cooldown_wakeups` stale-cooldown
  revivals (tried *before* jumping to `-go`), a *chronic parachute* for
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
- **Tool-call repair** (`tool_repair`): normalises the *shape* of tool-call
  arguments before the client validates them (JSON escaping, stringified
  objects/arrays, scalar coercion, trailing commas, truncated JSON), in both
  streaming and non-streaming. Levels `safe`/`aggressive`, per-deployment via
  the `tool_repair` CSV column, Google/Gemini off by default. Never changes
  tool names or semantics.
- **History normalize** (`history_normalize`): structural, cache-safe tail cleanup of the outgoing message copy. Handles both orphan `tool` results and **inverse orphans**: an `assistant` with `tool_calls` missing the matching `tool` result (even only for some ids) has the dangling calls stripped (content preserved), so strict providers never reject the chain. No synthesized results.
- **Sampling defaults** (`sampling_defaults`) + **loop detector** (`loop`): low-risk provider defaults (client wins) and n-gram/tool-call loop escalation to the next dim.
- **Corrective retry** (`corrective_retry`): one non-streaming retry on invalid/empty/JSON/schema content (no repair model).
- **Structured output** (`qc_json.struct_out_*`): fence/prose cleanup, JSON-Schema subset validation, schema-driven repair, optional `response_format` injection. **Gentle downgrade**: if the client asks for `json_schema` but the target provider is not in `native_schema_providers` (default `openai`, `azure`), the field is stripped and the schema is injected as a prompt instruction instead — no 400, uniform behaviour across heterogeneous free providers.
- **Text tool-call parser** (`text_toolcall`): recovers tool-calls written as text before the fake-call safety net.
- **Fake tool-call fallback** (`tool_repair.fake_call`): when a request
  declares `tools` but the model writes the call as text (`<arg_key>`,
  `<bash`, `antml:` ...), the deployment is marked failed and the gateway
  escalates straight to `-go`/`-fallback` (detection disabled there to avoid
  loops); on exhaustion a retryable 503 is returned. No LLM repair call.
- **Cache-aware routing & context trimming** (`cache_aware`): remembers
  the last deployment that served a session successfully and prefers it
  on failover (free buckets only, cache-preserving). Context is compacted
  (old tool outputs replaced by a *deterministic* head+tail stub: summary
  line `[{tool}] N char, M righe[, exit X]` + first/last `head_chars`/
  `tail_chars` cut at line boundaries; identical duplicates become a
  back-reference to the newest call) only when the request would not fit
  the chosen deployment, and the session stays compacted so the provider
  prefix remains cacheable. Error outputs (Traceback/…Error/Exception/
  exit≠0, or lines starting with `FAILED`/`ERROR`/`fatal:`) are never
  touched, not even on overflow. The frontier is **per-session watermarked**:
  rotating to a bigger window never un-stubs what was already written (byte
  stable). Duplicate back-references use a content hash (`msg@<hash>`) so a
  client rewriting its own history cannot shift them, and they say
  `(già compresso)` when the target itself is stubbed. When
  `tool_args_max_chars` > 0, oversized old `tool_calls` arguments are trimmed
  too — JSON-aware (only long string values, output stays valid JSON).
  A `X-Ctxcompact-Saved` response header and a per-tool
  `nx_ctxcompact_tool_total` counter expose how much was reclaimed.
  `cache_aware.prefix_audit` closes the loop from the other side: a content
  hash of the (pre-body) conversation prefix is kept per session and, when it
  mutates between requests, `[cache-audit]` logs `identity` (system/identity
  changed) or `prefix` — free forensics against phantom cache invalidation.
  Old assistant `reasoning_content` is trimmed too by history normalization
  (`history_normalize.reasoning_content_max_chars`: 0 strips, N keeps a
  deterministic head, -1 never touches; the last `reasoning_keep_recent`
  turns are spared) — R1/Qwen thinking can burn 5-15k tokens per turn and
  earns little once past the frontier.
- **Routing warm-start** (`var/routing_state.json`): cache holders, sticky
  targets, warm-dim ownership, per-session slow demotes, escalation pins and
  the ctxcompact frontier watermark are snapshotted (atomic, throttled 60 s +
  forced at shutdown) and restored on boot with TTLs revalidated — a deploy
  is no longer a cold reset of every session's routing memory.
  Kill-switch: `GATEWAY_PERSIST_ROUTING=0`.
- **Context-bucket latency + TTFT**: each deployment keeps two tables of
  four EMAs (buckets <8k / 8-32k / 32-128k / >128k context: one for total
  duration, one for time-to-first-content). Slow-demote is **absolute AND
  relative** (bucket EMA > 90 s *and* > 2× that bucket's baseline, so a dep
  that merely served a huge context is not punished); the adaptive httpx
  timeout and the first-content deadline read the *current request's* bucket
  (TTFT table for first-content); the once-dead `dynamic_scoring` p95 leg is
  alive, fed by per-bucket samples. A per-deployment **prefill rate**
  (ms per 1k context tokens, persisted) lets the TTFT be extrapolated when
  the required bucket has no samples yet — an 80 k context is no longer
  judged by the mixed EMA of small prompts — and the cross-bucket p95
  fallback is normalized to the requested size the same way.
- **503 breadcrumb** (`nx_chain_503_total{prefix=identity|prefix|clean}`):
  when a chain is exhausted (503 + Retry-After) the prefix-audit verdict of
  that request labels the counter, so retryable 503s caused by a mutated
  prefix (cache miss perceived as "dead provider") are countable.
- **First-content hedge** (`qc_json.stream_hedge_delay_ms`, default 1500,
  0 = off): on the FIRST attempt of a cache-COLD streaming chain, if the
  chosen upstream has no content after the delay, ONE canary opens on the
  next candidate and whoever commits first wins — everything is pre-byte, so
  the loser is cancelled without punishment and paid buckets (`-go`/
  `-fallback`) are never eligible canaries. The delay is **adaptive to the
  context bucket** (`stream_hedge_ttft_frac`/`_min_ms`/`_max_ms`): on >128k
  contexts the median TTFT *is* 4-6 s, so the canary waits
  `clamp(TTFT_p50_bucket × 0.6, 800, 2500) ms` instead of firing as noise on
  every heavy request.
- **Token-weighted concurrency** (`conc_token_ratio`, default 0.5): the
  inflight limiter weighs the REAL prefill (`inflight_tokens += ctx_est`),
  not the request count — 3 Hermes turns of 90k are 270k tokens of concurrent
  prefill, 3 turns of 5k are 15k. A row is skipped (pick-time only, no
  cooldown) when `inflight_tokens + ctx_est > max_input × ratio`, so six
  light requests still run in parallel but two heavy ones never do; the hard
  count cap (`conc_max_limit`) stays as anti-abuse. `0` = legacy counting.
- **Structured JSON tool-output truncation** (`json_struct_max_items`, 40):
  before the char-based head+tail cut, a JSON list/dict with too many
  elements is cut STRUCTURALLY — first `json_struct_head` (20) + a
  `{"...omessi": K, "totale": N}` marker + last `json_struct_tail` (5) —
  re-serialized as VALID JSON, so the agent can still parse the shape
  (structured search results keep their total count and first matches)
  instead of receiving a broken half-object. Pure function of the content
  (cache-correct), `0` = off. Plus **citation retention**
  (`cite_retention`/`cite_min_freq`, on/2): an old output is NOT stubbed
  while the protected tail still cites one of its distinctive tokens
  (repeated ≥ `cite_min_freq`, structural terms and tool names excluded).
- **Closed-loop estimator calibration** (`estimate_calib_alpha`, 0.05):
  every response carrying real `usage.prompt_tokens` moves that deployment's
  learned divisor toward the true one (`base / (pt/ctx_est)`) with an EMA,
  clamped 1.5..4.5 and persisted next to the prefill rate; the corrected
  context (`ctx_est × base/divisor`) then feeds `should_compact`. Only
  reliable samples (ctx ≥ 8k, prompt > 1k) are used. `0` = off.
- **Soft key blackout, zero blame**: fresh `X-RateLimit-*` snapshots and
  429-with-Retry-After are tracked per API-KEY HASH (never plaintext):
  near-the-wall keys only *skip* the row among the free dims (other rows of
  the same deployment, paid buckets and last-resort stages stay reachable);
  a 429 soft-blackouts every row sharing that key for the upstream-advertised
  seconds (capped by `key_soft_max_sec`). No cooldown record, no strikes, no
  reputation loss. While a key's headers stay fresh, the budget-guard's
  learned caps are suppressed for it — headers beat guesses.
- **Session-dep guard** (`session_dep_guard`): stops concurrent sessions from
  "grabbing" the same free deployments and burning them (rate-limits). The last
  session that *successfully* served a free-dims deployment is remembered (an
  attempt alone never claims it); while that session is alive its deployments
  stay its own (ownership refreshed on every request), and another session
  asking within `sec` (900) ignores them among the live keys — they remain
  eligible only in a *pre-last-resort* tier (between the last `-dim` and `-go`,
  ordered by the smallest fitting `max_input`). `sec` seconds of silence and the
  whole set is freed again. Only free-dims buckets are tracked (`-go`/`-fallback`
  and capability groups are never claimed). The routing session id comes from
  `x-opencode-session`/`x-session-affinity`/`x-session-id`, then the body, then
  the anonymous fingerprint.
- **Warm pool** (`warm_pool`, tier "caldi"): before the requested `-dim` and the
  whole ladder, the session exhausts the free-dims it has already served
  *successfully* (still alive, not cooling down, fitting `need` + `max_input`).
  Internal order: session cache-holder, then MRU (`last_used`), then `order`,
  then smallest `max_input`. Applies to automatic routing and explicit `-Nk`
  dims; never to `-go`/`-fallback` (deliberate paid escalation). The window is
  `session_dep_guard_sec` (or `ttl_sec`); `max_attempts` caps the pool (0 =
  unlimited). Log tag `[warm]`. A dep whose latency EMA exceeds
  `LATENCY_ROTATE_THRESHOLD_MS` (90s), **or that was slow for this session**
  (per-session latch), is excluded from the warm pool, sticky and cache-holder:
  it still competes as a normal ladder reserve, and for the same session it
  only becomes fishable again at the last stage (`-fallback`/last resort). The
  warm pool never picks a dim **smaller** than the requested one: for an
  explicit `...-200k` it ignores warm `-64k` deps of the same session (the
  `-Nk` is the client's *minimum*).
- **Paid cache on explicit `-go`/`-fallback`.** When the client *explicitly*
  calls a text `-go`/`-fallback` group, the session's cache holder (the key
  that last served it) wins even over the renewal tier: the session stays on
  the same account to keep its KV-cache warm, and on a 429 the holder drops
  out by itself and rotation continues in normal order — credits are summed
  one account at a time. Automatic routing and internal escalations do NOT
  use it: random-within-best-tier stays there.
- **Multi-SDK upstreams** (`api_style` CSV column): the gateway always speaks
  and accepts OpenAI Chat Completions, but each deployment can declare its
  upstream's native protocol — `responses` (OpenAI Responses `/responses`),
  `messages` (Anthropic `/messages`, auth `x-api-key` + `anthropic-version`),
  `google` (Gemini `:generateContent`, auth `x-goog-api-key`). Request, response
  and SSE stream are translated transparently, so models that only exist on those
  SDKs (e.g. `muse-spark-*-free` on opencode zen) work through the same
  OpenAI-compatible endpoint. Clients keep using `@ai-sdk/openai-compatible`.
  Thinking/reasoning crosses this boundary too: a client that asks for reasoning
  (`reasoning_effort`/`effort`/`x-effort`, gated on the row's `effort_capable`)
  gets it enabled natively upstream (`reasoning.summary` auto, Anthropic
  `thinking.budget_tokens`, Gemini `thinkingConfig`), and the upstream thinking is
  delivered back as `message.reasoning_content` (non-stream, in the final JSON
  too) / `delta.reasoning_content` (stream) — the same fields OpenAI-compatible
  providers already use on the pass-through `chat` style.
- **Hard context guard + `max_tokens` clamp**: `max_input` (and the dims
  estimate) is enforced on *every* chat pick — including explicit group requests
  like `-200k` — so a 32k model is never chosen for a 150k prompt. The outgoing
  `max_tokens` is also clamped to `max(1, max_input - estimated_input)` when both
  are known, so a client reserving a huge completion cannot push
  `input + output` past the model window (upstream 400/413); the estimate is
  the same calibrated one used everywhere (learned divisor + a per-image
  allowance — an N-image multimodal payload counts N × `image_token_estimate`).
  Log tag `[maxtok]`.
- **Predictive budget guard** (`budget_guard`): once a per-key cap is
  *learned from a real 429*, `safety_ratio` (default `0.8`) marks a
  deployment as virtually saturated — counting in-flight requests too
  (`count_inflight`) — and `pick_deployment` diverts new requests to a
  sibling still under threshold *before* the upstream answers 429. No
  learned cap means no throttling. `retry_after_min_sec` (default `10`)
  floors tiny/absent `Retry-After` so a burst of near-simultaneous 429s
  cannot loop; `retry_after_floor_by_provider` overrides the floor per
  provider (e.g. `{groq: 5, google: 30, openrouter: 15}`) so recovery is
  proportional to how fast each provider refills quota.
- **Persistent per-host HTTP pool**: the forwarder keeps one `httpx`
  client per origin (`keepalive=30`, `max=100`, `keepalive_expiry=120s`)
  so TCP/TLS connections are reused across the hundreds of deployments
  sharing a provider, cutting TLS handshakes and TTFB on bursts.
- **Anti-stall watchdog** (`stream_stall_sec`, default `8`): after a stream
  has started, if the upstream sends no chunk for N seconds (free-tier /
  reverse-proxy "hang" without closing), a `StreamStallError` aborts it
  immediately → standard failover/cooldown instead of blocking the client
  indefinitely. `0` disables. The effective timeout is **calibrated on the
  request's context bucket**: `max(stream_stall_sec, min(TTFT_p50_bucket ×
  stream_stall_ttft_mult, stream_stall_max_sec))` — a 128k+ prefill of 4-6 s
  is normal, so a flat 8 s watchdog was killing healthy heavy streams while
  staying too slow for light ones; an empty bucket falls back to the base.
- **Debug sniff memory guard**: with `debug.sniff` on, input payloads and
  responses are scanned before logging; huge base64/binary blobs (image data
  URIs, Anthropic base64 sources, byte arrays) become a synthetic placeholder
  (`[IMAGE_BASE64_TRUNCATED_BY_SNIFFER: 1.2MB]`) and the streamed SSE is capped,
  so a multimodal request can't blow up disk/RAM. Prompt text is preserved.
- **Stale-cooldown decay & passive probe** (`cooldown_probe_*`): a cooled-down
  deployment's latency/success penalty decays linearly as its cooldown elapses;
  when no live key remains, a "ripe" deployment (>= `cooldown_probe_after_ratio`,
  default 50%) is retried as a single passive probe — success clears the
  cooldown instantly, failure doubles it, without affecting other requests.
  `cooldown_streak_halflife_sec` (default 1800) decays the fail streak while a
  key is idle, so a key reactivated after hours doesn't get re-exiled by one
  isolated error. `probe_retire_after` (default 5) auto-RETIRES a key after that
  many consecutive failed probes (permanent problem, CSV untouched).
  Every cooldown also gets a **deterministic** per-deployment spread
  (`cooldown_jitter_sec_max`, default 2.0: `sha256(unique) → 0..2 s`, applied
  to the cooldown expiry and to the per-key 429 blackout) so the twins of one
  key never expire in the same millisecond — stable across restarts, unlike
  the legacy random multiplier (`cooldown_jitter_ratio`, now 0 by default).
  Cooldown duration and reputation damage are **class-aware** too
  (`error_class_cooldowns`): a 429/quota saturates the *key* → per-key soft
  blackout for the upstream Retry-After and **no** reputation penalty; a
  503/529/500/timeout is a *transient* deployment fault → short pause only
  (`cooldown_transient_sec` 15 s / `cooldown_timeout_sec` 60 s) with a light
  deployment-only penalty and the key left clean; 401/402/403 stay key-level.
  An explicit `seconds` (Retry-After, probes, anti-black-hole) always wins
  and is never shortened.
- **Cooldown autoprobe** (`cooldown_autoprobe_*`): on each text call a
  fire-and-forget pass probes the most "ready" cooled deployments (least
  remaining cooldown) across every text dim — up to `cooldown_autoprobe_per_dim`
  (2) per dim, capped by `cooldown_autoprobe_max_total` (6) — but never uses
  them to serve the response. A successful probe clears the cooldown (live again
  on the next call); a failed one makes the residual **at least double** (min
  +`cooldown_autoprobe_grow_sec`, 429/definitive; a modest transient bump
  otherwise), **multiplied by the number of probes on that deployment in the
  last 24h** (`cooldown_autoprobe_multiply_24h`: 120s, 240s, 360s…) so targets
  rotate and no needless close-together retries happen.
  `cooldown_autoprobe_min_age_sec` (300) skips just-cooled keys,
  `cooldown_autoprobe_min_gap_sec` (60) avoids re-probing the same deployment,
  and `cooldown_autoprobe_skip_over_sec` (7200) excludes anything already cooled
  > 2h — the *stale-cooldown wakeup* in the ladder (between `-dim` and `-go`) or
  the last-resort pass retries those, never the autoprobe. The live path is
  never slowed down (fresh probes use `note_result`/`mark_failed`; cooled probes
  stay purely reconnaissance).
- **Quality-weighted EMA, coalescing & error classification**: `note_result()`
  accepts a `quality` (1.0 clean; lower for tool-repair/text-parse/QC/fake
  tool-call) that scales the latency/success EMA update rate, so broken-but-alive
  deployments adapt slower. Identical non-streaming in-flight requests are
  coalesced into one upstream call (`request_coalescing_*`). `classify_error()`
  maps upstream failures to recovery strategies: key/model errors are
  PERMANENT_DEAD (deployment retired, no cooldown), quotas use the exact reset
  without escalation, transient/5xx get a short cooldown.
- **Model families** (`canonical_family`): provider-specific model names are
  canonicalized to a family id (e.g. `meta-llama/llama-3-8b-instruct` ==
  `llama3-8b`). When failover crosses providers but stays on the same family,
  the context-compaction `switch` trigger is suppressed (`same_family`) so the
  prompt cache stays warm. On a same-family failover the sticky session is also
  **handed off** to the new deployment (`sticky_handoff_same_family`) so the
  next turn pins directly to the warm cache holder (`[sticky-handoff]`).
- **Two-level circuit breaker** (`circuit_breaker_scope`, default `hybrid`): a
  model/provider failure trips only that deployment's breaker, while a
  *key-level* signal (401/402/403/429) trips the shared-key breaker — so a bad
  model never silences the sibling models that share the same API key.
  `dep`/`key` force a single level. `model_preference_base` (default 10) adds a
  floor to the reputation magnitude so `model_preference` still decides routing
  on a cold score: a favourite model wins on the first call, not only when warm.
- **Reputation time-decay & adaptive upstream timeout**: success/failure
  reputation scores (`_base_scores`/`_provider_scores`/`_key_scores`) decay
  toward zero with a half-life (`reputation_decay_halflife_sec`, default 36h)
  so routing reacts to *recent* provider quality instead of a stale past. Chat
  upstream timeouts adapt per deployment from the latency EMA:
  `read = clamp(max(adaptive_timeout_floor_sec, avg_latency *
  adaptive_timeout_multiplier), floor, adaptive_timeout_max_sec)` (defaults
  15s / 8x / 600s) — fast keys fail fast, slow ones get room. Logs:
  `[rep-decay]` (INFO, ~10 min) and `[timeout-adaptive]` (DEBUG).
- **Graceful shutdown**: on SIGTERM/SIGINT the lifespan stops new work, drains
  in-flight requests (up to `shutdown_drain_sec`, default `10`) and runs a
  blocking `flush_sync()` so the usage ledger never loses buffered rows.
- **Usage & cost insights**: persistent ledger + `GET /admin/insights`
  (per profile/model/day burn; provider-reported vs estimated costs).
- **Three-tier auth**: master key / deterministic `sk-<profile>` client keys
  / custom overrides.
- **Hot-reload everything**: credentials CSV + policy YAML are re-read
  atomically (~5s). No restarts, ever. The CSV reload is **two-phase**: a
  lint (row/column, e.g. non-numeric `intelligence_score`) plus a full shadow
  instance check (no empty groups, no duplicate ids) runs first; only if it
  passes does the atomic swap happen, otherwise the previous config stays
  intact and the exact problem is logged.
- **Terminal UI** (`./scrocco.sh`) + Prometheus `/metrics`, plus an optional
  web panel under `web/`.

## How routing works (short)

1. **Resolve** the requested model/alias to a group (`-vision`, `-200k`, …).
2. **Estimate** tokens and choose the target rung: the requested one, or the
   smallest dim that fits the estimate (`dims_ladder_floor`). The per-deployment
   `max_input` is enforced on every pick (even for explicit `-Nk` requests) and
   the outgoing `max_tokens` is clamped to the remaining window.
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
- `order`: optional explicit per-deployment ordering (integer). Lower value =
  earlier; deployments sharing the same value form a "tier" (you can aggregate
  providers by giving the same value to their deployments, e.g. to prefer a
  set of fast free providers before a slower but reliable one). Empty/absent =
  neutral, appended last. Text groups order by tier then context; within a
  tier the adaptive pick is unchanged. `-go`/`-fallback` stay last.
- `enabled`: optional declarative on/off switch (default true; `false`/`0`/`no`/
  `off` = disabled). A disabled row stays in the CSV — its provider/key pair is
  preserved — but is excluded from every routing bucket (dims, `-go`,
  `-fallback`, capability groups). Use it when a provider has no free models
  right now instead of deleting rows.
- `api_style`: optional upstream protocol for this row (default empty = `chat`,
  i.e. `/chat/completions`). `responses` (OpenAI Responses), `messages`
  (Anthropic) or `google` (native Gemini) make the gateway translate the request,
  response and SSE stream to/from that API (correct URL, auth header and body).

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
| `app/toolrepair.py` | tool-call argument repair (safe/aggressive), streaming SSE filter |
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
| `ladder_skip_after` / `ladder_stale_max` / `ladder_cooldown_wakeups` | 10 / 3 / 3 | attempts per dim before climbing / stale revivals / cooled-dim wakeup probes per request before `-go` |
| `cold_spread_pct` | 0.20 | cold-pick load spreading: hide the top % most-attempted dims (last 24h, ok+fail) so under-used providers get traffic regardless of `order`; session-owned deps are always exempt; also sets `min_pool = ladder_skip_after` |
| `initial_pick_cooldown_wakeup` | true | retry a stale cooled dim at the very first pick (before esc-win/ladder) |
| `cooldown_retry_max_fail_24h` / `chronic_fail_cooldown_sec` | 10 / 7200 | chronic threshold / mandatory pause after re-failure |
| `cooldown_probe_enabled` / `cooldown_probe_after_ratio` / `cooldown_probe_decay` | true / 0.5 / true | passive probe of cooled-down keys once 50% through their cooldown; penalty decays linearly |
| `cooldown_streak_halflife_sec` / `probe_retire_after` / `cooldown_jitter_ratio` | 1800 / 5 / 0.12 | streak decay while idle; auto-retire after N failed probes; cooldown jitter (±12%) |
| `cooldown_autoprobe_enabled` / `cooldown_autoprobe_per_dim` / `cooldown_autoprobe_max_total` | true / 2 / 6 | call-triggered probe of cooled text dims: targets per dim / total per pass |
| `cooldown_autoprobe_min_age_sec` / `cooldown_autoprobe_grow_sec` / `cooldown_autoprobe_min_gap_sec` / `cooldown_autoprobe_timeout_sec` | 300 / 120 / 60 / 20 | probe only cooled ≥N s; on KO residual at least doubles (min +grow, rotate targets); min gap between probes; probe timeout |
| `cooldown_autoprobe_multiply_24h` / `cooldown_autoprobe_skip_over_sec` | true / 7200 | KO increment × probes in the last 24h (1×, 2×, 3×…); cooled > 2h excluded from probing (ladder wakeup / last resort / time will retry) |
| `session_dep_guard.enabled` / `session_dep_guard.sec` | true / 900 | anti-usurpazione: un deployment free-dims servito con successo da un'ALTRA sessione negli ultimi N s resta eleggibile solo nel tier pre-ultima-spiaggia; N s di silenzio e torna libero |
| `warm_pool.enabled` / `warm_pool.ttl_sec` / `warm_pool.max_attempts` | true / 0 / 0 | tier "caldi" prima del `-dim` e della scala: esaurisce i free-dims serviti con successo da QUESTA sessione (ordine: cache-holder, MRU, `order`, `max_input`); `ttl_sec=0` usa `session_dep_guard_sec`; `max_attempts=0` illimitato; esclude i dep lenti (EMA > 90s o lenti-per-sessione) e non pesca mai dim < richiesta |
| `reputation_decay_halflife_sec` | 129600 | half-life (36h) for the time-decay of reputation scores; 0 = off |
| `adaptive_timeout_enabled` / `adaptive_timeout_floor_sec` / `adaptive_timeout_multiplier` / `adaptive_timeout_max_sec` | true / 15 / 8 / 600 | per-deployment chat read timeout from latency EMA: `max(floor, avg*mult)`, capped |
| `escalation_pin` / `escalation_pin_probe_dims` | true / 2 | escalation-winner shortcut and pre-pin probe count |
| `qc_json.stream_first_content_ms` / `stream_total_deadline_ms` | 240000 / 960000 | first-content deadline per deployment / total request deadline |
| `qc_json.stream_first_content_adaptive` / `stream_first_content_mult` / `stream_first_content_floor_ms` | true / 3.0 / 20000 | adaptive first-content deadline: `min(stream_first_content_ms, max(floor, mult * latency EMA))`; unknown EMA -> the cap. Avoids holding a 180s window on a normally-fast dep that stalled; the EMA used is the bucket of the CURRENT request (TTFT table) |
| `qc_json.stream_hedge_delay_ms` | 1500 | cold-chain first-content hedge: wait N ms, then ONE canary on the next candidate, commit whoever answers first (stream only, pre-byte, never paid buckets); 0 = off |
| `error_class_cooldowns` / `cooldown_transient_sec` / `cooldown_timeout_sec` | true / 15 / 60 | class-aware cooldowns: 503/529 and 500/timeout pause only that deployment briefly, key reputation untouched; 429/quota never penalizes reputation (per-key soft blackout instead); explicit `seconds` always wins. false = legacy (`timeout_cooldown_mult` x10) |
| `cooldown_jitter_sec_max` | 2.0 | deterministic per-deployment cooldown spread `sha256(unique) → 0..2 s` (also on the per-key 429 blackout); 0 = off. Replaces the random `cooldown_jitter_ratio` (now 0) |
| `stream_stall_ttft_mult` / `stream_stall_max_sec` | 2.5 / 60 | adaptive anti-stall watchdog: `max(stream_stall_sec, min(TTFT_p50_bucket × mult, max_sec))`; empty bucket → base |
| `retry_after_min_sec` | 10 | minimum cooldown floor applied to 429s that return a tiny/absent Retry-After (anti-loop; 0 disables) |
| `retry_after_floor_by_provider` | `{}` | per-provider Retry-After floor (provider -> seconds), overrides `retry_after_min_sec` |
| `rate_hint_skip_enabled` / `rate_hint_ttl_sec` / `rate_hint_remaining_max` / `rate_hint_proven_sec` | true / 20 / 5 / 900 | soft key skip from fresh rate headers (free dims only, zero blame); while headers stay this fresh, `budget_guard.suppress_with_headers` ignores the learned caps |
| `key_soft_429_enabled` / `key_soft_max_sec` | true / 900 | 429 -> soft blackout of ALL rows sharing that api_key for Retry-After seconds (capped); no strikes, no reputation damage |
| `cache_aware.prefix_audit` | true | per-session prefix content-hash audit: `[cache-audit]` logs `identity`/`prefix` when the conversation prefix mutates unexpectedly |
| `history_normalize.reasoning_content_max_chars` / `reasoning_keep_recent` | 0 / 1 | trim (N>0) or strip (0) OLD assistant `reasoning_content`; -1 = never touch; the last keep_recent turns are spared |
| `anon_session_fingerprint` | true | derive a deterministic `fq_<hash>` session id for anonymous clients (system + first user + user-agent) so sticky/cache apply (e.g. Hermes); false = stay anonymous |
| `anon_session_fp_system_chars` | 768 | anonymous fingerprint hashes only the first N chars of the system prompt (0 = whole prompt), tolerating per-turn appended context |
| `provider_models_ttl_sec` | 300 | in-memory TTL for the once-per-endpoint `GET /models` cache (0 = no cache) |
| `estimate_adaptive_enabled` / `estimate_adaptive_shadow` | false / true | adaptive token estimate (per-block density); shadow computes+logs both but keeps the legacy value |
| `tool_repair.enabled` / `tool_repair.default_level` | true / `aggressive` | tool-call argument repair and default level (per-deployment CSV overrides) |
| `tool_repair.disable_for_google` | true | Google/Gemini deployments opt out unless explicitly enabled in the CSV |
| `tool_repair.fake_call.enabled` | true | detect tool-calls rendered as text and escalate directly to -go/-fallback |
| `tool_repair.fake_call.max_escalations` | 2 | max direct escalations before a retryable 503 |
| `cache_aware.prefer_last_success` | true | on failover prefer the session's last-success deployment (free buckets only) |
| `cache_aware.holder_ttl_sec` | 3600 | how long the per-session cache holder is remembered |
| `cache_aware.skip_probe_when_holder` | true | skip the escalation-pin probe when the pinned winner is the holder |
| `cache_aware.context_truncation.enabled` | true | stub old tool outputs (overflow / absolute / cache-cold switch triggers) |
| `cache_aware.context_truncation.keep_turns` | 4 | number of most recent user turns kept intact |
| `cache_aware.context_truncation.head_chars` / `tail_chars` | 600 / 600 | fixed head/tail chars kept (line-boundary cut) inside each old tool output; 0/0 = bare legacy stub |
| `cache_aware.context_truncation.keep_tail_pct` | 2.0 | dynamic frontier: the message tail is protected while it fits in this % of the deployment window (tightens inside `keep_turns` on huge windows; 0 = off; floor 8 msgs) |
| `cache_aware.context_truncation.keep_error_outputs` | true | tool outputs containing Traceback/…Error/Exception or exit≠0 are never rewritten (overflow included) |
| `cache_aware.context_truncation.min_ctx_tokens` | 50000 | absolute context threshold that also triggers trimming |
| `cache_aware.context_truncation.on_deployment_switch` | true | trigger trimming when the cache is cold (no holder / different deployment) |
| `cache_aware.context_truncation.switch_min_tokens` | 8000 | minimum context to apply the deployment-switch trigger |
| `cache_aware.context_truncation.abs_headroom_ratio` | 0.8 | anti-churn hysteresis: the absolute trigger fires only within this fraction of the deployment window (0 disables) |
| `cache_aware.context_truncation.reasoning_headroom_ratio` | 0.7 | extra-early absolute trigger for `effort_capable` (reasoning-only) deployments, reserving window for the thinking block (0 disables) |
| `cache_aware.context_truncation.tool_args_max_chars` | 2000 | JSON-aware trim of oversized old `tool_calls` arguments (only long string values; output stays valid JSON); 0 = never touch args |
| `cache_aware.context_truncation.json_struct_max_items` / `json_struct_head` / `json_struct_tail` | 40 / 20 / 5 | JSON list/dict outputs with more than this many elements are cut STRUCTURALLY (first N + `{"...omessi":K,"totale":N}` + last M) keeping the JSON valid; 0 = off (char cut) |
| `cache_aware.context_truncation.cite_retention` / `cite_min_freq` | true / 2 | do not stub an old output while the protected tail still cites one of its distinctive tokens (≥ N occurrences; structural terms/tool names excluded) |
| `conc_token_ratio` | 0.5 | token-weighted concurrency: skip a row when `inflight_tokens + ctx_est > max_input × ratio`; hard count cap `conc_max_limit` still applies; 0 = legacy counting |
| `estimate_calib_alpha` | 0.05 | EMA rate at which each deployment's learned token divisor converges to the real one (`base/(prompt_tokens/ctx_est)`), clamped 1.5..4.5, persisted; 0 = off |
| `qc_json.stream_hedge_ttft_frac` / `stream_hedge_min_ms` / `stream_hedge_max_ms` | 0.6 / 800 / 2500 | adaptive hedge delay per context bucket: `clamp(TTFT_p50_bucket × frac, min, max)`; unknown TTFT falls back to `stream_hedge_delay_ms` |
| `request_coalescing_cache_sec` | 0 | also serve an identical non-stream payload arriving within this many seconds after the leader completed (credits/cost halved for tight retries/subagents); 0 = in-flight only |

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
`GET /models` **once per endpoint** (first working key wins; sibling keys are
skipped — same list, avoids anti-DDoS noise). They are read-only unless `--fix`:

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
