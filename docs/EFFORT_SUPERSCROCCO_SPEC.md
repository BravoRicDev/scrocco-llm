# Spec — Effort `superscrocco` (spinta speculativa raddoppiata)

Stato: **approvata per implementazione**. Fonte: decisioni utente del 2026-09-29.
Radice del repo: `/home/riccardo/progetti/scrocco-llm` (tutti i path sotto sono relativi a questa).

## 1. Obiettivo

Aggiungere un livello di effort **`superscrocco`** che:

1. si comporti **esattamente come `high`** per tutto il comportamento già esistente
   (bias di intelligence nel routing, iniezione di `reasoning_effort`, override di
   temperatura, budget di thinking nei protocolli nativi);
2. moltiplichi la **"spinta" speculativa** della *sola richiesta* che lo chiede
   (warm pool, canary in volo, gare), per rispondere il prima possibile;
3. abbia un **ratio parametrabile** (default `2.0`), modificabile a caldo via policy.

## 2. Decisioni confermate (utente, 2026-09-29)

| Fork | Decisione |
|---|---|
| Timer (`hedge_delay_ms`, `slow_canary_after_ms`, `*_slow_race_after_ms`) | **NON scalati.** Solo i tetti. Nessuna manopola `effort_super_time_ratio`. |
| `stream_slow_race_canaries` (manopola morta) | **Cablata davvero** (N canari invece di 1) **e scalata**. |
| Tetto assoluto anti-429 | **`effort_super_max_inflight_abs = 16`**: il ratio non lo supera mai. |
| Soglia warm adattiva | **Scalata** insieme al gate (`warm_ready_min` + `warm_ready_min_max`). |

## 3. Principio di design: tetti vs timer

- **Tetti** = "quanti tentativi posso spendere" → moltiplicarli è spinta pura.
- **Timer** = "quanto presto mollo" → sono **calibrati sulla fisiologia
  dell'upstream**. `hedge_delay_ms = clamp(TTFT_bucket × frac, min, max)` esiste
  perché su contesti grandi il TTFT fisiologico è di secondi: lanciare il canary
  prima è *rumore*. Accorciare un timer sotto il TTFT fisiologico fa partire
  canari che perdono quasi sempre → **spreco, non spinta**.

Da qui la regola: **il ratio scala i tetti, mai i timer.**

## 4. Canonicalizzazione: `superscrocco` NON è un quinto livello

`superscrocco` viene canonicalizzato a `"high"` in `normalize_effort`. Il ratio è
una dimensione **ortogonale**, tenuta in campi separati dello stato.

Perché: un quinto livello vero obbligherebbe a toccare `_VALID`, la validazione di
`effort_temperature_overrides` (`app/policy.py:2364`, ammette solo
`low|medium|high`), `_THINK_BUDGET` (`app/protocols.py`), le liste di
`app/admin.py:3706/3736`, `tui/policy_screen.py` e i 24 test esistenti. Con la
canonicalizzazione questi consumatori funzionano **senza modifiche** e il contratto
effort resta congelato.

Alias accettati: `superscrocco`, `super-scrocco`, `super`, `xhigh`, `max`, `ultra`.

**I separatori non contano.** `_super_key()` confronta la minuscola *senza* `-`,
`_` e spazi, quindi `x-high`, `x_high`, `x high` e `X-HIGH` valgono `xhigh`, e
`super_scrocco` vale `super-scrocco`. La tolleranza è una proprietà della
**regola**, non una lista di grafie da tenere aggiornata una per una: col
confronto letterale ogni variante non elencata cadeva su `default` **in
silenzio** (nessun bias, nessuna iniezione, nessuna spinta, nessun errore).

⚠ Il token super **non va a monte così com'è**: `apply_effort_policy` riscrive al
livello canonico `high` un `reasoning_effort` che sia un alias super — il client
vince solo per i livelli canonici (`low`/`medium`/`high`). Un provider che valida
l'enum rifiuta `xhigh`/`x-high`/`superscrocco`, e `protocols._reasoning_from_body`
scarta tutto ciò che non è `low|medium|high`: il token grezzo spegneva anche il
budget di thinking sui protocolli nativi (Anthropic/Gemini/Responses).

Gli alias accettati sono pubblicati in `/v1/models` via `advertised_efforts()`,
così un client li scopre senza indovinare la grafia.

### 4.1 Il token deve sopravvivere all'estrazione

⚠ `effort_from_request()` canonicalizza già a `"high"`, quindi `set_effort()`
non saprebbe che la richiesta era `superscrocco`. Serve una funzione che
**preserva il token**:

```python
def effort_token_from_request(payload, headers=None) -> str:
    """Come effort_from_request, ma PRESERVA il token super (non lo
    canonicalizza): serve a set_effort per sapere se applicare il ratio."""
```

Il call site `app/chat_completions.py:152` passa da `effort_from_request(...)` a
`effort_token_from_request(...)`. `effort_from_request` resta invariata (il suo
contratto è asserito dai test).

## 5. Modifiche per file

### 5.1 `app/effort.py`

Stato (`ContextVar`): aggiungere `"super": bool` e `"ratio": float`.
`get_effort()` continua a tornare `"high"` per superscrocco.

```python
_SUPER = ("superscrocco", "super-scrocco", "super", "xhigh", "max", "ultra")
_SUPER_KEYS = frozenset(...)   # confronto senza separatori: x-high == xhigh
_SUPER_RATIO_DEFAULT = 2.0
_RATIO_MAX = 8.0
```

Nuove API:

```python
def is_super_effort(raw) -> bool          # il token grezzo è un alias super?
def effort_token_from_request(payload, headers=None) -> str
def set_effort(effort, *, temp_enabled=False, temp_overrides=None,
               super_ratio=_SUPER_RATIO_DEFAULT, super_enabled=True,
               max_ratio=None) -> contextvars.Token
def is_super() -> bool                     # lo stato corrente è super?
def get_speculation_ratio() -> float       # 2.0 se super, 1.0 altrimenti

def scale_speculation(value, *, lo=0, hi=None, floor_one=False, ratio=None):
    """Scala una manopola di SPINTA per il ratio dell'effort corrente.
    None -> None · 0 -> 0 (una feature spenta NON si resuscita)
    bool -> rifiutato (mai moltiplicare un flag) · hi = clamp superiore
    floor_one: un valore > 0 non scende mai sotto 1."""

def spinta(policy, attr, default=0, *, lo=0, hi=None, floor_one=False) -> int:
    """Legge la manopola dalla policy e la scala. Una riga per call site."""
```

`set_effort` è retrocompatibile: i nuovi parametri sono keyword-only e hanno
default che riproducono il comportamento attuale (`ratio = 1.0` se non super).

### 5.2 `app/policy.py` — nuove manopole

```python
effort_super_enabled: bool = True
effort_super_ratio: float = 2.0            # validato (1.0, 8.0)
effort_super_max_inflight_abs: int = 16    # validato >= 1
```

Validazione in `_apply_tuning` (vicino al blocco effort, `app/policy.py:2350`):
- `effort_super_ratio` fuori `(1.0, 8.0)` → `ValueError`; il valore `< 1.0` è
  rifiutato perché rallenterebbe la spinta (footgun).
- `effort_super_max_inflight_abs < 1` → `ValueError`.
- `effort_super_enabled` con `_set_bool`.
- `scale_speculation` non deve mai applicare il ratio se `effort_super_enabled`
  è falso.

Esposizione: `app/admin.py:1062` (GET policy) e le liste di tuning
(`app/admin.py:3706`, `:3736`), più `tui/policy_screen.py`.

### 5.3 Accessor unico del tetto di volo — punto critico

`warm_refill_max_inflight` è letto in **tre** posti. Se il ratio venisse applicato
in due su tre, il gate di `chat_stream` e il troncamento `cands[:_free]` di
`chat_hedge` userebbero numeri diversi: il canary nascerebbe e verrebbe buttato.
È la stessa classe di bug già pagata con `_slow_ms` letto da `qcp` invece che da
`Policy` ("la gara lenta non partiva mai").

Un solo accessor, che applica anche il tetto assoluto:

```python
def max_inflight_effective(self, policy=None) -> int:
    pol = policy or self.policy
    base = int(getattr(pol, "warm_refill_max_inflight", 6) or 0)
    scaled = scale_speculation(base, lo=0)
    cap = int(getattr(pol, "effort_super_max_inflight_abs", 16) or 0)
    return min(scaled, cap) if cap > 0 else scaled
```

I tre siti lo chiamano: `app/chat_stream.py:453`, `app/chat_hedge.py:272`,
`app/forwarder.py:3218`.

### 5.4 Le 9 manopole scalate

| # | manopola | default | dove si legge | 0 = | ×2 |
|---|---|---|---|---|---|
| 1 | `warm_refill_max_inflight` | 6 | `chat_stream.py:453` · `chat_hedge.py:272` · `forwarder.py:3218` | nessun refill | 12 (cap 16) |
| 2 | `warm_ready_min` | 3 | `app/routing/warm.py:351` | soglia zero | 6 |
| 3 | `warm_ready_min_max` | 6 | `app/routing/warm.py:357` | — | 12 |
| 4 | `stream_hedge_tiers` (ramo classico) | 2 | `chat_stream.py:572` | — | 4 |
| 5 | `_hh_k` ramo refill (2 fisso) | 2 | `chat_stream.py:569` | — | 4 |
| 6 | `slow_race_max_warm` | 6 | `app/router.py:1864` | nessun gate | 12 |
| 7 | `stream_hedge_max_races` | 0 | `chat_stream.py:536` | **illimitato** | 0 (già ∞) |
| 8 | `warm_refill_wake_max_attempts` | 10 | `app/probes.py:255` | off | 20 |
| 9 | `stream_slow_race_canaries` | 1 | da cablare (vedi 5.5) | — | 2 |

Note:
- **(2)+(3) vanno scalate insieme**: `warm_ready_effective = min(ready_min +
  ceil((rpm−base)/step), min_max)`. Scalare solo `ready_min` lascia il cap a 6 a
  strozzare la spinta.
- **(6) è un freno, non una spesa**: il canary lento si apre se `n < cap`, quindi
  alzarlo lo fa partire *più spesso*. È l'unico della lista che **aumenta** il
  lavoro.
- **(7) ha lo zero con semantica opposta** (0 = illimitato): deve restare 0.
- **`hedge_canaries` regge k arbitrario** (`app/routing/canary.py:56-152`:
  `k = max(1, int(k))`, `out[:k]`). Il limite 1..2 è solo la *validazione della
  policy* (`app/policy.py:3067-3072`): **non va toccata**, perché si scala il
  valore al punto di lettura, non il valore in policy.

### 5.5 Cablare `stream_slow_race_canaries`

Era **morta**: dichiarata (`app/policy.py:1152`), mappata al path YAML
(`warm_pool.slow_race_canaries`, alias a `:194`), validata (`:2850-2856`),
esposta in TUI (`tui/policy_screen.py:112`) ma con **zero usi in `app/`**. Il
commento a `app/policy.py:1134-1135` promette che la gara lenta apra N canari,
ma il codice ne apriva sempre 1.

**Il sito e' UNO SOLO, ed e' `app/chat_hedge.py:500`**, dentro `_hedge_peek`
(il ramo **stream**): aveva `k=1` cablato e usava solo `_lc[0]`. **NON e' in
`forwarder.py`**: quello e' il ramo **non-stream**, dove `_call_single`
(`forwarder.py:3522`) e `_race_canaries` (`:3646`) aprono un canario singolo per
progetto, e **non esiste una manopola `nonstream_slow_race_canaries`**. Le
versioni precedenti di questa SPEC indicavano `forwarder.py:3521`/`:3645`: era un
errore, corretto dopo aver seguito il **timer** invece del nome del metodo —
`stream_slow_race_after_ms` → `chat_stream.py:524` → `_hedge_peek`, che e'
chiamata SOLO da `chat_stream.py:583`.

Fatto: `_ns_k = max(1, spinta(gw_state.router.policy, "stream_slow_race_canaries", 1, lo=1))`
a `chat_hedge.py:507-508`, `k=_ns_k` a `:519`, `for _cc2 in _lc:` a `:536`, nel
rispetto del vincolo che **il canario lento NON concorre al tetto per-sessione**.

Prova: `tests/test_slow_race.py::test_slow_race_canaries_apre_n_canari` e
`::test_superscrocco_raddoppia_i_canari_lenti` (falsificati: rimettendo `k=1`
cadono entrambi). Per isolare la gara lenta nei test serve `hold=True` + A che
emette il primo byte (`_skip_classic` → `cands = []`), altrimenti l'hedge
classico a 60ms apre lui un canario e chiude la gara prima della soglia lenta.

### 5.6 Cosa NON scalare

- **Booleani** (`warm_pool_enabled`, `warm_refill_enabled`, `warm_borrow_enabled`,
  `canary_provider_sweep_enabled`, …): moltiplicare un flag non significa nulla;
  `scale_speculation` li rifiuta.
- **`admission_max_inflight` (128)**: concorrenza *per deployment*, cioè un tetto
  di sicurezza verso l'upstream. Alzarlo non è spinta, è rischio 429 globale.
- **`warm_borrow_idle_sec` (240)**: è un tempo, non un tetto.

## 6. Metriche e log

**Fatto** — la riga `[effort]` (`app/forwarder.py:184-187`) ora riporta anche
`super=` e `ratio=`:

    [effort] <dep> effort=high super=True ratio=2.0 capable=... reasoning=high temp=...

E' l'osservabilita' giusta per questo caso: senza `ratio` **non si distingue una
richiesta super da una `high` normale**, perche' l'`effort` loggato e' `high` in
entrambi i casi (canonicalizzazione voluta). E' l'unico modo per misurare se
superscrocco *serve* (TTFT piu' basso) o solo *costa* di piu'.

**Volutamente NON fatto**: una metrica dedicata `nx_effort_total("superscrocco")`
e label super su `nx_refill_total`/`nx_hedge_total`. Il ratio e' per-richiesta e
la metrica non lo porta, quindi si creerebbe una serie temporale che non si sa
interpretare; il log per-richiesta basta. Se in futuro serve un aggregato, il
posto naturale e' un `metrics.inc` accanto a quelli gia' cablati
(`chat_hedge.py:497` `nx_slow_race_total("open")`).

## 7. Test

Estendere `tests/test_effort_routing.py` (24 test esistenti, devono restare verdi):

- `normalize_effort("superscrocco") == "high"`, idem per ogni alias.
- `effort_from_request({"reasoning_effort": "superscrocco"}) == "high"`.
- `effort_token_from_request({"reasoning_effort": "superscrocco"}) == "superscrocco"`.
- `is_super()` vero sotto `set_effort("superscrocco")`, falso sotto `"high"`.
- `get_speculation_ratio()` == ratio di policy se super, 1.0 altrimenti.
- `scale_speculation`: `0 → 0`, `None → None`, `6 → 12`, `hi` rispettato,
  `bool` rifiutato, `floor_one`.
- Integrazione: con ratio 2, `max_inflight_effective` == 12 e `_hh_k` == 4, **senza
  toccare la policy** (`stream_hedge_tiers` resta 2 e valido).
- Tetto assoluto: con `effort_super_max_inflight_abs = 10` e ratio 2 su base 6,
  il risultato è 10.
- Non-regressione: `default`/`low`/`medium` → ratio 1.0, score intatto.

## 8. Criteri di accettazione

1. `python3 -m pytest -q` → **0 failed, 1 skipped**, con i test nuovi tutti verdi.
   Baseline misurata all'inizio del lavoro: **2584 passed** (il 2574 citato nelle
   versioni precedenti veniva dall'handoff del 2026-09-29 ed era ormai vecchio).
2. `effort_super_ratio` cambiato a caldo via `PUT /admin/policy/raw` cambia la
   spinta senza recreate del container (la policy è letta per-richiesta).
3. Una richiesta con `reasoning_effort: superscrocco` produce nel log
   `[effort] ... effort=high ... super=True ratio=2.0`, e un body upstream con
   `reasoning_effort: "high"` (comportamento identico a `high`).
4. Con `effort_super_enabled: false` il comportamento è **identico** a `high`.
5. `ruff format` **non** viene applicato (regola di progetto).

## 9. Ordine di lavoro

1. `app/effort.py` (canonicalizzazione + token + helper + accessor ratio) + test.
2. `app/policy.py`: 3 manopole + validazione + esposizione admin/TUI.
3. Accessor unico `max_inflight_effective` + i 3 call site.
4. Gli altri punti di scala (5.4).
5. Cablare `stream_slow_race_canaries` (5.5).
6. Metriche + doc (`docs/CONFIGURATION.md`, `docs/ROUTING.md`,
   `HANDOFF-SCROCCO-LLM.md`).
7. `pytest -q` e commit.

## 10. Rischi

- **La spinta si paga in 429.** Il tetto `warm_refill_max_inflight` esiste
  *letteralmente* per non "riaccendere a ogni turno una tempesta di chiamate che
  poi prende 429" (`app/router.py:5165`). Caso concreto noto: **cubotto**, pool
  quasi sempre in cooldown. Il tetto assoluto `effort_super_max_inflight_abs`
  mitiga; le metriche (6) dicono se basta.
- **Divergenza fra i tre lettori** dello stesso tetto → accessor unico (5.3).
- **Nessuna modifica al contratto effort esistente**: la canonicalizzazione è
  l'unica scelta che lo garantisce.
