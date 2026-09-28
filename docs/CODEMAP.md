# Mappa del codice

Documento di ingresso per chi deve **modificare** scrocco-llm. Per capire cosa
fa il servizio e come si usa, vedi `README.md` e `docs/`. Qui trovi solo: dove
sta ogni cosa, e da dove iniziare quando devi toccare qualcosa.

Ultima verifica: struttura misurata sul codice, non a memoria.

---

## Entry point

`app/main.py` (379 righe) costruisce l'app FastAPI (riga 269) e contiene solo il
boot e le rotte. **Non e' piu' il file da leggere per capire il servizio**: era
7846 righe, e la sua logica e' stata spostata in moduli dedicati.

## Dove mettere le mani

| Sto modificando... | File | Dimensione |
|---|---|---|
| scelta del deployment, cooldown, key lease, punteggi | `app/router.py` | 6470 |
| chiamata all'upstream, fallback, gestione errori, SSE | `app/forwarder.py` | 5449 |
| API di gestione (CRUD deployment, policy, probe) | `app/admin.py` | 3749 |
| default e parsing dei parametri di configurazione | `app/policy.py` | 3649 |
| pipeline di streaming della chat | `app/chat_stream.py` | 1761 |
| caricamento config, `GatewayConfig` | `app/config.py` | 1287 |
| endpoint di compatibilita (Ollama, llama.cpp) | `app/compat/` | |

## Sotto-directory di `app/`

- `app/routing/` (14 moduli) — contiene due cose diverse, non confuse:
  - **9 mixin** composti in `class Router(...)` in `app/router.py`: `WarmMixin`
    (`warm`), `CanaryMixin` (`canary`), `SessionMixin` (`sessions`),
    `CircuitBreakerMixin` (`circuit_breaker`), `UsageMixin` (`usage`),
    `CooldownMixin` (`cooldown`), `FailureMixin` (`failure`), `KeyLeaseMixin`
    (`leases`), `DrainMixin` (`draining`).
  - **Moduli di funzioni**, che non sono mixin: `evict` e `lazy` (sole funzioni),
    `estimate` (`ErrorKind`, `DepStats`), `rolling` (`RollingWindow`).

  Quando aggiungi un modulo qui, decidi prima quale delle due forme e' e se
  entra davvero nella classe `Router`.
- `app/compat/` — endpoint non standard, esposti come `APIRouter`.
- `app/state.py` — stato condiviso. **Importalo da qui** se un nuovo modulo ha
  bisogno di stato: risolve il ciclo di import che altrimenti si risolve con
  l'adapter scomodo `import app.main as M`.

## Regole di composizione (quando crei un modulo nuovo)

Non sono arbitrarie, e sbagliarle e' costoso:

- **Metodi di `Router`** -> nuovo file in `app/routing/` come mixin, con import
  locali dentro le funzioni per evitare cicli. Componi nella classe `Router`.
- **Helper puri di funzione** -> modulo top-level in `app/` (es.
  `app/sse_utils.py`, `app/jsonl_store.py`).
- **Route endpoint** -> `APIRouter` in `app/compat/`, montato con
  `include_router` **dopo** `app = FastAPI(...)` e dopo gli helper che usa;
  dentro, `import app.main as M` (locale, e **dopo** il docstring, altrimenti
  il docstring si demota a stringa e `__doc__` diventa `None`).

## Vincoli che la suite non controlla da sola

- **Nessun cluster oltre 900 righe.** Se il blocco da estrarre e' piu' grande,
  dividi per responsabilita'. `chat_stream.py` (1761) e' il primo candidato.
- **`git revert` per singolo cluster.** Ogni commit del refactoring e' atomico e
  verde: se un cluster fa danno in produzione, si torna indietro con un revert
  di quel commit, non di un intero round.
- **`_last_*_save` e altri accumulatori** vivono in `app/main.py`: un modulo che
  li tocca deve scrivere `M.<nome> = ...` per lettura **e** scrittura. Qualificare
  solo le scritture lascia main.py su un valore diverso, e il throttle muore in
  verde.
- **`globals()` dentro un modulo spostato** scrive nel namespace del nuovo
  modulo, non in quello originale. Va riscritto come `M.<nome>`.

## Test

`tests/` — 213 file, 2570 test. Tutti verdi. I fixture che registrano
deployment finti stanno in `tests/conftest.py` (`register_fake_deployment`,
auto-pulente). Sono la risposta corretta a "nessun deployment nella config
globale", non un mock da evitare.

## Documentazione

- Attuale: `README.md`, `README.it.md`, `docs/*.md`.
- **Superata:** `docs/archive/` — non aggiornarla, non e' azionabile. Se un
  documento li contraddice, il codice ha ragione.
