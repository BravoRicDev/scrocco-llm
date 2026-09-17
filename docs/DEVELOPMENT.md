# Development

## Local setup

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt          # locked, with hashes
cp .env.gateway.example .env.gateway         # set GATEWAY_MASTER_KEY
cp var/keys_rotation.csv.example var/keys_rotation.csv
cp var/gateway.yaml.example var/gateway.yaml
```

Run the gateway locally:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 4001
```

## Tests

```bash
pytest -q                                    # full suite
pytest -q --cov=app --cov-fail-under=70      # coverage gate (CI uses 70%)
pytest -q tests/test_router_caps.py          # single file
```

`tests/conftest.py` isolates persistent state (key health and the
stats/cooldown/routing/thought-signature files) into a tmp dir per test, so the
suite is hermetic. There are no external network calls in tests: upstreams are
faked.

## Lint & types

```bash
ruff check .          # blocking in CI, conservative ruleset (E9,F63,F7,F82)
mypy app              # non-blocking in CI
```

To widen linting, add rules to `[tool.ruff.lint].select` in `pyproject.toml`
and fix incrementally.

## CI

`.github/workflows/ci.yml` runs two jobs on push/PR to `main`/`master`:

1. **python-tests** — Python 3.12: install `requirements-dev.txt`, `ruff`,
   `mypy` (non-blocking), then `pytest --cov=app --cov-fail-under=70`; uploads
   the coverage XML artifact.
2. **build-and-test** — validates all compose profiles
   (`docker compose ... config -q`), builds the image, starts the minimal
   profile, waits for `/healthz` and runs two HTTP smoke checks (401 without
   auth, 200 `/v1/models` with the master key).

## Reproducibility (locked dependencies)

Python dependencies are locked with hashes via pip-compile:

```bash
pip-compile --generate-hashes --strip-extras requirements.in      -o requirements.txt
pip-compile --generate-hashes --strip-extras requirements-dev.in  -o requirements-dev.txt
pip-compile --generate-hashes --strip-extras requirements-tui.in  -o requirements-tui.txt
```

Docker base images and sidecars are pinned by digest; refresh with:

```bash
docker buildx imagetools inspect <image:tag> --format '{{.Manifest.Digest}}'
```

`web/` uses `npm ci` against its committed `package-lock.json`.

## Code layout & conventions

- `app/main.py` — HTTP surface + pipeline; `app/router.py` — routing engine;
  `app/forwarder.py` — upstream HTTP; `app/admin.py` — admin API.
- Extracted routing mixins live in `app/routing/` (`warm.py`, `canary.py`,
  `sessions.py`) and are composed into `class Router(WarmMixin, CanaryMixin,
  SessionMixin)`. Each mixin keeps an explicit interface: it may only rely on
  `self.config`, `self.policy`, `self._dep_usable(...)` and a small set of
  router helpers/imports.
- Per-request state uses `contextvars` (session, effort, thought-sig flags,
  opencode gate) — safe under async and reset per request.
- Comments and docstrings are written in Italian (the maintainer's language);
  new/changed code should keep the same style, mixing English for
  protocol/technical terms.

### Adding a routing stage (mixin)

1. Create `app/routing/<name>.py` with `class <Name>Mixin:` containing the
   methods, moved **verbatim** (methods keep 4-space indentation as class
   methods).
2. Add the imports the methods need at the top of the file (the extractor/ruff
   will flag undefined names).
3. In `app/router.py`, import the mixin and append it to the `Router(...)`
   bases.
4. Run `ruff check` and the full suite; keep the diff behaviour-neutral.

### Browser/API compatibility

`app/protocols.py` adapts OpenAI Chat Completions to other upstream styles
(`responses`, Anthropic `messages`, Google `google`). Add a style by
implementing the request/response translation and `apply_auth` there, then set
`api_style` on the CSV row.
