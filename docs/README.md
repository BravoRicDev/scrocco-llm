# Documentation index

`scrocco-llm` is an OpenAI-compatible LLM gateway that fans a single endpoint
out over many upstream providers and API keys, with adaptive routing, warm
pools, canaries, cooldowns and per-client gating.

Start here:

| Document | What it covers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, request lifecycle (chat streaming and non-streaming), persistence map, extension points |
| [ROUTING.md](ROUTING.md) | Groups/dims/tiers, `initial_pick`, warm pool, canary/hedge, the resilient ladder, fallback, cooldowns, opencode zen/go gating |
| [CONFIGURATION.md](CONFIGURATION.md) | Environment variables, `var/gateway.yaml` policy reference, `var/keys_rotation.csv` columns |
| [OPERATIONS.md](OPERATIONS.md) | Deploy, compose profiles, admin API, TUI, observability, runbooks & troubleshooting |
| [SECURITY.md](SECURITY.md) | Auth model, `GATEWAY_ENV=production`, client-key generation/rotation, secret hygiene |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Local setup, test suite, CI, locked dependencies, code layout & refactors |
| [AGENT.md](AGENT.md) | MCP tool protocol exposed to agents |
| [BOOTSTRAP.md](BOOTSTRAP.md) | Zero-to-running self-bootstrap flow for agents |
| [FAKE_TOOLCALL_FALLBACK_SPEC.md](FAKE_TOOLCALL_FALLBACK_SPEC.md) | Spec: tool calls returned as plain text |
| [TOOL_REPAIR_SPEC.md](TOOL_REPAIR_SPEC.md) | Spec: tool-call argument repair |

User-facing docs live in the repository root: [`README.md`](../README.md)
(English) and [`README.it.md`](../README.it.md) (Italian).
