---
id: F1-02-profiles-view
fase: F1
dipende_da: [F0-11-integration-smoke]
puo_parallelo_con: tutti gli altri F1 read
---

# F1-02 — Pagina profili (sola lettura)

## Obiettivo

Pagina `/profiles` che elenca i profili del gateway (`GET /admin/profiles`) con
step-up, gruppi, deployment count, come la TUI (pannello profili). SOLA
LETTURA (create/purge in F2).

## File da creare/modificare

- `scrocco-web/src/routes/profiles.js` (crea — GET /profiles)
- `scrocco-web/views/profiles/index.ejs` (crea)

## Contratto

- `GET /profiles` [requireAuth, authorize("profiles","read")].
- Dati: `gateway.get('/admin/profiles')` → `{count, profiles:[{name,
  base_model, dims_k, groups, deployments, step_up_pct, speed_min_dim_k,
  speed_qualify_pct}]}`.
- View: card/tabella con nome profilo, base_model, dims_k, #groups, #deployments,
  step_up%, link "vedi deployment" → `/deployments?profile=<name>`.

## Criterio di done

Render manuale come in F1-01 senza errori con fixture mock (GATEWAY_MOCK=1).
`node --check` sulla route.

## Rischi / note

- `dims_k` è un array: render con join.
- Nuovo profilo (F2) crea il primo deployment; qui non serve il form.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la pagina read-only dei profili del gateway.

Crea:
1. `src/routes/profiles.js` — default export Router; `GET /profiles` con
   requireAuth + `authorize("profiles","read")`; `gateway.get(
   '/admin/profiles')`; render `views/profiles/index.ejs` con `{profiles,
   count}`. Gestisci GatewayError.
2. `views/profiles/index.ejs` — tabella: nome, base_model, dims_k (join ", "),
   gruppi, deployments, step_up%, speed_min_dim_k, speed_qualify_pct; link a
   `/deployments?profile=<name>`; header con count; stato vuoto.

Non toccare `src/index.js` né altri file. Verifica `node --check` e render
smoke con mock. Riepiloga in 2 righe.