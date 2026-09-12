# Spec — Riparazione argomenti tool-call (`tool_repair`)

Stato: **approvata per implementazione**. Fonte: decisioni utente del 2026-09-12.

## 1. Obiettivo
Aggiungere al gateway un livello che normalizza la **forma** degli argomenti dei
tool-call emessi dai modelli, prima che il client li validi ed esegua. Non cambia
mai la semantica né il nome del tool. Riduce turni morti, rotazioni inutili della
catena e errori "invalid arguments".

## 2. Decisioni confermate
- Attivo di default su TUTTI i deployment, livello default `aggressive`.
- Selezionabile **per-deployment** tramite nuova colonna CSV `tool_repair`.
- Valori colonna: vuoto = `aggressive` (default); `safe` = solo riparazioni base;
  `off` = disattivato per quel deployment.
- Modelli Google/Gemini: `off` di default (gestiscono i tool a modo loro), riusando
  `is_gemini_deployment()` di `app/thought_sig.py`. Un valore esplicito
  `safe`/`aggressive` in colonna **vince** sull'auto-off.
- Nome tool: **nessuna** canonicalizzazione/remapping contro `tools[]`; si garantisce
  solo la validità sintattica della struttura (name stringa non vuota, arguments
  stringa JSON valida, id/index coerenti).
- Streaming supportato da subito.
- Logging **sempre** attivo per ogni riparazione (non solo metriche).
- Modulo dedicato: `app/toolrepair.py`.

## 3. Catalogo riparazioni
### Base (`safe`)
- newline/tab/caratteri di controllo non escapati dentro stringhe JSON -> re-escaping
- fence markdown/testo attorno al JSON -> estrazione del blocco
- oggetti/array "stringificati" (una o più volte) -> parsing
- scalari stringificati dove lo schema vuole number/integer/boolean -> coercizione
- `null` su campo **opzionale** -> campo omesso
- stringa vuota dove serve oggetto su tool zero-arg -> `{}` (o omesso)
- trailing comma -> rimosse
- valori Python-style `True/False/None` -> `true/false/null`

### Aggressive (include `safe`)
- chiusura di JSON troncato (bilanciamento graffe/quadre/stringhe) se ricostruibile
  con certezza
- rimozione campi extra non previsti dallo schema (solo se schema strict /
  `additionalProperties:false`)
- `null` su campo obbligatorio con default esplicito -> default
- quoting misto (single -> double) con parser tollerante come ultimo tentativo
- collasso di doppie serializzazioni annidate e array-wrapping spurio se univoco

**Fuori scope**: tool sbagliato, parametri inventati, valori di dominio errati,
troncamento per `max_tokens` (resta al fallback).

## 4. Risoluzione livello effettivo (precedenza)
1. colonna `tool_repair` del deployment (`off`/`safe`/`aggressive`)
2. se vuota -> `aggressive`, TRANNE deployment Google -> `off`
3. interruttore globale policy (`enabled=false`) -> tutto spento

## 5. Innesto nel flusso
### Non-streaming
In `Forwarder.call_with_fallback`, per OGNI tentativo, DOPO la risposta upstream e
PRIMA del QC (`qc.py` `check_response`/`check_sanity`):
- arguments gia valida -> nessun intervento
- arguments invalida -> tenta riparazione col livello del deployment che ha risposto
- riparata -> consegna, **NIENTE** rotazione, deployment non penalizzato
- non riparabile -> comportamento attuale (rotazione + `qc_failed` + nota D3)

### Streaming
Trasformatore SSE agganciato a `Forwarder.stream_response` (cosi anche le logiche di
peek/summary a valle vedono i chunk riparati). Dettagli:
- buffer per `index` dei soli delta `arguments` quando compare `delta.tool_calls`
- testo/reasoning passano immediatamente (nessuna latenza per turni senza tool)
- a fine tool-call (o `finish_reason`) emetti il delta con arguments riparati
  (formato compatibile OpenAI)
- gestisci: tool-call in un solo chunk, tool-call multipli paralleli, args frammentati
  su piu delta, stream troncato (nessuna riparazione forzata; resta il watchdog)
- NON toccare `[DONE]`, `usage`, `finish_reason`

## 6. Schema-driven
- attivo solo se la request dichiara `tools`
- costruisci mappa `nome -> parameters` (JSON Schema) dalla request
- regola d'oro: cambia solo la **forma**, mai i valori; se non e riparabile con
  certezza, non toccare nulla

## 7. Flag CSV e admin
- nuova costante header `TOOL_REPAIR_HEADER = "tool_repair"` in `app/config.py`
- `_classify` la legge in `meta`; `_build_profile` la porta nel deployment dict
  (accanto a `effort_capable`)
- `app/csv_store.py`: aggiungi `tool_repair` a `PAYLOAD_FIELDS`; validazione valori
  (speculare a `validate_caps`); `ensure_*_column` sul percorso create; aggiungi
  `tool_repair` (e idealmente `effort_capable`/`intelligence_score`) all'insieme
  `known` di `row_id` per mantenere STABILI i `drow_*`
- `app/admin.py`: esporre il campo nei percorsi create/update/bulk e nell'elenco colonne
- aggiorna `var/keys_rotation.csv.example` + commento in testa + `README` + `docs/`

## 8. Policy (`gateway.yaml`) — hot reload
Nuovo blocco:
- `enabled` (default true)
- `default_level` (default `aggressive`)
- set di mosse attive per livello
- limite di dimensione degli argomenti da tentare
- `disable_for_google` (default true)
- `annotate_reasoning` (opzionale)
Precedenza: CSV per-deployment > policy globale.

## 9. Osservabilita
- log `[repair]` SEMPRE (successo/fallimento, tool, deployment, tipo fix, livello
  effettivo), senza contenuti sensibili
- metriche per tipo/deployment
- campo `repaired` nel summary per-richiesta (accanto a `qc`)

## 10. Test (`tests/`, pytest)
- unit `toolrepair`: ogni violazione -> output valido; input valido -> invariato
  (idempotenza); livello `off` -> nessun intervento
- unit `config`/`csv_store`: parsing flag, default, `off`, stabilita `drow_*`
- integrazione non-streaming: nessuna rotazione quando riparato
- integrazione streaming: frammentati, multipli, troncato
- non-regressione: richieste senza `tools` e contenuti utente non toccati

## 11. Vincoli
- NON modificare la history su disco del client
- NON cambiare la semantica ne i nomi dei tool
- nessuna regressione per richieste senza `tools`
- rispettare la struttura modulare esistente (`app/*`)
- a fine lavoro: test suite verde; commit su master con messagjgio descrittivo
