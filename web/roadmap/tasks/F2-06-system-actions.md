---
id: F2-06-system-actions
fase: F2
dipende_da: [F1-05-dashboard-state]
puo_parallelo_con: F2-01-deployment-crud, F2-02-deployments-bulk, F2-03-profiles-write, F2-04-policy-edit, F2-05-policy-keys, F2-07-probe, F2-08-capabilities-write
---

# F2-06 — Azioni di sistema (cooldown/sessioni/reload/unretire)

## Obiettivo

Pagina `/system` con le azioni di manutenzione del gateway: sblocca cooldown
(mirato/tutti), rilascia sessioni sticky (mirato/tutte), reload config,
unretire chiavi (da lista cooldown/health). Tutte con conferma e audit.

## File da creare/modificare

- `scrocco-web/src/routes/system-actions.js` (crea)
- `scrocco-web/views/system-actions/index.ejs` (crea)
- `scrocco-web/public/js/system-actions.js` (crea)

## Contratto

- `GET /system` [requireAuth, authorize("system","reload")] → carica
  `gateway.get('/admin/state')` (cooldowns_active, sticky_sessions) e render.
- `POST /system/cooldowns/clear` [authorize("system","cooldowns")] body
  `{unique?}` → `gateway.post('/admin/cooldowns/clear', {json})`; audit;
  flash; redirect.
- `POST /system/sessions/release` [authorize("system","release")]
  `{session_id?}` → `gateway.post('/admin/sessions/release', {json})`; audit;
  flash.
- `POST /system/reload` [authorize("system","reload")] →
  `gateway.post('/admin/reload', {json:{}})` → flash con risultato; audit
  "reload".
- `POST /system/unretire` [authorize("deployments","unretire")] `{unique}` →
  `gateway.post('/admin/deployments/unretire', {json:{unique}})`, audit, flash.
- View: tre card (Cooldown table con bottone sblocca per riga + "sblocca
  tutti", Sticky table "release" per riga + "release tutti", Azioni: bottone
  Reload con conferma). Tutti i form POST con `scw.confirm` per azioni
  distruttive.

## Criterio di done

Test mock: clear cooldown mirato → rimosso dal mock; release sessions →
sticky svuotato; reload → mock risponde `{reloaded:true}`. `node --check`.

## Rischi / note

- Queste azioni sono ad alto impatto: NESSUNA viene eseguita senza conferma
  esplicita (data-destruct) e sempre con audit.
- "reload" ricarica CSV+policy lato gateway: comportamento veloce; mostra
  esito.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa le azioni di sistema.

Crea 3 file:
1. `src/routes/system-actions.js` — Router con:
   - `GET /system` (requireAuth + authorize("system","reload")), carica
     `gateway.get('/admin/state')`, render.
   - `POST /system/cooldowns/clear` (zod {unique?}), gateway call, auditLog
     (entityType system, action "cooldown_clear//clear_all").
   - `POST /system/sessions/release` (zod {session_id?}), audit.
   - `POST /system/reload` → gateway.post reload, audit.
   - `POST /system/unretire` (zod {unique}) → gateway unretire, audit.
   - NOTE authorize: usa i nomi azione COERENTI della matrice F0-07
     (`system/reload`, `system/cooldowns`, `system/sessions`,
     `system/release`; unretire = `deployments/unretire`).
   Tutte flash + redirect `/system`.
2. `views/system-actions/index.ejs` — card Cooldown (tabella unique,
   remaining_sec humanizzato, attempts, bottone Sblocca → POST, "Sblocca
   tutti"); card Sticky (tabella session_id→group, bottone Release, "Release
   tutti"); card Azioni (Reload config con confirm). Tutti form `data-destruct`
   tranne reload.
3. `public/js/system-actions.js` — usa `scw.confirm` attacchi dopo il load.

Verifica `node --check` + test mock opzionale. Non toccare altri file.
Riepiloga in 3 righe.