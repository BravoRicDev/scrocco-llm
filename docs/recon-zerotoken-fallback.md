# Recon — fallback automatico su risposta a 0 token / stream troncato

Ricognizione fatta direttamente (la sessione opencode di recon si era impiantata
dietro il gateway degradato). File/righe verificati su codice attuale.

## 1) Dove si decide il fallback oggi

| percorso | funzione | file:riga | quando ruota al deployment successivo |
|---|---|---|---|
| non-streaming | `Forwarder.call_with_fallback` | `app/forwarder.py:425` | `UpstreamError` ritriabile, 404/model_missing, provider-4xx firmato, **QC contenuto fallito** |
| streaming | `_stream_with_fallback` | `app/main.py:720` | **solo PRIMA del primo byte** — `UpstreamError` da `stream_response` (header upstream, 4xx deployment-side, provider error body) |
| watchdog passivo stream | `sse()` blocco `finally` | `app/main.py:873-898` | **non ruota**: solo `mark_failed`/cooldown + log; il campo `wd` va nel `[summary]` |

Chain: `router.fallback_next(profile, dep, need, scope, ctx=)`; limite `len(tried) < 64`;
catena esaurita → non-stream restituisce l'ultimo tentativo (D3) o solleva; stream
restituisce `JSONResponse` 502 (o lo status vero se thought_signature).

## 2) Non-streaming — usage e contenuto vuoto

- Risposta letta intera in `Forwarder.call` (`app/forwarder.py:213`).
- `usage` NON ricalcolato: si legge quello del provider (`_usage_of`, `app/main.py:479`;
  chiavi `prompt_tokens/completion_tokens/total_tokens/cost`). Spesso assente.
- **Contenuto vuoto È GIÀ gestito**: `check_sanity` (`app/qc.py:101`) — se
  `choices[0].message.content` è `None` o `len(strip) < min_chars` e non ci sono
  `tool_calls`/`images` → ritorna un motivo → in `call_with_fallback:497-507`
  diventa `qc_failed` → `mark_failed(cur)` → `fallback_next` → `continue`.
- `qc_sanity.enabled` default **True** (`app/policy.py:64`); `var/gateway.yaml`
  ha `qc_sanity: {min_chars: 1}` → **attivo in produzione**.
- **Eccezione attuale** (`app/forwarder.py:483-496`): se il contenuto è vuoto ma
  `finish_reason == "length"` → consegnato SENZA rotazione (i reasoning token
  hanno mangiato `max_tokens`; ruotare non aiuta).

### Gap rispetto alla richiesta
- Il check guarda la **stringa** `content`, non `usage.completion_tokens`.
- Nessuna esenzione esplicita "anche input a 0 token" (oggi: prompt vuoto +
  risposta vuota → ruoterebbe comunque; caso degenere, basso impatto).
- Il bypass `finish_reason == "length"` copre anche il caso in cui il modello ha
  prodotto SOLO reasoning e 0 testo utile: oggi **non** fa fallback.

## 3) Streaming — accumulo chunk e conteggio

- `Forwarder.stream_response` (`app/forwarder.py:148`): `async for chunk in
  resp.aiter_bytes(): yield chunk` — **zero buffering**.
- `sse()` (`app/main.py:816`): inoltra ogni chunk subito (`yield chunk:871`).
  Traccia passivamente: `chunks`, `seen_done` (`b"[DONE]"`), `seen_error`
  (`b'data: {"error"'`), `usage_final` (parse best-effort di un chunk con `"usage"`).
- Nel `finally` (`app/main.py:876`):
  - `chunks == 0` → `wd="tier1-empty"` → `mark_failed(dep)` (cooldown).
  - `seen_error` → `wd="tier1-error"` → `mark_failed`.
  - `not seen_done` → `wd="tier2-no-done"` → **solo log**; cooldown solo se
    `qc_json.watchdog_mark_no_done` (default **False**, e in prod False).
- **Nessun** controllo "chunk presenti ma 0 contenuto testuale reale".
- Recupero via fallback dopo il primo byte: **impossibile** senza bufferizzare
  (inoltreresti due completamenti diversi concatenati → output corrotto).

### Gap rispetto alla richiesta
1. Stream con soli delta vuoti/role poi `[DONE]` pulito (chunks>0, seen_done) →
   **non segnalato**.
2. Stream troncato a metà → `tier2-no-done`, **solo log**, e comunque non
   recuperabile: il client riceve una risposta parziale **senza saperlo**.

## 4) QC come punto d'aggancio

- `check_response` (JSON) e `check_sanity` (vuoto) girano SOLO non-streaming,
  dentro `call_with_fallback`, gated su `collect_qc_failures`.
- `check_sanity` **è** il punto giusto per il 0-token non-streaming: va esteso a
  leggere `usage` e a rendere configurabile il bypass `finish_reason==length`.
- Per lo streaming NON esiste un gancio QC: c'è solo il watchdog passivo in
  `sse()`. Va potenziato lì.

## 5) Catena / limiti — vedi §1.

## 6) Casi limite
- `tool_calls` senza testo: `completion_tokens` può essere >0; `check_sanity`
  già li esenta (come `images`). Mantenere l'esenzione.
- Solo reasoning tokens + `finish_reason=length`: oggi consegnato. Decisione
  aperta se ruotare.
- `usage` assente (`null`): non si può usare `completion_tokens`. Serve il
  fallback sulla **lunghezza del testo** (già fatto non-stream; per lo stream:
  accumulare la lunghezza del contenuto dai delta).
- Stream interrotto per disconnessione client: il `finally` scatta comunque;
  non marcare il deployment se l'abort è lato client (verificare
  `asyncio.CancelledError`).

## PUNTI D'AGGANCIO PROPOSTI

### A. Non-streaming 0-token (piccolo, sicuro)
`app/qc.py::check_sanity` + `app/forwarder.py:483-496`:
- Se `usage` presente: `completion_tokens == 0` **e** `prompt_tokens > 0` **e**
  contenuto vuoto → motivo "output 0 token" (anche senza `finish_reason=length`).
- `prompt_tokens == 0` (o `usage` assente e prompt effettivo vuoto) → NON ruotare.
- Nuovo knob `qc_sanity.rotate_on_length_empty` (default False): se True,
  ruota anche quando `finish_reason == "length"` con contenuto vuoto.
- Pro: riusa il meccanismo esistente, test facili. Contro: nessuno.

### B. Streaming — finestra di buffer pre-contenuto (medio)
`app/main.py::_stream_with_fallback`: prima di inoltrare, accumulare i chunk
finché (a) si è visto il primo delta con `content` non vuoto, oppure (b) è
passato `stream_first_content_ms` (nuovo knob, es. 1500 ms), oppure (c) lo
stream è finito. Se lo stream finisce/erroria DENTRO la finestra senza contenuto
reale → `fallback_next` trasparente (nessun byte ancora inviato). Poi flush del
buffer e proxy normale.
- Pro: recupero trasparente dei fallimenti precoci (il 90% dei casi "0 token").
- Contro: +fino a `stream_first_content_ms` di TTFB nel caso peggiore; va tenuto
  basso. Non recupera troncamenti a metà.

### C. Streaming — coda d'errore sintetica (piccolo)
Nel `finally` di `sse()`, quando `wd in {tier1-empty, tier2-no-done}` **oppure**
contenuto testuale totale accumulato == 0 con `prompt` non vuoto: emettere un
ultimo evento `data: {"error":{"type":"incomplete_upstream","message":...}}` e
NON emettere `[DONE]`. Nuovo knob `qc_json.stream_emit_error_tail` (default True).
- Pro: il fallimento silenzioso diventa esplicito, il client può ritentare.
- Contro: non è recupero automatico; alcuni client potrebbero non guardare
  l'ultimo evento (ma la maggior parte sì, ed è meglio di niente).

### Raccomandazione
A + C subito (piccoli, alto valore, bassa regressione). B in un secondo momento
se il monitoraggio del nuovo `wd`/coda-errore mostra che i fallimenti precoci
sono frequenti e vale la pena della latenza.

## DOMANDE APERTE (per l'utente)
1. `finish_reason == "length"` con contenuto vuoto (solo reasoning): ruotare o
   consegnare? (default proposto: consegnare, knob per ruotare)
2. Streaming: ok ad accettare +≤1.5 s di TTFB nel caso peggiore per il recupero
   trasparente (opzione B)? O per ora solo coda d'errore esplicita (opzione C)?
3. La coda d'errore sintetica va bene come default-on?
