"""Stima token: estratto verbatim da app/router.py (R0).

Codice spostato senza modifiche di comportamento da app/router.py
righe 121-513. Contiene funzioni di stima, costanti e stato
condiviso del cluster di stima.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..capabilities import count_image_parts
from ..constants import CHARS_PER_TOKEN

# Constants moved from router.py (defined before the function block at line 91).
CTX_BUCKETS = (8000, 32000, 128000)
CTX_BUCKET_COUNT = len(CTX_BUCKETS) + 1

log = logging.getLogger("nx.router")


def _is_quota_evidence(reason: str | None, status: int | None = None) -> bool:
    """True se l'evidenza di fallimento e' di QUOTA (429/satura), non di guasto.

    Stessa regola di KeyHealth.observe (F30): una chiave satura e' viva, non
    rotta. Usata per NON contare questi fallimenti verso il ritiro automatico.
    """
    r = (reason or "").lower()
    try:
        st = abs(int(status)) if status else 0
    except (TypeError, ValueError):
        st = 0
    return bool(st == 429 or "429" in r or "quota" in r or "rate_limit" in r)


def _ctx_bucket(ctx_est) -> int:
    """Indice di bucket per la stima token del contesto; -1 = ignoto."""
    try:
        c = int(ctx_est)
    except (TypeError, ValueError):
        return -1
    for i, edge in enumerate(CTX_BUCKETS):
        if c < edge:
            return i
    return len(CTX_BUCKETS)


class ErrorKind:
    """Classificazione centralizzata degli errori upstream (strategia di
    recupero diversa per categoria).

    - TRANSIENT:      errore di rete/timeout/5xx -> cooldown breve, riprova.
    - QUOTA_RESET:    quota esaurita con reset temporale noto -> cooldown
                      ESATTO al reset, senza escalation.
    - PERMANENT_DEAD: chiave morta / modello rimosso -> niente cooldown,
                      si ritira il deployment (CSV intatto).
    - GENERIC_4XX:    altro 4xx -> escalation attuale.
    """

    TRANSIENT = "transient"
    QUOTA_RESET = "quota_reset"
    PERMANENT_DEAD = "permanent_dead"
    GENERIC_4XX = "generic_4xx"


LATENCY_PENALTY_PER_SEC = 0.5  # 0.5 points per second over threshold

# Dynamic scoring defaults (override via gateway.yaml -> policy)
DYNAMIC_SCORING_DEFAULTS = {
    "enabled": True,
    "latency_p95_weight": 1.0,
    "error_rate_weight": 2.0,
    "throughput_weight": 0.5,
    "history_window": 100,
}

# Provider bias normalization: "log" (default), "sqrt", "none"
PROVIDER_BIAS_NORMALIZATION = "log"


@dataclass
class DepStats:
    """Statistiche runtime per deployment (rotazione adattiva).

    I campi budget_* (Feature no-spreco) NON sono persistiti: le finestre
    ripartono pulite al restart e i cap si ri-apprendono al primo 429.
    """

    last_used: float = 0.0
    ema_latency_ms: float | None = None
    inflight: int = 0
    # Prefill REALE in volo (somma delle ctx_est): un heavy da 90k pesa come
    # 18 light da 5k. E' il limiter che resta vero anche coi gemelli, perche'
    # l'upstream vede la somma dei token, non il conteggio locale.
    inflight_tokens: int = 0
    fail_streak: int = 0  # fallimenti consecutivi (escalation cooldown)
    success_ema: float | None = None  # tasso successo stimato (penalità dolce)
    last_reason: str | None = None  # ultimo motivo di fallimento (402, 429...)
    last_provenance: str | None = None  # nascita del cooldown (P0)
    # --- budget guard: finestre scorrevoli + cap appresi dai 429 ---------
    minute_calls: int = 0  # chiamate nel minuto corrente
    minute_key: str = ""  # "YYYY-MM-DDTHH:MM" del bucket corrente
    day_calls: int = 0  # chiamate nel giorno corrente (UTC)
    day_key: str = ""  # "YYYY-MM-DD"
    min_cap_learned: float = 0.0  # 0 = nessun limite appreso
    day_cap_learned: float = 0.0
    # --- fail count 24h: cooldown lineare basato su fallimenti giornalieri ---
    fail_count_24h: int = 0
    fail_day_key: str = ""  # "YYYY-MM-DD" del giorno corrente
    # --- contatori cumulativi + timestamp (persistiti in adaptive_stats) ---
    ok_count: int = 0  # successi cumulativi (mai azzerati)
    fail_count: int = 0  # fallimenti cumulativi (mai azzerati)
    last_success_ts: float = 0.0  # timestamp ultimo successo
    last_fail_ts: float = 0.0  # timestamp ultimo fallimento
    probe_fail_streak: int = 0  # probe passivi consecutivi falliti (cap)
    json_fallback: int = 0  # quante volte ha ignorato stream:true (JSON->SSE)
    # --- dynamic scoring: feature osservate per-deployment ---
    latency_history: list = field(default_factory=list)  # ultimi N (bucket, ctx, ms)
    total_tokens: int = 0  # token completati cumulativi
    total_duration_ms: float = 0.0  # durata cumulativa ms
    recent_failures: int = 0  # fallimenti negli ultimi N tentativi
    recent_attempts: int = 0  # tentativi negli ultimi N


HOT_WORDS: dict[str, str] = {
    r"pensaci\s+bene": "max",
    r"pensa\s+a\s+fondo": "max",
    r"\bragiona\b": "max",
    r"deep\s*think": "max",
}
HOT_WORDS_WINDOW = 3


def _json_size(obj: Any) -> int:
    """Lunghezza del JSON serializzato; 0 su errore (mai bloccare il routing)."""
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return 0


def _prompt_chars(messages: Any, tools: Any = None) -> int:
    """Caratteri del payload prompt (stessa base di `_estimate_legacy`).

    Conta content (str o parti testuali), tool_calls (nome + arguments JSON),
    reasoning_content/reasoning e, se presenti, gli schemi `tools`. E' la base
    su cui si misura il rapporto REALE char/token del provider (vedi
    `note_session_estimate`): va chiamata sia sui messaggi EFFETTIVAMENTE
    inviati a monte (dopo histnorm/ctxcompact) sia sulla preview pre-ctxcompact
    usata al routing, cosi' i due lati della stima coincidono.
    """
    total = 0
    if tools:
        total += _json_size(list(tools))
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            total += sum(len(p.get("text", "")) for p in c if isinstance(p, dict))
        for tc in m.get("tool_calls") or ():
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                total += len(str(fn.get("name") or ""))
                total += _json_size(fn.get("arguments"))
        rc = m.get("reasoning_content") or m.get("reasoning")
        if isinstance(rc, str):
            total += len(rc)
    return total


def _estimate_legacy(
    messages: Any, divisor: int = CHARS_PER_TOKEN, image_token_estimate: int = 0, tools: Any = None
) -> int:
    """Stima storica: somma caratteri / divisor (default chars/4)."""
    tokens = _prompt_chars(messages, tools) // max(1, divisor)
    if image_token_estimate > 0:
        tokens += count_image_parts(messages) * image_token_estimate
    return tokens


def _tokens_for_text(s: str) -> int:
    """Stima token di UNA stringa con densita' adattiva (nostra, no upstream).

    Il semplice chars/4 e' tarato sulla prosa inglese: sottostima il codice/JSON
    (ricchi di simboli) e sopravvaluta testo CJK/accentato. Qui scegliamo un
    divisore per-blocco in base alla composizione del testo. Non e' una
    calibrazione dagli usage upstream (falsati da troncamento/compressione) ma
    una stima piu' realistica del contenuto reale.
    """
    n = len(s)
    if n == 0:
        return 0
    non_ascii = 0
    symbols = 0
    for ch in s:
        if ord(ch) > 127:
            non_ascii += 1
        elif not ch.isalnum() and not ch.isspace():
            symbols += 1
    div = float(CHARS_PER_TOKEN)
    # Codice/JSON: molti simboli -> piu' token per carattere.
    if symbols / n > 0.15:
        div -= 0.8
    # CJK/accentato: pochissimi caratteri per token.
    if non_ascii / n > 0.10:
        div = min(div, 2.2)
    div = max(1.5, div)
    return int(n / div)


def _estimate_adaptive(
    messages: Any, divisor: int = CHARS_PER_TOKEN, image_token_estimate: int = 0, tools: Any = None
) -> int:
    """Stima adattiva: densita' per-blocco invece di un divisore unico."""
    total = 0
    if tools:
        total += _tokens_for_text(json.dumps(list(tools), ensure_ascii=False, default=str))
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            total += _tokens_for_text(c)
        elif isinstance(c, list):
            total += sum(_tokens_for_text(p.get("text", "")) for p in c if isinstance(p, dict))
        for tc in m.get("tool_calls") or ():
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                total += _tokens_for_text(str(fn.get("name") or ""))
                total += _tokens_for_text(json.dumps(fn.get("arguments"), ensure_ascii=False, default=str))
        rc = m.get("reasoning_content") or m.get("reasoning")
        if isinstance(rc, str):
            total += _tokens_for_text(rc)
    if image_token_estimate > 0:
        total += count_image_parts(messages) * image_token_estimate
    return total


# Modalita' della stima: shadow = calcola e logga entrambe ma ritorna la
# legacy; adaptive = ritorna quella adattiva. Configurate dalla policy.
_ESTIMATE_ADAPTIVE = False
_ESTIMATE_SHADOW = True
_estimate_shadow_stats: dict[str, int] = {"n": 0, "legacy": 0, "adaptive": 0}
# AUTO-ADAPTIVE: il rollout della stima non deve dipendere da un intervento
# manuale ne' da una finestra di traffico che i deploy azzerano. I contatori
# shadow sopravvivono al restart (persistiti in adaptive_stats.json) e, appena
# ci sono campioni sufficienti con delta contenuto, la stima adattiva si
# attiva da sola. La policy resta il master switch manuale.
_ESTIMATE_AUTO_ALLOWED = False
_ESTIMATE_AUTO_ON = False
_ESTIMATE_AUTO_MIN_N = 200
_ESTIMATE_AUTO_MAX_DELTA_PCT = 5.0


def configure_estimate(
    *,
    adaptive: bool,
    shadow: bool,
    auto_enable: bool | None = None,
    auto_min_n: int | None = None,
    auto_max_delta_pct: float | None = None,
) -> None:
    global _ESTIMATE_ADAPTIVE, _ESTIMATE_SHADOW, _ESTIMATE_AUTO_ALLOWED
    global _ESTIMATE_AUTO_MIN_N, _ESTIMATE_AUTO_MAX_DELTA_PCT, _ESTIMATE_AUTO_ON
    _ESTIMATE_ADAPTIVE = bool(adaptive)
    _ESTIMATE_SHADOW = bool(shadow)
    if auto_enable is not None:
        _ESTIMATE_AUTO_ALLOWED = bool(auto_enable)
        if not _ESTIMATE_AUTO_ALLOWED:
            _ESTIMATE_AUTO_ON = False  # policy off -> spegne anche il runtime
    if auto_min_n is not None:
        _ESTIMATE_AUTO_MIN_N = max(1, int(auto_min_n))
    if auto_max_delta_pct is not None:
        _ESTIMATE_AUTO_MAX_DELTA_PCT = max(0.0, float(auto_max_delta_pct))
    if _ESTIMATE_ADAPTIVE:
        _ESTIMATE_AUTO_ON = False  # il master switch esplicito vince


def estimate_auto_state() -> dict:
    """Stato del rollout automatico (admin e test)."""
    return {
        "allowed": _ESTIMATE_AUTO_ALLOWED,
        "on": _ESTIMATE_AUTO_ON,
        "min_n": _ESTIMATE_AUTO_MIN_N,
        "max_delta_pct": _ESTIMATE_AUTO_MAX_DELTA_PCT,
    }


def load_estimate_shadow(data: dict) -> None:
    """Ripristina i contatori shadow (n/legacy/adaptive) da disco."""
    if not isinstance(data, dict):
        return
    try:
        n = max(0, int(data.get("n") or 0))
        lg = max(0, int(data.get("legacy") or 0))
        ad = max(0, int(data.get("adaptive") or 0))
    except (TypeError, ValueError):
        return
    if n and (lg <= 0 or ad < 0):
        return
    _estimate_shadow_stats["n"] = n
    _estimate_shadow_stats["legacy"] = lg
    _estimate_shadow_stats["adaptive"] = ad


def _maybe_auto_adaptive() -> None:
    """Accende la stima adattiva quando l'evidenza shadow e' sufficiente."""
    global _ESTIMATE_AUTO_ON
    if _ESTIMATE_AUTO_ON or _ESTIMATE_ADAPTIVE or not _ESTIMATE_AUTO_ALLOWED:
        return
    n = _estimate_shadow_stats["n"]
    if n < _ESTIMATE_AUTO_MIN_N:
        return
    lg = _estimate_shadow_stats["legacy"]
    if lg <= 0:
        return
    delta = abs(_estimate_shadow_stats["adaptive"] - lg) * 100.0 / lg
    if delta <= _ESTIMATE_AUTO_MAX_DELTA_PCT:
        _ESTIMATE_AUTO_ON = True
        log.info("[estimate] auto-adaptive ON: n=%d delta=%.2f%% (<= %.2f%%)", n, delta, _ESTIMATE_AUTO_MAX_DELTA_PCT)


def estimate_shadow_stats() -> dict:
    """Contatori cumulativi della modalita' shadow (per /admin/policy)."""
    s: dict[str, Any] = dict(_estimate_shadow_stats)
    n = s.get("n") or 0
    if n:
        s["legacy_avg"] = round(s["legacy"] / n, 1)
        s["adaptive_avg"] = round(s["adaptive"] / n, 1)
        s["delta_pct"] = round((s["adaptive"] - s["legacy"]) * 100.0 / max(1, s["legacy"]), 1)
    s["auto"] = estimate_auto_state()
    return s


def estimate_tokens(
    messages: Any, divisor: int = CHARS_PER_TOKEN, image_token_estimate: int = 0, tools: Any = None
) -> int:
    """Stima grezza del contesto: somma caratteri / divisor (default chars/4).

    Conta TUTTO cio' che l'upstream fatturera' nel prompt: content dei
    messaggi, tool_calls (nome + arguments), reasoning_content/reasoning e,
    se fornito, gli schemi in `tools`. Se non li si conta, tool_calls e
    reasoning restano invisibili alla stima e il contesto reale viene
    sottovalutato fino a ~12x (poi l'upstream risponde 413).
    Se image_token_estimate > 0, aggiunge quel valore per ogni parte-immagine.

    Con la stima adattiva attiva (shadow o adaptive, da policy) calcola anche
    la variante `_estimate_adaptive`; in shadow logga il confronto e ritorna la
    legacy, cosi' si valida senza cambiare il routing.
    """
    legacy = _estimate_legacy(messages, divisor, image_token_estimate, tools)
    if not (_ESTIMATE_ADAPTIVE or _ESTIMATE_SHADOW):
        return legacy
    adaptive = _estimate_adaptive(messages, divisor, image_token_estimate, tools)
    _estimate_shadow_stats["n"] += 1
    _estimate_shadow_stats["legacy"] += legacy
    _estimate_shadow_stats["adaptive"] += adaptive
    _maybe_auto_adaptive()
    if _ESTIMATE_SHADOW and not (_ESTIMATE_ADAPTIVE or _ESTIMATE_AUTO_ON):
        log.debug("[estimate] shadow legacy=%d adaptive=%d delta=%+d", legacy, adaptive, adaptive - legacy)
        return legacy
    return adaptive


def detect_hot_words(messages: Any, patterns: list[str] | None = None, window: int = HOT_WORDS_WINDOW) -> bool:
    """Hot word negli ultimi N messaggi USER (non assistant: falsi positivi)."""
    patterns = patterns or list(HOT_WORDS)
    if not messages:
        return False
    import re as _re

    for msg in messages[-window:]:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content:
            continue
        for pattern in patterns:
            if _re.search(pattern, content, _re.IGNORECASE):
                return True
    return False


def inject_identity(data: dict, dep: dict, router=None) -> None:
    """Sostituisce/inserisce il system message d'identità e setta il modello univoco.

    Replica _select_and_inject() dell'hook: certezza del modello che risponde.
    Con router passato, arricchisce il log con l'EMA di latenza del deployment.
    """
    real = dep["model"]
    sys_msg = {
        "role": "system",
        "content": (f"You are {real}. When the user asks which model you are, reply exactly: I am {real}."),
    }
    messages = list(data.get("messages") or [])
    if messages and messages[0].get("role") == "system":
        messages[0] = sys_msg
    else:
        messages.insert(0, sys_msg)
    data["messages"] = messages
    data["model"] = dep["unique"]
    extra = ""
    if router is not None:
        s = router.stats_for(dep["unique"])
        if s.ema_latency_ms:
            extra = f", ema={s.ema_latency_ms:.0f}ms"
    prov = dep.get("api_base", "")
    if "://" in prov:
        prov = prov.split("://", 1)[1].split("/", 1)[0]
    log.info("[identity] %s -> %s (%s via %s%s)", dep["group"], dep["unique"], real, prov, extra)
