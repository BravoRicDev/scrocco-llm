---
id: F4-01-csv-editor
fase: F4
dipende_da: [GP-02-csv-read-write, F2-09-integration]
puo_parallelo_con: F4-02-playground, F4-03-policy-raw-editor, F4-04-config-history
---

# F4-01 — Editor CSV grezzo (keys_rotation.csv)

## Obiettivo

Pagina `/csv-editor` per visualizzare e modificare il CSV grezzo
`keys_rotation.csv` con tabella (cell edizioni) e vista testo raw: validazione
e backup automatici PRIMA di ogni scrittura, reload dopo il salvataggio.
Solo admin. Usa i prerequisiti `GET /admin/csv` e `PUT /admin/csv` (GP-02).

## File da creare/modificare

- `scrocco-web/src/routes/csv-editor.js` (crea)
- `scrocco-web/views/csv-editor/index.ejs` (crea)
- `scrocco-web/public/js/csv-editor.js` (crea)

## Contratto

- `GET /csv-editor` [requireAuth, authorize("csv","read")] → render con il
  CSV attuale.
- API dati: `GET /api/csv-editor/data` (requireAuth, csv read) →
  `gateway.get('/admin/csv')` → **shape GP-02: `{path, raw,
  parsed:{header,rows}, count, backups:[...]}`** (header/rows ANNIDATI dentro
  `parsed`, non a livello top). La route fa **unwrap di `parsed`** e passa alla
  vista `{path, raw, header, rows, count, backups}` (oppure espone il JSON
  così com'è e il client legge `.parsed.header`/`.parsed.rows` — scegli UN modo
  coerente tra route e JS client).
- `POST /api/csv-editor/save` (requireAuth, authorize("csv","write")) body
  zod `{raw: string}` → `gateway.put('/admin/csv', {json:{raw}})` → GP-02
  risponde `{ok, parsed?, errors?, backup}`; se `errors` popolato →
  nessun salvataggio applicato (il gateway valida prima e fa backup prima di
  scrivere) → mostra errori; altrimenti flash "salvato + backup"; auditLog
  (entityType 'csv', action 'update', newData {rows, backup} — MAI raw intero).
- View: toggle "tabella / raw". Tabella con `<contenteditable>` per cella
  (header fisso + colonne profilo), pulsante "Salva" che trasforma in CSV
  testuale e invia. Vista raw: textarea + button. Sempre visibile: path,
  ultimo backup, righe.
- Vedi "backups" (elenco) e link alla pagina history.

## Criterio di done

Test mock (GP-02 mock): GET data → raw+parso; POST raw valido → `{ok:true,
backup}`; POST raw con errore → `{ok:false, errors:[...]}` e NON cambia lo
stato del mock. `node --check`.

## Rischi / note

- Durata stimata: 60–90 min (JS client: builder DOM + anteprima contenteditable).
- NON gestire stringa CSV manuale in TS: il parsing/validazione è del gateway
  (torna errori). Il client manda il raw e mostra gli errori.
- Il backup è del gateway (`var/backups/`); `auditLog` registra solo esito e
  nome backup, MAI il contenuto.
- Questa operazione NON tocca `keys` in chiaro: il CSV le contiene SEMPRE;
  lo si mostra mascherato se il mock/parsed li espone MAI (GP-02 deve
  mascherarle nella risposta "parsed": il raw è accessibile solo in GET).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa l'editor del CSV raw del gateway.

PREREQUISITO: l'endpoint gateway `GET /admin/csv` e `PUT /admin/csv`
(prerequisito GP-02) deve esistere e rispondere come da contratto GP-02
(stesso shape qui sotto; se non esiste ancora, il mock in `test/fixtures/` lo
simula e il task gira in dry-run).

Crea:
1. `src/routes/csv-editor.js` — Router: `GET /csv-editor` (requireAuth +
   authorize csv read) → render; `GET /api/csv-editor/data` (csv read) →
   `gateway.get('/admin/csv')`, fai l'**unwrap di `parsed`** e rispondi JSON
   `{path, raw, header, rows, count, backups}` (ricordati: nel GP-02 header/
   rows sono dentro `data.parsed`); `POST /api/csv-editor/save` (csv write) →
   zod {raw:string} →
   `gateway.put('/admin/csv',{json:{raw}})`, gestione `{ok:false, errors}` con
   status 400 (flash/JSON), auditLog (no contenuto), risposta `{ok}`.
2. `views/csv-editor/index.ejs` — header (path, righe, ultimo backup, link
   history); toggle "Tabella/Raw"; tabella (header riga + righe con
   contenteditable) e textarea raw; bottone Salva (confirm). Area errori/banner.
3. `public/js/csv-editor.js` — build CSV dalla tabella (escape celle con `"`
   e a-capo), toggle viste, submit `scw.fetch('/api/csv-editor/save',{json:
   {raw}})`.

Se la risposta `parsed` del gateway include le chiavi `api_key` in chiaro,
mascherale client-side (mai mostrare full key nell'editor table UI: mostra
semplicemente il prefisso+3). Verifica `node --check`. Non toccare altri
file. Riepiloga in 3 righe.