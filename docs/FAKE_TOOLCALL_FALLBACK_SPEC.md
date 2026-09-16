# Spec — Stadio 2: fake tool-call -> fallback diretto a -go/-fallback

## Contesto
Lo stadio 1 (`tool_repair`) ripara gli argomenti dei `tool_calls` reali. Questo
stadio gestisce il caso in cui il modello emette un tool-call **come testo** nel
`content` (nessun campo `tool_calls`). In quel caso il client (opencode) vede
solo testo e chiude il turno -> agente bloccato.

## Decisioni (bloccate)
1. Rilevamento attivo SOLO per richieste che dichiarano `tools`.
2. Rilevamento attivo SOLO su deployment NORMALI: esclusi `-go` e `-fallback`.
3. Su detection: NON consegnare la risposta; marcare il deployment fallito
   (`reason=fake_tool_call`); escalare DIRETTAMENTE a `{prefix}{profile}{go_suffix}`,
   poi `{prefix}{profile}{fallback_suffix}` (niente scala dims).
4. Su `-go`/`-fallback` la verifica e' DISATTIVATA: risposta consegnata cosi' com'e'
   (evita loop).
5. A esaurimento (nessun `-go`/`-fallback` disponibile o `max_escalations`
   raggiunto): rispondere **503 retryable**.
6. Nessuna chiamata LLM di riparazione.

## Rilevamento
- **Non-streaming** (`app/qc.py` `check_response`): se `has_tools` e
  `msg.get("tool_calls")` e' vuoto e il `content` matcha i pattern ->
  motivo `"tool_call reso come testo"`.
- **Streaming** (`app/forwarder.py`, `ToolRepairSSEFilter` o nuovo classificatore):
  strategia **hold-from-pattern**:
  - per richieste con `tools` si emette normalmente;
  - alla comparsa di un marker sospetto si smette di emettere e si trattiene il
    resto in buffer;
  - a fine stream: se fake -> scarta il buffer (MAI consegnato) e segnala failure;
    se non fake -> flush del buffer e chiusura normale;
  - anti-blocco: `stream_hold_max_bytes` / `stream_hold_timeout_ms` -> alla soglia
    si rilascia comunque.

## Pattern (set ampio; falsi positivi accettati)
`<arg_key>`, `<arg_value>`, `<function=`, `</function>`, `<tool_calls>`,
`</tool_calls>`, `<tool_call>`, `</tool_call>`, `<invoke`, `</invoke>`,
`<parameter=`, `</parameter>`, `<bash`, `</bash>`, `<edit>`, `</edit>`, `<write`,
`</write>`, `antml:` (configurabili).

## Escalation
- Helper per scegliere un deployment in `{prefix}{profile}{go_suffix}` e, se
  assente/fallito, `{prefix}{profile}{fallback_suffix}`, escludendo i gia' tentati.
- `mark_failed(current, reason="fake_tool_call")`.
- Contatore `max_escalations` per richiesta (default 2).
- Integrazione sia in `call_with_fallback` (non-streaming) sia in
  `_stream_with_fallback` (streaming).

## Policy (`gateway.yaml`, estende `tool_repair`)
```
tool_repair:
  fake_call:
    enabled: true
    detect_on: [normal]
    escalate_direct: [go, fallback]
    patterns: [...]
    max_escalations: 2
    stream_hold_max_bytes: 4096
    stream_hold_timeout_ms: 4000
```
- parsing in `app/policy.py` con validazione tipi.
- estendere `ToolRepairConfig` (o nuovo `FakeCallConfig`).

## Osservabilita
- log `[fake-tool-call]` (deployment, pattern, target, esito).
- metrica `nx_fake_toolcall_total` (dep, esito).
- summary per-richiesta: `fake_call: true` + catena escalation nel campo `fb`.

## Garanzia anti-blocco
- `detect_on: normal` -> escalation a `-go`/`-fallback` dove il check e' OFF ->
  risposta consegnata.
- esaurimento -> 503 retryable (opencode ritenta).
- hold streaming sempre bounded.
- `max_escalations`.

## Test (`tests/test_fake_toolcall.py`, nuovo)
- non-streaming: content fake -> `check_response` ritorna motivo.
- esenzione: deployment `-go`/`-fallback` non rilevati.
- richieste senza `tools`: nessun rilevamento.
- streaming: marker a fine stream -> buffer scartato, nessun byte fake emesso,
  failure segnalata.
- streaming: testo normale con `<` -> flush, nessun failure.
- escalation: da normal a `-go`; se `-go` assente -> `-fallback`; a esaurimento -> 503.
- bounded hold.
- non-regressione: suite completa.

## Vincoli
- NON toccare `app/router.py` ne `tests/test_effort_routing.py` (modifica separata
  gia' presente).
- commit separato con messaggio descrittivo.
- lo stadio 1 resta invariato.
