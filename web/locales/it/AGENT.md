# scrocco-web — Guida per agenti

`scrocco-web` è un pannello web + API JSON + server MCP davanti al gateway
`scrocco-llm`. Tiene la master key del gateway lato server; gli agenti si
autenticano a `scrocco-web` col proprio token e non vedono mai la master key.

## 1. Autenticazione

Manda `Authorization: Bearer <token>` su ogni richiesta. `<token>` è uno di:

- **JWT agente** — ottenuto con `scripts/agent-login.mjs` (email + password),
  valido 7 giorni, con claim `agent: true`. Anche via
  `POST /api/agent/login` `{email, password}` → `{token, user}`.
- **API token** (`agtok_…`) — a lunga durata, creato in UI su
  `/admin/api-tokens` o via `scripts/agent-login.mjs`. Salvato solo come
  SHA-256; il valore in chiaro è mostrato **una sola volta**. Revoca singola su
  `/admin/api-tokens` o `DELETE /api/agent/api-tokens/:id`. Durate consentite:
  30/60/90/120/180/365 giorni (default 120). Si mostra solo il prefisso
  (`agtok_xxxxxx…`).

Token mancante/non valido → `401 {"error":{"message":"…"}}`.

## 2. RBAC

Tre ruoli: `admin` (tutto), `operator` (CRUD/bulk/probe deployment, azioni di
sistema, seed capacità, playground), `viewer` (sola lettura).
`PATCH /api/v1/policy`, scrittura CSV, utenti e alert sono **solo admin**.
Azione non consentita → `403`.

## 3. Endpoint — `/api/v1`

### Lettura

| Metodo | Path | Query | RBAC |
|---|---|---|---|
| GET | `/api/v1/deployments` | `profile`, `q` | read |
| GET | `/api/v1/deployments/:id` | — | read |
| GET | `/api/v1/deployments/expiring` | `days` | read |
| GET | `/api/v1/profiles` | — | read |
| GET | `/api/v1/policy` | — | read (chiavi mascherate, niente `configured` grezzo) |
| GET | `/api/v1/state` | — | read |
| GET | `/api/v1/history` | `limit` (≤100) | read |
| GET | `/api/v1/insights` | `days`, `group_by` | read |
| GET | `/api/v1/insights/summary` | — | read |
| GET | `/api/v1/insights/leaderboard` | `window`, `sort`, `order`, `profile` | read |
| GET | `/api/v1/bootstrap` | — | read (testo → `{text}`) |
| GET | `/api/v1/bootstrap/status` | — | read |
| GET | `/api/v1/bootstrap/providers` | — | read |
| GET | `/api/v1/guide` | — | read (markdown → `{text}`) |

### Scrittura

| Metodo | Path | Body | RBAC |
|---|---|---|---|
| POST | `/api/v1/deployments` | `profile, modello, endpoint, data, key, context` (+ `provider, priority, caps`) | create |
| PUT | `/api/v1/deployments/:id` | qualsiasi sottoinsieme (`key` vuota = non ruota) | update |
| DELETE | `/api/v1/deployments/:id` | — | delete |
| POST | `/api/v1/deployments/bulk` | `{operations:[{action:create|update|delete, …}]}` (1..50, atomico) | bulk |
| POST | `/api/v1/deployments/probe` | `{id|unique, force?}` | probe |
| POST | `/api/v1/deployments/probe/bulk` | `{filter, force?}` | probe |
| POST | `/api/v1/deployments/unretire` | `{unique}` | unretire |
| POST | `/api/v1/system/reload` | — | system |
| POST | `/api/v1/system/cooldowns/clear` | `{unique?}` | system |
| POST | `/api/v1/system/sessions/release` | `{session_id?}` | system |
| POST | `/api/v1/capabilities/seed` | `{dry_run?}` | seed |
| POST | `/api/v1/capabilities/audit` | — | audit |
| PATCH | `/api/v1/policy` | oggetto policy parziale | **admin** |

Spec completa machine-readable: `GET /api/v1/openapi.json` · pagina umana: `/api/v1/docs`.

## 4. Esempi

```bash
BASE=http://127.0.0.1:3000
TOK=agtok_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# elenco
curl -s -H "Authorization: Bearer $TOK" "$BASE/api/v1/deployments?profile=mioaruba"

# crea
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"profile":"mioaruba","modello":"deepseek/deepseek-chat","endpoint":"https://openrouter.ai/api/v1","data":"free","key":"sk-or-…","context":200}' \
  "$BASE/api/v1/deployments"

# bulk
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"operations":[{"action":"delete","id":"…"},{"action":"update","id":"…","priority":9}]}' \
  "$BASE/api/v1/deployments/bulk"

# probe singolo (consuma quota upstream — una volta sola, mai in loop)
curl -s -X POST -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"unique":"…"}' "$BASE/api/v1/deployments/probe"

# patch policy (solo admin)
curl -s -X PATCH -H "Authorization: Bearer $ADMIN_TOK" -H 'Content-Type: application/json' \
  -d '{"step_up_pct":25}' "$BASE/api/v1/policy"
```

## 5. Errori

Tutti gli errori sono `{"error":{"message":"…"}}` con lo status HTTP:
`400` validazione, `401` token assente/non valido, `403` RBAC,
`404` non trovato, `503` gateway irraggiungibile.

## 6. MCP

`POST /api/mcp` — server MCP Streamable HTTP che espone la surface `/api/v1`
come tool (`deploy_list`, `deploy_create`, `policy_get`, `system_reload`,
`playground_run`, …). Richiede un token agente (JWT-agent o `agtok_`). Il
server inoltra ogni chiamata di tool alla rotta `/api/v1` corrispondente in
loopback, propagando il tuo header `Authorization` (quindi l'RBAC resta
valido). `GET`/`DELETE /api/mcp` → `405`.

## 7. Rate limit e buone pratiche

- `/api/mcp` e il login sono rate-limited per IP (loopback esente).
- **Il probe è costoso**: brucia quota free-tier. Sonda una volta, poi leggi
  il risultato cached; non farlo in loop.
- Per il monitoraggio usa `/api/v1/state` e `/api/v1/insights`: sono economici.
- Preferisci il bulk a tante scritture singole.
- Tratta `agtok_` come una password: salvalo in `~/.scrocco-web/agent.token`
  (mode 600), ruota/revoca se trapela.
