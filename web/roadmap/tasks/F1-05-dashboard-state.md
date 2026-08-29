---
id: F1-05-dashboard-state
fase: F1
dipende_da: [F0-11-integration-smoke]
puo_parallelo_con: tutti gli altri F1 read
---

# F1-05 — Dashboard (stato aggregato /admin/state)

## Obiettivo

Home `/` (o `/dashboard`) che mostra il quadro d'insieme dal gateway:
`/admin/state` (service/profiles/groups, cooldowns attivi, sticky, budget,
capabilities sintesi, health, adaptive, policy sintesi) e `/healthz` del nostro
server. È la dashboard principale dell'operatore.

## File da creare/modificare

- `scrocco-web/src/routes/dashboard.js` (crea — GET /)
- `scrocco-web/views/dashboard/index.ejs` (crea)

## Contratto

- `GET /` [requireAuth, authorize("state","read")] (redirect a /dashboard se
  preferisci; tieni `/` come dashboard).
- Dati: `gateway.get('/admin/state')` → strutture descritte in F1-04 più
  `cooldowns_active:[{unique,remaining_sec,attempts}]`,
  `sticky_sessions:[{session_id,group}]`, `budget`, `health`, `adaptive`,
  `policy{step_up_pct, step_up_per_profile, speed_hotwords, speed_min_dim_k,
  aliases, estimate_divisor, sticky_ttl_sec, cooldown_sec}`.
- View: KPI in alto (profiles, groups, deployments, cooldowns attivi, sticky,
  capacità text); sezioni: Health proattivo, Cooldown (tabella con unique/
  remaining/attemps), Sessioni sticky (count), Budget guard, Adaptive,
  Policy sintesi. Link veloci alle pagine dettaglio (F1/F2/F3).

## Criterio di done

Render manuale con mock, `node --check`. La home deve renderizzare senza
errori con fixture.

## Rischi / note

- `cooldowns_active` è una lista; formatted con remaining_sec → mm:ss.
- Page title "Dashboard".

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la dashboard del pannello.

Crea:
1. `src/routes/dashboard.js` — default export Router; `GET /` requireAuth +
   `authorize("state","read")`; `gateway.get('/admin/state')`; render
   `views/dashboard/index.ejs` con `{state}`. GatewayError → render error.
2. `views/dashboard/index.ejs` — card KPI (profiles, groups, deployments,
   cooldowns attivi, sticky, budget enabled, capability routing ON/OFF);
   sezione Cooldown (tabella unique, remaining mm:ss, attempts); sezione
   Sticky (session_id → group, count); sezione Health (last_cycle_at,
   marked, accounts, enabled); sezione Adattivo (enabled, tracked,
   recency_halflife_sec, latency_ref_ms); sezione Policy sintesi (step_up_pct,
   step_up_per_profile, aliases count, speed_hotwords count). Link alle pagine
   principali.

Non toccare altri file. Verifica `node --check` + render smoke mock. Riepiloga
in 2 righe.