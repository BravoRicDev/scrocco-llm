# Routing

The router turns a client request for a *group* into a concrete *deployment*,
then walks alternatives until one succeeds. This document describes the model
and the order in which candidates are tried.

## Vocabulary

- **Deployment**: one CSV row = provider + endpoint + model + key (+ metadata).
  Identified by `unique` = `<group>__<slugified-model>__<index>`.
- **Profile**: a named set of deployments and their dimension groups
  (the per-host key column in the CSV, e.g. `scrocco-llm-<profile>`).
- **Group**: the client-facing model id, e.g. `scrocco-llm-<profile>-200k`.
- **Dimension (`-Nk`)**: a context-size bucket group built from *free* rows
  (e.g. `-32k`, `-128k`, `-200k`, `-1000k`). Requested as `...-<dim>k` or
  auto-selected by `resolve_group_for_request`.
- **Buckets**: `-go` (paid/priority "go" rows) and `-fallback` (last-resort
  rows, also paid). They are *escalation* buckets, not dimension groups.
- **Warm**: a deployment that recently served a session successfully and can be
  reused cheaply (kept in the session's warm pool).
- **Cooldown**: a temporary penalty applied to a deployment after a failure.

## Tiering and ordering

Each row has an integer `order` (lower = preferred). The router never reads
`dep["order"]` directly in the hot path; it calls `Router._eff_order(dep)`:

- outside caution → `int(order)`;
- when the request is *spoofed* under opencode caution and the dep is an
  **opencode-zen** dep → `ORDER_LAST`, i.e. pure free tier, tried last.

`-go`/`-fallback` buckets live *after* all free dims in the ladder, so the
effective order for a spoofed request is:

```
free non-zen (by order)  →  zen (last of the free tier)  →  -go  →  -fallback
```

Provider chain interleaving inside a tier is done by `_provider_chain`
(round-robin across distinct providers, providers already warm are pushed to the
end). `_text_ladder` builds the ordered ladder for a group; `config.chains[profile]`
is the global profile ladder (dims + `-go` + `-fallback`).

## `initial_pick` — choosing the first deployment

Order of attempts (`Router.initial_pick`):

1. **Warm pool** — `routing/warm._warm_pool` for the session (own + borrowed).
   Skipped when the *requested* group is an escalation bucket
   (`_is_renewal_bucket`) or a capability group.
2. **Sticky deployment** — `dep_sticky_*` (a deployment the session already used).
3. **Adaptive pick** — `pick_deployment` on the requested group: filters by
   capability (`_cap_fits`, `_dep_supports`), cooldown, circuit breaker and
   `opencode_gate.dep_usable`, then scores candidates (priority, EMA latency,
   last-used, effort compatibility, provider bias…).
4. **Cold cooldown wakeup** — optionally retries a matured cooled dep
   (disabled under *generic* caution / `BACKGROUND_CAUTIOUS`).
5. **Escalation winner** — `_try_esc_win` may shortcut to a pinned/`-go` dep.
6. **Resilient ladder** — `_walk_ladder_resilient` over the profile ladder
   (this is the long tail, see below).

## Warm pool and borrowing

- `note_warm_owner(session, unique)` records that a deployment served a session
  (in `var/routing_state.json`).
- `_warm_pool(session)` returns the session's own warm deployments plus
  deployments warm in *other* sessions that are idle enough
  (`warm_borrow_idle_sec`) — idle sessions "lend" their warm deployments.
- A warm deployment is only offered if `warm_valid_for` still holds
  (TTL `_warm_ttl`, deployment alive, capability fit, `dep_usable`).
- **opencode-zen warm exception**: when the request is spoofed under caution, a
  warm zen deployment is reused only if its owner session is **not a native
  opencode session** (`ses_...`). Zen warm entries owned by fake sessions are
  reusable; those owned by a real opencode client are not.
- `warm_ready_effective` computes the target warm count (`session_rpm`-driven);
  `warm_valid_for` returns the current valid set; when the set is short the
  gateway triggers a **refill** (canary), either proactively in streaming or
  before a retry in non-streaming.

## Canary and hedge (speculative work)

- `warm_fill_canary` — warms a *new* free candidate for the session.
- `warm_wake_canary` — re-warms a cooled free deployment whose cooldown matured.
- `hedge_canaries` — extra one-shot probes used when the primary looks slow.
- Canaries are **free-only** (they never pick `-go`/`-fallback`) and are only
  started when the *requested* group is a normal (non-escalation) group: if the
  client explicitly asked for `-go`/`-fallback`, no warm/refill/canary/slow-race
  is started. If the gateway merely *fell back* to `-go`, speculative work stays
  active so it can climb back to the warm free tier.

## The resilient ladder — `_walk_ladder_resilient`

Walked with early escalation and linear cooldown. Steps (8, plus the zen block):

1. **dims alive** — up to `ladder_skip_after` live candidates.
1ter. **pre-last shared** — dims recently used by *another* session (shared here,
   before the paid buckets).
1bis. **dims cooldown-wakeup** — matured 429 cooldowns (disabled under generic
   caution).
1quater. **next dim** — continue climbing context dimensions.
2. **`-go` alive** — paid bucket.
3. **dims stale** — cooldowns older than `stale_cooldown_retry_sec`.
3bis. **[opencode caution] ZEN block** — zen alive + zen stale, as a separate
   block *after all free non-zen* and *before* `-go`. Implemented by splitting
   the ladder with `_zen_split` so rotation inside `_walk_chain` can never pull
   zen back to the front.
2bis. **`-go` alive (caution position)** — the `-go` step moves here under
   caution, i.e. after dims-stale and the zen block.
4. **`-go` stale** — dormant paid deployments.
4bis. **chronic parachute** — deployments failing repeatedly (`fail_24h` ≥
   threshold), retried from least- to most-failing; excluded zen under caution.
5. **`-fallback`** — the whole fallback bucket, cooldown ignored.
6. **last resort** — cooled deployments, cooldown ignored; zen sorted last.
6bis. **extreme last resort** — retired-but-not-permanent deployments; zen last.

Notes:

- `_walk_chain(chain, failed_unique, …)` walks an ordered list of uniques.
  When resuming after a failure with a dimension floor it *rotates* the chain
  (`chain[i+1:] + chain[:i]`) so it continues after the failed dep and wraps.
  Keeping zen out of the walked chain (via `_zen_split`/`_zen_split3`) is what
  prevents zen from reappearing immediately after the first failure.
- `_zen_split(uniques) -> (nonzen, zen)` and
  `_zen_split3(uniques) -> (free_nonzen, zen, gofb)` return unchanged lists when
  the request is not cautious, so non-opencode/uncautious behaviour is identical
  to before.
- `slow_race_allowed` decides whether an extra "slow race" call is allowed for a
  session; `_is_renewal_bucket(group)` is used to detect escalation buckets.

## Fallback — `fallback_after` / `fallback_next`

After a failed attempt, the next candidate is chosen by `fallback_after`
(wrapped by `fallback_next`):

- **scope**: if the requested group was explicit, stay within that group's
  chain; otherwise continue the profile ladder.
- it respects `need`, cooldowns and `dep_usable`, and honours the zen ordering
  (`_eff_order` / split).
- it performs the same last-resort stages as the ladder (cooled, then retired).
- `mark_failed(unique, seconds, reason)` sets a linear cooldown with jitter;
  quota errors (`maybe_account_quota_cooldown`) and mid-stream 502s
  (`maybe_host_transient_cooldown`) get specialised handling. A failed *wakeup*
  doubles the residual; a success clears the cooldown.

## Reputation, circuit breakers, sticky

- Per-deployment stats (EMA latency, success rate, streaks) live in
  `var/adaptive_stats.json`; `_reputation_score` combines them with recency.
- Circuit breakers (`_cb_*`) and key-level failure classification
  (`_is_key_level_failure`) prevent hammering a broken key/model.
- `KeyHealth` retires keys after repeated failures and can un-retire them via
  `/admin/deployments/unretire` (or a successful probe).
- Sticky sessions keep a session glued to the deployment/group it was using
  (`sticky_ttl`, `session_dep_guard_*`) to preserve provider-side prompt caches.

## opencode gate — zen/go, spoof and caution

`app/opencode_gate.py` centralises the rules for `opencode.ai` upstreams:

- `is_opencode_dep` (any `opencode.ai` base), `is_opencode_zen_dep`
  (`zen` in provider), `is_opencode_go_dep` (opencode.ai, not zen).
- `client_is_opencode(headers)` — `user-agent: opencode/...` or any
  `x-opencode-*` header.
- `dep_usable(dep)` — non-opencode deps: always usable; **zen**: requires
  `allow_opencode_zen()`; **go**: requires `opencode_go_enabled()`.
- `set_allow_opencode_zen(client_can_use_opencode_zen(attrs))` — a non-opencode
  client may use zen only when `OPENCODE_SPOOF_HEADERS` is enabled.
- `set_spoofing_request(spoof_enabled() and not client_is_opencode(attrs))` —
  "this request is being spoofed as opencode".
- `opencode_cautious_request() = spoofing_request() and opencode_cautious_enabled()`.

Semantics:

| Switch (env) | Default | Effect |
|---|---|---|
| `OPENCODE_SPOOF_HEADERS` | off | allow zen/go for non-opencode clients (synthesise native session + `x-opencode-*` headers upstream) |
| `OPENCODE_CAUTIOUS` | `= spoof` | demote zen to a separate block (last of the free tier); warm-owner rule; zen excluded from probe targets |
| `OPENCODE_GO` | on | enable the paid `opencode-go` bucket independently of spoof/client |
| `BACKGROUND_CAUTIOUS` | off | *generic* caution: disable probes/health/nightly/hotreload/cooldown-reprobe for **all** providers |

Real opencode clients are never demoted: for them `spoofing_request()` is false,
so zen keeps its native order 0 and is tried first.
