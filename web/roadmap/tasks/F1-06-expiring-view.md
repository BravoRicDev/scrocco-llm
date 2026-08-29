---
id: F1-06-expiring-view
fase: F1
dipende_da: [F0-11-integration-smoke]
puo_parallelo_con: tutti gli altri F1 read
---

# F1-06 — Pagina scadenze (sola lettura)

## Obiettivo

Pagina `/expiring` che mostra i deployment con rinnovo imminente
(`/admin/deployments/expiring?days=`), con select sul numero di giorni e
dropdown profilo, come la schermata TUI `E`.

## File da creare/modificare

- `scrocco-web/src/routes/expiring.js` (crea — GET /expiring)
- `scrocco-web/views/expiring/index.ejs` (crea)

## Contratto

- `GET /expiring?days=&profile=` [requireAuth, authorize("expiring","read")].
- Dati: `gateway.get('/admin/deployments/expiring', {params:{days}})` →
  `{days, expiring:[{id, modello, in_days, data_raw}]}`. `days` default 7,
  clippato [1..90]. Filtra client-side per `profile` se passato (il modello
  di visualizzazione profilo: il gateway non filtra, mostra data_raw).
- View: header "X scadono entro N giorni", tabella id/modello/in_days/data_raw,
  ordinata per in_days, form GET con select giorni (7/14/30/60) e profilo.

## Criterio di done

Render manuale con mock, `node --check`.

## Rischi / note

- `data_raw` è la colonna data originale (es "15"): mostrarla com'è con hint.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la pagina read-only delle scadenze.

Crea:
1. `src/routes/expiring.js` — Router; `GET /expiring` requireAuth +
   `authorize("expiring","read")`; legge `?days` (default 7, clamp 1..90) e
   `?profile`; `gateway.get('/admin/deployments/expiring',{params:{days}})`;
   render `views/expiring/index.ejs` con `{days, expiring, profile}` fittato
   client-side per profile se presente (usa `gateway.get('/admin/profiles')`
   per il select).
2. `views/expiring/index.ejs` — form GET (select giorni + select profilo),
   tabella: id, modello, in_days (badge rosso se ≤3), data_raw; count nei
   giorni scelti; stato vuoto.

Non toccare altri file. Verifica `node --check` + render smoke mock. Riepiloga
in 2 righe.