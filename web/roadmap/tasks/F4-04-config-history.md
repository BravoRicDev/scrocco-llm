---
id: F4-04-config-history
fase: F4
dipende_da: [GP-04-backups-restore, F2-09-integration]
puo_parallelo_con: F4-01-csv-editor, F4-02-playground, F4-03-policy-raw-editor
---

# F4-04 — Config history / rollback (snapshot PostgreSQL + diff + restore)

## Obiettivo

Ogni modifica significativa a CSV o gateway.yaml viene salvata come
SNAPSHOT nella tabella Postgres `config_snapshots` (già creata in F0-03);
pagina `/config-history` per navigare, confrontare (diff) e "ripristinare" uno
snapshot (che applica via `PUT /admin/csv` o `PUT /admin/policy/raw`, oppure
tramite `POST /admin/backups/restore` se usiamo i backup del gateway GP-04).

## File da creare/modificare

- `scrocco-web/src/services/config-snapshots.js` (crea)
- `scrocco-web/src/routes/config-history.js` (crea)
- `scrocco-web/views/config-history/index.ejs` (crea)
- `scrocco-web/public/js/config-history.js` (crea)

## Contratto

- `src/services/config-snapshots.js`:
  - `createSnapshot({kind:'csv'|'yaml', source, sha, userId})` → insert in
    `config_snapshots` (content, source_sha256, created_by).
  - `listSnapshots({kind?, limit})` → rows (senza content per la lista).
  - `getSnapshot(id)` → row pieno.
- Da chiamare da: F4-01 save (dopo `PUT /admin/csv` ok) e F4-03 save (dopo
  `PUT /admin/policy/raw` ok) — MA NON qui per non modificare quei file: qui
  crei il SERVICE; le epilogue hook si agganciano in F4-09 (integrazione)
  oppure vengono aggiunti nei task F4-01/F4-03 come include? DECISIONE: in
  F4-09 integration aggiungerai un piccolo `src/middleware/snapshot-hook.js`
  che wrappa le risposte `ok` dei save per creare lo snapshot (senza toccare
  le route). Qui crei solo service + route + view.
- `GET /config-history` [requireAuth, authorize("config_snapshots","read")] →
  render lista snapshot (kind badge, created_at, sha abbr).
- `GET /config-history/:id/diff` (read) → diff `diffLines` tra snapshot
  corrente (fetch live da /admin/csv o /admin/policy/raw) e quello selezionato
  → JSON righe.
- `POST /config-history/:id/restore` [authorize config_snapshots restore,
  admin] → applica: kind csv → `gateway.put('/admin/csv',{json:{raw: content}})`;
  kind yaml → `gateway.put('/admin/policy/raw',{json:{raw: content}})`; audit
  (entityType 'config_snapshot', action 'restore', newData {id, kind}). Se il
  gateway rifiuta → flash errore.

## Criterio di done

Test: creo snapshot (service insert), lista lo mostra, diff funziona con mock,
restore yaml → mock aggiorna e audit scritto. `node --check`.

## Rischi / note

- Il restore NON cancella snapshot; ogni restore crea a sua volta uno
  snapshot (in hook F4-09). 
- `content` può essere grande: indice su kind+created_at.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa la config history con rollback.

La tabella `config_snapshots` esiste già (db/002_services.sql): id, kind, 
content, source_sha256, created_by, created_at.

Crea:
1. `src/services/config-snapshots.js` — `createSnapshot({kind, source,
   sha, userId})`, `listSnapshots({kind?, limit=50})` (senza content),
   `getSnapshot(id)`. Usa `src/db.js`.
2. `src/routes/config-history.js` — Router:
   - `GET /config-history` (requireAuth + authorize config_snapshots read) →
     lista (kind/date/sha) render.
   - `GET /config-history/:id/diff` (read) → fetch CORRENTE dal gateway
     (kind csv → /admin/csv raw; kind yaml → /admin/policy/raw raw) e diff
     con `diffLines` da 'diff' → JSON `{lines}`.
   - `POST /config-history/:id/restore` (admin) → riapplica (PUT /admin/csv
     o PUT /admin/policy/raw con content); auditLog; flash; redirect.
3. `views/config-history/index.ejs` — tabella snapshot (id, kind badge,
   data, sha, "Diff" e "Ripristina" admin) + area diff quando `diffLines`.
4. `public/js/config-history.js` — carica diff via fetch e mostra righe.

Verifica `node --check` + un mini-test di service. Non toccare altri file.
Riepiloga in 3 righe.