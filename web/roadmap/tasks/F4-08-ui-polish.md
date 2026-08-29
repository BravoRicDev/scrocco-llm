---
id: F4-08-ui-polish
fase: F4
dipende_da: [F0-08-layout-theme]
puo_parallelo_con: F4-05-key-health, F4-06-alerts, F4-07-sticky-sessions
---

# F4-08 — Temi dark/light, ricerca globale, shortcut da tastiera

## Obiettivo

Completare la UX: toggle dark/light (persistito in localStorage, attivo da
`data-theme` su `<html>`, già predisposto in F0-08), layout denso supremo,
ricerca globale (filtra i link della sidebar + comando jump) e shortcut da
tastiera (`/` focus ricerca, `g d` dashboard, `?` aiuto, esc).

## File da creare/modificare

- `scrocco-web/views/layouts/admin.ejs` (modifica)
- `scrocco-web/public/css/app.css` (modifica — override `[data-theme="dark"]` +
  classi per il toggl)
- `scrocco-web/public/js/app.js` (modifica — estendi `scw` con theme,
  ricerca, shortcut)
- `scrocco-web/views/partials/search.ejs` (crea — componente input ricerca;
  **NON modificare `sidebar.ejs`**, che resta di proprietà dei task
  `-integration`: l'include del partial verrà aggiunto in F4-09 quando F4-09
  tocca la sidebar)
- `scrocco-web/test/ui-helpers.test.js` (crea, opzionale)

## Contratto

- Layout: `<html data-theme>`, bottone toggle nella topbar (luna/sole),
  `localStorage['scw-theme']`; default chiaro; applica prima del paint (mini
  script inline in `<head>`).
- `views/partials/search.ejs`: input `#navSearch` (id) con placeholder
  "Cerca…"; nessun altro markup (la sidebar lo include); i link della sidebar
  hanno già `data-nav-item` (attributo aggiunto in F4-09, che modifica la
  sidebar) — qui il JS aggancia `[data-nav-item]` con querySelectorAll.
- `app.js`: 
  - `scw.setTheme(theme)`, `scw.toggleTheme()`, preferenza da
    `localStorage` + `prefers-color-scheme`.
- Ricerca globale: input `#navSearch` (in `partials/search.ejs`) filtra
     `[data-nav-item]` (mostra/
     nasconde link) con evidenza; `g` + testo = jump.
  - Shortcut: `?` mostra Help modal (elenco), `/` focus ricerca,
    `g d`/`g l` ecc via rotta path, `esc` chiude modali/azzera ricerca.
    Nessuno shortcut con input focused.
- `app.css`: varianti dark (override variabili), `.nav-search`, `.kbd`,
  highlight ricerca.

## Criterio di done

`node --check` (solo i file modificati JS). Manuale: toggle cambia
`data-theme` e persiste; digitare in ricerca nasconde i link; `/` focus.
Niente test automatico richiesto (UI), ma se aggiungi `test/ui-helpers.test.js`
deve verificare pure functions (es. `scw.applyFilter(links, q)`).

## Rischi / note

- Nessuna libreria aggiunta: tutto in vanilla JS.
- Non modificare il CSS esistente oltre ad aggiungere override/blocchi nuovi
  (in coda al file).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Migliora la UX del pannello.

Modifica SOLO 4 file (append/estensione, non riscrivere):
1. `views/layouts/admin.ejs` — meta + script inline che legge
   `localStorage["scw-theme"]` e setta `document.documentElement.dataset.theme`
   PRIMA del render del body; bottone tema nella topbar (id="themeToggle");
   includere già `/js/app.js`.
2. `views/partials/search.ejs` (crea) — input `#navSearch`; NON toccare
   `sidebar.ejs` (di F4-09); aggiungi `data-nav-item` ai link SOLO se la
   sidebar li ha già, altrimenti lascia a F4-09 l'attributo e qui usa
   querySelectorAll su `[data-nav-item]` (guard `if (links.length)`).
3. `public/js/app.js` — estendi `window.scw`:
   - `scw.theme`: `setTheme`, `toggleTheme`, init (light default o saved).
   - `scw.search`: input `#navSearch` filtra `[data-nav-item]` (match
     case-insensitive su href/testo), pulisci su vuoto.
   - `scw.keys`: keydown handler — `/` (se non c'è focus su input) → focus
     navSearch; `?` → apre help modal (ul kbd); `g` `d` → location /
     dashboard (Buffer one-key g then d); `g` `l` ecc per i principali;
     `esc` → chiude modal/clear search.
   - Guard: ignora se `e.target` è input/textarea/select/contenteditable.
3. `public/css/app.css` — APPEND: `[data-theme="dark"] { --bg:#12141a;
   --sidebar:#171a23; --accent:#6366f1; --text:#e5e7eb; --muted:#9ca3af; ... }`,
   `.nav-search`, `.kbd`, `.hl` (highlight query).

Verifica `node --check public/js/app.js`. Non toccare altri file. Riepiloga in
2 righe.