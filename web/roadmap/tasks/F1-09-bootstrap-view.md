---
id: F1-09-bootstrap-view
fase: F1
dipende_da: [F0-11-integration-smoke]
puo_parallelo_con: tutti gli altri F1 read
---

# F1-09 — Pagina bootstrap (playbook + status + providers)

## Obiettivo

Pagina `/bootstrap` che mostra il playbook (`GET /bootstrap`), la gap-analysis
live (`GET /bootstrap/status`: issues + actions) e il registry provider
(`GET /bootstrap/providers`). Publie compatte per l'operatore.

## File da creare/modificare

- `scrocco-web/src/routes/bootstrap.js` (crea — GET /bootstrap)
- `scrocco-web/views/bootstrap/index.ejs` (crea)

## Contratto

- `GET /bootstrap` [requireAuth, authorize("bootstrap","read")].
- Dati: tre chiamate:
  - `gateway.get('/bootstrap')` → playbook markdown (stringa) — render con
    stile `<pre>` o conversione minimale (solo paragrafi/heading, niente
    librerie).
  - `gateway.get('/bootstrap/status')` → `{profiles, deployments,
    cap_coverage, issues:[{code,severity,detail,count?,items?}],
    actions:[{do,endpoint}], next}` — issues con severity
    (critical/warning/info) con badge colorati; ids `retired_keys` con
    `items.[].dead_since_days/last_reason`.
  - `gateway.get('/bootstrap/providers')` → `{disclaimer, data_categories,
    providers, research_hints}`.
- View: tab con 3 sezioni (Playbook, Status/Gap, Providers) via anch'ors o
  `details/summary`. Badge severità.

## Criterio di done

Render manuale con mock, `node --check`.

## Rischi / note

- `/bootstrap` può essere lungo: render in box scrollabile (max-height).
- `providers` è struttura grande: render come tabella compatto con disclaimer.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la pagina bootstrap (playbook+status+provider).

Crea:
1. `src/routes/bootstrap.js` — Router; `GET /bootstrap` requireAuth +
   `authorize("bootstrap","read")`; tre fetch (o Promise.all con try/catch
   per spendibilità: se uno fallisce mostra errore parziale);
   render `views/bootstrap/index.ejs` con `{playbook, status, providers}`.
2. `views/bootstrap/index.ejs` — tre sezioni `<details>`: Status/Gap (issues
   con badge per severity + actions con <code>endpoint</code>, cap_coverage
   table), Playbook (markdown in `<pre>` scrollabile), Providers (disclaimer +
   data_categories + tabella providers colonne essenziali).

Non toccare altri file. Verifica `node --check` + render smoke mock. Riepiloga
in 2 righe.