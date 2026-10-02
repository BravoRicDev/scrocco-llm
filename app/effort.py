"""Stato per-request dell'`effort` (reasoning_effort) e delle regole collegate.

L'effort arriva dal client nel body (`reasoning_effort`, oppure l'alias
`effort`) o dall'header `x-effort`. Valori normalizzati: default/low/medium/high.

`superscrocco` NON e' un quinto livello di comportamento: e' `high` PIU' un
moltiplicatore della SPINTA speculativa (warm pool, canary in volo, gare) della
sola richiesta che lo chiede. Viene canonicalizzato qui, una volta sola, cosi'
ogni consumatore esistente (`effort == "high"`) resta invariato e il contratto
effort non cambia: il ratio vive in campi separati dello stato, perche' e' una
dimensione ORTOGONALE al livello.

Il ratio scala i TETTI ("quanti tentativi posso spendere"), mai i TIMER: i timer
della spinta (`hedge_delay_ms`, `slow_canary_after_ms`) sono calibrati sul TTFT
fisiologico dell'upstream, e accorciarli sotto di esso fa partire canari che
perdono quasi sempre — spreco, non velocita'. Vedi `scale_speculation`.

Lo stato e' in una `ContextVar`: il router la legge per il bias di intelligence
e il forwarder per iniettare/rimuovere `reasoning_effort` e per l'override di
temperatura. Essendo contextvar, resta isolato per-task (una richiesta non
"sporca" le concorrenti). I task speculativi creati con `asyncio.ensure_future`
ereditano il contesto al momento della creazione, quindi vedono l'effort della
richiesta senza alcun plumbing aggiuntivo.
"""

from __future__ import annotations

import contextvars
import math
from typing import Any

DEFAULT = "default"
_VALID = (DEFAULT, "low", "medium", "high")

# Alias che NON sono un quinto livello: `superscrocco` e' `high` + ratio di
# spinta. Canonicalizzati a "high" da `normalize_effort`, una volta sola.
# Questa e' la lista delle grafie CANONICHE; il confronto vero passa da
# `_super_key`, che ignora i separatori (vedi sotto).
_SUPER = ("superscrocco", "super-scrocco", "super", "xhigh", "max", "ultra")

# Separatori ignorati nel confronto: `x-high`, `x_high` e `x high` sono la
# STESSA cosa di `xhigh`.
_SEP = str.maketrans("", "", "-_ \t")
_SUPER_KEYS = frozenset(str(a).translate(_SEP).strip().lower() for a in _SUPER)

# Grafie aggiuntive PUBBLICATE in `/v1/models` solo per rendere VISIBILE la
# regola dei separatori: `x-high` e' la stessa cosa di `xhigh`, e un client che
# legge la capability non deve scoprirlo provando.
_ADVERTISED_EXTRA = ("x-high",)

SUPER_RATIO_DEFAULT = 2.0
RATIO_MIN = 1.0
RATIO_MAX = 8.0


def _super_key(raw: Any) -> str:
    """Chiave di confronto di un token effort: minuscola, SENZA separatori.

    Perche' non un confronto letterale: i client scrivono la grafia che
    vogliono (`xhigh`, `x-high`, `x_high`, `X-HIGH`) e con `in _SUPER` ogni
    variante non elencata cadeva in silenzio su `default` — nessun bias,
    nessuna iniezione, nessuna spinta, e nessun errore che lo dicesse. Cosi'
    la tolleranza e' una proprieta' della REGOLA, non una lista da tenere
    aggiornata una grafia per volta.
    """
    return str(raw or "").translate(_SEP).strip().lower()


def advertised_efforts() -> list[str]:
    """Valori `reasoning_effort` accettati, per la capability di `/v1/models`.

    I 4 livelli canonici PIU' gli alias di `superscrocco`: un client che legge
    la capability deve poter scoprire `xhigh`/`x-high`/`superscrocco` senza
    indovinare la grafia. Una sola fonte di verita' (`_SUPER`) piu' una grafia
    con separatore pubblicata apposta per rendere visibile la regola, cosi' la
    lista pubblicata non puo' divergere da quella accettata.
    """
    return [DEFAULT, "low", "medium", "high", *_SUPER, *_ADVERTISED_EXTRA]


# Chiave di default neutra: nessun deployment e' penalizzato, nessuna iniezione,
# nessuna spinta extra (ratio 1.0).
_state: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "nx_effort_state",
    default={"effort": DEFAULT, "super": False, "ratio": RATIO_MIN, "temp_enabled": False, "temp_overrides": {}},
)


def is_super_effort(raw: Any) -> bool:
    """True se il valore GREZZO e' un alias di `superscrocco`.

    Da usare sul token non canonicalizzato: dopo `normalize_effort` l'origine
    super e' irriconoscibile (il livello canonico e' `high`).
    """
    return _super_key(raw) in _SUPER_KEYS


def normalize_effort(raw: Any) -> str:
    """normalizza il valore grezzo a uno dei 4 livelli.

    `superscrocco` (e i suoi alias) diventa `high`: il COMPORTAMENTO e' identico,
    la differenza sta solo nel ratio di spinta (`get_speculation_ratio`).

    Il confronto ignora i separatori (`_super_key`), quindi `x-high` vale
    `xhigh` e `super_scrocco` vale `super-scrocco`.
    """
    m = _super_key(raw)
    if m in _SUPER_KEYS:
        return "high"
    if m in ("low", "medium", "high"):
        return m
    if m in ("minimal", "min"):
        return "low"
    return DEFAULT


def _raw_effort(payload: Any, headers: Any = None) -> str:
    """Valore grezzo (minuscolo): body `reasoning_effort` > body `effort` > header."""
    raw = None
    if isinstance(payload, dict):
        raw = payload.get("reasoning_effort")
        if raw is None:
            raw = payload.get("effort")
    if raw is None and headers is not None:
        try:
            raw = headers.get("x-effort")
        except Exception:  # noqa: BLE001
            raw = None
    return str(raw or "").strip().lower()


def effort_from_request(payload: Any, headers: Any = None) -> str:
    """Estrae l'effort da body (`reasoning_effort`/`effort`) o header `x-effort`."""
    return normalize_effort(_raw_effort(payload, headers))


def effort_token_from_request(payload: Any, headers: Any = None) -> str:
    """Come `effort_from_request`, ma PRESERVA il token `superscrocco`.

    Serve a `set_effort`: `effort_from_request` canonicalizza a "high", quindi
    non permetterebbe piu' di distinguere una richiesta `high` da una
    `superscrocco` — e il ratio di spinta andrebbe perso. Qui il token super
    sopravvive; ogni altro valore resta normalizzato come sempre.
    """
    m = _raw_effort(payload, headers)
    # `is_super_effort` (NON `m in _SUPER`): il confronto letterale lascerebbe
    # fuori le grafie con separatori (`x-high`), che verrebbero canonicalizzate
    # a "high" PRIMA di `set_effort` e perderebbero il ratio in silenzio.
    return m if is_super_effort(m) else normalize_effort(m)


def set_effort(
    effort: Any,
    *,
    temp_enabled: bool = False,
    temp_overrides: dict | None = None,
    super_ratio: float = SUPER_RATIO_DEFAULT,
    super_enabled: bool = True,
) -> contextvars.Token:
    """Imposta lo stato per la richiesta corrente. Ritorna un token per reset.

    `effort` puo' essere il token grezzo (`effort_token_from_request`) oppure un
    livello gia' normalizzato: in entrambi i casi lo stato memorizza il livello
    canonico, e `super=True` solo se il token era un alias di `superscrocco` e
    la spinta extra e' abilitata.

    `super_ratio` e' la manopola `effort_super_ratio`, clampata in
    [RATIO_MIN, RATIO_MAX]: sotto 1.0 non ha senso (rallenterebbe la spinta
    invece di accelerarla).

    NON esiste qui un parametro per il tetto assoluto: `effort_super_max_inflight_abs`
    limita i CONTEGGI inflight, non il ratio, e si applica al punto di lettura
    via `scale_speculation(hi=...)`. Un parametro che sembrasse applicarlo qui
    sarebbe un falso amico (16 > RATIO_MAX = 8.0: non cambierebbe nulla e
    sembrerebbe invece attivo).
    """
    sup = bool(super_enabled) and is_super_effort(effort)
    ratio = RATIO_MIN
    if sup:
        try:
            ratio = float(super_ratio)
        except (TypeError, ValueError):
            ratio = SUPER_RATIO_DEFAULT
        ratio = max(RATIO_MIN, min(RATIO_MAX, ratio))
    return _state.set(
        {
            "effort": normalize_effort(effort),
            "super": sup,
            "ratio": ratio,
            "temp_enabled": bool(temp_enabled),
            "temp_overrides": dict(temp_overrides or {}),
        }
    )


def reset_effort(token: contextvars.Token) -> None:
    try:
        _state.reset(token)
    except (ValueError, LookupError):
        pass


def get_effort() -> str:
    return _state.get()["effort"]


def is_super() -> bool:
    """True se la richiesta corrente e' `superscrocco` con la spinta attiva."""
    return bool(_state.get().get("super"))


def get_speculation_ratio() -> float:
    """Moltiplicatore della spinta speculativa.

    1.0 per ogni livello diverso da `superscrocco`; `effort_super_ratio`
    (default 2.0) per superscrocco.
    """
    try:
        return max(RATIO_MIN, float(_state.get().get("ratio") or RATIO_MIN))
    except (TypeError, ValueError):
        return RATIO_MIN


def get_temperature_config() -> tuple[bool, dict]:
    s = _state.get()
    return bool(s.get("temp_enabled")), dict(s.get("temp_overrides") or {})


def scale_speculation(
    value: Any, *, lo: int = 0, hi: int | None = None, floor_one: bool = False, ratio: float | None = None
) -> int | None:
    """Scala una manopola di SPINTA per il ratio dell'effort corrente.

    Regole deliberate (ognuna difende da un caso reale):
    - `None` -> `None`: non si inventa un valore dove la policy non ne ha uno.
    - **0 -> 0**: una feature SPENTA non viene resuscitata dal ratio
      (`warm_refill_max_inflight = 0` significa "nessun canary refill"; 0 e'
      anche la semantica di `stream_hedge_max_races`, dove pero' significa
      ILLIMITATO — in entrambi i casi il valore giusto e' 0, non 0*ratio).
    - **i booleani sono rifiutati**: moltiplicare un flag non significa nulla, e
      un `True` silenziosamente scalato sarebbe un difetto invisibile.
    - `hi` clampa il risultato (per i tetti assoluti e i range validati).
    - `floor_one`: un valore > 0 non scende mai sotto 1.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("scale_speculation: un flag booleano non e' una manopola di spinta")
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v == 0:
        return 0
    r = get_speculation_ratio() if ratio is None else ratio
    try:
        r = max(RATIO_MIN, float(r))
    except (TypeError, ValueError):
        r = RATIO_MIN
    try:
        scaled = v if r <= RATIO_MIN else int(math.floor(v * r + 0.5))
    except (TypeError, ValueError, OverflowError):
        scaled = v
    if floor_one and v > 0 and scaled < 1:
        scaled = 1
    if scaled < lo:
        scaled = lo
    if hi is not None and scaled > hi:
        scaled = hi
    return scaled


def spinta(
    policy: Any, attr: str, default: int = 0, *, lo: int = 0, hi: int | None = None, floor_one: bool = False
) -> int:
    """Legge una manopola di spinta dalla policy e la scala per l'effort corrente.

    Una riga per call site, cosi' il ratio si applica in un punto solo: due
    lettori della stessa manopola non possono piu' divergere (e' la classe di bug
    gia' pagata con `_slow_ms` letto da `qcp` invece che da `Policy`, che lasciava
    la gara lenta a non partire mai).
    """
    try:
        raw = getattr(policy, attr, default)
    except Exception:  # noqa: BLE001
        raw = default
    if raw is None:
        raw = default
    out = scale_speculation(raw, lo=lo, hi=hi, floor_one=floor_one)
    if out is None:
        try:
            return int(default)
        except (TypeError, ValueError):
            return 0
    return out


def _abs_inflight_cap(policy: Any) -> int | None:
    """Tetto assoluto `effort_super_max_inflight_abs`, SOLO per superscrocco.

    Fuori da superscrocco ritorna None: il tetto non deve clampare un valore
    configurato piu' alto (es. `warm_refill_max_inflight: 24` resta 24 per
    `high`). E' una manopola anti-429 di superscrocco, non un limite generale.
    """
    if not is_super():
        return None
    try:
        cap = int(getattr(policy, "effort_super_max_inflight_abs", 0) or 0)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def max_inflight_effective(policy: Any, default: int = 6) -> int:
    """Tetto di canary in VOLO per il warm-refill, scalato dal ratio.

    Lettore UNICO di `warm_refill_max_inflight`: la manopola e' letta in tre
    posti (`chat_stream._plan_warm_refill`, `chat_hedge` ramo refill,
    `forwarder._init_call_state`), e se due di essi divergessero il canary
    nascerebbe e verrebbe buttato — la stessa classe di bug gia' pagata con
    `_slow_ms` letto da `qcp` invece che da `Policy`, che lasciava la gara
    lenta a non partire mai.
    """
    return spinta(
        policy,
        "warm_refill_max_inflight",
        default,
        lo=0,
        hi=_abs_inflight_cap(policy),
        floor_one=True,
    )


def slow_race_max_warm_effective(policy: Any, default: int = 6) -> int:
    """Lettore UNICO di `slow_race_max_warm` (1 gate + 3 messaggi di log).

    E' un FRENO, non un tetto: `router.slow_race_allowed` apre il canario lento
    solo se la sessione ha MENO di `cap` warm NON lenti. Alzare il cap lo
    ALLENTA, che e' esattamente la "spinta" richiesta da superscrocco.

    Perche' anche i log: `chat_hedge.py` e `forwarder.py` (due volte) stampano
    `warm gia' pieno (>=%s)` leggendo la manopola per conto proprio. Se solo il
    gate fosse scalato, il log annuncerebbe "6" mentre il freno e' a 12 —
    una diagnosi falsa proprio nel momento in cui si indaga un 429.
    """
    return spinta(policy, "slow_race_max_warm", default, lo=0)
