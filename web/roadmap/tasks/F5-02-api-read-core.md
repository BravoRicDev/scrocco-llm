---
id: F5-02-api-read-core
fase: F5
dipende_da: [F1-03-policy-view, F1-05-dashboard-state, F1-07-history-view, F1-08-insights-view, F2-09-integration]
puo_parallelo_con: F5-01-api-read-deployments, F5-03-api-write
---

# F5-02 — Surface `/api/v1` READ (policy, state, history, insights, bootstrap)

## Obiettivo

Endpoint API-read per il resto del "tutto ciò che la TUI sa fare": policy,
state, history, insights+summary+leaderboard, bootstrap (playbook/status/
providers), guide. Stesso envelope e auth di F5-01.

## File da creare/modificare

- `scrocco-web/src/routes/api-core.js` (crea)
- `scrocco-web/test/api-core.test.js` (crea)

## Contratto

- `GET /api/v1/policy` [authorize policy read] → stesso shape di `/admin/policy`
  (chiavi mascherate). **Nota: `_mask_configured` lato gateway maschera SOLO
  `alias_keys` nel blocco `configured`; se lo yaml contiene `client_keys` in
  chiaro finirebbero echeggiati. Vincolo: la risposta inoltra SOLO i campi
  `*_masked` (`alias_keys_masked`, `client_keys_masked`) e non il blocco
  `configured` grezzo per intero.**
- `GET /api/v1/state` [state read] → `/admin/state` (stesso).
- `GET /api/v1/history?limit=` [history read].
- `GET /api/v1/insights?days&group_by` [insights read].
- `GET /api/v1/insights/summary` [insights read].
- `GET /api/v1/insights/leaderboard?window&sort&order&profile` [read].
- `GET /api/v1/bootstrap` [bootstrap read] → playbook testo.
- `GET /api/v1/bootstrap/status` [read] → gap analysis.
- `GET /api/v1/bootstrap/providers` [read].
- `GET /api/v1/guide` [guide read] → markdown (rawGet).

## Criterio di done

Test con API token: ogni GET ritorna 200 JSON envelope (guide/playbook = testo
o {text}). `node --check`.

## Rischi / note

- Guide/playbook: usa `gateway.rawGet` per il testo, altrimenti envelope
  `{text}`.
- Coerenza payload con la UI: riuso esatto.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea gli endpoint API-read core.

Crea:
1. `src/routes/api-core.js` — default Router, ogni rotta `requireAuth` +
   `authorize(resource,"read")` + `gateway.*` → JSON envelope. Per `guide` e
   `bootstrap` usa `gateway.rawGet('/admin/guide')` e
   `gateway.rawGet('/bootstrap')` (testo) → risposta `{text}`.
2. `test/api-core.test.js` — user + agtok_; verifica 200 per ognuna delle
   9 rotte e che `policy` non contenga chiavi in chiaro (asserisci chiavi
   `alias_keys_masked` ecc. presenti, `alias_keys` no).

Verifica `node --check` + npm test. Non toccare altri file. Riepiloga in 2
righe.