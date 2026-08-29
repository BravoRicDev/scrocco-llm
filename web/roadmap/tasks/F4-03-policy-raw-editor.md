---
id: F4-03-policy-raw-editor
fase: F4
dipende_da: [GP-03-policy-raw, F2-09-integration]
puo_parallelo_con: F4-01-csv-editor, F4-02-playground, F4-04-config-history
---

# F4-03 — Editor gateway.yaml (raw) con diff + validazione + reload

## Obiettivo

Pagina `/policy-raw` per l'editing RAW del `gateway.yaml`: carica il contenuto
da `GET /admin/policy/raw` (GP-03), mostra diff rispetto alla "ultima versione
visuale" (preview), validazione client-side rapida (yaml parse) oltre a
quella server-side, e salva con `PUT /admin/policy/raw` che fa reload se
valido. Solo admin.

## File da creare/modificare

- `scrocco-web/src/routes/policy-raw.js` (crea)
- `scrocco-web/views/policy-raw/index.ejs` (crea)
- `scrocco-web/public/js/policy-raw.js` (crea)

## Contratto

- `GET /policy-raw` [requireAuth, authorize("policy","update")] → render.
- `GET /api/policy-raw/data` (requireAuth policy read/update) →
  `gateway.get('/admin/policy/raw')` (GP-03) → `{path, raw}` (yaml string).
- `POST /api/policy-raw/save` (authorize policy update) body zod `{raw}` →
  `gateway.put('/admin/policy/raw', {json:{raw}})` → GP-03: `{ok,
  validated:bool, errors?, reloaded:bool}`; se `errors` → 400 mostra; se ok →
  flash "salvato e ricaricato". auditLog (entityType 'policy', action 'update',
  newData {bytes, validated}).
- Preview/diff: in `policy-raw.js` sul textarea con `js-yaml` (dip. esistente)
  valida sintassi e mostra errore di riga; bottone "diff vs ultima versione"
  che usa `diff` package? il diff lato browser non ha il package: fai un diff
  testuale semplice (line-by-line) inline — o POST `/api/policy-raw/preview`
  che fa diff server con il package `diff` (in dipendenze). SOLUZIONE:
  endpoint `POST /api/policy-raw/diff` con {raw} → server fa `diffLines(prev,
  next)` usando `diff` → JSON con linee annottate. Race: NIENTE.
- View: editor mono (textarea) + barra stato validazione (JS yaml) + bottone
  "Mostra diff" (tabella righe −/+), "Salva (reload)".

## Criterio di done

Test mock GP-03: GET raw → yaml valido; POST raw invalido → `{ok:false,
errors:["line 12: ..."]}` e mock NON aggiorna; POST valido → `{ok:true}` e
mock aggiornato; diff endpoint ritorna array di righe con marker. `node --check`.

## Rischi / note

- `js-yaml` lato client valida la SINTASSI; la validazione SEMANTICA è del
  gateway (Policy.load) → mostra entrambe.
- Mai chiamare `PUT` se la validazione yaml client fallisce (evita round-trip
  inutili). Non mostrare chiavi in chiaro se il raw le contiene: il main
  editor raw contiene le chiavi; si può navigare, ma resta admin-only e
  l'audit non salva contenuto.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Implementa l'editor raw del gateway.yaml.

PREREQUISITO GP-03: `GET /admin/policy/raw` + `PUT /admin/policy/raw` (mock in
fixtures se non mergiate).

Crea:
1. `src/routes/policy-raw.js` — Router: `GET /policy-raw` (requireAuth +
   authorize policy update) render; `GET /api/policy-raw/data` (read) →
   gateway.get raw → `{path, raw}`; `POST /api/policy-raw/diff` (read) con
   {raw} → usa `import { diffLines } from 'diff'` server-side e ritorna
   `{previous, lines:[{type:'+'|'-'|' ', text}]}`; `POST /api/policy-raw/save`
   (update) → zod {raw}; gateway.put('/admin/policy/raw',{json:{raw}});
   gestisci `{errors}` 400; auditLog (bytes + validated); risposta `{ok}`.
2. `views/policy-raw/index.ejs` — header path + bottoni "Diff", "Salva
   (reload)"; textarea monospace (height 70vh); box validazione (errore riga);
   tabella diff nascosta.
3. `public/js/policy-raw.js` — su input: `js-yaml.load` in try/catch → riga
   errore; "Diff" → scw.fetch('/api/policy-raw/diff') → build tabella; "Salva"
   → se yaml valido → scw.fetch save → reload location.

Verifica `node --check` + render/diff test. Non toccare altri file. Riepiloga
in 3 righe.