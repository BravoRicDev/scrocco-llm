---
id: F0-08-layout-theme
fase: F0
dipende_da: []
puo_parallelo_con: F0-01-repo-scaffold, F0-02-infra-docker-compose, F0-03-db-migrations, F0-04-core-services
---

# F0-08 — Layout EJS, sidebar, partials, static (tema chiaro)

## Obiettivo

Copiare/sfoltire il layout admin del CMS (`views/layouts/admin.ejs`) per
scrocco-web: sidebar con le sezioni previste, topbar con utente+logout+switch
profilo (placeholder), partials (flash, header, footer), CSS base (tema chiaro,
pronto per dark via `data-theme` su `<html>` da F4-08), JS base con helper
comuni (fetch/X-Request-Id, conferma azioni distruttive) e la pagina di login
`views/auth/login.ejs` + `views/auth/verify.ejs` (senza ancora le API di auth,
che sono F0-07; qui solo le viste).

## File da creare/modificare

- `scrocco-web/views/layouts/admin.ejs` (crea)
- `scrocco-web/views/partials/sidebar.ejs` (crea)
- `scrocco-web/views/partials/topbar.ejs` (crea)
- `scrocco-web/views/partials/flash.ejs` (crea)
- `scrocco-web/views/error.ejs` (crea, calco di `views/error.ejs` del CMS)
- `scrocco-web/views/auth/login.ejs` (crea)
- `scrocco-web/views/auth/verify.ejs` (crea — pagina per inserire OTP/magic-link)
- `scrocco-web/public/css/app.css` (crea)
- `scrocco-web/public/js/app.js` (crea)
- `scrocco-web/src/services/i18n.js` (crea — minimo, con fallback=chiave)

NON toccare: `db/`, `roadmap/`, `package.json`, altri file `src/` (in
particolare `src/index.js` è del task di integrazione F0-11).

## Contratto

- `res.locals.app`, `res.locals.user`, `res.locals.path`, `res.locals.query`,
  `res.locals.flash`, `res.locals.t` (fallback identità), `res.locals.lang`:
  sono tutti valorizzati in `src/index.js` dal task F0-11; le viste li usano
  già (JS guarda con `typeof` per non rompersi prima dell'integrazione).
- Sidebar (navbar): sezioni (usando i path che i task successivi creeranno):
  Dashboard, Deployments, Capacità, Policy, Scadenze, Osservabilità
  (Live / Errori / Classifica), Insights, History, Bootstrap, Guide,
  Playground, CSV editor, Key health, Sistema (cooldown/sessioni/reload),
  Utenti (admin — path `/users`, pagina creata in **F2-10**, visibile solo a
  `admin`), API token, Alerts (admin). I link a pagine non ancora
  esistenti possono comparire con classe `disabled` o essere commentati: verranno
  abilitati dai task di fase. NIENTE hard link rotti nel criterio di done.
- Topbar: utente, profilo gateway selezionato (stato locale) e logout via
  `POST /api/auth/logout` (endpoint F0-07).
- `app.js`: helper `scw.fetch(path, opts)` che alza `X-Request-Id`
  (crea-un UUID) e `CSRF` header da `<meta>` (CSS aggiunge il meta in layout);
  `scw.confirm(form, message)` che chiede conferma prima del submit delle
  azioni distruttive (data-destruct).
- CSS: variabili CSS `--bg`, `--sidebar`, `--accent`, ecc.; layout denso
  (font ~13px, tabelle compatte); predisporre `[data-theme="dark"]` override
  (non ancora attivo di default).
- `i18n.js`: `translate(lang, key)` → restituisce la chiave se assente in un
  dizionario minimo `{it, en}` per i pochi string di layout (nav, logout);
  export default; `res.locals.t = (k) => translate(...)`.

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
node --check src/services/i18n.js
# smoke: render di ogni vista con dati fittizi (senza index.js) NON è possibile
# direttamente; quindi verifica manuale:
node -e "const ejs=require('ejs'); ejs.renderFile('views/partials/sidebar.ejs',{user:{role:'admin'},path:'/',t:s=>s,app:{name:'x'},lang:'it'},(e)=>{console.log(e?e.message:'sidebar OK')})"
```
Render senza errori di sintassi EJS (usa `ejs` dal package). Il layout non
deve richiedere roba non ancora esistente in fase di render con fixtures.

## Rischi / note

- Il task F0-07 (auth) crea `/login` e `/verify`: le viste qui sono pronte ma
  le route sono F0-07.
- `views/partials/flash.ejs` legge `locals.flash`; se assente non stampa nulla.
- Mantieni il CSS condiviso (dark/light) separato in `app.css` così F4-08 lo
  estende senza conflitti.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi `views/...`,
`public/...`, `src/services/i18n.js`; se `read` fallisce usa `cat`/`sed -n`).

Copiare lo stile EJS/CSS dal CMS gemello `gestione-siti-riccardom`
(leggi `views/layouts/admin.ejs`, `views/partials/footer.ejs`,
`views/auth/login.ejs`, `views/error.ejs`) e produrre il layout base del
pannello `scrocco-web` (codice in italiano, EJS + express-ejs-layouts,
design denso, CSS con variabili `--*` per il futuro tema dark).

Crea ESATTAMENTE:
1. `views/layouts/admin.ejs` — `<html lang>` con `<meta name="csrf-token">`,
   `<link stylesheet public/css/app.css>`, sidebar `<%- include('partials/sidebar') %>`,
   topbar `<%- include('partials/topbar') %>`, `<%- include('partials/flash') %>`,
   `<%- body %>`, `<script src="/js/app.js">`. Usa `res.locals.*` con `typeof`
   guard così renderizza anche senza index.js.
2. `views/partials/sidebar.ejs` — nav raggruppata con link (anche a pagine
   future) e classe `active` sul path corrente; voce "Logout" nel topbar.
3. `views/partials/topbar.ejs` — utente (email+ruolo+profilo), logout via
   fetch POST `/api/auth/logout` → redirect `/login`.
4. `views/partials/flash.ejs` — se `locals.flash` (array di {type,msg}) esiste
   li stampa come `.alert`.
5. `views/error.ejs` — messaggio errore + link dashboard (calco CMS).
6. `views/auth/login.ejs` — form email+password (action `/api/auth/login` via
   fetch), e link/badge "magic-link disponibile se SMTP" nascosto di default.
7. `views/auth/verify.ejs` — pagina token+OTP (o email+otp) per il flusso
   magic-link.
8. `public/css/app.css` — variabili `--bg` `--sidebar` `--accent` `--text`
   `--muted`; stile denso (13px, tabelle), `.btn`, `.btn-danger`, `.badge`,
   `.alert`, `.card`, form; override `[data-theme="dark"]`.
9. `public/js/app.js` — `window.scw = { fetch(path, opts) { aggiunge
   X-Request-Id e header CSRF dal meta, gestisce 401 → redirect /login },
   confirmForm(formSelector, message) }`.
10. `src/services/i18n.js` — `translate(lang, key)` identità + dizionario
    minimo {nav.dashboard, nav.logout}, default export.

Verifica: `node --check src/services/i18n.js` e render smoke delle viste con
fixtures (come da criterio). Non toccare `src/index.js` né altri file. In 2-3
righe elenca i file creati.