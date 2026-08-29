---
id: F1-08-insights-view
fase: F1
dipende_da: [F0-11-integration-smoke]
puo_parallelo_con: tutti gli altri F1 read
---

# F1-08 — Pagina insights + summary (sola lettura)

## Obiettivo

Pagina `/insights` che mostra consumo/costi aggregati dagli endpoint
`/admin/insights?days=&group_by=` e `/admin/insights/summary` (24h): tabella
per profilo/modello/deployment/day/kind con token, costi (reported/stimato),
durata media, fallback/qc/wd/bad rate. SOLA LETTURA (i grafici uPlot sono F3).

## File da creare/modificare

- `scrocco-web/src/routes/insights.js` (crea — GET /insights)
- `scrocco-web/views/insights/index.ejs` (crea)

## Contratto

- `GET /insights?days=&group_by=` [requireAuth, authorize("insights","read")].
- `days` default 7 (clamp 1..90); `group_by` in profile|model|deployment|day|
  kind|none.
- Dati: `gateway.get('/admin/insights',{params:{days,group_by}})` →
  `{total:{calls,days}, by_<group_by>: {key:{calls, prompt_tokens,
  completion_tokens, total_tokens, cost_reported_usd, cost_estimated_usd,
  avg_dur_ms, fallback_rate, qc_rate, wd_fail_rate, bad_rate}}}`; e
  `gateway.get('/admin/insights/summary')` → 24h `{window_hours, calls,
  total_tokens, cost_reported_usd, cost_estimated_usd, by_kind}`.
- View: card summary 24h; form (days, group_by); tabella per gruppo con
  percentuali inline; sort client-side per colonna (click header) leggero in
  JS.

## Criterio di done

Render manuale con mock, `node --check`.

## Rischi / note

- `group_by=none` ritorna `{total, aggregate}` → render una riga unica.
- Percentuali: tasso 0..1 → mostra come % (val*100).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la pagina read-only insights.

Crea:
1. `src/routes/insights.js` — Router; `GET /insights` requireAuth +
   `authorize("insights","read")`; `?days` (default 7, clamp 1..90), `?group_by`
   (default "model"; valida contro l'elenco); chiama entrambi gli endpoint
   (`/admin/insights` e `/admin/insights/summary`), gestendo il caso
   `group_by=none`; render `views/insights/index.ejs` con `{data, summary,
   days, groupBy}`.
2. `views/insights/index.ejs` — card summary 24h (chiamate, token, costi);
   form GET (days + group_by); tabella dinamica: chiave, calls, prompt_tokens,
   completion, total_tokens, cost reported, cost estimato, avg_dur_ms, fb%,
   qc%, wd%, bad%; tassi mostrati in % (x100). Order-preserving; JS mini per
   sort client sul click header (aggiungi inline handler senza librerie).

Non toccare altri file. Verifica `node --check` + render smoke. Riepiloga in 2
righe.