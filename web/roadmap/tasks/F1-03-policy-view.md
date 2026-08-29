---
id: F1-03-policy-view
fase: F1
dipende_da: [F0-11-integration-smoke]
puo_parallelo_con: tutti gli altri F1 read
---

# F1-03 — Vista policy (sola lettura)

## Obiettivo

Pagina `/policy` che mostra la policy del gateway (`GET /admin/policy`) in
forma leggibile: `effective` (valori applicati) e `configured` (ciao yaml),
con chiavi MASCHErate. SOLA LETTURA (PATCH in F2-04).

## File da creare/modificare

- `scrocco-web/src/routes/policy.js` (crea — GET /policy)
- `scrocco-web/views/policy/index.ejs` (crea)

## Contratto

- `GET /policy` [requireAuth, authorize("policy","read")].
- Dati: `gateway.get('/admin/policy')` → `{file, configured, effective}`.
- `effective` include: service_name, proxy_prefix, legacy_prefixes, step_up_pct,
  profile_step_up_pct, aliases, alias_keys_masked, client_keys_masked,
  estimate_divisor, sticky_ttl_sec, cooldown_sec, hotwords, speed_*,
  hotwords_window, response_model, adaptive_pick, recency_halflife_sec,
  latency_ref_ms, qc_json, qc_sanity, cooldown_escalation, max_cooldown_sec,
  proactive_health, capability_routing{...}.
- View: sezioni collassabili (Generali, Routing, Timing, Adattivo, Health, QC,
  Capability routing, Alias/Client keys). Valori con `pre` monospace per
  roba strutturata. Mostra `file` (path) e badge "effective".

## Criterio di done

Render manuale con mock, `node --check`. Nessuna chiave in chiaro (usa solo
i campi `*_masked`).

## Rischi / note

- MAI stampare `alias_keys`/`client_keys` in chiaro: usa `alias_keys_masked`/
  `client_keys_masked` (già mascherate dal gateway).
- Vincolo masking: `_mask_configured` lato gateway (_mask_configured in
  admin.py) maschera SOLO `alias_keys` nel blocco `configured`; se lo yaml
  contiene `client_keys` in chiaro verrebbero echeggiati in `configured`.
  La vista non deve mai fare un dump grezzo di `configured`: mostra solo i
  campi `*_masked` di `effective`/`configured`. (Stesso vincolo su F5-02 API.)
- `qc_json`/`qc_sanity` sono oggetti: render JSON pretty con `JSON.stringify`.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Crea la pagina read-only della policy del gateway.

Crea:
1. `src/routes/policy.js` — default export Router; `GET /policy` con requireAuth
   + `authorize("policy","read")`; `gateway.get('/admin/policy')`; render
   `views/policy/index.ejs` con `{policy}` (i campi `configured`, `effective`,
   `file` come da shape). Gestisci GatewayError.
2. `views/policy/index.ejs` — sezioni <details> per gruppo (Generali, Routing,
   Timing, Adattivo, Health, QC, Capability routing, Alias/Client keys);
   valore con `<code>`/`<pre>`; solo campi `_masked` per le chiavi; nella
   sezione Alias: tabella alias → key_masked; Client keys: profilo → key_masked.

Non toccare altri file. Verifica `node --check` + render smoke con mock.
Riepiloga in 2 righe.