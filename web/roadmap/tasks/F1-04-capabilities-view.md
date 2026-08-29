---
id: F1-04-capabilities-view
fase: F1
dipende_da: [F0-11-integration-smoke]
puo_parallelo_con: tutti gli altri F1 read
---

# F1-04 — Pagina capacità (sola lettura)

## Obiettivo

Pagina `/capabilities` che legge `/admin/state` e mostra il blocco
`capabilities`: per_capability, gruppi primary/go/fallback per profilo,
catene, strike auto-learn, contatori tts/stt, stato health proattivo (fedele
alla schermata TUI `m`). SOLA LETTURA (seed/audit in F2-08).

## File da creare/modificare

- `scrocco-web/src/routes/capabilities.js` (crea — GET /capabilities)
- `scrocco-web/views/capabilities/index.ejs` (crea)

## Contratto

- `GET /capabilities?profile=` [requireAuth, authorize("capabilities","read")].
- Dati: `gateway.get('/admin/state')` → il blocco `state.capabilities`
  (`routing_enabled`, `patterns`, `auto_learn{mode,threshold,strikes}`,
  `per_capability`, `fallback{profile→{cap:[unique]}}`, `groups{profile→
  {cap:{primary,go,fallback}}}`, `multimodal_last_resort`, 
  `same_model_failover`, `counters`). Anche `state.health` (proattivo).
- Select profilo (da `profiles` ripreso con `gateway.get('/admin/profiles')`).
- View: tabella per_capability, tabella gruppi del profilo selezionato
  (cap → primary/go/fallback), lista fallback per capacità (catena), health
  proattivo (last_cycle_at, marked, accounts), auto-learn strikes, contatori.

## Criterio di done

Render manuale con mock, `node --check`. Il blocco contatori ha chiavi
composte ("group,key") → render grezzo.

## Rischi / note

- `/admin/state` è pesante: una sola chiamata per render.
- Default profilo: primo di `profiles` o "tutti".

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la pagina read-only delle capability del gateway.

Crea:
1. `src/routes/capabilities.js` — default export Router; `GET /capabilities`
   con requireAuth + `authorize("capabilities","read")`; legge `?profile`;
   `gateway.get('/admin/state')` (usa il blocco `capabilities` e `health`) e
   `gateway.get('/admin/profiles')` per il select; render
   `views/capabilities/index.ejs` con `{state, profiles, currentProfile}`.
2. `views/capabilities/index.ejs` — riga status "routing ON/OFF" + health
   proattivo (enabled, last_cycle_at timestamp formattato, marked, accounts);
   tabella per_capability (cap → n deployments, badge modalità come TUI);
   tabella gruppi capacità del profilo corrente (cap → primary/go/fallback
   counts); lista "Fallback per capacità" ordinata (vision, video, audio,
   image_gen, tts, stt, tools) con catena di uniques tracciate; sezione
   auto-learn (mode, threshold, strikes). Form select profilo (GET).

Non toccare altri file. Verifica `node --check` + render smoke mock. Riepiloga
in 2 righe.