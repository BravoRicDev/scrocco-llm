# Security

## Authentication model

Three levels, checked in order (`app/auth.py`):

1. **Master key** — from `GATEWAY_MASTER_KEY` (or the `--master-key` argument).
   Grants admin access and access to every model. If unset, a random key is
   generated at startup and logged (the service still starts, but you cannot
   authenticate). Weak values (`sk-master`, `*change-me*`, empty) are detected.
2. **Explicit client keys** — `client_keys: {<profile>: <key>}` in
   `var/gateway.yaml`. A request presenting such a key is bound to that profile.
3. **Deterministic keys (development only)** — `sk-<profile>` (e.g. `sk-alice`)
   is accepted **only** when `<profile>` exists in the CSV and has no explicit
   key, and **only** when `GATEWAY_ENV` is not `production`.

`authorize_model` enforces the per-profile model whitelist (master bypasses it).

## Production hardening

Set `GATEWAY_ENV=production` (or `prod`). Then:

- deterministic `sk-<profile>` keys are **rejected** (explicit `client_keys` are
  required);
- startup is **fail-fast**: `AuthManager.enforce_startup()` raises and the
  container aborts if the master key is missing/placeholder or `client_keys` is
  empty;
- `GET /bootstrap` reports `no_client_keys` and `master_key_is_default` as
  **errors** instead of warnings.

Generate and install client keys with:

```bash
python scripts/gen_client_keys.py --profiles alice,bob          # print YAML
python scripts/gen_client_keys.py --profiles alice,bob --write  # update policy (backup .bak.<ts>)
```

Rotate the master key by changing `GATEWAY_MASTER_KEY` and recreating the
container. Never reuse a key across environments.

## Secrets handling

- `var/keys_rotation.csv`, `var/gateway.yaml`, `.env.gateway`, `web/.env` and
  `var/*` (except `*.example`) are **gitignored**. Keep it that way.
- The repository is **public**: do not commit real provider keys, master keys,
  client keys, internal hostnames, private IPs, customer names or absolute
  paths containing usernames. Use the neutral placeholders in the `.example`
  files (`example`, `host-a`, `sk-CHANGE-ME`, `sk-FAKE-KEY-...`).
- Operator scripts read their path/profile from the environment
  (`SCROCCO_CSV`, `SCROCCO_PROFILE_COL`) precisely so that no real value is
  hard-coded.
- If a real secret is ever committed, treat it as compromised: rotate it at the
  provider **and** purge it from history (`git filter-repo` + force-push) — a
  later commit removing it does not remove it from history.

## opencode.ai upstreams (zen/go)

`opencode.ai` upstreams are gated per client (`app/opencode_gate.py`):

- a **real opencode client** is recognised by `user-agent: opencode/...` or any
  `x-opencode-*` header and may use zen/go normally;
- a **non-opencode client** (e.g. a generic SDK) may use them only when
  `OPENCODE_SPOOF_HEADERS` is enabled, in which case the gateway synthesises a
  native `ses_...` session id and the required `x-opencode-*` headers;
- with `OPENCODE_CAUTIOUS` (default = spoof) those spoofed requests get zen as a
  last-of-free-tier block and never reuse zen warm owned by a real opencode
  session;
- `OPENCODE_GO` (default on) controls the paid `opencode-go` bucket
  independently.

`BACKGROUND_CAUTIOUS` disables automatic probes/health/nightly for **all**
providers: use it to avoid burning quota or attracting attention when the
gateway is running in a "quiet" mode.

## Reporting

Security issues: open a private advisory on the repository or contact the
maintainer directly. Do not paste real keys, hostnames or customer data into
issues or pull requests.
