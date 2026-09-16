"""Rotazione adattiva: pick, fallback, cooldown, sticky session.

[IT] COSA: sceglie CHI risponde dentro un gruppo e CHI subentra dopo un
errore. HOW: punteggio = EMA latenza + freshness (recency_halflife) +
inflight + tasso successo; cooldown con ESCALATION esponenziale
(600s * 2^streak, cap 24h). WHY:
  - EMA/freshness: evita il martellamento della prima chiave del CSV e
    scarta da sola i provider lentiti.
  - escalation: un 429 ripetuto = quota esausta per ORE, non minuti.
  - SAME-MODEL FAILOVER (gen/stt): cambiare modello a meta job media/video
    cambia costi/formati -> prima le ALTRE CHIAVI dello stesso modello;
    cross-model solo a esaurimento (log CROSS-MODEL).
  - MEDIA DEFER (multimodal_last_resort): per testo puro i multimodali
    sono ultima spiaggia -- protegge il free-tier.
  - DIMS LADDER (dims_ladder_floor): un -200k ESPLICITO e un MINIMO:
    se il gruppo muore si scala SU, mai giu.
  - sticky: stessa conversazione -> stesso gruppo, mai sugli espliciti.

[EN] WHAT: adaptive deployment selection + failover. WHY: latency EMA +
freshness prevents hammering row #1; exponential cooldown matches quota
exhaustion timescales; same-model-first keeps media jobs coherent;
multimodal last resort for pure text; explicit floors escalate upward.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import math
import random
import re
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .config import GatewayConfig, CAP_PRIORITY_ORDER, ORDER_LAST
from .policy import Policy
from .capabilities import required_caps, count_image_parts
from .effort import get_effort
from .thought_sig import is_gemini_deployment, should_avoid_gemini
from . import metrics

log = logging.getLogger("nx.router")

# Peso del bias di intelligence nel pick in base all'effort richiesto.
# Il punteggio di reputazione e' "lower is better": per effort=high i modelli
# piu' intelligenti ricevono un bonus (score negativo), per effort=low un
# malus; per medium si penalizza la distanza da 5 (intelligenza media).
# Valore configurabile via gateway.yaml (policy.effort_intel_weight), default 10.
EFFORT_CAPABLE_BONUS = 1.5

# Constants imported from app/constants.py
from app.constants import CHARS_PER_TOKEN as CHARS_PER_TOKEN
from app.constants import STICKY_TTL_SECONDS as STICKY_TTL_SECONDS
from app.constants import COOLDOWN_BASE_SECONDS as COOLDOWN_SECONDS
from app.constants import SCORING_WEIGHTS as SW

# Latency-based routing parameters (configurable via env vars / gateway.yaml)
LATENCY_ROTATE_THRESHOLD_MS = 90000   # 90 seconds default threshold
# SOFT demote (C size-aware): un successo 60-90s su una richiesta PESANTE
# (>30k token) marca il dep "lento soft" SOLO per le successive richieste
# pesanti della stessa sessione; le leggere lo usano normalmente. Il hard
# (> LATENCY_ROTATE_THRESHOLD_MS) vale invece per qualunque richiesta.
SOFT_SLOW_LATENCY_MS = 60000
SOFT_SLOW_CTX_MIN = 30000
# BUCKET DI CONTESTO per le EMA di latenza: un dep che vola su 5k token non e'
# lo stesso dep che stalla su 100k. Bordi DESTRI: ctx < 8000 -> 0;
# 8000..31999 -> 1; 32000..127999 -> 2; >=128000 -> 3.
CTX_BUCKETS = (8000, 32000, 128000)
# I bordi DESTRI dividono N+1 bucket: l'ultimo (>=128000) ha indice 3. Le righe
# EMA devono essere lunghe CTX_BUCKET_COUNT, NON len(CTX_BUCKETS) (off-by-one
# che su ctx>128k faceva IndexError su v[b] e perdeva il bucket 3 al reload).
CTX_BUCKET_COUNT = len(CTX_BUCKETS) + 1
# PREFILL RATE (F9 "tassa dei gemelli"): il TTFT di un 80k non e' confrontabile
# con quello di un 8k. Teniamo per-deployment un EMA in ms per 1k token di
# contesto: e' la quantita' INVARIANTE al mix di contesti, e serve a estrapolare
# il TTFT atteso quando il bucket richiesto non ha campioni (prima si ripiegava
# sull'EMA globale mista -> deadline troppo stretto -> rotazioni/503 su catene
# fredde con contesti grossi).
TTFT_RATE_MIN_CTX = 8000        # sotto: il prefill e' trascurabile/rumoroso
TTFT_RATE_FLOOR_MS = 250.0      # pavimento assoluto dell'estrapolazione
# Demote per-sessione SOLO se la chiamata e' lenta sia in termini ASSOLUTI sia
# RELATIVI alla baseline del dep nello STESSO bucket (>2x): chi ha sempre
# servito i contesti grossi non viene punito per la sua natura.
SLOW_REL_BASELINE_MULT = 2.0

# SOGLIA "LENTO" SIZE-AWARE (B3 ibrida): "lento" = peggiore del doppio della
# norma PER QUELLA TAGLIA, non oltre i 90s fissi (un 128k che risponde in
# 100s e' normale, non lento). Baseline: mediana di FLOTTA del bucket ->
# stima dal rate di prefill/generazione -> 90s legacy. Floor assoluto per non
# marchiare quando la flotta e' tutta veloce.
SLOW_LATENCY_ABS_FLOOR_MS = 45000.0
SLOW_LATENCY_REL_MULT = 2.0
SLOW_LATENCY_MIN_PEERS = 5
SLOW_GEN_MULT = 6.0                 # total atteso ~ ttft * mult (fallback)
SLOW_TYPICAL_COMPLETION_TOKENS = 600.0   # output tipico per la stima gen


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
LATENCY_PENALTY_PER_SEC = 0.5         # 0.5 points per second over threshold

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
    fail_streak: int = 0              # fallimenti consecutivi (escalation cooldown)
    success_ema: float | None = None  # tasso successo stimato (penalità dolce)
    last_reason: str | None = None    # ultimo motivo di fallimento (402, 429...)
    last_provenance: str | None = None  # nascita del cooldown (P0)
    # --- budget guard: finestre scorrevoli + cap appresi dai 429 ---------
    minute_calls: int = 0             # chiamate nel minuto corrente
    minute_key: str = ""              # "YYYY-MM-DDTHH:MM" del bucket corrente
    day_calls: int = 0                # chiamate nel giorno corrente (UTC)
    day_key: str = ""                 # "YYYY-MM-DD"
    min_cap_learned: float = 0.0      # 0 = nessun limite appreso
    day_cap_learned: float = 0.0
    # --- fail count 24h: cooldown lineare basato su fallimenti giornalieri ---
    fail_count_24h: int = 0
    fail_day_key: str = ""            # "YYYY-MM-DD" del giorno corrente
    # --- contatori cumulativi + timestamp (persistiti in adaptive_stats) ---
    ok_count: int = 0                 # successi cumulativi (mai azzerati)
    fail_count: int = 0               # fallimenti cumulativi (mai azzerati)
    last_success_ts: float = 0.0      # timestamp ultimo successo
    last_fail_ts: float = 0.0         # timestamp ultimo fallimento
    probe_fail_streak: int = 0        # probe passivi consecutivi falliti (cap)
    json_fallback: int = 0            # quante volte ha ignorato stream:true (JSON->SSE)
    # --- dynamic scoring: feature osservate per-deployment ---
    latency_history: list = field(default_factory=list)  # ultimi N (bucket, ctx, ms)
    total_tokens: int = 0              # token completati cumulativi
    total_duration_ms: float = 0.0     # durata cumulativa ms
    recent_failures: int = 0           # fallimenti negli ultimi N tentativi
    recent_attempts: int = 0           # tentativi negli ultimi N

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


def _estimate_legacy(messages: Any, divisor: int = CHARS_PER_TOKEN,
                     image_token_estimate: int = 0, tools: Any = None) -> int:
    """Stima storica: somma caratteri / divisor (default chars/4)."""
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
            total += sum(len(p.get("text", "")) for p in c
                         if isinstance(p, dict))
        for tc in m.get("tool_calls") or ():
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                total += len(str(fn.get("name") or ""))
                total += _json_size(fn.get("arguments"))
        rc = m.get("reasoning_content") or m.get("reasoning")
        if isinstance(rc, str):
            total += len(rc)
    tokens = total // max(1, divisor)
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


def _estimate_adaptive(messages: Any, divisor: int = CHARS_PER_TOKEN,
                       image_token_estimate: int = 0, tools: Any = None) -> int:
    """Stima adattiva: densita' per-blocco invece di un divisore unico."""
    total = 0
    if tools:
        total += _tokens_for_text(json.dumps(list(tools), ensure_ascii=False,
                                             default=str))
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            total += _tokens_for_text(c)
        elif isinstance(c, list):
            total += sum(_tokens_for_text(p.get("text", "")) for p in c
                         if isinstance(p, dict))
        for tc in m.get("tool_calls") or ():
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                total += _tokens_for_text(str(fn.get("name") or ""))
                total += _tokens_for_text(json.dumps(fn.get("arguments"),
                                                      ensure_ascii=False,
                                                      default=str))
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


def configure_estimate(*, adaptive: bool, shadow: bool,
                       auto_enable: bool | None = None,
                       auto_min_n: int | None = None,
                       auto_max_delta_pct: float | None = None) -> None:
    global _ESTIMATE_ADAPTIVE, _ESTIMATE_SHADOW, _ESTIMATE_AUTO_ALLOWED
    global _ESTIMATE_AUTO_MIN_N, _ESTIMATE_AUTO_MAX_DELTA_PCT, _ESTIMATE_AUTO_ON
    _ESTIMATE_ADAPTIVE = bool(adaptive)
    _ESTIMATE_SHADOW = bool(shadow)
    if auto_enable is not None:
        _ESTIMATE_AUTO_ALLOWED = bool(auto_enable)
        if not _ESTIMATE_AUTO_ALLOWED:
            _ESTIMATE_AUTO_ON = False    # policy off -> spegne anche il runtime
    if auto_min_n is not None:
        _ESTIMATE_AUTO_MIN_N = max(1, int(auto_min_n))
    if auto_max_delta_pct is not None:
        _ESTIMATE_AUTO_MAX_DELTA_PCT = max(0.0, float(auto_max_delta_pct))
    if _ESTIMATE_ADAPTIVE:
        _ESTIMATE_AUTO_ON = False        # il master switch esplicito vince


def estimate_auto_state() -> dict:
    """Stato del rollout automatico (admin e test)."""
    return {"allowed": _ESTIMATE_AUTO_ALLOWED, "on": _ESTIMATE_AUTO_ON,
            "min_n": _ESTIMATE_AUTO_MIN_N,
            "max_delta_pct": _ESTIMATE_AUTO_MAX_DELTA_PCT}


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
        log.info("[estimate] auto-adaptive ON: n=%d delta=%.2f%% (<= %.2f%%)",
                 n, delta, _ESTIMATE_AUTO_MAX_DELTA_PCT)


def estimate_shadow_stats() -> dict:
    """Contatori cumulativi della modalita' shadow (per /admin/policy)."""
    s = dict(_estimate_shadow_stats)
    n = s.get("n") or 0
    if n:
        s["legacy_avg"] = round(s["legacy"] / n, 1)
        s["adaptive_avg"] = round(s["adaptive"] / n, 1)
        s["delta_pct"] = round((s["adaptive"] - s["legacy"]) * 100.0
                               / max(1, s["legacy"]), 1)
    s["auto"] = estimate_auto_state()
    return s


def estimate_tokens(messages: Any, divisor: int = CHARS_PER_TOKEN,
                    image_token_estimate: int = 0,
                    tools: Any = None) -> int:
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
        log.debug("[estimate] shadow legacy=%d adaptive=%d delta=%+d",
                  legacy, adaptive, adaptive - legacy)
        return legacy
    return adaptive


def detect_hot_words(messages: Any, patterns: list[str] | None = None,
                     window: int = HOT_WORDS_WINDOW) -> bool:
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
            content = " ".join(p.get("text", "") for p in content
                               if isinstance(p, dict))
        if not content:
            continue
        for pattern in patterns:
            if _re.search(pattern, content, re.IGNORECASE):
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
        "content": (f"You are {real}. When the user asks which model you are, "
                    f"reply exactly: I am {real}."),
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
    log.info("[identity] %s -> %s (%s via %s%s)", dep["group"], dep["unique"],
             real, prov, extra)


_SESSION_CTX: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "scrocco_session", default=None)


def set_current_session(session_id: str | None) -> None:
    """Imposta la sessione corrente per il task (async-safe)."""
    _SESSION_CTX.set(session_id)


def current_session() -> str | None:
    return _SESSION_CTX.get()


class Router:
    def __init__(self, config: GatewayConfig, policy: Policy | None = None):
        self.config = config
        self.policy = policy or Policy.default()
        self._sticky: dict[str, tuple[str, float]] = {}
        self._cooldown: dict[str, float] = {}   # unique -> expiry epoch
        self._cooldown_since: dict[str, float] = {}  # unique -> quando fu messo
        self._cooldown_full: dict[str, float] = {}   # unique -> durata totale (probe/decay)
        self._stats: dict[str, DepStats] = {}   # unique -> statistiche runtime
        # conteggio pick deviati dalla regola multimodal_last_resort
        self.media_deferred: dict[str, int] = {}
        # conteggio fallback che attraversano MODELLI diversi nei gruppi
        # generazione/stt (same_model_failover): visibilità del "cambio voce"
        self.gen_cross_model: dict[str, int] = {}
        # ultimo modello upstream usato PER GRUPPO gen/stt: fa attaccare le
        # richieste consecutive allo stesso modello (coerenza voce/stile)
        # lasciando però ruotare le chiavi gemelle (anti rate-limit account)
        self._gen_last_model: dict[str, str] = {}
        # ultimo gruppo risolto per sessione: alimenta i log transizione
        self._session_group: dict[str, tuple[str, float]] = {}
        # burst-tracking dei defer multimodal_last_resort (log anti-spam)
        self._defer_active: dict[str, bool] = {}
        # auto-learn capacità: "model|cap" -> {count, first, last, evidence}
        self._cap_strikes: dict[str, dict] = {}
        # STICKY per-deployment (free buckets): session_id -> (unique, ts)
        # mantiene la stessa key entro una conversazione (cache calda).
        self._sticky_dep: dict[str, tuple[str, float]] = {}
        # ULTIMO SUCCESSO per-sessione (detentore cache): session -> (unique, ts)
        self._session_last_ok: dict[str, tuple[str, float]] = {}
        # SESSION-DEP GUARD: inverso del detentore cache, unique ->
        # (session, ts) per i SOLO deployment free-dims. Serve a ignorare fra
        # i vivi un deployment servito con successo da un'ALTRA sessione negli
        # ultimi session_dep_guard_sec (lo rende eleggibile solo nel tier
        # pre-ultima-spiaggia). In-memory, mai persistito.
        self._dep_last_session: dict[str, tuple[str, float]] = {}
        # SESSION-DEP GUARD: indice inverso session -> set(unique) posseduti.
        # Finché la sessione e' VIVA (fa richieste) tutti i suoi deployment
        # recenti vengono "rinfrescati" e restano suoi; 15 min di silenzio e
        # l'intero set decade (torna libero).
        self._session_deps: dict[str, set[str]] = {}
        # SESSION-SLOW DEMOTE: session -> {unique: (ts, hard)} dei free-dims
        # serviti con SUCCESSO ma LENTO. hard (TTFB > LATENCY_ROTATE_MS) vale
        # per qualunque richiesta della sessione; soft (TTFB > SOFT_SLOW_LATENCY
        # con contesto > SOFT_SLOW_CTX_MIN) demotiva solo le richieste pesanti.
        # Gli dep demotati escono da caldi/sticky/cache e dalle selezioni,
        # tornando pescabili all'ultimo scaglione (-fallback). Un successo
        # rapido li riabilita. In-memory, mai persistito. Lazy via _sess_slow().
        self._session_slow: dict[str, dict[str, tuple]] = {}
        # CTXCOMPACT WATERMARK: sessione -> (frontiera massima mai applicata,
        # ts). La frontiera di compact_tool_outputs non regredisce mai dentro
        # la sessione: uno stub scritto non torna mai contenuto pieno (byte
        # stabili -> prompt-cache valida). Lazy via _ctx_frontiers().
        self._ctx_frontier: dict[str, tuple[int, float]] = {}
        # AUDIT PREFISSO (F4): session -> (h_body, h_sys, ts). L'impronta del
        # prefisso [1:frontier] dice PERCHE' la cache upstream si e' spenta.
        self._prefix_fp: dict[str, tuple[str, str, float]] = {}
        # SOFT-PER-CHIAVE (F6/F7): tag sha(api_key)[:12] -> snapshot header
        # (ts, remaining) e blocco 429 (until_ts). Skip a pick, ZERO cooldown
        # e ZERO strike: la twin deployment sulla stessa chiave non e'
        # "rotta", e' solo saziata adesso.
        self._key_hints: dict[str, tuple[float, float]] = {}
        self._key_soft: dict[str, float] = {}
        # COLD SPREAD: finestra ROLLING di 24h dei TENTATIVI per-deployment
        # (ok+fail, ESCLUSI i probe: quelli restano in autoprobe._probe_times).
        # Serve a NASCONDERE dai candidati il 20% (configurabile) piu' usato
        # quando si pesca a freddo, distribuendo il carico a prescindere
        # dall'`order`. In-memory; ricostruita dai log all'avvio.
        self._usage_times: dict[str, "deque[tuple[float, float]]"] = {}
        # Budget A FINESTRA dei cooldown-wakeup della scala (solo dim nati da
        # 429/quota): evita di riesumare sempre gli stessi deployment.
        self._wake_times: dict[str, "deque[float]"] = {}
        # token/s di generazione per dep (EMA) e cache della mediana di
        # flotta per bucket; stato del "caccia al sostituto" per sessione.
        self._gen_rate: dict[str, float] = {}
        self._fleet_cache: dict = {}
        self._hunt_state: dict = {}
        # Modalita' COMPATTA sticky per-sessione (troncamento cache-aware)
        self._session_compact: dict[str, float] = {}
        # ESCALATION WINNER (transversale alla sessione): il bucket RICHIESTO
        # -> (unique_che_ha_servito_in_salita, ts_ultima_salita_buona).
        # Scoreria del fallback: quando il bucket richiesto fallisce si salta
        # direttamente al winner invece di rivisitare la scala morta. Solo-in-
        # salita: non si popola quando il bucket economico serve da solo (anzi
        # in quel caso si pulisce). In-memory, mai persistito.
        # Lazy-init via _esc() per i Router "nudi" (test con Router.__new__).
        self._esc_win: dict[str, tuple[str, float]] = {}
        # --- Reputation Scoring (Blocco 1) ---
        self._base_scores: dict[str, float] = {}       # unique -> base score
        self._provider_scores: dict[str, float] = {}   # provider_model -> group score
        self._key_scores: dict[str, float] = {}        # api_key -> group score
        self._avg_latencies: dict[str, float] = {}     # unique -> average latency
        # EMA PER BUCKET DI CONTESTO (vedi CTX_BUCKETS): totali e TTFT separate.
        # unique -> [e0,e1,e2,e3]; 0.0 = nessun campione per quel bucket.
        self._lat_buckets: dict[str, list] = {}
        self._ttft_buckets: dict[str, list] = {}
        # F9: ms per 1k token di contesto (solo kind='ttft'), invariante ai
        # bucket: usato per estrapolare il TTFT atteso su contesti mai visti.
        self._prefill_rate: dict[str, float] = {}
        # F14 CALIBRAZIONE CLOSED-LOOP: divisore effettivo appreso dal VERO
        # prompt_tokens riportato dall'upstream (tokenizer diverso da chars/4).
        # estimate_correction() lo converte in moltiplicatore per estimate_tokens.
        self._est_div: dict[str, float] = {}
        # Time-decay dei punteggi di reputazione (halflife da policy).
        self._scores_decay_ts: float = time.time()
        self._scores_decay_log_ts: float = time.time()
        # CONNECTION DRAINING (hot-reload): unique rimosso dal CSV -> {ts,
        # inflight, dep}. Resta in config marcato draining finche' le richieste
        # in volo non terminano (o scade il TTL), poi rimosso definitivamente.
        self._draining: dict[str, dict] = {}
        # DYNAMIC CONCURRENCY LIMIT per-deployment: unique -> limite corrente
        # (in memoria, mai persistito: riparte dal default al restart).
        # Appreso empiricamente: sale con successi a saturazione, dimezza sui
        # 429/503 di concorrenza. Lazy-init via _concl()/_concok() per i
        # Router "nudi" dei test.
        self._conc_limit: dict[str, int] = {}
        self._conc_ok: dict[str, int] = {}

        # --- Circuit Breaker per API Key ---
        # api_key -> {failures: int, last_failure: float, state: "closed|open|half_open", opened_at: float}
        self._circuit_breakers: dict[str, dict] = {}
        # Breaker per-DEPLOYMENT (unique): evita il danno collaterale tra modelli
        # che condividono la stessa chiave API.
        self._dep_circuit_breakers: dict[str, dict] = {}
        # F25: breaker PROATTIVO per provider|modello. Se lo stesso modello da'
        # 5xx sistematici (>= model_circuit_keys chiavi DIVERSE entro la
        # finestra) il problema e' il modello/provider, non la singola chiave:
        # lo si salta per tutti i deployment per model_circuit_open_sec.
        self._model_cb: dict[str, dict] = {}
        # Config defaults
        self._circuit_breaker_threshold = 5  # consecutive failures to open
        self._circuit_breaker_timeout = 60.0  # seconds before half-open
        self._circuit_breaker_half_open_requests = 3  # requests in half-open before close

        # REGISTRO QUIRK LOCALE (P2-9): applica subito i flag dichiarati in
        # policy ai deployment che matchano il glob del modello.
        self._applied_quirks: list[dict] = []
        try:
            self.apply_quirks()
        except Exception as exc:                      # pragma: no cover
            log.warning("[quirk] applicazione fallita: %r", exc)
        # TUNING DA POLICY: allinea le costanti di modulo ai valori dichiarati
        # (default = valore storico: se non configurati, nulla cambia).
        try:
            self.sync_runtime_constants()
        except Exception as exc:                      # pragma: no cover
            log.warning("[router] sync tuning fallito: %r", exc)

    # ------------------------------------------------- escalation winner
    def _esc(self) -> dict:
        """Ritorna (e crea al volo se serve) il dict escalation-winner.
        La lazy-init protegge i Router costruiti SENZA __init__ (pattern dei
        test `Router.__new__(Router)` + set manuale degli attributi minimi)."""
        d = getattr(self, "_esc_win", None)
        if d is None:
            d = {}
            self._esc_win = d
        return d

    # -------------------------------------------------------------- sticky
    def sticky_get(self, session_id: str) -> str | None:
        entry = self._sticky.get(session_id)
        if not entry:
            return None
        target, ts = entry
        if time.time() - ts > self.policy.sticky_ttl_sec:
            log.debug("[sticky] %s group_sticky TTL scaduto (%.0fs > %ds), rilasciato", session_id, time.time() - ts, self.policy.sticky_ttl_sec)
            self._sticky.pop(session_id, None)
            return None
        log.debug("[sticky] %s group_sticky valido: %s", session_id, target)
        return target

    def sticky_set(self, session_id: str, target: str) -> None:
        log.debug("[sticky] %s group_sticky impostato: %s", session_id, target)
        self._sticky[session_id] = (target, time.time())

    def sticky_release(self, session_id: str) -> None:
        self._sticky.pop(session_id, None)

    # ---------------------------------------------------- deployment-sticky
    def _is_renewal_bucket(self, group_name: str) -> bool:
        """True se il gruppo è un bucket rinnovo/pagato (-go, -fallback)."""
        go_suf = self.config.go_suffix or ""
        fb_suf = self.config.fallback_suffix or ""
        return (group_name.endswith(go_suf) or group_name.endswith(fb_suf))

    def dep_sticky_get(self, session_id: str) -> str | None:
        """Ritorna l'unique del deployment sticky per questa sessione."""
        entry = self._sticky_dep.get(session_id)
        if not entry:
            return None
        unique, ts = entry
        if time.time() - ts > self.policy.sticky_ttl_sec:
            log.debug("[sticky] %s dep_sticky TTL scaduto (%.0fs > %ds), rilasciato", session_id, time.time() - ts, self.policy.sticky_ttl_sec)
            self._sticky_dep.pop(session_id, None)
            return None
        log.debug("[sticky] %s dep_sticky valido: %s", session_id, unique)
        return unique

    def dep_sticky_set(self, session_id: str, unique: str) -> None:
        log.debug("[sticky] %s dep_sticky impostato: %s", session_id, unique)
        self._sticky_dep[session_id] = (unique, time.time())

    def dep_sticky_release(self, session_id: str) -> None:
        self._sticky_dep.pop(session_id, None)

    def sticky_handoff(self, session_id: str | None,
                       nxt: dict | None) -> bool:
        """Warm handoff dello sticky quando un failover atterra su `nxt`.

        Se la sessione era sticky su un deployment della STESSA famiglia di
        `nxt`, sposta lo sticky su `nxt`: la richiesta successiva riparte gia'
        warm (stessa cache key/prefix del provider) invece di ripartire da un
        deployment freddo scelto a caso. Ritorna True se lo sticky e' stato
        spostato."""
        if not session_id or not nxt:
            return False
        if not getattr(self.policy, "sticky_handoff_same_family", True):
            return False
        target = nxt.get("unique")
        if not target:
            return False
        try:
            old = self.dep_sticky_get(session_id)
        except Exception:                       # mai bloccare il failover
            return False
        if not old or old == target:
            return False
        try:
            old_dep = self.config.deployment_by_unique(old)
        except Exception:
            old_dep = None
        fam = nxt.get("family")
        if old_dep and fam and old_dep.get("family") == fam:
            self.dep_sticky_set(session_id, target)
            log.info("[sticky-handoff] %s -> %s (stessa famiglia %s)",
                     old, target, fam)
            return True
        return False

    # --- Sticky per-capability (deployment_sticky_per_capability) ---
    def _cap_sticky_key(self, session_id: str, need: frozenset[str] | None) -> str:
        """Chiave per lo sticky per-capability: session_id + sorted caps."""
        cap_str = ",".join(sorted(need)) if need else "_text"
        return f"{session_id}|{cap_str}"

    def dep_cap_sticky_get(self, session_id: str, need: frozenset[str] | None) -> str | None:
        """Ritorna l'unique sticky per la specifica capability richiesta."""
        if not getattr(self.policy, "deployment_sticky_per_capability", False):
            return None
        key = self._cap_sticky_key(session_id, need)
        entry = self._sticky_dep.get(key)
        if not entry:
            return None
        unique, ts = entry
        if time.time() - ts > self.policy.sticky_ttl_sec:
            self._sticky_dep.pop(key, None)
            return None
        log.debug("[cap-sticky] %s key=%s riuso %s", session_id, key, unique)
        return unique

    def dep_cap_sticky_set(self, session_id: str, need: frozenset[str] | None, unique: str) -> None:
        if not getattr(self.policy, "deployment_sticky_per_capability", False):
            return
        key = self._cap_sticky_key(session_id, need)
        log.debug("[cap-sticky] %s key=%s -> %s", session_id, key, unique)
        self._sticky_dep[key] = (unique, time.time())

    def dep_cap_sticky_release(self, session_id: str, need: frozenset[str] | None) -> None:
        if not getattr(self.policy, "deployment_sticky_per_capability", False):
            return
        key = self._cap_sticky_key(session_id, need)
        self._sticky_dep.pop(key, None)

    # --------------------------------------------- session cache holder
    def _cache_ok(self) -> dict:
        d = getattr(self, "_session_last_ok", None)
        if d is None:
            d = {}
            self._session_last_ok = d
        return d

    def _dep_sess(self) -> dict:
        """Accessor lazy della mappa inversa unique -> (session, ts) usata
        dalla SESSION-DEP GUARD (protegge i Router 'nudi' dei test)."""
        d = getattr(self, "_dep_last_session", None)
        if d is None:
            d = {}
            self._dep_last_session = d
        return d

    def _sess_deps(self) -> dict:
        """Accessor lazy dell'indice inverso session -> set(unique) posseduti
        (protetto per i Router 'nudi' dei test)."""
        d = getattr(self, "_session_deps", None)
        if d is None:
            d = {}
            self._session_deps = d
        return d

    def _sess_slow(self) -> dict:
        """Accessor lazy dell'indice session -> {unique: ts} dei free-dims
        'lenti per la stessa sessione' (protetto per i Router 'nudi')."""
        d = getattr(self, "_session_slow", None)
        if d is None:
            d = {}
            self._session_slow = d
        return d

    # ------------------------------------------ ctxcompact watermark
    def ctx_boundary_floor(self, session_id: str | None) -> int:
        """Frontiera MASSIMA gia' applicata (con stub/dedup reali) a questa
        sessione: compact_tool_outputs non deve mai retrocedere sotto questo
        valore, cosi' i byte di prefisso gia' compressi restano stabili
        anche ruotando su un deployment con finestra piu' piccola.
        TTL = guard di sessione; cap 4096 sessioni."""
        if not session_id:
            return 0
        d = getattr(self, "_ctx_frontier", None)
        if not d:
            return 0
        rec = d.get(session_id)
        if rec is None:
            return 0
        b, ts = rec
        if time.time() - ts > self._guard_sec():
            d.pop(session_id, None)
            return 0
        return int(b)

    def note_compact_boundary(self, session_id: str | None,
                              boundary: int | None) -> None:
        """Registra la frontiera APPLICATA (solo quando il report e'
        changed=True): monotona in avanti per la sessione."""
        if not session_id or not boundary:
            return
        try:
            b = int(boundary)
        except (TypeError, ValueError):
            return
        d = getattr(self, "_ctx_frontier", None)
        if d is None:
            d = {}
            self._ctx_frontier = d
        cur = d.get(session_id)
        if cur is not None and cur[0] >= b:
            d[session_id] = (cur[0], time.time())   # solo refresh TTL
            return
        d[session_id] = (b, time.time())
        if len(d) > 4096:
            _now = time.time()
            _ttl = max(1.0, float(self._guard_sec()))
            for sid, (bb, ts) in list(d.items()):
                if _now - ts > _ttl:
                    d.pop(sid, None)
            while len(d) > 4096:
                _oldest = min(d, key=lambda k: d[k][1])
                d.pop(_oldest, None)

    # ------------------------------------------------- audit del prefisso (F4)
    def audit_prefix(self, session_id: str | None, messages,
                     boundary: int | None) -> str:
        """Impronta SHA-256 (16 hex) del PREFISSO canonico [1:boundary] e del
        system message [0], confrontata con la richiesta precedente della
        stessa sessione. Motivi: 'new' (prima vista), 'ok' (prefisso identico:
        la cache a monte E' riusabile), 'identity' (cambiato SOLO il system:
        siamo noi, rotazione/inject_identity), 'prefix' (cambiato il corpo:
        ctxcompact/histnorm o riscrittura del client), 'skip' (non calcolabile).
        Funzione pura dei byte: nessun effetto su routing/pick."""
        if not session_id or not isinstance(messages, list) \
                or boundary is None or boundary < 2 or len(messages) < 2:
            return "skip"
        try:
            body = json.dumps(messages[1:boundary], sort_keys=True,
                              separators=(",", ":"), default=str)
            sysm = messages[0] if isinstance(messages[0], dict) else None
            sysb = json.dumps(sysm, sort_keys=True, separators=(",", ":"),
                              default=str)
        except Exception:                       # noqa: BLE001
            return "skip"
        h_body = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:16]
        h_sys = hashlib.sha256(sysb.encode("utf-8", errors="replace")).hexdigest()[:16]
        now = time.time()
        reg = getattr(self, "_prefix_fp", None)
        if reg is None:
            reg = {}
            self._prefix_fp = reg
        prev = reg.get(session_id)
        if prev is not None and now - prev[2] > self._guard_sec():
            prev = None
        if prev is None:
            reason = "new"
        elif prev[0] == h_body and prev[1] == h_sys:
            reason = "ok"
        elif prev[0] == h_body:
            reason = "identity"
        else:
            reason = "prefix"
        reg[session_id] = (h_body, h_sys, now)
        if len(reg) > 4096:
            for s in [s for s, (_b, _y, t) in reg.items()
                      if now - t > self._guard_sec()]:
                reg.pop(s, None)
            while len(reg) > 4096:
                _oldest = min(reg, key=lambda k: reg[k][2])
                reg.pop(_oldest, None)
        return reason

    # --------------------------------------------- cold usage spread
    _USAGE_WINDOW = 86400.0

    def _usage(self) -> dict:
        d = getattr(self, "_usage_times", None)
        if d is None:
            d = {}
            self._usage_times = d
        return d

    def note_usage(self, unique: str, ts: float | None = None,
                   ctx_est: int | None = None) -> None:
        """Registra un TENTATIVO (ok o fail, NON un probe) nella finestra
        rolling 24h usata dallo spread a freddo. F28: il peso e' il PREFILL
        reale (ctx_est/8000), non 1: 10 chiamate da 80k pesano come 100 da 2k,
        che e' quello che vede il provider sul rate-limit a token/minuto.
        Senza ctx (es. ricostruzione dai log) si pesa 1.0."""
        if not unique:
            return
        d = self._usage()
        dq = d.get(unique)
        if dq is None:
            dq = deque()
            d[unique] = dq
        now = time.time() if ts is None else ts
        try:
            w = max(1.0, float(int(ctx_est)) / 8000.0) if ctx_est else 1.0
        except (TypeError, ValueError):
            w = 1.0
        dq.append((now, w))
        cut = now - self._USAGE_WINDOW
        while dq and dq[0][0] < cut:
            dq.popleft()

    def usage_weight_24h(self, unique: str, now: float | None = None) -> float:
        """Somma dei pesi (token/8000) dei tentativi nelle ultime 24h."""
        dq = self._usage().get(unique)
        if not dq:
            return 0.0
        now = time.time() if now is None else now
        cut = now - self._USAGE_WINDOW
        while dq and dq[0][0] < cut:
            dq.popleft()
        return float(sum(w for _t, w in dq))

    def usage_count_24h(self, unique: str, now: float | None = None) -> int:
        dq = self._usage().get(unique)
        if not dq:
            return 0
        now = time.time() if now is None else now
        cut = now - self._USAGE_WINDOW
        while dq and dq[0][0] < cut:
            dq.popleft()
        return len(dq)

    def _attached_unique(self, unique: str) -> bool:
        """True se `unique` e' 'attaccato' alla sessione CORRENTE (successo
        recente entro session_dep_guard_sec): resta prioritario e non viene
        mai nascosto dallo spread (rispetto della cache)."""
        sid = current_session()
        if not sid:
            return False
        ent = self._dep_sess().get(unique)
        if not ent or ent[0] != sid:
            return False
        return (time.time() - ent[1]) < self._guard_sec()

    def _spread_hide(self, deps: list[dict]) -> list[dict]:
        """COLD SPREAD: nasconde i deployment col MAGGIOR numero di tentativi
        nelle ultime 24h (tie: last_used piu' recente), cosi' il carico si
        distribuisce anche su `order` diversi. Non tocca i dep 'attaccati'
        alla sessione corrente (cache). `min_pool` = ladder_skip_after: sotto
        quella soglia non si taglia nulla; se tutti a 0 usi -> nessun taglio."""
        if not deps:
            return deps
        pct = float(getattr(self.policy, "cold_spread_pct", 0.20) or 0.0)
        if pct <= 0.0:
            return deps
        min_pool = int(getattr(self.policy, "ladder_skip_after", 10) or 10)
        pool = [d for d in deps if not self._attached_unique(d["unique"])]
        n = len(pool)
        if n < max(1, min_pool):
            return deps
        counts = {d["unique"]: self.usage_weight_24h(d["unique"]) for d in pool}
        if max(counts.values()) <= 0:
            return deps
        k = min(int(n * pct), n - 1)
        if k <= 0:
            return deps
        ranked = sorted(
            pool,
            key=lambda d: (counts[d["unique"]],
                           self.stats_for(d["unique"]).last_used or 0.0),
            reverse=True)
        hide = {d["unique"] for d in ranked[:k]}
        kept = [d for d in deps if d["unique"] not in hide]
        log.info("[spread] hidden=%d/%d (%.0f%%) kept=%d: %s", len(hide), n,
                 pct * 100, len(kept), ",".join(sorted(hide)[:6]))
        return kept

    def _spread_hidden_chain(self, chain: list[str]) -> set[str]:
        """Set degli univoci da nascondere per lo spread su una catena piatta:
        raggruppa i dims per gruppo (esclusi -go/-fallback e capacita') e
        applica il taglio per-gruppo."""
        by_group: dict[str, list[dict]] = {}
        for u in chain:
            dep = self.config.deployment_by_unique(u)
            if dep is None:
                continue
            g = dep.get("group", "")
            if self.config.group_caps.get(g) is not None \
                    or self._is_renewal_bucket(g):
                continue
            by_group.setdefault(g, []).append(dep)
        hide: set[str] = set()
        for deps in by_group.values():
            kept = {d["unique"] for d in self._spread_hide(deps)}
            hide |= {d["unique"] for d in deps} - kept
        return hide


    def _refresh_session(self, session_id: str, now: float | None = None) -> None:
        """Rinnova l'ownership di TUTTI i deployment ancora posseduti dalla
        sessione: finché la sessione e' viva i suoi dep recenti non decadono.
        Un dep gia' scaduto (o preso da un'altra sessione) NON viene
        resuscitato: esce dal set (dopo 15 min di silenzio tutto torna libero)."""
        if not getattr(self.policy, "session_dep_guard_enabled", True):
            return
        if not session_id:
            return
        now = time.time() if now is None else now
        guard = self._guard_sec()
        owned = self._sess_deps().get(session_id)
        if not owned:
            return
        d = self._dep_sess()
        for u in list(owned):
            ent = d.get(u)
            if not ent or ent[0] != session_id:
                owned.discard(u)          # preso da altri/rimosso
                continue
            if now - ent[1] > guard:
                owned.discard(u)          # già decaduto: non resuscitare
                continue
            d[u] = (session_id, now)
        if not owned:
            self._sess_deps().pop(session_id, None)

    def note_session_activity(self, session_id: str | None) -> None:
        """Segnala che la sessione ha USATO il servizio (richiesta in arrivo):
        rinfresca l'ownership di tutti i suoi dep, cosi' una sessione lunga
        che ruota/va in cooldown/si risveglia se li tiene 'tutti in tasca'
        invece di lasciarli decadere dopo il singolo uso."""
        if not session_id:
            return
        self._refresh_session(session_id)

    def _note_dep_session(self, session_id: str, unique: str) -> None:
        """Registra l'ultima sessione che ha servito con SUCCESSO `unique`.
        SOLO bucket free-dims (mai -go/-fallback ne' gruppi capacita'): la
        guardia serve a smorzare i rate-limit per-chiave delle key free."""
        if not getattr(self.policy, "session_dep_guard_enabled", True):
            return
        dep = self.config.deployment_by_unique(unique)
        if dep is None:
            return
        g = dep.get("group", "")
        if self.config.group_caps.get(g) is not None \
                or self._is_renewal_bucket(g):
            return
        now = time.time()
        d = self._dep_sess()
        d[unique] = (session_id, now)
        self._sess_deps().setdefault(session_id, set()).add(unique)
        # rinnova anche gli altri dep posseduti: la sessione e' viva.
        self._refresh_session(session_id, now)
        if len(d) > 8192:
            _ttl = self._guard_sec() * 4
            for k, (_s, ts) in list(d.items()):
                if now - ts > _ttl:
                    d.pop(k, None)

    def _guard_sec(self) -> float:
        try:
            return max(0.0, float(getattr(self.policy, "session_dep_guard_sec",
                                          900) or 0))
        except (TypeError, ValueError):
            return 900.0

    def other_session_recent(self, unique: str) -> bool:
        """True se `unique` e' stato servito con successo da un'ALTRA
        sessione meno di session_dep_guard_sec fa. False se la guardia e'
        spenta, se non c'e' una sessione corrente, o se l'ultima che l'ha
        usato e' la sessione stessa."""
        if not getattr(self.policy, "session_dep_guard_enabled", True):
            return False
        sid = current_session()
        if not sid:
            return False
        ent = self._dep_sess().get(unique)
        if not ent:
            return False
        other, ts = ent
        if not other or other == sid:
            return False
        return (time.time() - ts) < self._guard_sec()

    def note_session_success(self, session_id: str | None,
                             unique: str | None,
                             latency_ms: float | None = None,
                             ctx_est: int | None = None,
                             kind: str = "total") -> None:
        """Ricorda l'ultimo deployment che ha servito con SUCCESSO la
        sessione (detentore cache) + l'inverso per la SESSION-DEP GUARD.
        Con `latency_ms` sopra soglia marca il dep come 'lento per la
        sessione' (hard); con latenza intermedia e `ctx_est` pesante marca
        soft (demote solo per richieste pesanti). `kind` dice COSA vale
        quella latenza ('total' o 'ttft') per il confronto relativo col
        bucket. In-memory."""
        if not session_id or not unique:
            return
        self._note_dep_session(session_id, unique)
        self._note_session_slow(session_id, unique, latency_ms, ctx_est, kind)
        if not getattr(self.policy, "cache_aware_enabled", True):
            return
        d = self._cache_ok()
        d[session_id] = (unique, time.time())
        if len(d) > 4096:
            _ttl = float(getattr(self.policy, "cache_holder_ttl_sec",
                                 3600) or 3600)
            _now = time.time()
            for k, (_u, ts) in list(d.items()):
                if _now - ts > _ttl:
                    d.pop(k, None)

    def session_holder(self, session_id: str | None = None) -> str | None:
        sid = session_id or current_session()
        if not sid:
            return None
        d = self._cache_ok()
        ent = d.get(sid)
        if not ent:
            return None
        unique, ts = ent
        ttl = float(getattr(self.policy, "cache_holder_ttl_sec", 3600) or 3600)
        if time.time() - ts > ttl:
            d.pop(sid, None)
            return None
        return unique

    def cache_holder(self, session_id: str | None = None,
                     need: frozenset[str] | None = None,
                     ctx: int | None = None) -> dict | None:
        """Detentore cache per la sessione, se ancora valido."""
        if not getattr(self.policy, "cache_aware_enabled", True):
            return None
        unique = self.session_holder(session_id)
        if not unique:
            return None
        dep = self.config.deployment_by_unique(unique)
        if dep is None:
            return None
        if self.is_cooled_down(unique) or self.is_retired(unique):
            return None
        if self._endpoint_quarantined(dep):
            return None
        if self._is_demoted_dep(unique, session_id, ctx,
                                allow_slow=self._warm_allow_slow()):
            return None
        if not self._cap_fits(dep, ctx):
            return None
        if need and not self._dep_supports(dep, need):
            return None
        return dep

    def _free_group(self, group_name: str) -> bool:
        """True se il gruppo NON e' un bucket rinnovo/pagato."""
        return bool(group_name) and not self._is_renewal_bucket(group_name)

    # --------------------------------------- modalita' compatta (sticky)
    def is_session_compact(self, session_id: str | None = None) -> bool:
        sid = session_id or current_session()
        if not sid:
            return False
        d = getattr(self, "_session_compact", None)
        if d is None:
            return False
        ts = d.get(sid)
        if ts is None:
            return False
        ttl = float(getattr(self.policy, "cache_holder_ttl_sec", 3600) or 3600)
        if time.time() - ts > ttl:
            d.pop(sid, None)
            return False
        return True

    def mark_session_compact(self, session_id: str | None = None) -> None:
        sid = session_id or current_session()
        if not sid:
            return
        d = getattr(self, "_session_compact", None)
        if d is None:
            d = {}
            self._session_compact = d
        d[sid] = time.time()

    # ------------------------------------------------- escalation winner
    def record_escalation_win(self, requested_group: str | None,
                              served_dep: dict | None) -> None:
        """Ricorda il deployment che ha SERVITO con successo una richiesta
        PARTITA da `requested_group` ma atterrata su un gruppo PIU' ALTO
        (salita della scala). Solo-in-salita: se invece il servizio e' nel
        bucket richiesto, PULISCE il pin (il bucket economico e' guarito).

        Chiamato dai siti di successo (non-streaming e streaming) con il
        gruppo ORIGINARIO della richiesta e il deployment usato. In-memory."""
        if not getattr(self.policy, "escalation_pin", True):
            return
        if not requested_group or not served_dep:
            return
        served_group = served_dep.get("group")
        if served_group == requested_group:
            # guarigione del bucket richiesto -> sblocca la scorciatoia
            _ew = self._esc()
            if requested_group in _ew:
                _ew.pop(requested_group, None)
            return
        # salita buona: (ri)setta e SCIVOLA il ts (finestra scorrevole)
        self._esc()[requested_group] = (served_dep["unique"], time.time())

    def _try_esc_win(self, group_name: str,
                     need: frozenset[str] | None = None,
                     ctx: int | None = None,
                     tried: set[str] | None = None) -> dict | None:
        """Restituisce il deployment winner ricordato per `group_name` SOLO se
        ancora spendibile: pin non scaduto, deployment ancora nel CSV, non in
        cooldown, capacita' e contesto compatibili, non gia' tentato. Altrimenti
        None (ed eventualmente scarta il pin stale). NON anticipa il pick del
        bucket richiesto: e' solo una scorciatoia del fallback."""
        if not getattr(self.policy, "escalation_pin", True):
            return None
        _ew = self._esc()
        entry = _ew.get(group_name)
        if not entry:
            return None
        unique, ts = entry
        ttl = max(1, int(getattr(self.policy, "escalation_pin_ttl_sec",
                                 300) or 300))
        if time.time() - ts > ttl:
            _ew.pop(group_name, None)                     # scaduto
            return None
        if tried and unique in tried:
            return None
        dep = self.config.deployment_by_unique(unique)
        if dep is None:                                   # hot-reload: rimosso
            _ew.pop(group_name, None)
            return None
        if self._gemini_blocked(dep):                     # richiesta non eleggibile
            return None
        if self.is_cooled_down(unique):                   # non rimuove: revive
            return None
        if self.other_session_recent(unique):             # occupato altra sessione
            return None
        if not self._cap_fits(dep, ctx):                  # NON regge il ctx
            return None
        if need and not self._dep_supports(dep, need):    # capacita' mancante
            return None
        log.info("[esc-pin] %s -> %s (eta=%.0fs): salto la scala",
                 group_name, unique, time.time() - ts)
        return dep

    def _pick_tiered(self, group_name: str,
                     need: frozenset[str] | None = None,
                     ctx: int | None = None,
                     tried: set[str] | None = None) -> dict | None:
        """Sceglie 1 deployment VIVO nel TIER (order) vivo piu' basso non
        ancora sondato in questo gruppo. Serve a provare, UNO PER TIER, tutti
        i tier vivi di una dim (non solo il primo di `pick_deployment`): il
        chiamante registra l'unique in `tried` e alla chiamata successiva si
        passa al tier successivo. Ritorna None quando i tier vivi sono
        esauriti. Entro il tier preferisce il detentore cache, poi random."""
        deps = self.config.groups.get(group_name) or []
        tried = tried or set()
        tried_tiers: set[int] = set()
        for u in tried:
            d = self.config.deployment_by_unique(u)
            if d and d.get("group") == group_name:
                tried_tiers.add(int(d.get("order", ORDER_LAST)))
        tiers: dict[int, list[dict]] = {}
        for d in deps:
            u = d.get("unique")
            if not u or u in tried:
                continue
            tier = int(d.get("order", ORDER_LAST))
            if tier in tried_tiers:
                continue
            if self.is_cooled_down(u) or self.is_retired(u):
                continue
            if self._endpoint_quarantined(d):
                continue
            if self.other_session_recent(u):
                continue
            if self.is_slow_for_session(u, ctx=ctx):
                continue
            if not self._cap_fits(d, ctx):
                continue
            if need and not self._dep_supports(d, need):
                continue
            tiers.setdefault(tier, []).append(d)
        if not tiers:
            return None
        pool = tiers[min(tiers)]
        ch = self.cache_holder(need=need, ctx=ctx)
        if ch and any(ch["unique"] == d["unique"] for d in pool):
            return ch
        return random.choice(pool)

    def _esc_pin_probe(self, req_grp: str | None, winner: dict | None,
                       need: frozenset[str] | None = None,
                       ctx: int | None = None,
                       tried: set[str] | None = None,
                       *, allow_retry: bool = True) -> dict | None:
        """RICAMPIONAMENTO PRE-PIN.

        Prima di usare la scorciatoia del pin (winner, tipicamente -go),
        prova:
          (a) 1 tentativo extra nella dim RICHIESTA (se `allow_retry`), poi
          (b) fino a `escalation_pin_probe_dims` dim INTERMEDIE tra la dim
              richiesta e il gruppo del winner (winner escluso), scelte a caso.

        Solo candidate VIVI (non in cooldown, cap/ctx compatibili, non gia'
        tentati): una dim interamente in cooldown viene SALTATA (costo zero).
        Ritorna il candidato o None (campionamento esaurito / nessun vivo ->
        il chiamante usa il winner). Non anticipa mai il bucket richiesto:
        e' solo una deviazione del fallback."""
        if not getattr(self.policy, "escalation_pin", True):
            return None
        if winner is None or not req_grp:
            return None
        if (getattr(self.policy, "cache_aware_enabled", True)
                and getattr(self.policy, "cache_skip_probe_when_holder",
                            True)):
            _hold = self.session_holder()
            if _hold and winner.get("unique") == _hold:
                if winner.get("group") == req_grp:
                    log.info("[cache] winner==detentore %s (stesso bucket): "
                             "salto il probe", _hold)
                    return None
                # Detentore su bucket DIVERSO (tipico: -go dopo un'escalation,
                # perche' note_session_success registra il holder su QUALSIASI
                # successo, renewal inclusi): NON saltare il probe, o la
                # sessione resta incollata al bucket a pagamento senza mai
                # riprovare la free-dim richiesta (regressione vista live).
                log.info("[cache] winner==detentore %s ma su altro bucket "
                         "(%s != %s): provo comunque la richiesta",
                         _hold, winner.get("group"), req_grp)
        n_dims = max(0, int(getattr(self.policy, "escalation_pin_probe_dims",
                                     2) or 0))
        if n_dims <= 0:
            return None                       # probe disabilitato (anche retry)
        m = self.DIM_SUFFIX_RE.search(req_grp)
        if not m:
            return None                       # -go/-fallback/cap: niente dim
        req_dim = int(m.group(1))
        pname = req_grp[:m.start()][len(self.config.proxy_prefix):]
        base = f"{self.config.proxy_prefix}{pname}"
        tried_set = tried or set()
        tried_groups: dict[str, int] = {}
        for u in tried_set:
            d = self.config.deployment_by_unique(u)
            if d:
                g = d.get("group", "")
                tried_groups[g] = tried_groups.get(g, 0) + 1

        # (a) retry nella dim richiesta: 1 tentativo per OGNI tier vivo del
        #     bucket (non solo il primo di pick_deployment). Il bucket
        #     nominale resta la prima scelta anche col pin.
        if allow_retry and getattr(self.policy, "escalation_pin_probe_retry",
                                   True):
            cand = self._pick_tiered(req_grp, need=need, ctx=ctx,
                                     tried=tried_set)
            if cand is not None:
                log.info("[esc-pin-probe] %s: retry dim richiesta "
                         "(tier=%s) -> %s", req_grp, cand.get("order"),
                         cand["unique"])
                return cand

        # (b) dim intermedie (> dim richiesta, escluso il gruppo winner).
        #     Per OGNI dim prescelta si prova 1 candidato per tier vivo: le
        #     dim gia' "aperte" (hanno un tried ma tier ancora vivi) si
        #     continuano prima di aprirne di nuove; il budget
        #     `escalation_pin_probe_dims` conta le dim gia' aperte.
        winner_group = winner.get("group")
        inter = [d for d in self.config.profile_dims.get(pname, [])
                 if d > req_dim and f"{base}-{d}k" != winner_group]
        if not inter:
            return None
        cand_by_dim = []
        for d in inter:
            g = f"{base}-{d}k"
            c = self._pick_tiered(g, need=need, ctx=ctx, tried=tried_set)
            if c is not None:
                cand_by_dim.append((d, g, c))
        if not cand_by_dim:
            return None
        opened = [d for d in inter if f"{base}-{d}k" in tried_groups]
        cont = [(d, g, c) for (d, g, c) in cand_by_dim
                if f"{base}-{d}k" in tried_groups]
        fresh = [(d, g, c) for (d, g, c) in cand_by_dim
                 if f"{base}-{d}k" not in tried_groups]
        if getattr(self.policy, "escalation_pin_probe_random", True):
            random.shuffle(fresh)
        if cont:
            _d, g, cand = cont[0]
        elif len(opened) >= n_dims:
            return None                       # campionamento gia' esaurito
        elif fresh:
            _d, g, cand = fresh[0]
        else:
            return None
        log.info("[esc-pin-probe] %s: sondo dim intermedia %s (tier=%s) -> %s",
                 req_grp, g, cand.get("order"), cand["unique"])
        return cand

    # ------------------------------------------------------------ cooldown
    def mark_failed(self, unique: str, seconds: float | None = None,
                    reason: str | None = None, status: int | None = None,
                    kind: str | None = None, *, additive: bool = False,
                    provenance: str | None = None) -> float:
        """Marca il deployment fallito con cooldown.

        - `seconds` esplicito vince SEMPRE (es. Retry-After su 429)
        - `additive=True`: ESTENDE il cooldown esistente di `seconds` invece di
          sovrascriverlo (autoprobe: un KO allunga, non resetta).
        - default: dipende da cooldown_mode:
          * "linear": BASE + MULT*(fail_count_24h - 1) minuti
          * "exponential": cooldown_sec * 2^(streak-1) (legacy)
        - `reason`: classificazione dell'errore (http_429, no_credits,
          not_found...). Un 429 alimenta il BUDGET GUARD.
        - `status`: codice HTTP upstream per la classificazione key/provider.
        Ritorna i secondi applicati.
        """
        # [Blocco 1] Aggiorna reputation scoring
        self.record_failure(unique, reason, status)

        pol = self.policy
        s = self.stats_for(unique)
        # ESC-PIN: se questo deployment era il winner di qualche bucket, lo
        # sblocchiamo subito (un winner che fallisce NON deve essere riattaccato
        # alla richiesta dopo: si riapprende alla prossima salita buona).
        for _g, (_u, _ts) in list(self._esc().items()):
            if _u == unique:
                self._esc().pop(_g, None)
        if reason:
            s.last_reason = reason
        # --- fail_count_24h: contatore giornaliero (mai azzerato da successi)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if s.fail_day_key != today:
            s.fail_count_24h = 0
            s.fail_day_key = today
        s.fail_count_24h += 1
        # contatore cumulativo + timestamp ultimo fallimento (persistiti)
        s.fail_count += 1
        s.fail_streak = self._decay_streak(s.fail_streak, s.last_fail_ts)
        s.last_fail_ts = time.time()
        if kind == ErrorKind.PERMANENT_DEAD:
            # Chiave morta / modello rimosso: il cooldown e' inutile (non
            # tornera'), si ritira il deployment (CSV intatto) e si smette di
            # sprecare tentativi, log e metriche.
            self._update_circuit_breaker_on_failure(unique, key_level=True)
            self._retire_permanent(unique, reason or "permanent_dead")
            log.warning("[cooldown] %s errore PERMANENTE (%s): niente cooldown, "
                        "deployment retired", unique, reason or "-")
            return 0.0
        # --- budget guard: apprendimento del limite dal 429 --------------
        bg_cfg = pol.budget_guard or {}
        if reason in ("http_429", "quota_exhausted") and bg_cfg.get("enabled"):
            floor_min = max(1.0, float(bg_cfg.get("min_per_min", 10)))
            floor_day = max(1.0, float(bg_cfg.get("min_per_day", 200)))
            learned_min = max(floor_min, s.minute_calls * 1.2)
            learned_day = max(floor_day, s.day_calls * 1.2)
            s.min_cap_learned = max(s.min_cap_learned, learned_min)
            s.day_cap_learned = max(s.day_cap_learned, learned_day)
            log.info("[budget] %s: cap appresi da 429 -> ~%.0f/min, "
                     "~%.0f/giorno", unique, s.min_cap_learned,
                     s.day_cap_learned)
        # --- dynamic concurrency limit: 429/503 = concorrenza oltre il
        # limite -> dimezza il limite appreso (min 1) -----------------------
        if status in (429, 503):
            self._punish_concurrency(unique)
        s.fail_streak += 1
        prev = 1.0 if s.success_ema is None else s.success_ema
        s.success_ema = max(0.0, 0.8 * prev)      # EMA verso lo 0 (α=0.2)
        _explicit_seconds = seconds is not None   # F18: solo per i DERIVATI
        # PROVENIENZA del cooldown (P0): da cosa NASCE. Solo gli 'heuristic'
        # (nostra stima) possono essere testati in anticipo dalla sveglia;
        # 'authoritative' (Retry-After/quota dichiarati dal provider), 'credit'
        # (402) e 'tier' (403) NON si toccano: il provider ha detto quando
        # torna e ritentare prima e' solo rumore (e rischio ban).
        _prov = provenance or self._infer_provenance(reason, status,
                                                     _explicit_seconds)
        s.last_provenance = _prov
        self._cooldown_prov()[unique] = _prov
        if seconds is None:
            mode = getattr(pol, "cooldown_mode", "linear") or "linear"
            if mode == "linear":
                base_m = max(1, int(getattr(pol, "cooldown_base_min", 30) or 30))
                mult_m = max(0, int(getattr(pol, "cooldown_linear_mult_min", 30) or 30))
                seconds = (base_m + mult_m * (s.fail_count_24h - 1)) * 60.0
                seconds = min(seconds, float(pol.max_cooldown_sec))
            elif pol.cooldown_escalation and s.fail_streak > 1:
                expo = min(s.fail_streak - 1, 24)
                seconds = min(float(pol.max_cooldown_sec),
                              float(pol.cooldown_sec) * (2 ** expo))
            else:
                seconds = float(pol.cooldown_sec)
        seconds = max(1.0, float(seconds))
        # TETTO operatore sui cooldown STIMATI ('heuristic'): un retry
        # dichiarato dal provider (Retry-After/quota => 'authoritative') o un
        # credit/tier non si tocca MAI. 0 = nessun tetto.
        _is_quota = str(reason or "").startswith("quota_exhausted")
        if _prov == "heuristic" and not _is_quota:
            _ceil = max(0, int(getattr(pol, "cooldown_estimate_ceiling_sec",
                                       0) or 0))
            if _ceil > 0:
                seconds = min(seconds, float(_ceil))
        # ── CLASSI DI ERRORE (F18) ────────────────────────────────────────
        # 429 = quota: chiave satura -> soft per-chiave (sotto) + durata
        #   dettata dal Retry-After; 503/529 = dep sovraccarico -> cooldown
        #   breve per-unique; 500/timeout = transitorio -> breve dedicato.
        # Evita che un 503 isolato tenga la chiave fuori per 60s e mangi
        # _key_scores per ore (halflife) e che un timeout esploda a minuti.
        _cls = self._error_class(status, reason)
        if getattr(pol, "error_class_cooldowns", True):
            if _cls == "transient" and not _explicit_seconds:
                # Solo i cooldown DERIVATI vengono accorciati: un `seconds`
                # esplicito (Retry-After, soft anti-black-hole) e' un segnale
                # concreto del chiamante e vince.
                if reason == "timeout":
                    _sc = int(getattr(pol, "cooldown_timeout_sec", 60) or 60)
                else:
                    _sc = int(getattr(pol, "cooldown_transient_sec", 15) or 15)
                seconds = min(seconds, max(1.0, float(_sc)))
                log.debug("[cooldown-class] %s classe=transient(%s) -> %.0fs",
                          unique, reason or "5xx", seconds)
            elif _cls == "quota":
                log.debug("[cooldown-class] %s classe=quota -> %.0fs "
                          "(retry-after)", unique, seconds)
        # TIMEOUT: solo in modalita' storica (classi disattivate) vale il
        # moltiplicatore `timeout_cooldown_mult`; con le classi attive il
        # timeout ha gia' il suo cooldown breve dedicato (anti black-hole:
        # resta >0, ma non esplode a minuti).
        if reason == "timeout" and not getattr(pol, "error_class_cooldowns", True):
            _tm = max(1, int(getattr(pol, "timeout_cooldown_mult", 10) or 10))
            seconds = min(seconds * _tm, float(pol.max_cooldown_sec))
        # Leva B: un deployment CRONICO (fail_24h >= soglia) che fallisce
        # di nuovo va in pausa MINIMA longa (chronic_fail_cooldown_sec, 2h):
        # dopo essere stato "svegliato" dal paracadute e aver fallito, non
        # deve essere ritentato a breve. clear_cooldown su successo lo azzera.
        thr = max(1, int(getattr(pol, "cooldown_retry_max_fail_24h", 10) or 10))
        if s.fail_count_24h >= thr and kind != ErrorKind.QUOTA_RESET \
                and not _is_quota:
            floor_cd = max(1.0, float(getattr(
                pol, "chronic_fail_cooldown_sec", 7200) or 7200))
            seconds = min(max(seconds, floor_cd), float(pol.max_cooldown_sec))
        if kind != ErrorKind.QUOTA_RESET:
            seconds = self._apply_jitter(seconds, unique)
        _now = time.time()
        if additive:
            # ESTENSIONE ADDITIVA: non sovrascrivere il residuo esistente ma
            # sommargli `seconds` (usato dall'autoprobe: un KO deve ALLUNGARE il
            # cooldown di grow/transient, non resettarlo a un valore secco).
            base = max(self._cooldown.get(unique, 0.0), _now)
            since = self._cooldown_since.get(unique)
            if since is None or since > _now:
                since = _now
            new_exp = base + seconds
            self._cooldown[unique] = new_exp
            self._cooldown_since[unique] = since
            self._cooldown_full_map()[unique] = float(new_exp - since)
        else:
            self._cooldown[unique] = _now + seconds
            self._cooldown_since[unique] = _now
            self._cooldown_full_map()[unique] = float(seconds)
        esc = ""
        if seconds is not None and s.fail_streak > 1 \
                and getattr(pol, "cooldown_mode", "linear") == "exponential":
            esc = " escalation"
        _tag = "esteso di" if additive else "inattivo per"
        log.warning("[cooldown] %s %s %ds%s (streak=%d, fail_24h=%d)",
                    unique, _tag, int(seconds), esc, s.fail_streak,
                    s.fail_count_24h)

        # F7: il 429 e' quasi sempre un fatto di CHIAVE/account, non del
        # singolo deployment: bloccare soft (skip a pick, zero strike/zero
        # cooldown) TUTTE le twin sulla stessa api_key per il Retry-After.
        # F18: vale anche per 'quota_exhausted' (limite giornaliero/mensile
        # della chiave: stesso effetto, stessa gestione).
        if reason in ("http_429", "quota_exhausted") and \
                getattr(pol, "key_soft_429_enabled", True):
            _d429 = self.config.deployment_by_unique(unique)
            _k429 = (_d429 or {}).get("api_key")
            if isinstance(_k429, str) and _k429:
                _tag429 = hashlib.sha256(
                    _k429.encode("utf-8", errors="replace")).hexdigest()[:12]
                _cap = max(10.0, float(getattr(pol, "key_soft_max_sec",
                                               900) or 900))
                # jitter DETERMINISTICO (F19): le twin non devono ripartire
                # tutte nel medesimo millisecondo al termine del Retry-After.
                _until = _now + min(float(seconds), _cap) \
                    + self._jitter_spread(_tag429)
                _sd = getattr(self, "_key_soft", None)
                if _sd is None:
                    _sd = {}
                    self._key_soft = _sd
                if float(_sd.get(_tag429, 0.0)) < _until:
                    _sd[_tag429] = _until
                    log.info("[key-soft] chiave %s*: skip soft %ds "
                             "(429 su %s)", _tag429[:6],
                             int(min(float(seconds), _cap)), unique)

        # --- Circuit Breaker (hybrid: dep sempre, key solo errori di chiave) ---
        self._update_circuit_breaker_on_failure(
            unique, key_level=self._is_key_level_failure(status, reason))
        # F25: 5xx sistematici su chiavi diverse -> breaker di MODELLO
        self._note_model_failure(self._dep_of(unique), status)

        return seconds

    # ------------------------------- soft skip PER-CHIAVE (F6/F7, zero colpa)
    def _key_tag(self, unique: str) -> str | None:
        """Tag stabile (hash, mai la chiave in chiaro) della api_key del
        deployment; None se ignota."""
        dep = self.config.deployment_by_unique(unique)
        if not dep:
            return None
        k = dep.get("api_key")
        if not isinstance(k, str) or not k:
            return None
        return hashlib.sha256(k.encode("utf-8", errors="replace")).hexdigest()[:12]

    def _key_fault_blocked(self, unique: str) -> bool:
        """Vero se la CHIAVE del deployment e' momentaneamente sazia:
        - F7: 429 appena preso -> blocco soft per tutto il Retry-After
          (tetto key_soft_max_sec) sull'INTERA chiave: le twin sulla stessa
          account rifarebbero solo un altro 429;
        - F6: header X-RateLimit-Requests-remaining fresco (ttl) e basso.
        Nessuna punizione: niente cooldown reale, niente strike, niente
        reputazione. Vale SOLO sul free-world (gruppi dims/cap): i bucket
        pagati -go/-fallback sono la RISERVA a cui si chiede comunque il
        possibile. Altra sessione, stessa chiave: bloccata uguale (e' verita'
        del provider, non della sessione)."""
        if not getattr(self.policy, "rate_hint_skip_enabled", True):
            return False
        dep = self.config.deployment_by_unique(unique)
        if not dep:
            return False
        g = dep.get("group", "")
        try:
            if self.config.group_caps.get(g) is not None \
                    or self._is_renewal_bucket(g):
                return False
        except Exception:                       # noqa: BLE001
            return False
        k = dep.get("api_key")
        if not isinstance(k, str) or not k:
            return False
        tag = hashlib.sha256(k.encode("utf-8", errors="replace")).hexdigest()[:12]
        now = time.time()
        if not getattr(self.policy, "key_soft_429_enabled", True):
            soft = {}
        else:
            soft = getattr(self, "_key_soft", None) or {}
        if float(soft.get(tag, 0.0)) > now:
            return True
        if not getattr(self.policy, "rate_hint_skip_enabled", True):
            return False
        rec = (getattr(self, "_key_hints", None) or {}).get(tag)
        if not rec:
            return False
        ts, rem = rec
        ttl = max(1.0, float(getattr(self.policy, "rate_hint_ttl_sec",
                                     20.0) or 20.0))
        if now - ts > ttl:
            return False
        cap = int(getattr(self.policy, "rate_hint_remaining_max", 5) or 5)
        return rem <= cap

    def note_rate_limit(self, unique: str, rl: dict) -> None:
        """Hint quota dagli header X-RateLimit-*: se le richieste rimanenti
        sono poche (<= soglia), abbassa il cap appreso PRIMA che scatti il 429.
        Il budget guard dosera' lo scoring (deprioritizzazione) senza bisogno
        di un cooldown. Solo-riduzione: il cap puo' solo scendere con gli hint,
        poi si ri-apprende dai 429 reali."""
        # F6: snapshot SEMPRE (indipendente dal budget guard): alimenta il
        # soft skip per-chiave di durata ttl, senza alcuna punizione.
        if getattr(self.policy, "rate_hint_skip_enabled", True):
            try:
                _rem = float((rl or {}).get("requests_remaining"))
            except (TypeError, ValueError):
                _rem = -1.0
            if _rem >= 0:
                tag = self._key_tag(unique)
                if tag:
                    d = getattr(self, "_key_hints", None)
                    if d is None:
                        d = {}
                        self._key_hints = d
                    d[tag] = (time.time(), _rem)
                    if len(d) > 8192:
                        _now = time.time()
                        for k in [k for k, (t, _r) in d.items()
                                  if _now - t > 3600]:
                            d.pop(k, None)
        bg = self.policy.budget_guard or {}
        if not bg.get("enabled"):
            return
        rl = rl or {}
        try:
            remaining = float(rl.get("requests_remaining") or -1)
        except (TypeError, ValueError):
            return
        if remaining < 0:
            return
        thr = max(1, int(bg.get("rate_hint_threshold", 3) or 3))
        if remaining > thr:
            return
        s = self.stats_for(unique)
        new_cap = max(1.0, remaining + 1.0)
        if s.min_cap_learned <= 0 or new_cap < s.min_cap_learned:
            s.min_cap_learned = new_cap
            log.info("[budget] %s: hint rate-limit (remaining=%.0f<=%d) -> "
                     "cap ~%.0f/min", unique, remaining, thr, new_cap)

    def _retire_permanent(self, unique: str, reason: str) -> None:
        """Ritira un deployment permanentemente rotto (chiave morta, modello
        rimosso). NON tocca il CSV: usa il lifecycle keyhealth (retired)."""
        try:
            from . import main as _gw_mod          # lazy: evita cicli import
            kh = getattr(_gw_mod, "KEYHEALTH", None)
            if kh is None:
                return
            kh.set_state(unique, "retired", reason=reason)
            kh.save()
            log.warning("[lifecycle] %s RETIRED (permanent: %s)", unique, reason)
        except Exception:  # noqa: BLE001
            log.debug("[lifecycle] retire %s fallito", unique, exc_info=True)

    def escalate_cooldown(self, base_seconds: float,
                          fail_count_24h: int) -> float:
        """Escalation DOLCE del cooldown per fallimenti ricorrenti (24h).

        - primo fallimento (~1): ritorna `base_seconds` invariato;
        - fallimenti successivi: `base + 10%` del cooldown "potente" lineare
          (cooldown_base_min + cooldown_linear_mult_min*(fail_24h-1) minuti,
          cap max_cooldown_sec), così una chiave che fallisce in continuazione
          (es. 18 volte/24h) viene esclusa per minuti/ore invece di essere
          riesumata ogni 2 minuti.

        Usata dai fallimenti SOFT/transitori (stream vuoto, 429, body vuoto):
        non per gli errori duri (auth/modello assente) che hanno già cooldown
        propri. `clear_cooldown` su successo resetta fail_count_24h, quindi
        un deployment "svegliato" dalla catena e che risponde riparte dal
        base appena riabilitato.
        """
        n = max(0, int(fail_count_24h or 0))
        if n <= 1:
            return float(base_seconds)
        base_m = max(1, int(getattr(self.policy, "cooldown_base_min", 30) or 30))
        mult_m = max(0, int(getattr(self.policy, "cooldown_linear_mult_min", 30) or 30))
        potent = (base_m + mult_m * max(0, n - 1)) * 60.0
        potent = min(potent, float(getattr(self.policy, "max_cooldown_sec", 18000) or 18000))
        return min(float(base_seconds) + 0.1 * potent,
                   float(getattr(self.policy, "max_cooldown_sec", 18000) or 18000))

    # --- Circuit Breaker methods (hybrid: per-deployment + per-API-key) ---
    def _cb_scope(self) -> str:
        s = str(getattr(self.policy, "circuit_breaker_scope", "hybrid")
                or "hybrid").lower()
        return s if s in ("hybrid", "dep", "key") else "hybrid"

    def _dep_cb_store(self) -> dict:
        store = getattr(self, "_dep_circuit_breakers", None)
        if store is None:
            store = {}
            self._dep_circuit_breakers = store
        return store

    def _get_cb_entry(self, store: dict, key: str) -> dict:
        cb = store.get(key)
        if cb is None:
            cb = {"failures": 0, "last_failure": 0.0, "state": "closed",
                  "opened_at": 0.0, "half_open_successes": 0}
            store[key] = cb
        return cb

    def _dep_of(self, unique: str) -> dict | None:
        if not hasattr(self, 'config') or self.config is None:
            return None
        # Handle Router.__new__ test pattern where config may lack this method
        if not hasattr(self.config, 'deployment_by_unique'):
            return None
        return self.config.deployment_by_unique(unique)

    def _get_circuit_breaker(self, unique: str) -> dict | None:
        """Legacy: breaker per CHIAVE API (store per-key), usato da admin/test."""
        dep = self._dep_of(unique)
        if not dep:
            return None
        ak = self._api_key_str(dep)
        if not ak:
            return None
        return self._get_cb_entry(self._circuit_breakers, ak)

    def _get_dep_circuit_breaker(self, unique: str) -> dict:
        """Breaker per-DEPLOYMENT (unique)."""
        return self._get_cb_entry(self._dep_cb_store(), unique)

    def _cb_register_failure(self, store: dict, key: str, label: str,
                             name: str) -> None:
        cb = self._get_cb_entry(store, key)
        cb["failures"] += 1
        cb["last_failure"] = time.time()
        threshold = getattr(self.policy, "circuit_breaker_threshold", 5)
        if cb["state"] == "closed" and cb["failures"] >= threshold:
            cb["state"] = "open"
            cb["opened_at"] = time.time()
            log.warning("[circuit-breaker] %s %s OPEN (failures=%d)",
                        label, name, cb["failures"])
        elif cb["state"] == "half_open":
            cb["state"] = "open"
            cb["opened_at"] = time.time()
            cb["half_open_successes"] = 0
            log.warning("[circuit-breaker] %s %s RE-OPEN after half-open failure",
                        label, name)

    def _cb_register_success(self, store: dict, key: str, label: str,
                             name: str) -> None:
        cb = store.get(key)
        if cb is None:
            return
        if cb["state"] == "half_open":
            cb["half_open_successes"] += 1
            half_open_reqs = getattr(self.policy,
                                     "circuit_breaker_half_open_requests", 3)
            if cb["half_open_successes"] >= half_open_reqs:
                cb["state"] = "closed"
                cb["failures"] = 0
                log.info("[circuit-breaker] %s %s CLOSED after %d half-open "
                         "successes", label, name, cb["half_open_successes"])
        elif cb["state"] == "closed":
            cb["failures"] = 0

    def _cb_block_or_transition(self, store: dict, key: str, label: str,
                                name: str) -> bool:
        cb = store.get(key)
        if cb is None or cb["state"] == "closed":
            return False
        if cb["state"] == "open":
            timeout = getattr(self.policy, "circuit_breaker_timeout", 60.0)
            if time.time() - cb["opened_at"] >= timeout:
                cb["state"] = "half_open"
                cb["half_open_successes"] = 0
                log.info("[circuit-breaker] %s %s HALF-OPEN (timeout %ds)",
                         label, name, int(timeout))
                return False  # Allow one request through
            return True
        return False  # half-open: allow through

    @staticmethod
    def _is_key_level_failure(status, reason) -> bool:
        """True se il fallimento riguarda la CHIAVE (auth/quota), non il modello."""
        try:
            if status is not None and abs(int(status)) in (401, 402, 403, 429):
                return True
        except (TypeError, ValueError):
            pass
        r = (reason or "").lower()
        for tok in ("401", "402", "403", "429", "auth", "forbidden",
                    "unauthorized", "quota", "rate_limit", "rate-limit"):
            if tok in r:
                return True
        return False

    def _update_circuit_breaker_on_failure(self, unique: str,
                                           key_level: bool = True) -> None:
        """Registra un fallimento. Hybrid: dep sempre; key solo errori di chiave."""
        scope = self._cb_scope()
        dep = self._dep_of(unique)
        if dep is None:
            return
        if scope in ("hybrid", "dep"):
            self._cb_register_failure(self._dep_cb_store(), unique, "dep", unique)
        if scope == "dep":
            return
        if scope == "key" or key_level:
            ak = self._api_key_str(dep)
            if ak:
                self._cb_register_failure(self._circuit_breakers, ak, "key", ak)

    def _update_circuit_breaker_on_success(self, unique: str) -> None:
        """Registra un successo (hybrid: dep + key)."""
        scope = self._cb_scope()
        dep = self._dep_of(unique)
        if dep is None:
            return
        if scope in ("hybrid", "dep"):
            self._cb_register_success(self._dep_cb_store(), unique, "dep", unique)
        if scope in ("hybrid", "key"):
            ak = self._api_key_str(dep)
            if ak:
                self._cb_register_success(self._circuit_breakers, ak, "key", ak)

    def _is_circuit_open(self, unique: str) -> bool:
        """True se il breaker (dep o key, secondo scope) blocca il deployment."""
        scope = self._cb_scope()
        dep = self._dep_of(unique)
        if dep is None:
            return False
        ak = self._api_key_str(dep)
        if scope == "dep":
            return self._cb_block_or_transition(
                self._dep_cb_store(), unique, "dep", unique)
        if scope == "key":
            return bool(ak) and self._cb_block_or_transition(
                self._circuit_breakers, ak, "key", ak)
        # hybrid: prima la chiave (retro-compat), poi il deployment
        blocked_key = bool(ak) and self._cb_block_or_transition(
            self._circuit_breakers, ak, "key", ak)
        blocked_dep = self._cb_block_or_transition(
            self._dep_cb_store(), unique, "dep", unique)
        return blocked_key or blocked_dep

    # --- F25: circuit breaker proattivo per provider|modello ---
    @staticmethod
    def _model_cb_key(dep: dict) -> str:
        return f"{dep.get('provider', '')}|{dep.get('model', '')}"

    def _key_tag_of(self, dep: dict) -> str:
        ak = self._api_key_str(dep)
        if not ak:
            return ""
        return hashlib.sha256(ak.encode()).hexdigest()[:12]

    def _note_model_failure(self, dep: dict | None, status=None) -> None:
        """F25: accumula i 5xx per provider|modello. Quando arrivano da
        almeno `model_circuit_keys` CHIAVI diverse nella finestra si apre il
        breaker di modello (skip nel pick, zero penale reputazionale)."""
        if not dep:
            return
        if not getattr(self.policy, "model_circuit_enabled", True):
            return
        try:
            st = abs(int(status)) if status else 0
        except (TypeError, ValueError):
            st = 0
        if st < 500:
            return
        if not dep.get("model"):
            return
        now = time.time()
        win = float(getattr(self.policy, "model_circuit_window_sec", 60) or 60)
        need = int(getattr(self.policy, "model_circuit_keys", 3) or 3)
        store = getattr(self, "_model_cb", None)
        if store is None:
            store = {}
            self._model_cb = store
        mkey = self._model_cb_key(dep)
        ent = store.get(mkey)
        if ent is None or now - ent.get("ts", 0.0) > win:
            ent = {"tags": set(), "ts": now, "opened": 0.0}
            store[mkey] = ent
        tag = self._key_tag_of(dep)
        if tag:
            ent["tags"].add(tag)
        if len(ent["tags"]) >= need and not ent.get("opened"):
            ent["opened"] = now
            log.warning("[model-cb] %s APERTO: %d chiavi distinte in 5xx in "
                        "%.0fs", mkey, len(ent["tags"]), win)

    def _model_blocked(self, dep: dict | None) -> bool:
        """F25: True se il modello del dep e' in breaker aperto (soft skip)."""
        if not dep or not getattr(self.policy, "model_circuit_enabled", True):
            return False
        if not dep.get("model"):
            return False
        ent = getattr(self, "_model_cb", {}).get(self._model_cb_key(dep))
        if not ent or not ent.get("opened"):
            return False
        open_sec = float(getattr(self.policy, "model_circuit_open_sec", 60) or 60)
        if time.time() - ent["opened"] > open_sec:
            return False     # finestra chiusa: si riprova (half-open implicito)
        return True

    def mark_failed_double_residual(self, unique: str,
                                     reason: str | None = None,
                                     status: int | None = None) -> float:
        """Raddoppia il cooldown residuo quando un deployment dormiente fallisce
        di nuovo. Usato al posto di mark_failed per i retry di deployment
        in cooldown (stale/ultima spiaggia)."""
        # [Blocco 1] Aggiorna reputation scoring
        self.record_failure(unique, reason, status)

        now = time.time()
        remaining = max(1.0, self._cooldown.get(unique, now) - now)
        new_cd = remaining * 2.0
        new_cd = min(new_cd, float(self.policy.max_cooldown_sec))
        if reason == "timeout":
            _tm = max(1, int(getattr(self.policy, "timeout_cooldown_mult", 10)
                             or 10))
            new_cd = min(new_cd * _tm, float(self.policy.max_cooldown_sec))
        s = self.stats_for(unique)
        # ESC-PIN: idem mark_failed (il winner fallito si sblocca).
        for _g, (_u, _ts) in list(self._esc().items()):
            if _u == unique:
                self._esc().pop(_g, None)
        if reason:
            s.last_reason = reason
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if s.fail_day_key != today:
            s.fail_count_24h = 0
            s.fail_day_key = today
        s.fail_count_24h += 1
        s.fail_count += 1
        s.fail_streak = self._decay_streak(s.fail_streak, s.last_fail_ts, now)
        s.last_fail_ts = now
        s.fail_streak += 1
        # Un 429/quota NON e' "chiave rotta" ma "chiave satura": non deve
        # contare verso il ritiro automatico, altrimenti una free-key con
        # quota giornaliera bassa verrebbe parcheggiata per sempre solo
        # perche' saturata oggi (stessa regola di KeyHealth.observe, F30).
        if not _is_quota_evidence(reason, status):
            s.probe_fail_streak += 1
        prev = 1.0 if s.success_ema is None else s.success_ema
        s.success_ema = max(0.0, 0.8 * prev)
        # Leva B: stessa pausa minima longa (2h) se il cronico fallisce
        # di nuovo anche da dormiente (mark_failed_double_residual usa già
        # il raddoppio del residuo; qui garantiamo almeno il floor).
        thr = max(1, int(getattr(self.policy, "cooldown_retry_max_fail_24h", 10)
                                    or 10))
        if s.fail_count_24h >= thr:
            floor_cd = max(1.0, float(getattr(
                self.policy, "chronic_fail_cooldown_sec", 7200) or 7200))
            new_cd = min(max(new_cd, floor_cd),
                         float(self.policy.max_cooldown_sec))
        new_cd = self._apply_jitter(new_cd, unique)
        self._cooldown[unique] = now + new_cd
        self._cooldown_since[unique] = now
        self._cooldown_full_map()[unique] = float(new_cd)
        log.warning("[cooldown] %s dormiente ri-fallito -> cooldown "
                    "raddoppiato a %ds (residuo era %ds, fail_24h=%d)",
                    unique, int(new_cd), int(remaining), s.fail_count_24h)
        self._maybe_retire_on_probe_fail(unique, s)
        return new_cd

    def clear_cooldown(self, unique: str) -> None:
        """Rimuove cooldown e azzera contatori fail per un deployment che ha
        risposto con successo dopo essere stato in cooldown."""
        self._cooldown.pop(unique, None)
        self._cooldown_since.pop(unique, None)
        self._cooldown_full_map().pop(unique, None)
        self._cooldown_prov().pop(unique, None)
        s = self.stats_for(unique)
        s.fail_streak = 0
        s.fail_count_24h = 0
        s.fail_day_key = ""
        s.probe_fail_streak = 0
        log.info("[cooldown] %s riabilitato (successo da dormiente)", unique)

        # --- Circuit Breaker: success updates ---
        self._update_circuit_breaker_on_success(unique)

    # --------------------------------------------------- Reputation scoring
    def _init_scoring_if_needed(self) -> None:
        """Initialize scoring dicts if not already initialized (for __new__ pattern)."""
        if not hasattr(self, '_base_scores'):
            self._base_scores: dict[str, float] = {}
        if not hasattr(self, '_provider_scores'):
            self._provider_scores: dict[str, float] = {}
        if not hasattr(self, '_key_scores'):
            self._key_scores: dict[str, float] = {}
        if not hasattr(self, '_avg_latencies'):
            self._avg_latencies: dict[str, float] = {}
        if not hasattr(self, '_scores_decay_ts'):
            self._scores_decay_ts: float = time.time()
        if not hasattr(self, '_scores_decay_log_ts'):
            self._scores_decay_log_ts: float = time.time()

    def _decay_scores(self, now: float | None = None) -> float:
        """Time-decay verso lo zero dei punteggi di reputazione.

        Moltiplica _base_scores/_provider_scores/_key_scores per
        0.5 ** (dt / halflife): il passato viene gradualmente "dimenticato" e
        contano i comportamenti recenti. Halflife <= 0 disabilita. Ritorna il
        fattore applicato (1.0 = nessun decay)."""
        now = time.time() if now is None else now
        self._init_scoring_if_needed()
        hl = float(getattr(self.policy, "reputation_decay_halflife_sec", 0.0)
                   or 0.0)
        last = self._scores_decay_ts
        self._scores_decay_ts = now
        if hl <= 0.0 or now <= last:
            return 1.0
        dt = now - last
        factor = 0.5 ** (dt / hl)
        pruned = 0
        for d in (self._base_scores, self._provider_scores,
                  self._key_scores):
            for k in list(d.keys()):
                v = d[k] * factor
                if abs(v) < 1e-4:
                    del d[k]
                    pruned += 1
                else:
                    d[k] = v
        # Heartbeat INFO ogni ~10 minuti (il watcher gira ogni pochi secondi:
        # loggare ad ogni tick sarebbe rumore); dettaglio a DEBUG.
        if now - self._scores_decay_log_ts >= 600.0:
            log.info("[rep-decay] punteggi *=%.4f (halflife=%.1fh, "
                     "finestra=%.0fs, potati=%d)", factor, hl / 3600.0, dt,
                     pruned)
            self._scores_decay_log_ts = now
        else:
            log.debug("[rep-decay] *=%.6f dt=%.0fs potati=%d", factor, dt,
                      pruned)
        return factor

    def _provider_key(self, dep: dict) -> str:
        """Restituisce la chiave provider/modello per il scoring di gruppo."""
        self._init_scoring_if_needed()
        if not hasattr(self, 'config') or self.config is None:
            return "default|default"
        prov = dep.get("api_base", "")
        model = dep.get("model", "")
        return f"{prov}|{model}"

    def _api_key_str(self, dep: dict) -> str:
        """Restituisce la chiave API per il scoring di gruppo."""
        self._init_scoring_if_needed()
        if not hasattr(self, 'config') or self.config is None:
            return ""
        return dep.get("api_key", "")

    def record_attempt(self, unique: str) -> None:
        """Registra un tentativo: incrementa il punteggio del provider e della chiave."""
        if not hasattr(self, 'config') or self.config is None:
            return
        dep = self.config.deployment_by_unique(unique)
        if dep is None:
            return
        # Incrementa punteggio provider/modello per TUTTI i deployment tranne quello attuale
        pk = self._provider_key(dep)
        self._provider_scores[pk] = self._provider_scores.get(pk, 0) + SW["ATTEMPT_PROVIDER"]
        # Incrementa punteggio chiave API per TUTTI i deployment tranne quello attuale
        ak = self._api_key_str(dep)
        self._key_scores[ak] = self._key_scores.get(ak, 0) + SW["ATTEMPT_KEY"]
        log.debug("[rep-attempt] %s provider+=%d key+=%d", unique, SW["ATTEMPT_PROVIDER"], SW["ATTEMPT_KEY"])

    def record_success(self, unique: str, latency_ms: float,
                       quality: float = 1.0, ctx_est=None,
                       kind: str = "total") -> None:
        """Registra un successo: decrementa i punteggi per deployment, provider, chiave.

        `quality` in [0.1, 1.0] scala l'alpha dell'EMA di latenza: una risposta
        "sporca" (tool_repair, fake tool-call, QC fallita, stallo) pesa meno e
        degrada l'EMA piu' lentamente.

        `ctx_est` + `kind` attivano le EMA PER BUCKET DI CONTESTO: 'total' e'
        la durata del giro completo (non-stream e fine-stream), 'ttft' e' il
        tempo al primo contenuto (commit dello stream). L'EMA globale
        `_avg_latencies` resta mista per compatibilita' (timeout adattivo,
        tie-breaker); i consumatori sensibili al contesto leggono i bucket."""
        if not hasattr(self, 'config') or self.config is None:
            return
        dep = self.config.deployment_by_unique(unique)
        if dep is None:
            return
        q = min(1.0, max(0.1, float(quality)))
        alpha = max(0.05, 0.3 * q)
        self._base_scores[unique] = self._base_scores.get(unique, 0) + SW["SUCCESS_DEPLOYMENT"]
        pk = self._provider_key(dep)
        self._provider_scores[pk] = self._provider_scores.get(pk, 0) + SW["SUCCESS_PROVIDER"]
        ak = self._api_key_str(dep)
        self._key_scores[ak] = self._key_scores.get(ak, 0) + SW["SUCCESS_KEY"]
        # Aggiorna media latenza storica per il tie-breaker
        old_avg = self._avg_latencies.get(unique)
        if old_avg is None:
            self._avg_latencies[unique] = latency_ms
        else:
            self._avg_latencies[unique] = old_avg * (1 - alpha) + latency_ms * alpha
        ema_new = self._avg_latencies[unique]
        if old_avg is None or (old_avg < LATENCY_ROTATE_THRESHOLD_MS and ema_new >= LATENCY_ROTATE_THRESHOLD_MS):
            log.info("[latency-cross] %s ema superata soglia: da %.0fms a %.0fms (threshold=%dms)", unique, old_avg or 0, ema_new, LATENCY_ROTATE_THRESHOLD_MS)
        elif old_avg and old_avg >= LATENCY_ROTATE_THRESHOLD_MS and ema_new < LATENCY_ROTATE_THRESHOLD_MS:
            log.info("[latency-cross] %s ema rientrata sotto soglia: da %.0fms a %.0fms (threshold=%dms)", unique, old_avg, ema_new, LATENCY_ROTATE_THRESHOLD_MS)
        # --- EMA per bucket di contesto (F1) + history per il p95 dinamico ---
        self._note_latency_sample(unique, latency_ms, ctx_est, kind, alpha)
        log.debug("[rep-success] %s dep+=%d provider+=%d key+=%d ema=%.0fms", unique, SW["SUCCESS_DEPLOYMENT"], SW["SUCCESS_PROVIDER"], SW["SUCCESS_KEY"], latency_ms)

        # --- Circuit Breaker: success updates ---
        self._update_circuit_breaker_on_success(unique)

    def note_estimate_error(self, unique: str, ctx_est, prompt_tokens) -> None:
        """Calibrazione CLOSED-LOOP dell'estimator (F14).

        Il provider risponde col VERO `usage.prompt_tokens`: il rapporto
        r = reali/stimati dice se `estimate_tokens` sottostima (r>1: il
        tokenizer e' piu' denso di chars/4) o sovrastima (r<1). Aggiorniamo
        un divisore per-deployment in modo che la stima converga:
            div <- div / (1 + alpha*(r-1))     (clamp 1.5..4.5)
        Il segno: r>1 -> divisore piu' PICCOLO -> stima piu' alta (e
        viceversa). Solo campioni affidabili (ctx>=8000 e prompt>1000)."""
        try:
            ctx = int(ctx_est or 0)
            pt = int(prompt_tokens or 0)
        except (TypeError, ValueError):
            return
        if ctx < 8000 or pt <= 1000:
            return
        try:
            alpha = float(getattr(self.policy, "estimate_calib_alpha", 0.05)
                          or 0.05)
        except (TypeError, ValueError):
            alpha = 0.05
        base = float(getattr(self.policy, "estimate_divisor", 4) or 4)
        d = getattr(self, "_est_div", None)
        if d is None:
            d = self._est_div = {}
        cur = float(d.get(unique, base) or base)
        r = pt / float(ctx)
        # EMA ADDITIVA verso il divisor VERO (base/r): stima = chars/cur, e
        # vogliamo chars/base * (base/cur) ~ pt  =>  cur -> base/r. La forma
        # puramente moltiplicativa (cur*(1±err*alpha)) NON ha punto fisso e
        # deriverebbe fino ai clamp; questa converge a base/r e resta stabile.
        target = base / r if r > 0 else base
        new = cur + alpha * (target - cur)
        d[unique] = max(1.5, min(4.5, new))

    def estimate_correction(self, unique: str) -> float:
        """Moltiplicatore da applicare alla stima grezza per `unique`
        (1.0 = nessuna correzione appresa): divisor_base / divisor_appreso."""
        try:
            base = float(getattr(self.policy, "estimate_divisor", 4) or 4)
        except (TypeError, ValueError):
            base = 4.0
        d = getattr(self, "_est_div", {}) or {}
        cur = d.get(unique)
        if not cur or cur <= 0:
            return 1.0
        return base / float(cur)

    def effective_divisor(self, unique: str) -> float:
        """H2: divisore chars/token CALIBRATO per `unique` (F14). Su Qwen
        (~3.2 char/token) vale ~3.2 invece del 4 fisso: chi conta i token per
        il budget della frontiera ctxcompact deve usare QUESTO, altrimenti
        sottostima il contesto e lascia il payload sopra max_input."""
        try:
            base = float(getattr(self.policy, "estimate_divisor", 4) or 4)
        except (TypeError, ValueError):
            base = 4.0
        d = getattr(self, "_est_div", {}) or {}
        cur = d.get(unique)
        try:
            cur = float(cur)
        except (TypeError, ValueError):
            return base
        if not cur or cur <= 0:
            return base
        return cur

    def _note_latency_sample(self, unique: str, latency_ms: float,
                             ctx_est, kind: str, alpha: float) -> None:
        """Aggiorna l'EMA del bucket giusto (total o ttft) e, per i totali con
        contesto noto, la history usata dal p95 del dynamic scoring."""
        try:
            lat = float(latency_ms)
        except (TypeError, ValueError):
            return
        if lat <= 0:
            return
        b = _ctx_bucket(ctx_est)
        if b < 0:
            return
        table = getattr(self, "_ttft_buckets" if kind == "ttft"
                        else "_lat_buckets", None)
        if not isinstance(table, dict):
            return
        v = table.get(unique)
        if v is None:
            v = [0.0] * CTX_BUCKET_COUNT
            table[unique] = v
        while len(v) < CTX_BUCKET_COUNT:
            v.append(0.0)
        old = v[b]
        v[b] = lat if old <= 0 else old * (1 - alpha) + lat * alpha
        try:
            cx = int(ctx_est)
        except (TypeError, ValueError):
            cx = 0
        if kind == "ttft":
            # F9: rate di prefill (ms/1k token) solo con contesto utile; e' la
            # stessa misura per tutti i bucket, quindi regge il cambio di taglia.
            if cx >= TTFT_RATE_MIN_CTX:
                rate = getattr(self, "_prefill_rate", None)
                if not isinstance(rate, dict):
                    rate = self._prefill_rate = {}
                r = lat / (cx / 1000.0)
                prev = rate.get(unique)
                rate[unique] = r if prev is None or prev <= 0 else \
                    prev * (1 - alpha) + r * alpha
            return
        s = self.stats_for(unique)
        if s is not None:
            h = s.latency_history
            if h is None:
                h = s.latency_history = []
            h.append((b, cx, lat))
            if len(h) > 20:
                del h[:-20]

    def bucket_latency_ms(self, unique: str, ctx_est=None,
                          kind: str = "total") -> float | None:
        """EMA del deployment nel bucket di contesto della richiesta.

        Se il bucket non ha campioni: per kind='ttft' (F9) estrapola dal rate
        di prefill (ms per 1k token) alla taglia richiesta, cosi' un dep visto
        solo su contesti piccoli non sembra "veloce" anche su 100k; per gli
        altri kind ripiega sull'EMA globale (compat)."""
        b = _ctx_bucket(ctx_est)
        table = getattr(self, "_ttft_buckets" if kind == "ttft"
                        else "_lat_buckets", None)
        if b >= 0 and isinstance(table, dict):
            v = table.get(unique)
            if v and len(v) > b and v[b] > 0:
                return float(v[b])
        if kind == "ttft":
            try:
                cx = int(ctx_est)
            except (TypeError, ValueError):
                cx = 0
            rate = getattr(self, "_prefill_rate", None)
            r = rate.get(unique) if isinstance(rate, dict) else None
            if r and r > 0 and cx > 0:
                return max(TTFT_RATE_FLOOR_MS, float(r) * cx / 1000.0)
        return getattr(self, "_avg_latencies", {}).get(unique)

    def note_stream_end(self, unique: str, dur_ms: float, ctx_est=None,
                        completion_tokens=None) -> None:
        """Durata TOTALE di uno stream committed: alimenta solo i bucket
        'total' (il punteggio di reputazione e l'EMA globale li ha gia'
        contati il commit sul primo contenuto)."""
        self._note_latency_sample(unique, dur_ms, ctx_est, "total", 0.1)
        # Rate di generazione (token/s): servono alla stima "total" quando il
        # bucket non ha campioni propri (B3, fallback rate-based).
        try:
            ct = int(completion_tokens or 0)
            if ct > 0 and dur_ms and dur_ms > 0:
                tps = ct / (float(dur_ms) / 1000.0)
                gr = getattr(self, "_gen_rate", None)
                if gr is None:
                    gr = {}
                    self._gen_rate = gr
                prev = gr.get(unique)
                gr[unique] = (tps if not prev else prev * 0.8 + tps * 0.2)
        except (TypeError, ValueError):
            pass

    def record_failure(self, unique: str, reason: str | None, status: int | None) -> None:
        """Registra un fallimento nei punteggi, PER CLASSE:

        - quota (429/rate-limit): NESSUNA penale. La chiave e' satura, non
          rotta: la gestisce il soft per-chiave (F7). Avvelenare _key_scores
          per ore al primo 429 rendeva la chiave "cattiva" anche dopo il reset.
        - transitorio (5xx/timeout/network): penale LIEVE e solo sul
          deployment (FAIL_TRANSIENT). Provider e chiave non c'entrano.
        - chiave (401/402/403/auth): come sempre (FAIL_KEY).
        - altro 4xx (schema/payload): come sempre (deployment+provider+chiave:
          spesso e' specifico del modello/deployment).
        """
        if not hasattr(self, 'config') or self.config is None:
            return
        dep = self.config.deployment_by_unique(unique)
        if dep is None:
            return
        cls = self._error_class(status, reason)
        ak = self._api_key_str(dep)
        if cls == "quota":
            log.debug("[rep-fail] %s classe=quota: nessuna penale", unique)
            return
        if cls == "transient":
            self._base_scores[unique] = self._base_scores.get(unique, 0) \
                + SW["FAIL_TRANSIENT"]
            log.debug("[rep-fail] %s classe=transient dep+=%d (chiave e "
                      "provider intatti)", unique, SW["FAIL_TRANSIENT"])
            return
        self._base_scores[unique] = self._base_scores.get(unique, 0) + SW["FAIL_DEPLOYMENT"]
        pk = self._provider_key(dep)
        # Classificazione: 401/403 = chiave, tutto il resto = provider
        is_key_fail = status in (401, 403)
        if is_key_fail:
            self._key_scores[ak] = self._key_scores.get(ak, 0) + SW["FAIL_KEY"]
        else:
            self._provider_scores[pk] = self._provider_scores.get(pk, 0) + SW["FAIL_PROVIDER"]
            self._key_scores[ak] = self._key_scores.get(ak, 0) + SW["FAIL_KEY"]
        log.debug("[rep-fail] %s dep+=%d provider+=%d key+=%d is_key=%s", unique, SW["FAIL_DEPLOYMENT"], SW["FAIL_PROVIDER"], SW["FAIL_KEY"], is_key_fail)

    @staticmethod
    def _error_class(status: int | None, reason: str | None = None) -> str:
        """Classe di errore per cooldown/penalita': 'quota' | 'transient' |
        'key' | 'generic'. Unica fonte di verita' per record_failure e
        mark_failed (il vecchio classify_error di forwarder resta per il
        retire dei PERMANENT_DEAD)."""
        st = abs(int(status)) if status else 0
        r = (reason or "").lower()
        if st == 429 or "429" in r or "rate_limit" in r or "quota" in r:
            return "quota"
        if st in (401, 402, 403) or any(x in r for x in (
                "401", "402", "403", "auth", "forbidden", "unauthorized")):
            return "key"
        if st >= 500 or (st == 0 and r in (
                "timeout", "read_timeout", "network", "network_error",
                "provider_transient", "provider_fault")):
            return "transient"
        return "generic"

    def _reputation_score(self, unique: str, dep: dict,
                          ctx_est=None) -> float:
        """Calcola il punteggio di reputazione per un deployment."""
        if not hasattr(self, 'config') or self.config is None:
            return 0.0
        # COLD START: il deployment parte da -(preferenza × model_preference_base).
        # Un modello preferito (pref>0) parte molto avanti (es. pref=100, base=10
        # -> -1000); uno indesiderato (pref<0) parte in fondo (+1000). A
        # differenza del vecchio aggiustamento dinamico (che scalava con |score|
        # e premiava chi aveva gia' storia), qui la preferenza domina DA FREDDO:
        # saranno i cooldown/retirement a escludere davvero i deployment rotti,
        # non la mancanza di cronologia. Il seed e' PERSISTITO in _base_scores
        # cosi' successi/fallimenti vi si sommano sopra (e sopravvive al restart).
        if unique not in self._base_scores:
            _pref0 = float(dep.get("model_preference", 0) or 0)
            if _pref0:
                self._base_scores[unique] = -_pref0 * float(
                    getattr(self.policy, "model_preference_base", 10.0) or 0.0)
        score = self._base_scores.get(unique, 0.0)
        pk = self._provider_key(dep)
        ak = self._api_key_str(dep)

        # --- Provider/Key bias normalization (log-scaling) ---
        # Conta deployment per provider/model e per api_key per normalizzare
        prov_score = self._provider_scores.get(pk, 0.0)
        key_score = self._key_scores.get(ak, 0.0)

        # Normalizzazione log-scaling: provider/key con molti deployment non dominano
        # log(n+1) dove n = deployment count per provider/key
        norm = PROVIDER_BIAS_NORMALIZATION
        if norm != "none" and hasattr(self, 'config') and self.config and hasattr(self.config, 'groups'):
            if norm == "log":
                # Conta deployment per questo provider/model e per api_key
                prov_count = sum(
                    1 for lst in self.config.groups.values()
                    for d in lst if self._provider_key(d) == pk
                )
                key_count = sum(
                    1 for lst in self.config.groups.values()
                    for d in lst if self._api_key_str(d) == ak
                )
                prov_score = prov_score / math.log(prov_count + 1) if prov_count > 0 else prov_score
                key_score = key_score / math.log(key_count + 1) if key_count > 0 else key_score
            elif norm == "sqrt":
                prov_count = sum(
                    1 for lst in self.config.groups.values()
                    for d in lst if self._provider_key(d) == pk
                )
                key_count = sum(
                    1 for lst in self.config.groups.values()
                    for d in lst if self._api_key_str(d) == ak
                )
                prov_score = prov_score / math.sqrt(prov_count) if prov_count > 0 else prov_score
                key_score = key_score / math.sqrt(key_count) if key_count > 0 else key_score
        # "none" = no normalization

        score = self._base_scores.get(unique, 0.0)
        score += prov_score
        score += key_score

        # NEW: Latency penalty — annuls success advantage for slow deployments
        # Uses the deployment's latency EMA IN THE CONTEXT BUCKET of this very
        # request (per-deployment scoped: altri dep sulla stessa chiave non
        # vengono toccati), con fallback all'EMA globale.
        ema = self.bucket_latency_ms(unique, ctx_est) or 0
        _thr = self._slow_threshold_ms(unique, ctx_est)
        if ema > _thr:
            over_seconds = (ema - _thr) / 1000.0
            penalty = over_seconds * LATENCY_PENALTY_PER_SEC  # e.g., 0.5 per second
            _dcy = self._cooldown_decay(unique)
            if _dcy < 1.0:
                penalty *= _dcy        # penalita' che decade col cooldown
            score += penalty
            log.debug("[latency-penalty] %s ema=%.0fms threshold=%.0fms "
                      "penalty=%.1f (over=%.1fs)",
                      unique, ema, _thr, penalty, over_seconds)

        # Bias di EFFORT: SOLO quando il client chiede esplicitamente effort
        # "high" si sposta la scelta verso l'intelligence alta e si premia chi
        # accetta `reasoning_effort`. Per default/low/medium NESSUN bias: lo
        # score resta pulito (le metriche non devono piegarsi all'effort).
        # Il punteggio e' "lower is better" e puo' essere NEGATIVO: un fattore
        # che cambia segno (es. 1-(intel-5)*w) ribalterebbe l'ordinamento. Qui
        # il fattore e' SEMPRE POSITIVO e viene orientato dal segno del
        # punteggio, cosi' il vantaggio (riduzione del punteggio) e' sempre
        # negativo per il modello favorito. Peso da policy.effort_intel_weight.
        effort = get_effort()
        if effort == "high":
            intel = float(dep.get("intelligence", 5) or 5)
            weight = abs(float(self.policy.effort_intel_weight or 0)) / 100.0
            g = intel - 5.0
            base = max(0.05, 1.0 + weight * g)
            factor = (1.0 / base) if score > 0 else base
            factor = max(0.05, min(20.0, factor))
            score *= factor
            if dep.get("effort_capable"):
                score -= EFFORT_CAPABLE_BONUS
            log.debug("[effort-bias] %s effort=high intel=%.0f factor=%.3f "
                      "totale=%.1f", unique, intel, factor, score)

        # --- Dynamic scoring: latency p95, error_rate, throughput ---
        # Pesi configurabili via policy (DYNAMIC_SCORING_DEFAULTS override)
        ds_enabled = getattr(self.policy, "dynamic_scoring_enabled", True)
        if ds_enabled and hasattr(self, '_stats') and unique in self._stats:
            stats = self._stats[unique]
            hist = stats.latency_history or []
            if hist and len(hist) >= 3:
                # F1/F9: i campioni sono (bucket, ctx, latenza). p95 SUL BUCKET
                # della richiesta quando ci sono >=3 campioni li', altrimenti su
                # tutti ma NORMALIZZATI alla taglia richiesta via rate (lat *
                # ctx_req/ctx_s): senza questo un dep provato solo su contesti
                # grossi sembrerebbe lento anche su richieste piccole (e
                # viceversa). Cap di sicurezza sul penalty (il blocco era morto
                # fino a F1: non deve dominare il punteggio SW).
                b = _ctx_bucket(ctx_est)
                try:
                    cx_req = int(ctx_est)
                except (TypeError, ValueError):
                    cx_req = 0

                def _sample(x):
                    if isinstance(x, (tuple, list)):
                        if len(x) == 3:
                            return int(x[0]), int(x[1] or 0), float(x[2])
                        if len(x) == 2:
                            return int(x[0]), 0, float(x[1])
                    return -1, 0, float(x)

                def _norm(lat, cx_s):
                    if cx_req > 0 and cx_s > 0:
                        return lat * (cx_req / float(cx_s))
                    return lat

                pairs = [_sample(x) for x in hist]
                if b >= 0:
                    samples = [lat for bb, _c, lat in pairs if bb == b]
                else:
                    samples = [lat for _b, _c, lat in pairs]
                if len(samples) < 3:
                    samples = [_norm(lat, c) for _b, c, lat in pairs]
                if samples and len(samples) >= 3:
                    # p95 latency
                    sorted_hist = sorted(samples)
                    p95_idx = max(0, int(len(sorted_hist) * 0.95) - 1)
                    p95_latency = sorted_hist[p95_idx]
                    p95_weight = getattr(self.policy, "dynamic_scoring_latency_p95_weight", 1.0)
                    score += min(25.0, (p95_latency / 1000.0) * p95_weight)

                # error_rate in recent window
                recent_attempts = stats.recent_attempts or 0
                recent_failures = stats.recent_failures or 0
                if recent_attempts > 0:
                    error_rate = recent_failures / recent_attempts
                    err_weight = getattr(self.policy, "dynamic_scoring_error_rate_weight", 2.0)
                    score += error_rate * 100.0 * err_weight  # penalty up to 200 points

                # throughput (tokens/sec)
                total_tokens = stats.total_tokens or 0
                total_duration = stats.total_duration_ms or 0.0
                if total_tokens > 0 and total_duration > 0:
                    throughput = (total_tokens / total_duration) * 1000.0  # tokens/sec
                    tput_weight = getattr(self.policy, "dynamic_scoring_throughput_weight", 0.5)
                    # Higher throughput = better (lower score)
                    score -= min(throughput, 1000.0) * tput_weight / 100.0  # cap at 1000 tok/s

                log.debug("[dynamic-scoring] %s p95=%.0fms err=%.2f tput=%.1f score=%.1f",
                          unique, p95_latency, error_rate if recent_attempts else 0,
                          throughput if total_tokens else 0, score)

        return score

    def _get_avg_latency(self, unique: str) -> float | None:
        """Restituisce la media storica della latenza per un deployment."""
        if not hasattr(self, 'config') or self.config is None:
            return None
        return self._avg_latencies.get(unique)

    def _fleet_bucket_median(self, kind: str, bucket: int):
        """Mediana di FLOTTA degli EMA nello stesso bucket (cache 60s).

        E' il termine di paragone "sensato": quanto e' normale per quella
        taglia di contesto. None se non ci sono abbastanza pari (min_peers)."""
        if bucket < 0:
            return None
        now = time.time()
        ck = (kind, bucket)
        cache = getattr(self, "_fleet_cache", None)
        if cache is None:
            cache = {}
            self._fleet_cache = cache
        c = cache.get(ck)
        if c and now - c[1] < 60.0:
            return c[0]
        table = getattr(self, "_ttft_buckets" if kind == "ttft"
                        else "_lat_buckets", None)
        vals = []
        for v in (table or {}).values():
            try:
                if v and len(v) > bucket and float(v[bucket]) > 0:
                    vals.append(float(v[bucket]))
            except (TypeError, ValueError, IndexError):
                continue
        med = None
        if len(vals) >= max(1, int(getattr(self.policy, 'slow_latency_min_peers',
                                            SLOW_LATENCY_MIN_PEERS))):
            vals.sort()
            med = vals[len(vals) // 2]
        cache[ck] = (med, now)
        return med

    def _fleet_global_median(self):
        """Mediana GLOBALE degli EMA: baseline quando il bucket non ha dati
        (es. chiamate senza ctx). Cache 60s."""
        now = time.time()
        cache = getattr(self, "_fleet_cache", None)
        if cache is None:
            cache = {}
            self._fleet_cache = cache
        c = cache.get("__global__")
        if c and now - c[1] < 60.0:
            return c[0]
        vals = [float(v) for v in (getattr(self, "_avg_latencies", {}) or {})
                .values() if v and float(v) > 0]
        med = None
        if len(vals) >= max(1, int(getattr(self.policy, 'slow_latency_min_peers',
                                            SLOW_LATENCY_MIN_PEERS))):
            vals.sort()
            med = vals[len(vals) // 2]
        cache["__global__"] = (med, now)
        return med

    def _expected_latency_ms(self, unique: str, ctx_est=None,
                             kind: str = "total"):
        """Latenza ATTESA per questa taglia: mediana di flotta del bucket,
        altrimenti mediana GLOBALE, altrimenti il bucket del dep, altrimenti
        stima dal rate (prefill + generazione)."""
        med = self._fleet_bucket_median(kind, _ctx_bucket(ctx_est))
        if med and med > 0:
            return float(med)
        gmed = self._fleet_global_median()
        if gmed and gmed > 0:
            return float(gmed)
        # NIENTE fallback all'EMA propria del dep: sarebbe baseline di se
        # stesso e "lento" non scatterebbe MAI. Senza flotta si stima dal
        # rate, altrimenti legacy 90s (vedi _slow_threshold_ms).
        try:
            cx = int(ctx_est or 0)
        except (TypeError, ValueError):
            cx = 0
        if cx <= 0:
            return None
        r = (getattr(self, "_prefill_rate", {}) or {}).get(unique)
        if not r or r <= 0:
            return None
        ttft = max(TTFT_RATE_FLOOR_MS, float(r) * cx / 1000.0)
        if kind == "ttft":
            return ttft
        g = (getattr(self, "_gen_rate", {}) or {}).get(unique)
        if g and g > 0:
            return ttft + (SLOW_TYPICAL_COMPLETION_TOKENS / float(g)) * 1000.0
        return ttft * SLOW_GEN_MULT

    def _slow_threshold_ms(self, unique: str, ctx_est=None,
                           kind: str = "total") -> float:
        """Soglia size-aware oltre la quale un dep e' "lento"."""
        base = self._expected_latency_ms(unique, ctx_est, kind)
        if not base or base <= 0:
            return float(LATENCY_ROTATE_THRESHOLD_MS)
        return max(float(SLOW_LATENCY_ABS_FLOOR_MS),
                   float(SLOW_LATENCY_REL_MULT) * float(base))

    # ---- caccia al sostituto: budget/backoff (anti-spreco) ---------------
    def hunt_allowed(self, session_id: str | None, ctx_est=None) -> bool:
        """False se per questa sessione/bucket la caccia e' in backoff (una
        caccia senza guadagno = il buono non esiste) o ha superato il cap."""
        if not session_id:
            return True
        st = (getattr(self, "_hunt_state", None) or {}).get(
            (session_id, _ctx_bucket(ctx_est)))
        if not st:
            return True
        now = time.time()
        if float(st.get("backoff_until") or 0.0) > now:
            return False
        dq = st.get("races")
        if dq:
            win = float(getattr(self.policy, "hunt_window_sec", 3600) or 3600)
            while dq and now - dq[0] > win:
                dq.popleft()
            cap = int(getattr(self.policy, "hunt_max_per_window", 5) or 0)
            if cap > 0 and len(dq) >= cap:
                return False
        return True

    def note_hunt(self, session_id: str | None, ctx_est, gained: bool) -> None:
        """Registra una caccia; senza guadagno (nessun canary migliore) mette
        in backoff per `hunt_backoff_sec`: non si ri-caccia a ogni turno su un
        modello che E' il migliore disponibile per quella taglia."""
        if not session_id:
            return
        now = time.time()
        hs = getattr(self, "_hunt_state", None)
        if hs is None:
            hs = {}
            self._hunt_state = hs
        st = hs.setdefault(
            (session_id, _ctx_bucket(ctx_est)),
            {"races": deque(), "backoff_until": 0.0})
        st["races"].append(now)
        if not gained:
            st["backoff_until"] = now + float(
                getattr(self.policy, "hunt_backoff_sec", 600) or 600)

    def _is_slow_dep(self, unique: str, ctx_est=None) -> bool:
        """True se la latenza del deployment supera la soglia di rotazione
        (LATENCY_ROTATE_THRESHOLD_MS, 90s di default).

        Con `ctx_est` noto usa l'EMA nel BUCKET DI CONTESTO giusto (un key
        velocissimo su 5k non e' velocissimo su 100k); senza dati nel bucket
        ripiega sull'EMA globale, come prima.

        Serve a NON tenere un key lento nel tier "caldi"/sticky: una latenza
        sopra soglia lo fa USCIIRE dal pool caldo e dallo sticky, ma resta
        eleggibile nel ladder come riserva (non lo mettiamo in quarantena)."""
        avg = self.bucket_latency_ms(unique, ctx_est)
        if avg is None:
            return False
        return float(avg) > self._slow_threshold_ms(unique, ctx_est)

    def is_slow_for_session(self, unique: str,
                            session_id: str | None = None,
                            ctx: int | None = None) -> bool:
        """True se QUESTA sessione ha avuto un successo LENTO su `unique`
        entro la finestra warm. Due severita':

        - HARD (TTFB > LATENCY_ROTATE_THRESHOLD_MS): vale per qualunque
          richiesta della sessione (comportamento storico);
        - SOFT (TTFB > SOFT_SLOW_LATENCY_MS su richiesta > SOFT_SLOW_CTX_MIN
          token): demotiva SOLO le richieste pesanti; ctx ignoto = non
          demotiva (il key declassato continua a servire il traffico leggero).

        Un key demotato esce da caldi/sticky/cache e dalle selezioni della
        STESSA sessione: torna pescabile solo all'ultimo scaglione
        (-fallback/ultima spiaggia). Un successo rapido ripulisce il marchio.
        Le ALTRE sessioni non sono toccate."""
        if self._key_fault_blocked(unique):
            return True
        if not getattr(self.policy, "warm_pool_enabled", True):
            return False
        sid = session_id or current_session()
        if not sid:
            return False
        m = self._sess_slow().get(sid)
        if not m:
            return False
        rec = m.get(unique)
        if rec is None:
            return False
        if isinstance(rec, tuple):
            ts, hard = rec
        else:                                   # formato storico (solo ts)
            ts, hard = rec, True
        if time.time() - ts > self._warm_ttl():
            m.pop(unique, None)
            return False
        if not hard:
            try:
                if ctx is None or int(ctx) <= SOFT_SLOW_CTX_MIN:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _note_session_slow(self, session_id: str | None, unique: str,
                           latency_ms: float | None,
                           ctx_est: int | None = None,
                           kind: str = "total") -> None:
        """Marchia (o ripulisce) `unique` come 'lento per la sessione'. Solo
        free-dims (mai -go/-fallback ne' gruppi capacita'), come la warm
        ownership: e' li' che la latenza e' un segnale utile. HARD oltre
        LATENCY_ROTATE_THRESHOLD_MS (vale sempre); SOFT tra
        SOFT_SLOW_LATENCY_MS e la soglia hard SOLO se la chiamata marcatrice
        era pesante (ctx_est > SOFT_SLOW_CTX_MIN): demotera' solo le future
        richieste pesanti della sessione.

        Novita' F1: la demote scatta solo se la chiamata e' lenta ANCHE
        RELATIVAMENTE alla baseline del dep nello STESSO bucket di contesto
        e dello STESSO tipo di misura (`kind`, > SLOW_REL_BASELINE_MULT *):
        un dep che serve abitualmente 100k in 95s non e' 'lento', e' cosi'."""
        if not session_id or not unique:
            return
        if not getattr(self.policy, "warm_pool_enabled", True):
            return
        dep = self.config.deployment_by_unique(unique)
        if dep is None:
            return
        g = dep.get("group", "")
        if self.config.group_caps.get(g) is not None \
                or self._is_renewal_bucket(g):
            return
        try:
            lat = None if latency_ms is None else float(latency_ms)
        except (TypeError, ValueError):
            lat = None
        try:
            heavy = ctx_est is not None and int(ctx_est) > SOFT_SLOW_CTX_MIN
        except (TypeError, ValueError):
            heavy = False
        d = self._sess_slow()
        # Baseline del dep nel bucket/tipo DI QUESTA chiamata (include il
        # campione corrente: l'alpha 0.3 lascia comunque vincere gli outlier).
        base = self.bucket_latency_ms(unique, ctx_est,
                                      kind="ttft" if kind == "ttft" else "total")
        relative_ok = base is None or base <= 0 or lat is None \
            or lat > SLOW_REL_BASELINE_MULT * float(base)
        _thr = self._slow_threshold_ms(unique, ctx_est, kind)
        hard = (lat is not None and lat > _thr and relative_ok)
        soft = (not hard) and lat is not None \
            and lat > max(SOFT_SLOW_LATENCY_MS, _thr * 0.6) \
            and heavy and relative_ok
        if hard or soft:
            d.setdefault(session_id, {})[unique] = (time.time(), hard)
        else:
            m = d.get(session_id)
            if m:
                m.pop(unique, None)
                if not m:
                    d.pop(session_id, None)
        if len(d) > 4096:
            _ttl = self._warm_ttl() * 4
            _now = time.time()
            for sid, m in list(d.items()):
                for u, rec in list(m.items()):
                    _ts = rec[0] if isinstance(rec, tuple) else rec
                    if _now - _ts > _ttl:
                        m.pop(u, None)
                if not m:
                    d.pop(sid, None)

    def _is_demoted_dep(self, unique: str,
                        session_id: str | None = None,
                        ctx: int | None = None,
                        allow_slow: bool = False) -> bool:
        """Dep fuori dai tier 'economici' per la sessione: EMA globale sopra
        soglia OPPURE successo lento registrato per QUESTA sessione (hard:
        sempre; soft: solo con ctx pesante > SOFT_SLOW_CTX_MIN). Resta
        eleggibile nell'ultimo scaglione (-fallback/ultima spiaggia).

        Con `allow_slow=True` (warm/sticky/holder) la LATENZA non demote: un
        successo lento va comunque registrato nel warm (cosi' la sessione lo
        conosce e la gara puo' cercargli un sostituto). Una chiave SATURA
        (soft-429/fault) resta invece SEMPRE fuori."""
        if self._key_fault_blocked(unique):
            return True
        if allow_slow:
            return False
        return (self._is_slow_dep(unique, ctx)
                or self.is_slow_for_session(unique, session_id, ctx))

    _DIM_GROUP_RE = __import__("re").compile(r"-(\d+)k$")

    def climb_dim_group(self, group_name: str | None,
                        ctx_est) -> str | None:
        """SALITA DI DIM: se il payload non entra nel gruppo `-Nk` richiesto,
        ritorna il gruppo dim PIU' PICCOLO (stesso profilo) il cui
        max_input >= ctx_est; None se nessun dim basta o il gruppo non e'
        un dim testo. Il routing poi pesca lì (il floor warm segue la nuova
        richiesta, m0204)."""
        try:
            cx = int(ctx_est or 0)
        except (TypeError, ValueError):
            return None
        if cx <= 0 or not group_name:
            return None
        m = self._DIM_GROUP_RE.search(group_name)
        if m is None or self.config.group_caps.get(group_name) is not None \
                or self._is_renewal_bucket(group_name):
            return None
        head = group_name[:m.start()]
        cur_mx = 0
        best = None
        for g, deps in (self.config.groups or {}).items():
            if self.config.group_caps.get(g) is not None \
                    or self._is_renewal_bucket(g):
                continue
            mm = self._DIM_GROUP_RE.search(g)
            if mm is None or g[:mm.start()] != head:
                continue
            try:
                mx = max((int(d.get("max_input_tokens") or 0) for d in deps),
                         default=0)
            except (TypeError, ValueError):
                continue
            if g == group_name:
                cur_mx = mx
            if mx >= cx and (best is None or mx < best[1]):
                best = (g, mx)
        if best and best[0] != group_name and best[1] > cur_mx:
            return best[0]
        return None

    def _warm_allow_slow(self) -> bool:
        return bool(getattr(self.policy, "warm_pool_allow_slow", True))

    def first_content_deadline_ms(self, unique: str, ctx_est=None) -> int:
        """Finestra d'attesa del primo contenuto per `unique`.

        Fissa se `stream_first_content_adaptive` e' False; altrimenti:
            min(cap, max(floor, mult * TTFT_EMA_del_bucket))
        con `cap = stream_first_content_ms`. Il segnale e' il TEMPO AL PRIMO
        CONTENUTO (cio' che il peek aspetta davvero), nel bucket di contesto
        della richiesta; senza campioni TTFT ripiega sull'EMA globale, EMA
        ignota/0 -> cap. Cosi' un dep normalmente veloce che stalla ruota
        presto, mentre un dep lento mantiene un margine proporzionato (mai
        oltre il cap)."""
        qcp = getattr(self.policy, "qc_json", None)
        cap = max(2000, int(getattr(qcp, "stream_first_content_ms", 20000)
                            or 20000))
        if not bool(getattr(qcp, "stream_first_content_adaptive", True)):
            return cap
        ema = float(self.bucket_latency_ms(unique, ctx_est,
                                           kind="ttft") or 0.0)
        if ema <= 0:
            return cap
        mult = float(getattr(qcp, "stream_first_content_mult", 3.0) or 3.0)
        floor = min(int(getattr(qcp, "stream_first_content_floor_ms", 20000)
                        or 0), cap)
        return max(2000, min(cap, max(floor, int(ema * mult))))

    def hedge_delay_ms(self, unique: str, ctx_est=None) -> int:
        """Ritardo del canary HEDGE calibrato sul bucket di contesto.

        Su contesti grandi il TTFT fisiologico e' di secondi: lanciare il
        canary a un valore fisso (1500ms) e' rumore — si pagherebbe un
        tentativo in piu' quasi su ogni heavy. Formula:
            clamp(TTFT_bucket * frac, min_ms, max_ms)
        Senza stima TTFT vale il valore fisso `stream_hedge_delay_ms`
        (0 = hedge spento del tutto)."""
        qcp = getattr(self.policy, "qc_json", None)
        base = int(getattr(qcp, "stream_hedge_delay_ms", 0) or 0)
        if base <= 0:
            return 0
        try:
            frac = float(getattr(qcp, "stream_hedge_ttft_frac", 0.6) or 0.6)
            lo = int(getattr(qcp, "stream_hedge_min_ms", 800) or 800)
            hi = int(getattr(qcp, "stream_hedge_max_ms", 2500) or 2500)
        except (TypeError, ValueError):
            frac, lo, hi = 0.6, 800, 2500
        if hi < lo:
            hi = lo
        ttft = float(self.bucket_latency_ms(unique, ctx_est,
                                           kind="ttft") or 0.0)
        if ttft <= 0:
            return base
        return max(lo, min(hi, int(ttft * frac)))

    # --------------------------------------------------- auto-learn capacità
    _CAP_STRIKE_WINDOW_SEC = 7 * 86400   # strike più vecchi di 7gg si azzerano

    def note_cap_strike(self, model: str, caps, evidence: str) -> list[str]:
        """Registra un rifiuto modalità per (model, cap). Ritorna le cap che
        hanno raggiunto policy.cap_auto_learn_threshold nella finestra."""
        now = time.time()
        thr = max(1, int(self.policy.cap_auto_learn_threshold))
        hit: list[str] = []
        for cap in caps:
            key = f"{model}|{cap}"
            st = self._cap_strikes.get(key)
            if st is None or now - st["last"] > self._CAP_STRIKE_WINDOW_SEC:
                st = {"count": 0, "first": now}
                self._cap_strikes[key] = st
            st["count"] = int(st.get("count", 0)) + 1
            st["last"] = now
            st["evidence"] = (evidence or "")[:200]
            if st["count"] >= thr:
                hit.append(cap)
        return hit

    def cap_strikes_view(self) -> list[dict]:
        """Snapshot serializzabile degli strike (per /admin/state e TUI)."""
        out = []
        for key, st in sorted(self._cap_strikes.items()):
            model, _, cap = key.partition("|")
            out.append({"model": model, "cap": cap,
                        "count": int(st.get("count", 0)),
                        "first": st.get("first"), "last": st.get("last"),
                        "evidence": st.get("evidence", "")})
        return out

    def _pref_for(self, unique: str) -> int:
        """model_preference del deployment (0 se non disponibile)."""
        try:
            dep = self.config.deployment_by_unique(unique)
        except Exception:  # noqa: BLE001
            dep = None
        if not dep:
            return 0
        try:
            return int(dep.get("model_preference") or 0)
        except (TypeError, ValueError):
            return 0

    def _pref_cooldown_factor(self, raw_full: float, pref: int) -> float:
        """Fattore >0 da applicare alla durata GREZZA del cooldown: >1 allunga
        (preferenza negativa = riposa di piu'), <1 accorcia (positiva = ritenta
        prima). Sotto i 60s (dato grezzo) nessuna modifica. Ampiezza max
        +/-50%, pari a preferenza/2."""
        if raw_full <= 60.0:
            return 1.0
        pct = max(-50.0, min(50.0, float(pref) / 2.0))
        return 1.0 - pct / 100.0

    def _effective_cooldown_full(self, unique: str, full: float) -> float:
        """Durata efficace = durata grezza scalata dalla preferenza del
        deployment, clampata a [1, max_cooldown_sec]. ECCEZIONE: i cooldown di
        QUOTA (giornaliera/mensile) seguono il RESET dichiarato o stimato
        (mezzanotte UTC / "Resets in 9 days") e non vanno tagliati dal ceiling
        operatore, altrimenti la chiave torna prima del reset e si riprova a
        vuoto (osservato: quota CF giornaliera tagliata a 5h)."""
        factor = self._pref_cooldown_factor(full, self._pref_for(unique))
        _pol = getattr(self, "policy", None)
        _mx = float(getattr(_pol, "max_cooldown_sec", 18000) or 18000)
        _s = self._stats.get(unique)
        _rs = str(getattr(_s, "last_reason", "") or "")
        if _rs.startswith("quota_exhausted"):
            _mx = max(_mx, 7 * 86400.0)     # QUOTA_MAX_COOLDOWN_S (7 giorni)
        return max(1.0, min(_mx, full * factor))

    def cooldown_residual(self, unique: str) -> float:
        """Residuo (secondi) del cooldown RESIDUO, scalato dalla preferenza del
        deployment. Lo storage resta GREZZO: qui si applica solo il fattore."""
        exp = self._cooldown.get(unique)
        if exp is None:
            return 0.0
        now = time.time()
        since = self._cooldown_since.get(unique)
        full = self._cooldown_full_map().get(unique) or 0.0
        if full > 0 and since is not None:
            return max(0.0, since + self._effective_cooldown_full(unique, full)
                       - now)
        return max(0.0, exp - now)

    def is_cooled_down(self, unique: str) -> bool:
        exp = self._cooldown.get(unique)
        if exp is None:
            return False
        if self.cooldown_residual(unique) > 0.0:
            return True
        # Residuo efficace esaurito: pota l'entry solo se anche il grezzo e'
        # passato; se il cooldown e' stato accorciato dalla preferenza lascio
        # il dato grezzo fino alla sua scadenza.
        if time.time() > exp:
            self._cooldown.pop(unique, None)
            self._cooldown_since.pop(unique, None)
            self._cooldown_full_map().pop(unique, None)
        return False

    def cooldown_age(self, unique: str) -> float | None:
        """Da quanti secondi e' in cooldown questo deployment (None se non lo e'
        o se non lo sappiamo)."""
        since = self._cooldown_since.get(unique)
        return (time.time() - since) if since is not None else None

    def cooldown_progress(self, unique: str) -> float | None:
        """Frazione di cooldown TRASCORSA (0..1); None se non in cooldown o se
        la durata totale non e' nota."""
        if self._cooldown.get(unique) is None:
            return None
        full = self._cooldown_full_map().get(unique) or 0.0
        if full <= 0:
            return None
        resid = self.cooldown_residual(unique)
        if resid <= 0.0:
            return 1.0
        eff = self._effective_cooldown_full(unique, full)
        return max(0.0, min(1.0, 1.0 - resid / max(1e-6, eff)))

    def _cooldown_full_map(self) -> dict:
        """Mappa unique -> durata totale del cooldown (lazy: alcuni test
        costruiscono il Router via __new__ senza inizializzarla)."""
        m = getattr(self, "_cooldown_full", None)
        if m is None:
            m = {}
            self._cooldown_full = m
        return m

    def probe_ready(self, unique: str) -> bool:
        """True se un deployment dormiente e' maturo per un probe passivo
        (>= cooldown_probe_after_ratio del cooldown trascorso)."""
        if not getattr(self.policy, "cooldown_probe_enabled", True):
            return False
        ratio = float(getattr(self.policy, "cooldown_probe_after_ratio",
                              0.5) or 0.0)
        pr = self.cooldown_progress(unique)
        return pr is not None and pr >= ratio

    def _cooldown_decay(self, unique: str) -> float:
        """Fattore di DECADIMENTO della penalita' durante il cooldown:
        1.0 = penalita' piena (appena messo), 0.0 = neutra (cooldown finito).
        Con `cooldown_probe_decay` off ritorna sempre 1.0."""
        if not getattr(self.policy, "cooldown_probe_decay", True):
            return 1.0
        pr = self.cooldown_progress(unique)
        if pr is None:
            return 1.0
        return max(0.0, 1.0 - pr)

    def _decay_streak(self, streak: int, last_fail_ts: float,
                      now: float | None = None) -> int:
        """Decadimento del fail_streak per inattivita' (halflife).

        Dopo `cooldown_streak_halflife_sec` senza fallimenti lo streak si
        dimezza, cosi' una chiave riattivata dopo ore non viene riesiliata
        per un singolo errore isolato. 0 = nessun decadimento."""
        if streak <= 0:
            return 0
        hl = float(getattr(self.policy, "cooldown_streak_halflife_sec", 0) or 0)
        if hl <= 0 or not last_fail_ts:
            return streak
        now = time.time() if now is None else now
        elapsed = max(0.0, now - float(last_fail_ts))
        if elapsed <= 0:
            return streak
        decayed = int(round(streak * (0.5 ** (elapsed / hl))))
        decayed = max(0, min(streak, decayed))
        if decayed < streak:
            log.info("[streak] %d -> %d dopo %.0f min di inattivita'",
                     streak, decayed, elapsed / 60.0)
        return decayed

    def _jitter_spread(self, unique: str) -> float:
        """Spread DETERMINISTICO per-unique (0..cooldown_jitter_sec_max s).

        Anti-herd: i gemelli che prendono 429 nello stesso secondo scadono
        spalmati su ~2s invece che al medesimo millisecondo (altrimenti al
        secondo N ripartono tutti insieme -> nuova raffica di 429). E' una
        funzione pura di `unique`: stabile tra restart, niente random."""
        cap = float(getattr(self.policy, "cooldown_jitter_sec_max", 2.0) or 0.0)
        if cap <= 0:
            return 0.0
        h = hashlib.sha256(str(unique).encode("utf-8")).digest()
        return (int.from_bytes(h[:4], "big") % 2000) / 1000.0 * (cap / 2.0)

    def _apply_jitter(self, seconds: float, unique: str | None = None) -> float:
        """Jitter sul cooldown: componente ADDITIVA deterministica per-unique
        (0..cooldown_jitter_sec_max s) + eventuale componente random
        moltiplicativa (`cooldown_jitter_ratio`, default 0 = disattivata).

        Lo spread additivo spalma la scadenza dei gemelli che incassano 429
        nello stesso secondo: senza, ripartono tutti al medesimo ms e
        rifanno raffica."""
        sec = max(1.0, float(seconds))
        ratio = float(getattr(self.policy, "cooldown_jitter_ratio", 0.0) or 0.0)
        if ratio > 0:
            sec = max(1.0, sec * random.uniform(1.0 - ratio, 1.0 + ratio))
        if unique:
            sec += self._jitter_spread(unique)
        return max(1.0, sec)

    def _maybe_retire_on_probe_fail(self, unique: str, s) -> bool:
        """Auto-retirement dopo N probe passivi consecutivi falliti: se il
        problema non e' temporaneo (credenziali/modello morti) smettiamo di
        sprecare probe. Il CSV non viene toccato (unretire manuale o probe ok).

        Un'evidenza di QUOTA (429/satura) non basta a ritirare: la chiave e'
        viva, ha solo finito il budget del momento. Il ritiro scatta solo su
        fallimenti sostanziali (401/403/modello morto/5xx permanenti)."""
        cap = int(getattr(self.policy, "probe_retire_after", 0) or 0)
        if cap <= 0 or s.probe_fail_streak < cap:
            return False
        if _is_quota_evidence(getattr(s, "last_reason", None)):
            log.debug("[probe] %s non ritirato: ultima evidenza di quota "
                      "(%s)", unique, s.last_reason)
            return False
        try:
            from . import main as _gw_mod      # lazy: evita cicli d'import
            kh = getattr(_gw_mod, "KEYHEALTH", None)
            if kh is not None and not kh.is_retired(unique):
                kh.set_state(unique, "retired",
                             reason="probe_escalation_cap")
                kh.save()
                log.warning("[probe] %s RETIRED: %d probe consecutivi falliti "
                            "(problema permanente, non temporaneo)",
                            unique, s.probe_fail_streak)
                return True
        except Exception:                      # mai bloccare il routing
            log.debug("[probe] auto-retirement di %s fallito", unique,
                      exc_info=True)
        return False

    def is_retired(self, unique: str) -> bool:
        """Chiave RETIRED (lifecycle keyhealth): esclusa dal routing.

        NON e' una cancellazione: il CSV resta intatto; si sblocca con
        POST /admin/deployments/unretire o con un probe riuscito.
        """
        try:
            from . import main as _gw_mod      # lazy: evita cicli d'import
            kh = getattr(_gw_mod, "KEYHEALTH", None)
            return bool(kh and kh.is_retired(unique))
        except Exception:                      # mai bloccare il routing
            return False

    def _retired_permanent(self, unique: str) -> bool:
        """Ritirato per motivo PERMANENTE: mai riusabile, nemmeno in ultima
        spiaggia (spam di errori inutili su una chiave/modello morti)."""
        try:
            from . import main as _gw_mod      # lazy: evita cicli d'import
            kh = getattr(_gw_mod, "KEYHEALTH", None)
            return bool(kh and kh.is_permanently_retired(unique))
        except Exception:                      # mai bloccare il routing
            return False

    def _retired_usable(self, unique: str) -> bool:
        """Ritirato NON permanente (quota/probe-cap): fuori dai tier normali,
        eleggibile SOLO come ultima spiaggia. Un successo lo ripulisce dal
        lifecycle (successo = prova di vita), senza spendere probe."""
        return self.is_retired(unique) and not self._retired_permanent(unique)

    def _prune_wake_times(self, now: float | None = None) -> None:
        """Pota i timestamp di wakeup oltre la finestra (memoria)."""
        now = now if now is not None else time.time()
        _win = max(1.0, float(getattr(
            self.policy, "ladder_cooldown_wakeup_window_sec", 3600) or 3600))
        for u, dq in list((getattr(self, "_wake_times", None) or {}).items()):
            while dq and now - dq[0] > _win:
                dq.popleft()
            if not dq:
                getattr(self, "_wake_times", {}).pop(u, None)

    def _prune_hunt_state(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        win = float(getattr(self.policy, "hunt_window_sec", 3600) or 3600)
        for k, st in list((getattr(self, "_hunt_state", None) or {}).items()):
            dq = st.get("races")
            while dq and now - dq[0] > win:
                dq.popleft()
            if not dq and float(st.get("backoff_until") or 0.0) <= now:
                getattr(self, "_hunt_state", {}).pop(k, None)
        if len(self._fleet_cache) > 4096:
            self._fleet_cache.clear()

    def purge_expired(self) -> tuple[int, int]:
        """Rimuove sticky scadute e cooldown espirati (chiamato dal watcher).

        Senza purge, con tanti session_id unici, i dict crescerebbero senza
        limite sugli uptime lunghi. Ritorna (sticky_rimosse, cooldown_rimossi).
        
        [Blocco 1] Ora include anche la pulizia di _stats (memory leak fix):
        rimuove entry stale (>48h) per prevenire crescita infinita della memoria.
        """
        self._prune_hunt_state()
        self._prune_wake_times()
        now = time.time()
        self._decay_scores(now)     # time-decay reputazione (halflife policy)
        dead_sessions = [s for s, (_t, ts) in self._sticky.items()
                         if now - ts > self.policy.sticky_ttl_sec]
        for s in dead_sessions:
            self._sticky.pop(s, None)
        dead_sg = [s for s, (_g, ts) in self._session_group.items()
                   if now - ts > self.policy.sticky_ttl_sec]
        for s in dead_sg:
            self._session_group.pop(s, None)
        # PURGE deployment-sticky: stessa TTL dello sticky di gruppo
        dead_dep = [s for s, (_u, ts) in self._sticky_dep.items()
                    if now - ts > self.policy.sticky_ttl_sec]
        for s in dead_dep:
            self._sticky_dep.pop(s, None)
        # SESSION-DEP GUARD: entry piu' vecchi della finestra (x2) non servono.
        _gttl = self._guard_sec() * 2
        _ds = self._dep_sess()
        for u in [u for u, (_s, ts) in _ds.items() if now - ts > _gttl]:
            _ds.pop(u, None)
        # Indice session -> dep posseduti: tieni solo i dep ancora tracciati.
        _sd = self._sess_deps()
        for s in list(_sd):
            live = {u for u in _sd[s] if u in _ds}
            if live:
                _sd[s] = live
            else:
                _sd.pop(s, None)
        # AUDIT prefisso: le impronte vecchie come la guard non servono.
        _pf = getattr(self, "_prefix_fp", None)
        if isinstance(_pf, dict):
            for s in [s for s, (_b, _y, t) in _pf.items() if now - t > _gttl]:
                _pf.pop(s, None)
        # SOFT-PER-CHIAVE: hint scaduti (ttl x2) e blocchi oltre la scadenza.
        _kh = getattr(self, "_key_hints", None)
        if isinstance(_kh, dict):
            _kttl = max(120.0, float(getattr(self.policy, "rate_hint_ttl_sec",
                                             20.0) or 20.0) * 2)
            for tag in [t for t, (ts, _r) in _kh.items()
                        if now - ts > _kttl]:
                _kh.pop(tag, None)
        _ks = getattr(self, "_key_soft", None)
        if isinstance(_ks, dict):
            for tag in [t for t, until in _ks.items() if until <= now]:
                _ks.pop(tag, None)
        # F25: breaker di modello — dimentica le aperture molto scadute.
        _mcb = getattr(self, "_model_cb", None)
        if isinstance(_mcb, dict):
            _mttl = max(300.0, float(getattr(self.policy,
                                             "model_circuit_open_sec",
                                             60) or 60) * 4)
            for k in [k for k, e in _mcb.items()
                      if now - max(e.get("opened") or 0.0,
                                   e.get("ts") or 0.0) > _mttl]:
                _mcb.pop(k, None)
        dead_cd = [u for u, exp in self._cooldown.items() if now > exp]
        for u in dead_cd:
            self._cooldown.pop(u, None)
            self._cooldown_since.pop(u, None)
            self._cooldown_full_map().pop(u, None)
        # PURGE escalation-winner: TTL a finestra scorrevole; gli entries
        # vecchi di escalation_pin_ttl_sec vengono droppati.
        _epp = max(1, int(getattr(self.policy, "escalation_pin_ttl_sec",
                                  300) or 300))
        _ewd = self._esc()
        dead_ew = [g for g, (_u, ts) in _ewd.items()
                   if now - ts > _epp]
        for g in dead_ew:
            _ewd.pop(g, None)
        # [Blocco 1] Cleanup _stats: rimuovi entry vecchie di 48h (fix memoria)
        stale_stats = [u for u, s in self._stats.items()
                       if now - s.last_used > 172800]  # 48h
        for u in stale_stats:
            del self._stats[u]
        # [Blocco 1] Cleanup _cap_strikes: rimuovi strike vecchi di 7gg
        stale_strikes = [k for k, st in self._cap_strikes.items()
                         if now - st.get("last", 0) > 604800]  # 7gg
        for k in stale_strikes:
            del self._cap_strikes[k]
        # [Blocco 1] Cleanup scoring: rimuovi punteggi dei deployment non più attivi.
        # Difensivo: alcuni test usano Router.__new__ senza config né attributi
        # di scoring inizializzati: in quel caso si salta la pulizia.
        try:
            self._init_scoring_if_needed()
            active_uniques = (set(self.config.all_uniques())
                              if hasattr(self.config, 'all_uniques') else set())
            if not active_uniques:
                # Fallback: raccogli tutti gli unique dai gruppi
                for deps in self.config.groups.values():
                    for d in deps:
                        active_uniques.add(d["unique"])
            stale_base = [u for u in self._base_scores if u not in active_uniques]
            for u in stale_base:
                self._base_scores.pop(u, None)
                self._avg_latencies.pop(u, None)
                getattr(self, "_lat_buckets", {}).pop(u, None)
                getattr(self, "_ttft_buckets", {}).pop(u, None)
                getattr(self, "_prefill_rate", {}).pop(u, None)
                getattr(self, "_est_div", {}).pop(u, None)
            # Cleanup provider/key scores vecchi: mantieni solo chiavi attive
            active_providers = set()
            active_keys = set()
            for deps in self.config.groups.values():
                for d in deps:
                    active_providers.add(self._provider_key(d))
                    active_keys.add(self._api_key_str(d))
            stale_prov = [k for k in self._provider_scores if k not in active_providers]
            for k in stale_prov:
                self._provider_scores.pop(k, None)
            stale_keys = [k for k in self._key_scores if k not in active_keys]
            for k in stale_keys:
                self._key_scores.pop(k, None)
        except Exception as exc:               # noqa: BLE001
            log.debug("[purge] scoring cleanup saltato (%s)", exc)
        if dead_sessions or dead_cd or dead_sg or dead_dep:
            log.debug("[purge] sticky=%d cooldown=%d sessioni=%d dep_sticky=%d",
                      len(dead_sessions), len(dead_cd), len(dead_sg),
                      len(dead_dep))
        if stale_stats or stale_strikes:
            log.debug("[purge] stats=%d strikes=%d cleaned",
                      len(stale_stats), len(stale_strikes))
        return len(dead_sessions), len(dead_cd)

    # ------------------------------------------------------------- routing
    def is_explicit(self, name: str) -> bool:
        """True se 'name' è una richiesta ESPLICITA: gruppo (-Nk/-go/-fallback)
        o deployment univoco (__). Le richieste esplicite NON leggono e NON
        scrivono la sticky session: il client ha chiesto quello specifico."""
        if "__" in name:
            return True
        return any(name.endswith(s) for s in self.config.known_suffixes())

    # -------------------------------------------------------- capability helpers
    def _dep_supports(self, dep: dict, need: frozenset[str]) -> bool:
        """True se il deployment dichiara tutte le capacità richieste.

        Dichiarazione = UNIONE di (a) mappa model_capabilities (metadato/
        advisory) e (b) membership strutturale dalla colonna caps del CSV:
        nell'architettura a gruppi è l'appartenenza la fonte di verità."""
        if not need:
            return True
        declared = self.policy.caps_for(dep.get("model", "")) \
            | (dep.get("caps") or frozenset())
        return need.issubset(declared)

    # token che rendono un modello "multimodale" ai fini della protezione
    # free-tier (multimodal_last_resort): input media + cap generativi.
    MEDIA_TOKENS = frozenset(
        {"vision", "video", "audio", "image_gen", "video_gen", "tts", "stt"})

    # gruppi dove il FALLBACK deve preferire lo stesso modello upstream:
    # cambiare voce (tts/stt) o generare immagini/video con un modello
    # diverso inatteso rompe la coerenza dell'output. Altri modelli sono
    # ammessi solo a esaurimento degli same-model, con log + contatore.
    SAME_MODEL_PRIORITY_CAPS = frozenset(
        {"image_gen", "video_gen", "tts", "stt"})

    def _is_media_capable(self, dep: dict) -> bool:
        """True se il deployment accetta/produce media (union mappa+caps)."""
        declared = self.policy.caps_for(dep.get("model", "")) \
            | (dep.get("caps") or frozenset())
        return bool(declared & self.MEDIA_TOKENS)

    def _is_deferrable(self, dep: dict) -> bool:
        """True se il deployment partecipa al MEDIA DEFER (multimodal_last_resort):
        multimodale E non esentato dalla colonna CSV `media_defer`.
        media_defer=false -> il deployment resta eleggibile per il testo puro
        senza toccare le sue capacità reali (caps intatti)."""
        return (self._is_media_capable(dep)
                and bool(dep.get("media_defer", True)))

    def _prefer_same_model(self, cap: str | None, cur_model: str) -> bool:
        """True se il fallback da questo gruppo cap deve PRIORIZZARE (non
        escludere) gli altri deployment dello stesso modello upstream."""
        return (self.policy.gen_same_model_failover and cap is not None
                and cap in self.SAME_MODEL_PRIORITY_CAPS and bool(cur_model))

    def _note_cross(self, group_name: str, from_model: str,
                    to_model: str) -> None:
        """Attraversamento verso un modello DIVERSO nei gruppi gen/stt:
        sempre loggato + contato (osservabilità del 'cambio voce/stile')."""
        self.gen_cross_model[group_name] = \
            self.gen_cross_model.get(group_name, 0) + 1
        log.warning("[fallback] %s CROSS-MODEL %s -> %s",
                    group_name, from_model, to_model)

    def _defer_media(self, group_name: str, need: frozenset[str] | None,
                     deps: list[dict]) -> tuple[list[dict], bool]:
        """MULTIMODAL LAST RESORT (hard, tutti i tier): in un gruppo DIMS le
        richieste pure-testo non devono cadere su modelli con input media
        finché esiste almeno un text-only vivo. Ritorna (pool, deferito?).

        Non si applica a: richieste media (need con token media), gruppi cap
        (-vision ecc.: lì il multimodale È lo scopo), pool tutto-multimodale,
        regola disattivata in policy."""
        if not self.policy.multimodal_last_resort:
            return deps, False
        if need and not need.isdisjoint(self.MEDIA_TOKENS):
            return deps, False                      # richiesta media: mai
        if self.config.group_caps.get(group_name) is not None:
            return deps, False                      # gruppo cap: mai
        text_only = [d for d in deps if not self._is_deferrable(d)]
        if text_only and len(text_only) < len(deps):
            self.media_deferred[group_name] = \
                self.media_deferred.get(group_name, 0) + 1
            if not self._defer_active.get(group_name):
                log.info("[defer] %s: quota protetta, scartati %d "
                         "multimodali (%d text-only attivi)",
                         group_name, len(deps) - len(text_only),
                         len(text_only))
                self._defer_active[group_name] = True
            else:
                log.debug("[defer] %s: scartati %d multimodali",
                          group_name, len(deps) - len(text_only))
            return text_only, True
        if self._defer_active.pop(group_name, None):
            log.info("[defer] %s: multimodali di nuovo eleggibili",
                     group_name)
        return deps, False

    def _capable_dims(self, pname: str, need: frozenset[str]) -> list[int]:
        """Dimensioni (in k) che hanno almeno un deployment CAPACE (cooldown
        ignorato: è responsabilità di pick_deployment/fallback_after saltare
        i deployment temporaneamente giù, con last-resort finale)."""
        if not need:
            return self.config.profile_dims.get(pname, [])
        cfg = self.config
        dims = cfg.profile_dims.get(pname, [])
        capable: list[int] = []
        for d in dims:
            gname = f"{cfg.proxy_prefix}{pname}-{d}k"
            for dep in cfg.groups.get(gname, []):
                if self._dep_supports(dep, need):
                    capable.append(d)
                    break
        return capable

    def _any_capable_in_group(self, group_name: str, need: frozenset[str]) -> bool:
        """True se il gruppo ha almeno un deployment CAPACE (cooldown ignorato)."""
        if not need:
            return True
        for dep in self.config.groups.get(group_name, []):
            if self._dep_supports(dep, need):
                return True
        return False

    # --------------------------------------------------- rotazione adattiva
    def stats_for(self, unique: str) -> DepStats:
        s = self._stats.get(unique)
        if s is None:
            s = DepStats()
            self._stats[unique] = s
        return s

    # ------------------------------------------ ESENZIONE RIPARAZIONE (P0)
    # Gli errori della famiglia "riparazione" (reasoning/schema/format) NON
    # sono segnali di salute del deployment: si ruota senza cooldown. Ma
    # l'esenzione e' LIMITATA: dopo `repair_exempt_streak_limit` fallimenti
    # CONSECUTIVI dello stesso dep si torna al trattamento normale (cooldown),
    # altrimenti un dep che risponde sempre con quell'errore non verrebbe
    # mai messo da parte. Un successo azzera lo streak.
    def _repair_exempt(self) -> dict:
        d = getattr(self, "_repair_exempt_map", None)
        if d is None:
            d = self._repair_exempt_map = {}
        return d

    def note_repair_exempt(self, unique: str | None) -> int:
        """Registra un fallimento ESENTE (riparato senza penale) e ritorna lo
        streak consecutivo aggiornato."""
        if not unique:
            return 0
        m = self._repair_exempt()
        m[unique] = int(m.get(unique, 0)) + 1
        return m[unique]

    def repair_exempt_blocked(self, unique: str | None,
                              limit: int = 3) -> bool:
        """True quando lo streak ha ESAURITO il budget di esenzione: da qui in
        poi il fallimento va trattato come KO normale."""
        if not unique or int(limit) <= 0:
            return False
        return int(self._repair_exempt().get(unique, 0)) >= int(limit)

    def reset_repair_exempt(self, unique: str | None) -> None:
        if unique:
            self._repair_exempt().pop(unique, None)

    # -------------------------------- FINESTRA DI FALLIMENTO DEL MODELLO (P1-4)
    # 3 KO (non esenti) entro `model_fail_window_sec` sullo stesso MODELLO —
    # anche su chiavi DIVERSE — mettono il modello in pausa su TUTTE le sue
    # chiavi per `model_fail_cooldown_sec`: le chiavi gemelle non ripescano un
    # modello malato. Un successo azzera la finestra del modello.
    def _model_fail_win(self) -> dict:
        d = getattr(self, "_model_fail_win_map", None)
        if d is None:
            d = self._model_fail_win_map = {}
        return d

    def note_model_failure(self, dep_or_unique) -> int:
        """Registra un KO normale e, alla soglia, bencha il MODELLO su tutte
        le sue chiavi. Ritorna il numero di KO nella finestra (post-soglia: la
        soglia stessa)."""
        if isinstance(dep_or_unique, dict):
            dep = dep_or_unique
            u = dep.get("unique")
        else:
            u = dep_or_unique
            dep = self.config.deployment_by_unique(u) if u else None
        model = (dep or {}).get("model")
        thr = max(0, int(getattr(self.policy, "model_fail_threshold", 3) or 0))
        if not u or not model or thr <= 0:
            return 0
        win = float(getattr(self.policy, "model_fail_window_sec", 900) or 900)
        now = time.time()
        dq = self._model_fail_win().setdefault(model, deque())
        dq.append(now)
        while dq and (now - dq[0]) > win:
            dq.popleft()
        if len(dq) < thr:
            return len(dq)
        cd = float(getattr(self.policy, "model_fail_cooldown_sec", 600) or 600)
        n = 0
        for _lst in self.config.groups.values():
            for d in _lst:
                if d.get("model") != model:
                    continue
                try:
                    if self.cooldown_residual(d["unique"]) > cd:
                        continue          # mai accorciare un bench piu' lungo
                    self.mark_failed(d["unique"], seconds=cd,
                                     reason="model_unhealthy")
                    n += 1
                except Exception:                          # noqa: BLE001
                    pass
        self._model_fail_win().pop(model, None)
        try:
            log.warning("[model-bench] %s: %d KO in %.0fs -> modello in pausa "
                        "%.0fs su %d chiavi", model, thr, win, cd, n)
        except Exception:                                  # noqa: BLE001
            pass
        return thr

    def note_model_success(self, unique: str | None) -> None:
        """Un successo reale azzera la finestra di fallimento del modello."""
        if not unique:
            return
        try:
            dep = self.config.deployment_by_unique(unique)
            m = (dep or {}).get("model")
            if m:
                self._model_fail_win().pop(m, None)
        except Exception:                                  # noqa: BLE001
            pass

    # ------------------------------------------------------- DEGRADED MODE (P1)
    # "Rete degradata": se la quota di HOST sani scende sotto
    # `degraded_healthy_ratio` per `degraded_entry_grace_sec`, sospendiamo
    # l'ESPLORAZIONE (cascata refill, hedge canary, hunt, sveglia): durante un
    # blackout upstream quelle richieste speculative bruciano rate-limit e
    # chiavi senza portare a casa nulla. Resta la rotazione normale della
    # ladder (il servizio deve rispondere). Uscita solo dopo
    # `degraded_exit_grace_sec` SOPRA soglia (anti-flap).
    def _degraded_state(self) -> dict:
        st = getattr(self, "_degraded_st", None)
        if st is None:
            st = self._degraded_st = {"since": None, "healthy": None,
                                      "active": False}
        return st

    @staticmethod
    def _dep_host(dep: dict) -> str:
        raw = (dep or {}).get("api_base") or (dep or {}).get("endpoint") or ""
        try:
            return urllib.parse.urlparse(raw).hostname or ""
        except Exception:                                  # noqa: BLE001
            return ""

    def hosts_health(self) -> tuple[int, int]:
        """(host sani, host totali). Un host e' sano se HA almeno un
        deployment utilizzabile ADESSO (non cooled, non quarantenato, non
        retiring/draining)."""
        tot: set[str] = set()
        ok: set[str] = set()
        try:
            for lst in self.config.groups.values():
                for d in lst:
                    u = d.get("unique")
                    h = self._dep_host(d)
                    if not u or not h:
                        continue
                    tot.add(h)
                    if (u in ok or self.is_cooled_down(u)
                            or self.is_retired(u)
                            or self._endpoint_quarantined(d)):
                        continue
                    try:
                        if self.is_draining(u):
                            continue
                    except Exception:                      # noqa: BLE001
                        pass
                    ok.add(h)
        except Exception:                                  # noqa: BLE001
            return 0, 0
        return len(ok), len(tot)

    def degraded_active(self) -> bool:
        """Valuta la macchina a stati (con grace) e ritorna True se siamo in
        modalita' degradata. Non ha effetti collaterali oltre ai log di
        transizione."""
        try:
            p = self.policy
            if not bool(getattr(p, "degraded_mode_enabled", True)):
                return False
            ratio_thr = float(getattr(p, "degraded_healthy_ratio", 0.5) or 0.0)
            min_prov = int(getattr(p, "degraded_min_providers", 3) or 0)
            entry = max(0.0, float(getattr(p, "degraded_entry_grace_sec",
                                            60) or 0.0))
            exitg = max(0.0, float(getattr(p, "degraded_exit_grace_sec",
                                            120) or 0.0))
        except Exception:                                  # noqa: BLE001
            return False
        if ratio_thr <= 0:
            return False
        h, t = self.hosts_health()
        st = self._degraded_state()
        if t < min_prov or t <= 0:
            st["since"] = None
            st["healthy"] = None
            st["active"] = False
            return False
        ratio = h / float(t)
        now = time.time()
        if ratio < ratio_thr:
            st["healthy"] = None
            if st["since"] is None:
                st["since"] = now
            if not st["active"] and (now - st["since"]) >= entry:
                st["active"] = True
                log.warning("[degraded] host sani %d/%d (%.0f%% < %.0f%%): "
                            "sospendo cascata/hedge/hunt finche' la rete non "
                            "si riprende", h, t, ratio * 100.0, ratio_thr * 100.0)
        else:
            st["since"] = None
            if st["active"]:
                if st["healthy"] is None:
                    st["healthy"] = now
                # exit grace 0 = esce subito (il tempo di grazia e' gia'
                # trascorso o non e' richiesto); altrimenti serve che la
                # soglia resti superata per `degraded_exit_grace_sec`.
                if exitg <= 0.0 or (now - st["healthy"]) >= exitg:
                    st["active"] = False
                    st["healthy"] = None
                    log.info("[degraded] host sani %d/%d -> riprendo "
                             "l'esplorazione", h, t)
            else:
                st["healthy"] = None
        return bool(st["active"])

    def degraded_view(self) -> dict:
        st = self._degraded_state()
        h, t = self.hosts_health()
        return {"active": bool(st.get("active")), "hosts_healthy": h,
                "hosts_total": t,
                "ratio": (round(h / float(t), 3) if t else None),
                "since": st.get("since")}

    # ------------------------------------------------- LEASE PER CHIAVE (P2)
    def _key_leases(self) -> dict:
        d = getattr(self, "_key_leases_map", None)
        if d is None:
            d = self._key_leases_map = {}
        return d

    def _lease_max_age(self) -> float:
        return max(1.0, float(getattr(
            self.policy, "key_concurrency_lease_max_age_sec", 120) or 120))

    def _prune_key_leases(self, now: float | None = None) -> None:
        """Scarta le lease piu' vecchie del tetto (una richiesta interrotta
        non deve saturare la chiave per sempre)."""
        now = time.time() if now is None else now
        age = self._lease_max_age()
        m = self._key_leases()
        for k in list(m):
            fresh = [e for e in m[k] if now - e[1] <= age]
            if fresh:
                m[k] = fresh
            else:
                m.pop(k, None)

    def key_inflight(self, dep: dict | None) -> int:
        if not dep:
            return 0
        return len(self._key_leases().get(dep.get("api_key") or "", ()))

    def key_lease_acquire(self, dep: dict | None) -> tuple | None:
        """Riserva una "lease" (richiesta in volo) per la api_key del dep.

        Opt-in (`key_concurrency_enabled`): con il cap raggiunto ritorna None
        — il chiamante NON deve bloccare la richiesta (cap SOFT: la chiave
        viene solo deprioritizzata finche' esistono alternative), quindi il
        None serve solo a non incrementare il contatore."""
        if not dep or not bool(getattr(self.policy,
                                        "key_concurrency_enabled", False)):
            return None
        now = time.time()
        self._prune_key_leases(now)
        key = dep.get("api_key") or ""
        cap = max(0, int(getattr(self.policy, "key_concurrency_max", 2) or 0))
        ent = self._key_leases().setdefault(key, [])
        if cap and len(ent) >= cap:
            return None
        tok = ("%s|%s|%d" % (key[:12], dep.get("unique"), now))
        ent.append((tok, now, dep.get("unique")))
        return (key, tok)

    def key_lease_release(self, lease: tuple | None) -> None:
        if not lease:
            return
        key, tok = lease
        ent = self._key_leases().get(key)
        if not ent:
            return
        self._key_leases()[key] = [e for e in ent if e[0] != tok] or None
        if not self._key_leases()[key]:
            self._key_leases().pop(key, None)

    def _lease_filter(self, deps: list[dict]) -> list[dict]:
        """Depriorizza (non elimina) i dep la cui api_key e' al cap di
        concorrenza: se TUTTI sono al cap ritorna la lista intera — il cap
        non deve mai trasformarsi in un 503."""
        if not bool(getattr(self.policy, "key_concurrency_enabled", False)):
            return deps
        cap = max(0, int(getattr(self.policy, "key_concurrency_max", 2) or 0))
        if not cap:
            return deps
        self._prune_key_leases()
        m = self._key_leases()
        if not m:
            return deps
        kept = [d for d in deps
                if len(m.get(d.get("api_key") or "", ())) < cap]
        return kept or deps

    def key_leases_view(self) -> dict:
        self._prune_key_leases()
        m = self._key_leases()
        return {k: len(v) for k, v in sorted(m.items())}

    # --------------------------------------------------- QUIRK LOCALE (P2-9)
    QUIRK_FLAGS = ("thinking_replay", "strip_reasoning", "no_thinking",
                   "hold_until_finish", "media_defer")

    def sync_runtime_constants(self) -> dict:
        """Propaga i parametri di tuning della policy nelle costanti di
        modulo del router. Ogni default coincide col valore storico: se il
        parametro non e' configurato, il comportamento NON cambia. Ritorna la
        mappa dei soli valori effettivamente modificati."""
        p = self.policy

        def _num(name: str, val, cast):
            try:
                return cast(val)
            except (TypeError, ValueError):
                return globals().get(name)

        changed: dict[str, dict] = {}

        def _set(name: str, val):
            old = globals().get(name)
            if old != val:
                globals()[name] = val
                changed[name] = {"old": old, "new": val}

        _set("LATENCY_ROTATE_THRESHOLD_MS", _num(
            "LATENCY_ROTATE_THRESHOLD_MS",
            getattr(p, "latency_rotate_threshold_ms",
                    LATENCY_ROTATE_THRESHOLD_MS), int))
        _set("SOFT_SLOW_LATENCY_MS", _num(
            "SOFT_SLOW_LATENCY_MS",
            getattr(p, "soft_slow_latency_ms", SOFT_SLOW_LATENCY_MS), int))
        _set("SOFT_SLOW_CTX_MIN", _num(
            "SOFT_SLOW_CTX_MIN",
            getattr(p, "soft_slow_ctx_min", SOFT_SLOW_CTX_MIN), int))
        edges = getattr(p, "ctx_bucket_edges", None) or list(CTX_BUCKETS)
        try:
            edges_t = tuple(int(e) for e in edges)
        except (TypeError, ValueError):
            edges_t = tuple(CTX_BUCKETS)
        _set("CTX_BUCKETS", edges_t)
        _set("CTX_BUCKET_COUNT", len(edges_t) + 1)
        _set("TTFT_RATE_MIN_CTX", _num(
            "TTFT_RATE_MIN_CTX",
            getattr(p, "ttft_rate_min_ctx", TTFT_RATE_MIN_CTX), int))
        _set("TTFT_RATE_FLOOR_MS", _num(
            "TTFT_RATE_FLOOR_MS",
            getattr(p, "ttft_rate_floor_ms", TTFT_RATE_FLOOR_MS), float))
        _set("SLOW_LATENCY_ABS_FLOOR_MS", _num(
            "SLOW_LATENCY_ABS_FLOOR_MS",
            getattr(p, "slow_latency_abs_floor_ms",
                    SLOW_LATENCY_ABS_FLOOR_MS), float))
        _set("SLOW_LATENCY_REL_MULT", _num(
            "SLOW_LATENCY_REL_MULT",
            getattr(p, "slow_latency_rel_mult", SLOW_LATENCY_REL_MULT), float))
        _set("SLOW_LATENCY_MIN_PEERS", _num(
            "SLOW_LATENCY_MIN_PEERS",
            getattr(p, "slow_latency_min_peers",
                    SLOW_LATENCY_MIN_PEERS), int))
        _set("SLOW_GEN_MULT", _num(
            "SLOW_GEN_MULT",
            getattr(p, "slow_gen_mult", SLOW_GEN_MULT), float))
        _set("SLOW_TYPICAL_COMPLETION_TOKENS", _num(
            "SLOW_TYPICAL_COMPLETION_TOKENS",
            getattr(p, "slow_typical_completion_tokens",
                    SLOW_TYPICAL_COMPLETION_TOKENS), float))
        _set("SLOW_REL_BASELINE_MULT", _num(
            "SLOW_REL_BASELINE_MULT",
            getattr(p, "slow_rel_baseline_mult",
                    SLOW_REL_BASELINE_MULT), float))
        _set("EFFORT_CAPABLE_BONUS", _num(
            "EFFORT_CAPABLE_BONUS",
            getattr(p, "effort_capable_bonus", EFFORT_CAPABLE_BONUS), float))
        _set("LATENCY_PENALTY_PER_SEC", _num(
            "LATENCY_PENALTY_PER_SEC",
            getattr(p, "latency_penalty_per_sec",
                    LATENCY_PENALTY_PER_SEC), float))
        _pbn = str(getattr(p, "provider_bias_normalization",
                           PROVIDER_BIAS_NORMALIZATION) or "log").lower()
        if _pbn in ("log", "sqrt", "none"):
            _set("PROVIDER_BIAS_NORMALIZATION", _pbn)
        # Pesi reputazione: SW e' un dict importato per riferimento, quindi lo
        # aggiorniamo IN PLACE (SW["..."] ovunque vede i nuovi valori). I pesi
        # sono configurabili da policy.scoring_weights; i default coincidono
        # con constants.SCORING_WEIGHTS, quindi senza config NON cambia nulla.
        sw_cfg = getattr(p, "scoring_weights", None)
        if isinstance(sw_cfg, dict) and sw_cfg:
            for wk in list(SW.keys()):
                if wk in sw_cfg:
                    try:
                        nv = float(sw_cfg[wk])
                    except (TypeError, ValueError):
                        continue
                    if SW.get(wk) != nv:
                        changed.setdefault("SCORING_WEIGHTS", {})[wk] = nv
                        SW[wk] = nv
        if changed:
            log.info("[router] tuning da policy applicato: %s",
                     ", ".join(sorted(changed)))
        return changed

    def apply_quirks(self) -> int:
        """Applica IN MEMORIA i quirk dichiarati in policy ai deployment
        il cui modello matcha il glob (case-insensitive), mappandoli sui flag
        esistenti. Non riscrive il CSV: e' conoscenza dichiarativa locale.
        Ritorna il numero di coppie (deployment, flag) applicate."""
        # Allinea SEMPRE le costanti di tuning ai valori di policy correnti
        # (anche quando non ci sono quirk): il hot-reload della policy le
        # propaga senza restart.
        try:
            self.sync_runtime_constants()
        except Exception as exc:                      # pragma: no cover
            log.warning("[router] sync tuning fallito: %r", exc)
        quirks = list(getattr(self.policy, "quirks", None) or [])
        self._applied_quirks = []
        if not quirks:
            return 0
        import fnmatch
        n = 0
        for dep in self._all_deps():
            model = str(dep.get("model") or "").lower()
            for q in quirks:
                glob = str(q.get("model") or "")
                if not glob or not fnmatch.fnmatch(model, glob):
                    continue
                flag = str(q.get("flag") or "")
                if flag not in self.QUIRK_FLAGS:
                    log.warning("[quirk] flag sconosciuto '%s' per %s",
                                flag, glob)
                    continue
                dep[flag] = True
                n += 1
                self._applied_quirks.append(
                    {"model": glob, "flag": flag,
                     "severity": q.get("severity") or "warning",
                     "note": q.get("note") or "", "unique": dep.get("unique")})
                if (q.get("severity") == "blocker"):
                    log.warning("[quirk] BLOCKER %s (%s) -> %s",
                                dep.get("model"), flag, dep.get("unique"))
        if self._applied_quirks:
            log.info("[quirk] %d applicazioni su %d quirk dichiarati",
                     n, len(quirks))
        return n

    def quirks_view(self) -> dict:
        applied = getattr(self, "_applied_quirks", []) or []
        by_model: dict[str, int] = {}
        for a in applied:
            by_model[a["model"]] = by_model.get(a["model"], 0) + 1
        return {"declared": len(getattr(self.policy, "quirks", None) or []),
                "applied": len(applied), "by_model": by_model}

    def _all_deps(self):
        for deps in self.config.groups.values():
            for d in deps:
                yield d

    # ------------------------------------------------ PRESSURE (P2-10)
    def pressure_view(self, limit: int = 40) -> dict:
        """Perche' un modello/deployment viene saltato: cooldown attivi con
        provenienza, bench di modello, chiavi sature (lease), quarantene
        endpoint, esenzioni-riparazione esaurite. Ordinato per pressione
        (residuo di cooldown decrescente)."""
        now = time.time()
        self._prune_key_leases(now)
        rows = []
        for u in list(self._cooldown):
            # cooldown EFFETTIVO (il model_preference puo' accorciarlo): il
            # pannello deve mostrare chi e' DAVVERO tenuto fuori adesso.
            if not self.is_cooled_down(u):
                continue
            rem = self.cooldown_residual(u)
            d = self.config.deployment_by_unique(u) or {}
            s = self.stats_for(u)
            rows.append({
                "unique": u, "model": d.get("model"), "group": d.get("group"),
                "remaining_sec": int(rem),
                "reason": getattr(s, "last_reason", None),
                "provenance": self.cooldown_provenance(u),
                "fail_24h": getattr(s, "fail_count_24h", None),
                "probeable": self.cooldown_probeable(u),
            })
        rows.sort(key=lambda r: -r["remaining_sec"])
        benches: dict[str, int] = {}
        for u in self._cooldown:
            if not self.is_cooled_down(u):
                continue
            d = self.config.deployment_by_unique(u) or {}
            if getattr(self.stats_for(u), "last_reason", None) == \
                    "model_unhealthy":
                benches[str(d.get("model"))] = benches.get(
                    str(d.get("model")), 0) + 1
        exempt = {u: n for u, n in (getattr(self, "_repair_exempt_map", {})
                                    or {}).items() if n}
        return {
            "cooldowns_total": len(rows),
            "cooldowns": rows[:limit],
            "model_bench": dict(sorted(benches.items(),
                                       key=lambda kv: -kv[1])[:limit]),
            "endpoint_quarantine": self.endpoint_quarantine_view(),
            "key_leases": self.key_leases_view(),
            "repair_exempt": exempt,
            "model_fail_window": {m: len(q) for m, q in
                                  (getattr(self, "_model_fail_win_map", {})
                                   or {}).items() if q},
        }

    def clear_pressure(self, model: str | None = None,
                       unique: str | None = None) -> dict:
        """Azzera cooldown + penalita' + finestre di fallimento (operatore).

        `unique` -> solo quel deployment; `model` -> tutte le sue chiavi;
        nessun filtro -> tutto. La pressione si ricostruisce dai risultati
        live: il caso peggiore di un clear prematuro e' un altro giro di
        fallimenti, meglio di un pool che non riesce a ruotare."""
        now = time.time()
        want_model = (model or "").strip()
        cleared: list[str] = []
        for u in list(self._cooldown):
            d = self.config.deployment_by_unique(u) or {}
            if unique and u != unique:
                continue
            if want_model and str(d.get("model") or "") != want_model:
                continue
            if self._cooldown.get(u, 0) > now:
                cleared.append(u)
            self._cooldown.pop(u, None)
        m = getattr(self, "_cooldown_prov_map", None)
        if m:
            for u in cleared:
                m.pop(u, None)
        rex = getattr(self, "_repair_exempt_map", None)
        if rex:
            for u in list(rex):
                d = self.config.deployment_by_unique(u) or {}
                if unique and u != unique:
                    continue
                if want_model and str(d.get("model") or "") != want_model:
                    continue
                rex.pop(u, None)
        mw = getattr(self, "_model_fail_win_map", None)
        if mw:
            for mk in list(mw):
                if want_model and mk != want_model:
                    continue
                mw.pop(mk, None)
        q = getattr(self, "_endpoint_quarantine", None)
        if q and not unique:
            for h in list(q):
                if want_model:
                    continue
                q.pop(h, None)
        if not want_model and not unique:
            mcb = getattr(self, "_model_cb", None)
            if mcb:
                mcb.clear()
        self._key_leases().clear()
        log.warning("[pressure] clear (model=%s unique=%s): %d cooldown "
                    "rimossi", want_model or "-", unique or "-", len(cleared))
        return {"ok": True, "cleared": cleared, "count": len(cleared)}

    # ------------------------------------------------ PROVENIENZA COOLDOWN (P0)
    def _cooldown_prov(self) -> dict:
        d = getattr(self, "_cooldown_prov_map", None)
        if d is None:
            d = self._cooldown_prov_map = {}
        return d

    def _infer_provenance(self, reason: str | None, status: int | None,
                          explicit_seconds: bool) -> str:
        """Classifica la NASCITA di un cooldown: 'credit' (402/no_credits),
        'tier' (403/forbidden/modello fuori tier), 'authoritative' (secondi
        dichiarati dal provider: Retry-After o reset quota), altrimenti
        'heuristic' (nostra stima)."""
        st = abs(int(status)) if status else 0
        r = (reason or "").lower()
        if st == 402 or "credit" in r or "402" in r:
            return "credit"
        if st == 403 or "403" in r or "forbidden" in r or "tier" in r:
            return "tier"
        if explicit_seconds and (st == 429 or "429" in r or "quota" in r):
            return "authoritative"
        return "heuristic"

    def cooldown_provenance(self, unique: str | None) -> str | None:
        """Provenienza dell'ULTIMO cooldown applicato a questo dep (None se
        nessun cooldown attivo). Un cooldown senza provenienza registrata
        (scritto direttamente, o precedente a questa feature) vale come
        'heuristic': e' il comportamento storico."""
        if not unique or not self.is_cooled_down(unique):
            return None
        return self._cooldown_prov().get(unique) or "heuristic"

    def cooldown_probeable(self, unique: str | None) -> bool:
        """True SOLO se il cooldown e' una NOSTRA stima ('heuristic'): la
        sveglia puo' testarlo in anticipo. Mai per authoritative/credit/tier."""
        return self.cooldown_provenance(unique) == "heuristic"


    def inflight_total(self) -> int:
        """Richieste attualmente in volo su tutti i deployment (usato dal
        graceful shutdown per attendere il drain prima del flush finale)."""
        return sum(s.inflight for s in self._stats.values())

    def note_start(self, unique: str, ctx_est: int | None = None) -> None:
        """Richiesta inviata: tocca last_used (penalità anti rate-limit),
        incrementa inflight e le FINESTRE budget (minuto/giorno). Nei gruppi
        gen/stt registra anche l'ultimo modello per la stickiness.
        [Blocco 1] Registra anche il tentativo per il reputation scoring.
        ctx_est (se noto) alimenta il prefill pesato in volo."""
        s = self.stats_for(unique)
        s.last_used = time.time()
        s.inflight += 1
        if ctx_est:
            try:
                s.inflight_tokens += max(0, int(ctx_est))
            except (TypeError, ValueError):
                pass
        # --- finestre budget (Feature no-spreco) -------------------------
        now = time.time()
        mk = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(now))
        dk = time.strftime("%Y-%m-%d", time.gmtime(now))
        if mk != s.minute_key:              # rollover minuto
            s.minute_key, s.minute_calls = mk, 0
        if dk != s.day_key:                 # rollover giorno + cap appresi
            s.day_key, s.day_calls = dk, 0
            s.day_cap_learned = 0.0         # i limiti giornalieri ripartono
        s.minute_calls += 1
        s.day_calls += 1
        dep = self.config.deployment_by_unique(unique)
        if dep is not None:
            cap = self.config.group_caps.get(dep["group"])
            if cap in self.SAME_MODEL_PRIORITY_CAPS \
                    and self.policy.gen_same_model_failover:
                self._gen_last_model[dep["group"]] = dep["model"]
        # [Blocco 1] Registra tentativo per reputation scoring
        self.record_attempt(unique)
        # COLD SPREAD: il tentativo entra nella finestra rolling 24h
        self.note_usage(unique, ctx_est=ctx_est)

    def note_result(self, unique: str, latency_ms: float,
                    quality: float = 1.0, ctx_est=None,
                    kind: str = "total") -> None:
        """Risposta ricevuta: aggiorna l'EMA di latenza (non tocca inflight:
        per lo streaming chiude la nota_end al termine del flusso).
        Resetta anche lo streak di fallimenti e aggiorna il tasso successo.
        [Blocco 1] Registra anche il successo per il reputation scoring.

        `quality` in [0.1, 1.0] scala l'alpha delle EMA: risposte "sporche"
        (tool_repair/fake tool-call/QC fallita/stallo) pesano meno, cosi' un
        deployment rotto-ma-vivo non viene giudicato come uno sano."""
        q = min(1.0, max(0.1, float(quality)))
        alpha = max(0.05, 0.3 * q)
        s = self.stats_for(unique)
        s.ema_latency_ms = (latency_ms if s.ema_latency_ms is None
                            else s.ema_latency_ms * (1 - alpha) + latency_ms * alpha)
        s.fail_streak = 0
        s.probe_fail_streak = 0
        # successo reale: azzera anche l'esenzione-riparazione (P0)
        self.reset_repair_exempt(unique)
        self.note_model_success(unique)
        prev = 1.0 if s.success_ema is None else s.success_ema
        s.success_ema = min(1.0, 0.8 * prev + 0.2 * q)
        # contatore cumulativo + timestamp ultimo successo (persistiti)
        s.ok_count += 1
        s.last_success_ts = time.time()
        # [Blocco 1] Registra successo per reputation scoring
        self.record_success(unique, latency_ms, quality=q,
                            ctx_est=ctx_est, kind=kind)
        # Dynamic concurrency limit: successo a saturazione -> il limite sale
        self._learn_concurrency(unique)

    def note_end(self, unique: str, ctx_est: int | None = None) -> None:
        _s = self.stats_for(unique)
        _s.inflight = max(0, _s.inflight - 1)
        if ctx_est:
            try:
                _s.inflight_tokens = max(
                    0, _s.inflight_tokens - max(0, int(ctx_est)))
            except (TypeError, ValueError):
                pass
        # Connection draining: una richiesta a un deployment in draining e'
        # terminata -> decrementa il contatore; a zero il deployment viene
        # rimosso definitivamente (anche dalla config).
        d = self._drain().get(unique)
        if d:
            d["inflight"] = max(0, d["inflight"] - 1)
            if d["inflight"] == 0:
                self._finish_drain(unique)

    # ------------------------------------------- connection draining (hot-reload)
    def start_draining(self, unique: str, dep: dict,
                       inflight: int) -> None:
        """Archivia un deployment rimosso dal CSV ma con richieste in volo.

        Il dep viene RI-AGGIUNTO alla config (se assente) marcato draining:
        riferimenti/retry/record_* dello stesso ciclo continuano a risolverlo,
        mentre pick_deployment lo ignora per le nuove richieste.
        """
        n = max(0, int(inflight))
        self._drain()[unique] = {
            "ts": time.time(),
            "inflight": n,
            "dep": dep,
        }
        grp = (dep or {}).get("group")
        if grp and self.config is not None:
            lst = self.config.groups.setdefault(grp, [])
            if not any(x.get("unique") == unique for x in lst):
                lst.append(dep)

    def _drain(self) -> dict:
        """Accessor lazy di `_draining` (pattern `_esc`): protegge i Router
        costruiti senza __init__ (`Router.__new__(Router)` nei test)."""
        d = getattr(self, "_draining", None)
        if d is None:
            d = {}
            self._draining = d
        return d

    def is_draining(self, unique: str) -> bool:
        return unique in self._drain()

    def purge_draining(self) -> int:
        """Rimuove i draining oltre il TTL (anche con inflight residua)."""
        now = time.time()
        ttl = max(1.0, float(getattr(self.policy, "hotreload_drain_ttl_sec",
                                     120.0) or 120.0))
        dead = [u for u, d in list(self._drain().items())
                if now - d.get("ts", 0) > ttl]
        for u in dead:
            log.info("[drain] %s: TTL %ds scaduto (%d inflight) -> rimosso "
                     "definitivamente", u, int(ttl),
                     self._drain()[u].get("inflight", 0))
            self._finish_drain(u)
        return len(dead)

    def _finish_drain(self, unique: str) -> None:
        d = self._drain().pop(unique, None)
        if d is None:
            return
        dep = d.get("dep") or {}
        grp = dep.get("group")
        if grp and self.config is not None and grp in self.config.groups:
            self.config.groups[grp] = [x for x in self.config.groups[grp]
                                       if x.get("unique") != unique]
        log.info("[drain] %s: draining completata -> rimosso dalla config",
                 unique)

    # ------------------------------------------------ persistenza (F4)
    def dump_stats(self) -> dict:
        """Snapshot serializzabile: EMA+last_used e scadenze cooldown."""
        self._init_scoring_if_needed()
        return {
            "stats": {u: {"ema_latency_ms": s.ema_latency_ms,
                          "last_used": s.last_used,
                          "fail_streak": s.fail_streak,
                          "success_ema": s.success_ema,
                          "fail_count_24h": s.fail_count_24h,
                          "fail_day_key": s.fail_day_key,
                          "last_reason": s.last_reason,
                          "last_provenance": s.last_provenance,
                          "ok_count": s.ok_count,
                          "fail_count": s.fail_count,
                          "last_success_ts": s.last_success_ts,
                          "last_fail_ts": s.last_fail_ts,
                          "probe_fail_streak": s.probe_fail_streak}
                      for u, s in self._stats.items()},
            "cap_strikes": [{"key": k, **v} for k, v in
                            self._cap_strikes.items()],
            # [Blocco 1] Reputation scoring
            "base_scores": dict(self._base_scores),
            "provider_scores": dict(self._provider_scores),
            "key_scores": dict(self._key_scores),
            "avg_latencies": dict(self._avg_latencies),
            "ctx_lat": {u: list(v) for u, v in
                        getattr(self, "_lat_buckets", {}).items()},
            "ctx_ttft": {u: list(v) for u, v in
                         getattr(self, "_ttft_buckets", {}).items()},
            "ttft_rate": dict(getattr(self, "_prefill_rate", {})),
            "est_div": dict(getattr(self, "_est_div", {})),
            # evidenza shadow del rollout auto-adaptive: sopravvive al restart
            "estimate_shadow": dict(_estimate_shadow_stats),
            "saved_at": time.time(),
        }

    def load_stats(self, data: dict) -> None:
        """Ricarica lo snapshot dopo un restart. File corrotto/campi strani ->
        si riparte puliti (mai crash all'avvio). inflight NON si persiste."""
        self._init_scoring_if_needed()
        try:
            for u, st in (data.get("stats") or {}).items():
                if not isinstance(st, dict):
                    continue
                s = self.stats_for(u)
                s.last_used = float(st.get("last_used") or 0)
                ema = st.get("ema_latency_ms")
                s.ema_latency_ms = float(ema) if ema else None
                try:
                    # clamp a 30: streak più alti sono corruzione storica
                    # (es. 429 ripetuti) e non aggiungono nulla all'escalation
                    s.fail_streak = min(30, max(0, int(st.get("fail_streak") or 0)))
                except (TypeError, ValueError):
                    pass
                sema = st.get("success_ema")
                if sema is not None:
                    try:
                        s.success_ema = max(0.0, min(1.0, float(sema)))
                    except (TypeError, ValueError):
                        pass
                try:
                    s.fail_count_24h = max(0, int(st.get("fail_count_24h") or 0))
                    s.fail_day_key = str(st.get("fail_day_key") or "")
                except (TypeError, ValueError):
                    pass
                s.last_reason = st.get("last_reason") or None
                s.last_provenance = st.get("last_provenance") or None
                try:
                    s.ok_count = max(0, int(st.get("ok_count") or 0))
                    s.fail_count = max(0, int(st.get("fail_count") or 0))
                except (TypeError, ValueError):
                    pass
                try:
                    s.last_success_ts = float(st.get("last_success_ts") or 0)
                    s.last_fail_ts = float(st.get("last_fail_ts") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    s.probe_fail_streak = max(
                        0, int(st.get("probe_fail_streak") or 0))
                except (TypeError, ValueError):
                    pass
            for st in (data.get("cap_strikes") or []):
                if not isinstance(st, dict) or "|" not in str(st.get("key", "")):
                    continue
                self._cap_strikes[str(st["key"])] = {
                    "count": int(st.get("count") or 0),
                    "first": float(st.get("first") or 0),
                    "last": float(st.get("last") or 0),
                    "evidence": str(st.get("evidence") or "")[:200],
                }
            # [Blocco 1] Load reputation scoring
            for u, score in (data.get("base_scores") or {}).items():
                self._base_scores[str(u)] = float(score)
            for pk, score in (data.get("provider_scores") or {}).items():
                self._provider_scores[str(pk)] = float(score)
            for ak, score in (data.get("key_scores") or {}).items():
                self._key_scores[str(ak)] = float(score)
            for u, lat in (data.get("avg_latencies") or {}).items():
                self._avg_latencies[str(u)] = float(lat)
            for key, attr in (("ctx_lat", "_lat_buckets"),
                              ("ctx_ttft", "_ttft_buckets")):
                table = getattr(self, attr, None)
                if not isinstance(table, dict):
                    continue
                for u, v in (data.get(key) or {}).items():
                    try:
                        fv = [float(x) for x in list(v or [])]
                    except (TypeError, ValueError):
                        continue
                    if not fv or any(x < 0 for x in fv):
                        continue
                    fv = (fv + [0.0] * CTX_BUCKET_COUNT)[:CTX_BUCKET_COUNT]
                    table[str(u)] = fv
            rate = getattr(self, "_prefill_rate", None)
            if isinstance(rate, dict):
                for u, r in (data.get("ttft_rate") or {}).items():
                    try:
                        fr = float(r)
                    except (TypeError, ValueError):
                        continue
                    if fr > 0:
                        rate[str(u)] = fr
            ediv = getattr(self, "_est_div", None)
            if isinstance(ediv, dict):
                for u, d in (data.get("est_div") or {}).items():
                    try:
                        fd = float(d)
                    except (TypeError, ValueError):
                        continue
                    if 1.5 <= fd <= 4.5:
                        ediv[str(u)] = fd
            load_estimate_shadow(data.get("estimate_shadow") or {})
        except Exception as exc:             # noqa: BLE001
            log.warning("[stats] load fallito (%s): riparto pulito", exc)

    # -------------------------------------------------- cooldown su disco
    def save_cooldowns(self) -> dict:
        """Snapshot cooldown attivo per la persistenza dedicata
        (var/cooldown_state.json): {unique: {expires, since, full}}. Solo le
        entry NON scadute: lo stato muorto non va scritto ne' riletto."""
        now = time.time()
        out: dict = {}
        for u, exp in list(self._cooldown.items()):
            if exp <= now:
                continue
            out[str(u)] = {
                "expires": float(exp),
                "since": float(self._cooldown_since.get(u, now) or now),
                "full": float(self._cooldown_full_map().get(u, 0.0) or 0.0),
            }
        return out

    def load_cooldowns(self, data: dict) -> int:
        """Ripristina SOLO i cooldown non ancora scaduti, ripristinando anche
        `since` (eta' reali, niente retry-stantio) e `full` (durata totale per
        il probe/decay). Ritorna il numero di cooldown riattivati. File
        corrotto/campi strani -> si riparte puliti (mai crash all'avvio)."""
        n = 0
        if not isinstance(data, dict):
            return 0
        now = time.time()
        try:
            for u, c in data.items():
                if not isinstance(c, dict):
                    continue
                try:
                    exp = float(c.get("expires") or 0)
                except (TypeError, ValueError):
                    continue
                if exp <= now:
                    continue
                self._cooldown[str(u)] = exp
                try:
                    since = float(c.get("since") or 0)
                except (TypeError, ValueError):
                    since = 0.0
                self._cooldown_since[str(u)] = since if since > 0 else now
                try:
                    full = float(c.get("full") or 0)
                except (TypeError, ValueError):
                    full = 0.0
                if full > 0:
                    self._cooldown_full_map()[str(u)] = full
                n += 1
        except Exception as exc:             # noqa: BLE001
            log.warning("[cooldown] load fallito (%s): riparto pulito", exc)
            return 0
        if n:
            log.info("[cooldown] ripristinati %d cooldown da disco", n)
        return n

    # ------------------------------------------- warm-start stato di routing
    def dump_routing_state(self) -> dict:
        """Snapshot delle mappe LEGATE ALLE SESSIONI (holder cache, sticky,
        ownership warm, demote, pin escalation, watermark ctxcompact): prima
        morivano tutte a ogni restart, costringendo a re-rotazione e
        re-apprendimento a ogni deploy. I timestamp sono gia' epoch ovunque;
        si scrivono solo le voci NON scadute secondo il TTL di ciascuna
        famiglia (validato anche al caricamento)."""
        now = time.time()
        p = self.policy
        sticky_ttl = float(getattr(p, "sticky_ttl_sec", 3600) or 3600)
        holder_ttl = float(getattr(p, "cache_holder_ttl_sec", 3600) or 3600)
        guard = self._guard_sec()
        slow_ttl = self._warm_ttl()
        esc_ttl = float(getattr(p, "escalation_pin_ttl_sec", 300) or 300)

        def _pairs(d, ttl):
            out = {}
            for k, v in (d or {}).items():
                try:
                    if isinstance(v, tuple) and len(v) == 2 \
                            and now - float(v[1]) <= ttl:
                        out[str(k)] = [v[0], float(v[1])]
                except (TypeError, ValueError):
                    continue
            return out

        deps_map = _pairs(getattr(self, "_dep_last_session", None), guard)
        dsl = self._sess_slow()
        session_slow = {}
        for sid, m in (dsl or {}).items():
            keep = {}
            for u, rec in (m or {}).items():
                try:
                    ts, hard = (rec if isinstance(rec, tuple) else (rec, True))
                    if now - float(ts) <= slow_ttl:
                        keep[str(u)] = [float(ts), bool(hard)]
                except (TypeError, ValueError):
                    continue
            if keep:
                session_slow[str(sid)] = keep
        return {
            "saved_at": now,
            "sticky": _pairs(getattr(self, "_sticky", None), sticky_ttl),
            "session_group": _pairs(getattr(self, "_session_group", None),
                                     sticky_ttl),
            "sticky_dep": _pairs(getattr(self, "_sticky_dep", None),
                                  sticky_ttl),
            "session_last_ok": _pairs(getattr(self, "_session_last_ok", None),
                                       holder_ttl),
            "dep_last_session": deps_map,
            "session_deps": {
                sid: sorted(u for u, (s2, _t) in deps_map.items()
                            if s2 == sid)
                for sid in {str(v[0]) for v in deps_map.values()}},
            "session_slow": session_slow,
            "ctx_frontier": _pairs(getattr(self, "_ctx_frontier", None),
                                   guard),
            "esc_win": _pairs(getattr(self, "_esc_win", None), esc_ttl),
            "discovered_max_input": {str(u): int(v) for u, v in
                                     (getattr(self, "_discovered_max_input",
                                              None) or {}).items()
                                     if v},
        }

    def load_routing_state(self, data: dict) -> dict:
        """Ripristina il warm-start al boot: TTL riverificati, tipi validati,
        mai crash su file sporco. Ritorna {famiglia: n} per il log."""
        if not isinstance(data, dict):
            return {}
        now = time.time()
        p = self.policy
        sticky_ttl = float(getattr(p, "sticky_ttl_sec", 3600) or 3600)
        holder_ttl = float(getattr(p, "cache_holder_ttl_sec", 3600) or 3600)
        guard = self._guard_sec()
        slow_ttl = self._warm_ttl()
        esc_ttl = float(getattr(p, "escalation_pin_ttl_sec", 300) or 300)
        report: dict[str, int] = {}

        def _load_pairs(key, table, ttl, report_name):
            n = 0
            for k, v in (data.get(key) or {}).items():
                try:
                    if not isinstance(v, (list, tuple)) or len(v) != 2:
                        continue
                    ts = float(v[1])
                    if now - ts > ttl or now < ts:
                        continue
                    table[str(k)] = (v[0], ts)
                    n += 1
                except (TypeError, ValueError):
                    continue
            report[report_name] = n

        _load_pairs("sticky", self._sticky, sticky_ttl, "sticky")
        _load_pairs("session_group", self._session_group, sticky_ttl,
                    "session_group")
        _load_pairs("sticky_dep", self._sticky_dep, sticky_ttl, "sticky_dep")
        _load_pairs("session_last_ok", self._cache_ok(), holder_ttl, "holder")
        _load_pairs("esc_win", self._esc(), esc_ttl, "esc_win")
        dls = getattr(self, "_dep_last_session", None)
        if not isinstance(dls, dict):
            dls = {}
            self._dep_last_session = dls
        n = 0
        for u, v in (data.get("dep_last_session") or {}).items():
            try:
                sid, ts = str(v[0]), float(v[1])
                if now - ts > guard or now < ts:
                    continue
                dls[str(u)] = (sid, ts)
                self._sess_deps().setdefault(sid, set()).add(str(u))
                n += 1
            except (TypeError, ValueError, IndexError):
                continue
        report["warm_owner"] = n
        dslow = self._sess_slow()
        n = 0
        for sid, m in (data.get("session_slow") or {}).items():
            if not isinstance(m, dict):
                continue
            for u, rec in m.items():
                try:
                    ts, hard = float(rec[0]), bool(rec[1])
                    if now - ts > slow_ttl or now < ts:
                        continue
                    dslow.setdefault(str(sid), {})[str(u)] = (ts, hard)
                    n += 1
                except (TypeError, ValueError, IndexError):
                    continue
        report["session_slow"] = n
        dis = self._discovered()
        n = 0
        for u, lim in (data.get("discovered_max_input") or {}).items():
            try:
                v = int(lim)
            except (TypeError, ValueError):
                continue
            if v > 0:
                dis[str(u)] = v
                n += 1
        report["discovered_max_input"] = n
        df = getattr(self, "_ctx_frontier", None)
        if not isinstance(df, dict):
            df = {}
            self._ctx_frontier = df
        n = 0
        for sid, v in (data.get("ctx_frontier") or {}).items():
            try:
                b, ts = int(v[0]), float(v[1])
                if b > 0 and now - ts <= guard and now >= ts:
                    df[str(sid)] = (b, ts)
                    n += 1
            except (TypeError, ValueError, IndexError):
                continue
        report["ctx_frontier"] = n
        return report

    def _score(self, dep: dict, now: float | None = None) -> float:
        """Punteggio adattivo: base priority × freschezza × velocità ÷ saturazione.
        A freddo (nessuna statistica) tutti i fattori sono neutri (=1)."""
        pol = self.policy
        now = now if now is not None else time.time()
        s = self._stats.get(dep["unique"])
        priority = max(0, int(dep.get("priority", 0) or 0)) + 1
        if s is None:
            return float(priority)
        # HALFLIFE PER-CATEGORIA: go/fallback usano un tempo più lungo
        # (minuti) per preservare la cache prompt (le richieste turn-by-turn
        # restano sulla stessa key, i 28 abbonamenti si consumano uniformemente
        # a livello di sessione, non di singolo turn). Free/Priority usano il
        # default più corto (20s) dove la distribuzione è anti-rate-limit al
        # minuto. NOTA: per i free, deployment_sticky previene la rotazione
        # alla fonte (stessa sessione = stessa key); questo halflife riguarda
        # solo il primo pick o la ripresa dopo cooldown.
        # I deployment dict di config.groups NON portano "meta" (vedi
        # _build_profile): la categoria go/fallback si deduce dal SUFFISSO del
        # gruppo — vale sia per il mondo testo (-go/-fallback) sia per le terne
        # capacità (…-C-go / …-C-fallback).
        _grp = dep.get("group") or ""
        _cfg = getattr(self, "config", None)
        if _grp.endswith(getattr(_cfg, "go_suffix", "") or "-go") \
                or _grp.endswith(getattr(_cfg, "fallback_suffix", "")
                                 or "-fallback"):
            hl = pol.go_recency_halflife_sec
        else:
            hl = pol.recency_halflife_sec
        freshness = 1.0 if s.last_used <= 0 else max(
            0.05, math.exp(-(now - s.last_used) / max(0.001, hl)))
        speed = 1.0
        if s.ema_latency_ms and s.ema_latency_ms > 0:
            speed = min(2.0, max(0.4,
                                 pol.latency_ref_ms / s.ema_latency_ms))
        sat = 1.0 / (1 + s.inflight)
        # penalità dolce per affidabilità: mai esclusione dura (quella la fa
        # il cooldown); un deployment flaky scende fino a ~25% del peso base
        rel = 1.0
        if s.success_ema is not None:
            rel = 0.25 + 0.75 * max(0.0, min(1.0, s.success_ema))
        # DECAY del cooldown: un deployment dormiente maturo torna gradualmente
        # neutro (speed/rel -> 1.0) man mano che il cooldown trascorre, cosi' il
        # probe passivo non parte da una penalita' piena e stantia.
        _dcy = self._cooldown_decay(dep["unique"])
        if _dcy < 1.0:
            speed = speed * _dcy + (1.0 - _dcy)
            rel = rel * _dcy + (1.0 - _dcy)
        # --- BUDGET GUARD (Feature no-spreco): dosa PRIMA del muro 429 ----
        # Semantica: la penalita' scatta SOLO con un cap APPRESO da almeno
        # un 429 reale di quel deployment (min_cap_learned/day_cap_learned).
        # Senza evidenza non si indovina nessun limite: una chiave pagata
        # che fa 50 chiamate/minuto non deve essere frenata.
        bg = pol.budget_guard or {}
        budget = 1.0
        if bg.get("enabled"):
            ratio = 1.0
            for used, cap in ((s.minute_calls, s.min_cap_learned),
                              (s.day_calls, s.day_cap_learned)):
                if not cap or cap <= 0:
                    continue              # niente evidenza -> nessuna pena
                r = max(0.0, 1.0 - (used / float(cap)))
                ratio = min(ratio, r)
            soft = float(bg.get("soft_factor", 0.8))
            if ratio < 1.0 - soft:        # sotto la soglia morbida
                # falloff lineare fino al peso residuo 5% a quota esaurita
                span = max(0.05, 1.0 - soft)
                budget = max(0.05, ratio / span)
                if budget < 0.3:
                    log.info("[budget] %s deprioritizzato (quota residua "
                             "%.0f%%)", dep["unique"], ratio * 100)
        return max(0.05, priority * freshness * speed * sat * rel * budget)

    # -------------------------------------------------- chiave custom alias
    def resolve_alias_key(self, raw_model: str, canonical_model: str) -> str | None:
        """Chiave CUSTOM opzionale per l'alias GENERICO usato dal client.

        Si applica SOLO se l'alias richiesto risolve a un nome BASE (routing
        contestuale): un alias FISSO (-> gruppo/unique esplicito) ignora
        sempre l'override, così come una richiesta senza alias_keys configurata.
        """
        key = self.policy.alias_keys.get(raw_model)
        if not key or self.is_explicit(canonical_model):
            return None
        return key

    # ---------------------------------------------------- scala dims (ladder)
    DIM_SUFFIX_RE = re.compile(r"-(\d+)k$")

    def _note_session_group(self, session_id: str | None,
                            gname: str | None) -> None:
        """Registra il gruppo risolto per la sessione; al cambio logga la
        transizione (osservabilità dei passaggi 200k->1000k ecc.). Log only."""
        if not session_id or not gname:
            return
        prev = self._session_group.get(session_id)
        self._session_group[session_id] = (gname, time.time())
        if prev is not None and prev[0] != gname:
            log.info("[session] %s: gruppo %s -> %s", session_id, prev[0],
                     gname)

    def _text_ladder(self, pname: str, start_dim: int | None = None,
                     start_tier: str = "primary") -> list[str]:
        """SCALA UNICA del mondo-testo (dims_ladder_floor):

            [primari dims >= start_dim ascendenti] + [-go] + [-fallback]

        Stateless (ricostruita dai gruppi esistenti). start_tier 'go'/
        'fallback' taglia la parte sopra: -go esplicito non tocca i primari
        (il paracadute dims a fine scala e' gestito da _walk_ladder_resilient);
        -fallback resta solo fallback. I bucket go/fallback compaiono UNA
        volta sola in coda."""
        cfg = self.config
        base = f"{cfg.proxy_prefix}{pname}"
        chain: list[str] = []
        if start_tier == "primary":
            dim_deps: list[dict] = []
            for d in sorted(cfg.profile_dims.get(pname, [])):
                if d < (start_dim or 0):
                    continue
                dim_deps.extend(cfg.groups.get(f"{base}-{d}k", []))
            # tier (colonna `order`) primario, poi dim crescente: i dims sono
            # raccolti in ordine dim-ascendente, quindi uno stable sort per
            # `order` mantiene l'ordine interno del gruppo dentro (order, dim).
            dim_deps.sort(key=lambda dep: int(dep.get("order", ORDER_LAST)))
            for dep in dim_deps:
                chain.append(dep["unique"])
        if start_tier in ("primary", "go"):
            for u in cfg.groups.get(f"{base}{cfg.go_suffix}", []):
                chain.append(u["unique"])
        for u in cfg.groups.get(f"{base}{cfg.fallback_suffix}", []):
            chain.append(u["unique"])
        return chain

    def _ladder_for_group(self, group_name: str) -> list[str]:
        """Ladder che parte dal gruppo del deployment corrente (mai indietro).
        Gruppo inesistente/irriconoscibile -> [] (nessun candidato inventato)."""
        cfg = self.config
        if group_name not in cfg.groups:
            return []
        if group_name.endswith(cfg.fallback_suffix):
            pname = group_name[:-len(cfg.fallback_suffix)][len(cfg.proxy_prefix):]
            return self._text_ladder(pname, start_tier="fallback")
        if group_name.endswith(cfg.go_suffix):
            pname = group_name[:-len(cfg.go_suffix)][len(cfg.proxy_prefix):]
            return self._text_ladder(pname, start_tier="go")
        m = self.DIM_SUFFIX_RE.search(group_name)
        if not m:
            return []
        pname = group_name[:m.start()][len(cfg.proxy_prefix):]
        return self._text_ladder(pname, start_dim=int(m.group(1)))

    def _resolve_explicit(self, requested: str, pname: str | None,
                          need: frozenset[str] | None,
                          session_id: str | None,
                          ctx_est: int) -> str:
        """Nome esplicito -> gruppo destinazione.

        - unique completo (__...)              -> passthrough esatto
        - gruppo CAP (-vision, -video_gen, ..) -> passthrough esatto
        - suffisso -go / -fallback             -> tier-start della scala
        - suffisso -Nk                         -> SOGLIA MINIMA: la dim scelta
          è la più piccola >= max(N, stima ctx); mai dim < N.
        Con dims_ladder_floor OFF tutto torna passthrough legacy."""
        cfg = self.config
        if "__" in requested or not self.policy.dims_ladder_floor:
            if need and requested in cfg.groups \
                    and not self._any_capable_in_group(requested, need):
                log.warning("[caps] %s richiede %s ma %s non ha deployment "
                            "capace: pass-through",
                            requested, sorted(need), requested)
            return requested
        if requested in cfg.groups \
                and cfg.group_caps.get(requested) is not None:
            if need and not self._any_capable_in_group(requested, need):
                log.warning("[caps] %s richiede %s ma %s non ha deployment "
                            "capace: pass-through",
                            requested, sorted(need), requested)
            self._note_session_group(session_id, requested)
            return requested
        tier_start = None
        if requested.endswith(cfg.fallback_suffix):
            tier_start = "fallback"
        elif requested.endswith(cfg.go_suffix):
            tier_start = "go"
        if tier_start is not None:
            if requested not in cfg.groups:
                return requested                    # nostro ma inesistente
            self._note_session_group(session_id, requested)
            return requested
        m = self.DIM_SUFFIX_RE.search(requested)
        if not m or requested not in cfg.groups:
            return requested                        # non è un dims nostro noto
        floor = int(m.group(1))
        pname2 = requested[:m.start()][len(cfg.proxy_prefix):]
        all_dims = [d for d in cfg.profile_dims.get(pname2, []) if d >= floor]
        if not all_dims:
            top = max(cfg.profile_dims.get(pname2, []) or [floor])
            log.warning("[ladder] soglia %dk oltre il massimo del profilo "
                        "'%s': uso -%dk", floor, pname2, top)
            all_dims = [top]
        cand = all_dims
        if need:
            capable = set(self._capable_dims(pname2, need))
            cand = [d for d in all_dims if d in capable]
            if not cand:
                log.warning("[ladder] nessuna dim >=%dk capace di %s nel "
                            "profilo '%s': pass-through legacy",
                            floor, sorted(need), pname2)
                return requested
        pct = self.policy.step_up_for(pname2)
        chosen = next((d for d in cand
                       if ctx_est <= d * 1000 * pct // 100), cand[-1])
        target = f"{cfg.proxy_prefix}{pname2}-{chosen}k"
        self._note_session_group(session_id, target)
        return target

    def resolve_group_for_request(self, requested: str, messages: Any,
                                   session_id: str | None,
                                   need: frozenset[str] | None = None,
                                   ctx: int | None = None) -> str | None:
        """Ritorna il NOME GRUPPO destinazione (o unique esplicito già valido).

        Regole:
          - nome non nostro            -> None (pass-through)
          - suffisso esplicito         -> rispettato SEMPRE (mai sticky!)
          - nome base + media          -> gruppo capacità -C (se strutturale ON)
                                          altrimenti filtro dinamico legacy
          - nome base + solo testo     -> hot-word / sticky / minimo sufficiente

        `need` = capacità richieste dal payload; `ctx` = stima token opzionale
        (guardia soft max_input nei gruppi capacità).
        """
        cfg = self.config
        if not requested.startswith(cfg.proxy_prefix):
            return None

        pname = cfg.profile_of_base(requested.split("__")[0]) \
            or cfg.profile_of_base(requested)

        explicit = self.is_explicit(requested)

        # stima contesto (testo + image_token_estimate per parte immagine):
        # serve anche al percorso esplicito -Nk (soglia minima -> start dim)
        img_est = getattr(self.policy, "image_token_estimate", 0) or 0
        if ctx is None:
            ctx = estimate_tokens(messages, self.policy.estimate_divisor,
                                  img_est)

        # richiesta ESPLICITA: soglia minima / tier-start / passthrough
        if explicit:
            return self._resolve_explicit(requested, pname, need,
                                          session_id, ctx)

        media_need = {c for c in (need or ()) if c != "text"}
        dims = cfg.profile_dims.get(requested, []) if requested in cfg.profile_dims \
            else (cfg.profile_dims.get(pname, []) if pname else [])

        # ---- GRUPPI CAPACITÀ STRUTTURALI ---------------------------------
        # il dispatcher base instrada la richiesta media al gruppo -C dedicato
        # (ordine di specificità: image_gen > tts > stt > video > audio > vision)
        if media_need and getattr(self.policy, "cap_groups_enabled", False) \
                and pname:
            pcaps = cfg.profile_caps.get(pname, [])
            target = next((c for c in CAP_PRIORITY_ORDER
                           if c in media_need and c in pcaps), None)
            if target is not None:
                gname = f"{cfg.proxy_prefix}{pname}-{target}"
                log.info("[caps] dispatch strutturale %s -> %s (need=%s)",
                         requested, gname, sorted(need))
                self._note_session_group(session_id, gname)
                return gname
            # nessun gruppo per le cap richieste: degrade o 400 rigoroso
            if getattr(self.policy, "cap_groups_on_missing", "dynamic") == "error":
                log.warning("[caps] nessun gruppo %s nel profilo '%s': "
                            "on_missing=error", sorted(media_need), pname)
                return None
            log.info("[caps] nessun gruppo %s nel profilo '%s': degrade "
                     "dinamico", sorted(media_need), pname)
            # prosegue sul percorso dinamico legacy qui sotto

        # ---- filtro dinamico (percorso testo / degrade) -------------------
        if need:
            capable_dims = self._capable_dims(pname, need)
            if not capable_dims:
                # prova -go e -fallback del profilo
                for suf in (cfg.go_suffix, cfg.fallback_suffix):
                    g = f"{cfg.proxy_prefix}{pname}{suf}"
                    if g in cfg.groups and self._any_capable_in_group(g, need):
                        log.info("[caps] nessun gruppo dim capace per %s -> uso %s", need, g)
                        self._note_session_group(session_id, g)
                        return g
                return None  # nessun gruppo capace -> 400 in main
            dims = capable_dims

        # hot-word SOLO percorso testo (i media seguono il gruppo dedicato)
        if not media_need:
            speed_hit = detect_hot_words(messages,
                                         patterns=self.policy.speed_hotwords,
                                         window=self.policy.hotwords_window)
            reason_hit = detect_hot_words(messages,
                                          patterns=self.policy.hotwords,
                                          window=self.policy.hotwords_window)
            if (speed_hit or reason_hit) and dims:
                if speed_hit:
                    target = self._pick_fast_group(pname, dims, ctx, need)
                    log.info("[vigile] speed-word -> %s (session=%s)", target,
                             session_id or "anonima")
                else:
                    target = f"{cfg.proxy_prefix}{pname}-{dims[-1]}k"
                    log.info("[vigile] hot-word -> %s (session=%s)", target,
                             session_id or "anonima")
                self._note_session_group(session_id, target)
                return target

            # sticky valida -> riusa il gruppo scelto dal ROUTING AUTOMATICO
            # solo se compatibile col need corrente E abbastanza grande per
            # la stima attuale: senza il check ctx una sessione cresciuta
            # resterebbe incollata al tier piccolo.
            if session_id:
                sticky = self.sticky_get(session_id)
                if sticky and (not need or self._any_capable_in_group(sticky, need)):
                    m_dim = re.search(r"-(\d+)k$", sticky)
                    fits = (ctx is None or m_dim is None
                            or ctx <= int(m_dim.group(1)) * 1000)
                    if fits:
                        self._note_session_group(session_id, sticky)
                        return sticky

        if pname is None or not dims:
            # profilo ignoto senza gruppi -> fallback finale se esiste
            fb = f"{requested}{cfg.fallback_suffix}"
            return fb if fb in cfg.groups else None

        def pick(pct: int) -> int:
            """Gruppo minimo il cui contesto*pct copre la stima (o il massimo)."""
            for d in dims:
                if ctx <= d * 1000 * pct // 100:
                    return d
            return dims[-1]

        chosen = pick(pct := self.policy.step_up_for(pname))
        # log della salita ANTICIPATA rispetto al comportamento legacy (100%)
        if pct < 100 and chosen != pick(100):
            log.info("[vigile] step-up %d%%: ~%d tok -> -%dk "
                     "(legacy sarebbe stato -%dk)",
                     pct, ctx, chosen, pick(100))
        target = f"{cfg.proxy_prefix}{pname}-{chosen}k"
        self._note_session_group(session_id, target)
        return target

    def _pick_fast_group(self, pname: str | None, dims: list[int],
                          ctx: int, need: frozenset[str] | None = None) -> str:
        """Hot-word di VELOCITÀ: tra i gruppi candidati vince quello col
        deployment più rapido (EMA latenza minima tra i sani).

        Candidati = gruppi con contesto >= speed_min_dim_k che ospitano la
        richiesta con margine (stima <= dim*speed_qualify_pct%). Nessun
        candidato col margine -> si rilassa al puro fit; nulla entra nemmeno
        così -> gruppo massimo. A freddo (nessuna EMA) -> candidato più
        piccolo (deterministico).

        Se `need` è fornito, considera solo gruppi con deployment capaci.
        """
        cfg = self.config
        pol = self.policy
        min_k = pol.speed_min_for(pname)
        qual = pol.speed_qualify_for(pname)

        # Filtra per gruppi capaci se need è fornito
        if need:
            dims = [d for d in dims if self._any_capable_in_group(f"{cfg.proxy_prefix}{pname}-{d}k", need)]

        cands = [d for d in dims
                 if d >= min_k and ctx <= d * 1000 * qual // 100]
        if not cands:
            cands = [d for d in dims if ctx <= d * 1000]
        if not cands:
            cands = [dims[-1]] if dims else [min_k]

        best_d: int | None = None
        best_ema: float | None = None
        for d in cands:
            for dep in cfg.groups.get(f"{cfg.proxy_prefix}{pname}-{d}k", []):
                if self.is_cooled_down(dep["unique"]):
                    continue
                if need and not self._dep_supports(dep, need):
                    continue
                s = self._stats.get(dep["unique"])
                ema = s.ema_latency_ms if s else None
                if ema and (best_ema is None or ema < best_ema):
                    best_ema, best_d = ema, d
        if best_d is None:
            best_d = cands[0]          # freddo: candidato più piccolo
        return f"{cfg.proxy_prefix}{pname}-{best_d}k"

    def _cap_fits(self, dep: dict, ctx: int | None) -> bool:
        """Guardia SOFT max_input nei gruppi capacità: se la stima supera il
        limite dichiarato (>0) il deployment viene saltato. ctx None = off."""
        if ctx is None:
            return True
        mi = self._eff_max_input(dep)
        return mi <= 0 or ctx <= mi

    def _gemini_blocked(self, dep: dict) -> bool:
        """True se il deployment e' Gemini e la richiesta corrente NON puo'
        usarlo (history con tool_call prive di thought_signature). In quel caso
        Gemini va escluso A MONTE dalla selezione, come una capability mancante:
        non compare tra i candidati (niente tentativi finti). Se la richiesta e'
        'buona' (should_avoid_gemini() False) Gemini resta eleggibile e riceve
        normalmente il suo model_preference."""
        return should_avoid_gemini() and is_gemini_deployment(dep)

    # ------------------------------------------- budget guard predittivo
    def _virtually_saturated(self, dep: dict, safety: float,
                             count_inflight: bool) -> bool:
        """True se il deployment ha raggiunto >= safety * cap appreso.

        Il cap esiste SOLO dopo un 429 reale (min/day_cap_learned): finche'
        non c'e' evidenza non si indovina nessun limite. Le chiamate del
        minuto corrente (minute_calls) includono gia' quelle in volo; l'inflight
        viene preso col max per coprire il rollover di minuto (quando
        minute_calls riparte da 0 ma esistono ancora richieste in volo).

        F8: con `budget_guard.suppress_with_headers` una chiave che MANDA
        header X-RateLimit-* (snapshot visto di recente) non paga i cap
        appresi: gli header live sono la verità, il tetto imparato a forza
        di 429 era la versione buia della stessa informazione.
        """
        if (self.policy.budget_guard or {}).get("suppress_with_headers", True) \
                and getattr(self.policy, "rate_hint_skip_enabled", True):
            k = dep.get("api_key")
            if isinstance(k, str) and k:
                tag = hashlib.sha256(
                    k.encode("utf-8", errors="replace")).hexdigest()[:12]
                rec = (getattr(self, "_key_hints", None) or {}).get(tag)
                proven = max(0.0, float(getattr(self.policy,
                                                "rate_hint_proven_sec",
                                                900.0) or 0.0))
                if rec and proven > 0 \
                        and time.time() - rec[0] <= proven:
                    return False
        s = self.stats_for(dep["unique"])
        minute_used = s.minute_calls
        if count_inflight:
            minute_used = max(minute_used, s.inflight)
        for used, cap in ((minute_used, s.min_cap_learned),
                          (s.day_calls, s.day_cap_learned)):
            if cap and cap > 0 and used >= safety * cap:
                return True
        return False

    def _apply_inflight_guard(self, deps: list[dict]) -> list[dict]:
        """Limitatore predittivo anti-burst.

        Se un cap e' stato appreso e una parte dei deployment e' oltre la
        soglia di sicurezza, li salta a favore di quelli ancora sotto soglia
        PRIMA che l'upstream risponda 429. Ritorna la lista invariata quando
        non ci sono alternative sane (l'ultima spiaggia resta il soft-score).
        """
        bg = self.policy.budget_guard or {}
        if not bg.get("enabled"):
            return deps
        try:
            safety = float(bg.get("safety_ratio", 0.8))
        except (TypeError, ValueError):
            safety = 0.8
        if safety <= 0:
            return deps
        count_inflight = bool(bg.get("count_inflight", True))
        alive = [d for d in deps
                 if not self._virtually_saturated(d, safety, count_inflight)]
        if not alive:
            return deps
        if len(alive) < len(deps):
            log.info("[budget] inflight-guard: %d/%d deployment oltre la "
                     "soglia di sicurezza (%.0f%%), devio sui restanti",
                     len(deps) - len(alive), len(deps), safety * 100)
        return alive

    # --------------------------------- dynamic concurrency limit (per-deployment)
    def _concl(self) -> dict:
        d = getattr(self, "_conc_limit", None)
        if d is None:
            d = {}
            self._conc_limit = d
        return d

    def _concok(self) -> dict:
        d = getattr(self, "_conc_ok", None)
        if d is None:
            d = {}
            self._conc_ok = d
        return d

    def _conc_default(self) -> int:
        return max(1, int(getattr(self.policy, "conc_default_limit", 3) or 3))

    def _conc_max(self) -> int:
        return max(1, int(getattr(self.policy, "conc_max_limit", 10) or 10))

    def _conc_streak(self) -> int:
        return max(1, int(getattr(self.policy, "conc_learn_success_streak",
                                  20) or 20))

    def _concurrent_limit_for(self, d: dict) -> int:
        """Limite di concorrenza per il deployment: il valore FISSO dalla
        colonna `concurrent_limit` del CSV vince SEMPRE; altrimenti il limite
        DINAMICO appreso (default conservativo, resettato al restart)."""
        fixed = d.get("concurrent_limit")
        if fixed:
            return max(1, int(fixed))
        return self._concl().get(d["unique"], self._conc_default())

    def _apply_concurrency_limit(self, deps: list[dict],
                                 ctx_est: int | None = None) -> list[dict]:
        """Esclude i deployment con inflight >= limite di concorrenza: le
        richieste gia' assegnate proseguono, le NUOVE vengono deviate su chiavi
        disponibili. Se TUTTO il gruppo e' saturo, lascia passare (l'edge
        estremo non deve svuotare il pick: il fallback/ultima-spiaggia decide).
        Questo NON e' rate-limiting temporale: e' il collo di bottiglia delle
        connessioni concorrenti per chiave (oltre il limite: 429/refused).

        CONCORRENZA PESATA A TOKEN (conc_token_ratio > 0): il peso e' il
        PREFILL REALE (inflight_tokens), non il numero di richieste — 3 turni
        da 90k valgono 270k token di prefill, 3 da 5k ne valgono 15k. Budget
        per dep = max_input * ratio; un heavy non parte se un altro e' gia'
        in volo sullo stesso dep, mentre N light passano in parallelo. Resta
        il tetto duro a conteggio (conc_max_limit). Una richiesta da SOLA
        passa sempre (mai deadlock: si salta solo con altra roba in volo).
        ratio=0 o contesto ignoto -> comportamento storico a conteggio."""
        if not deps:
            return deps
        try:
            ratio = float(getattr(self.policy, "conc_token_ratio", 0.0) or 0.0)
        except (TypeError, ValueError):
            ratio = 0.0
        try:
            ctx = int(ctx_est) if ctx_est else 0
        except (TypeError, ValueError):
            ctx = 0
        hard = self._conc_max()

        def _ok(d: dict) -> bool:
            s = self.stats_for(d["unique"])
            if s.inflight >= hard:               # tetto duro anti-abuso
                return False
            fixed = d.get("concurrent_limit")
            if fixed:                            # limite FISSO dal CSV
                return s.inflight < max(1, int(fixed))
            mxi = int(d.get("max_input_tokens") or 0)
            if ratio <= 0 or ctx <= 0 or mxi <= 0:
                return s.inflight < self._concurrent_limit_for(d)
            used = s.inflight_tokens
            if used <= 0:
                return True                      # niente prefill contato
            return used + ctx <= mxi * ratio

        avail = [d for d in deps if _ok(d)]
        if avail:
            return avail
        return deps

    def _learn_concurrency(self, unique: str) -> None:
        """Adatta il limite dinamico DOPO un successo (in note_result):
        se la richiesta e' terminata con inflight >= limite (saturazione
        gestita senza errori) per N successi consecutivi, il limite sale di 1
        (max conc_max_limit). Sotto saturazione la streak si azzera."""
        cfg = getattr(self, "config", None)
        if cfg is None:
            return                        # Router "nudo" (test): no-op
        dep = cfg.deployment_by_unique(unique)
        if dep is None or dep.get("concurrent_limit"):
            return                        # limite FISSO: niente apprendimento
        s = self.stats_for(unique)
        if s.inflight >= self._concurrent_limit_for(dep):
            ok = self._concok()
            n = ok.get(unique, 0) + 1
            ok[unique] = n
            if n >= self._conc_streak():
                ok.pop(unique, None)
                lim = self._concl().get(unique, self._conc_default())
                newlim = min(self._conc_max(), lim + 1)
                self._concl()[unique] = newlim
                log.info("[concurrency] %s: limite dinamico %d -> %d "
                         "(%d successi a saturazione)",
                         unique, lim, newlim, n)
        else:
            self._concok().pop(unique, None)

    def _punish_concurrency(self, unique: str) -> None:
        """Un errore di concorrenza (429/503) dimezza il limite dinamico
        (min 1): il provider ha rifiutato connessioni concorrenti. Chiamato
        da mark_failed sui 429/503. Il limite FISSO non viene toccato."""
        cfg = getattr(self, "config", None)
        if cfg is None:
            return                        # Router "nudo" (test): no-op
        dep = cfg.deployment_by_unique(unique)
        if dep is None or dep.get("concurrent_limit"):
            return
        self._concok().pop(unique, None)
        lim = self._concl().get(unique, self._conc_default())
        newlim = max(1, lim // 2)
        if newlim != lim:
            self._concl()[unique] = newlim
            log.info("[concurrency] %s: 429/503 -> limite dimezzato %d -> %d",
                     unique, lim, newlim)

    def pick_deployment(self, group_name: str, need: frozenset[str] | None = None,
                        exclude: str | None = None,
                        ctx: int | None = None,
                        restrict_model: str | None = None,
                        *, live_only: bool = False,
                        prefer_holder: bool = False) -> dict | None:
        """Selezione pesata dentro un gruppo, saltando i cooled-down.

        Con policy.adaptive_pick (default True): punteggio dinamico che
        penalizza l'ultimo usato, premia la velocità (EMA) ed evita chi ha
        richieste in corso. Disattivato: comportamento legacy priority-only.

        `need` filtra per capacità dichiarate; `exclude` esclude un unique
        (rotazione post-fallimento); `ctx` attiva la guardia soft max_input
        nei gruppi capacità; `restrict_model` limita a un solo modello
        upstream (failover same-model dei gruppi gen/stt).
        """
        def _ok(d: dict, allow_retired: bool = False) -> bool:
            if d["unique"] == exclude:
                return False
            if self._gemini_blocked(d):
                return False
            if self._endpoint_quarantined(d):
                return False
            if self.is_retired(d["unique"]):
                # I tier normali non usano MAI i ritirati. In ULTIMA SPIAGGIA
                # (allow_retired) sono ammessi quelli ritirati per quota/
                # probe-cap, mai quelli permanenti (chiave/modello morti).
                if not allow_retired or self._retired_permanent(d["unique"]):
                    return False
            if self.is_draining(d["unique"]):
                return False
            # SESSION-DEP GUARD: ignora fra i vivi i free-dims usati con
            # successo da un'ALTRA sessione negli ultimi session_dep_guard_sec
            # (eleggibili solo nel tier pre-ultima-spiaggia).
            if self.other_session_recent(d["unique"]):
                return False
            # SESSION-SLOW DEMOTE: un free-dim andato lento a QUESTA sessione
            # non va ri-pescato nei tier economici; torna solo a -fallback/
            # ultima spiaggia (vedi _walk_chain allow_slow).
            if self.config.group_caps.get(group_name) is None \
                    and not self._is_renewal_bucket(group_name) \
                    and self.is_slow_for_session(d["unique"], ctx=ctx):
                return False
            if getattr(self.policy, "circuit_breaker_enabled", True) and self._is_circuit_open(d["unique"]):
                return False
            # F25: modello in breaker aperto (5xx da piu' chiavi) -> skip
            if self._model_blocked(d):
                return False
            if restrict_model and d.get("model") != restrict_model:
                return False
            if need and not self._dep_supports(d, need):
                return False
            # guardia su OGNI gruppo: _cap_fits e' gia' no-op se ctx e' None
            # o max_input<=0. Prima era attiva solo sui gruppi capacita'
            # (-vision/-stt/...), lasciando i gruppi dims liberi di scegliere
            # deployment con max_input dichiarato < payload reale (Groq 413).
            if not self._cap_fits(d, ctx):
                return False
            return True

        deps = [d for d in self.config.groups.get(group_name, [])
                if not self.is_cooled_down(d["unique"]) and _ok(d)]
        if not deps and ctx:
            # F31: se il ctx non entra in NESSUN deployment del gruppo (tutti
            # con max_input dichiarato minore) l'"ultima spiaggia" sarebbe un
            # 413/400 PAGATO e deterministico (3-5s di prefill per tentativo).
            # Fail-fast: None -> il chiamante risponde 400 context_length_exceeded.
            _grp_all = self.config.groups.get(group_name) or []
            _tok = [int(d.get("max_input_tokens") or 0) for d in _grp_all]
            if _tok and all(t > 0 and t < int(ctx) for t in _tok):
                metrics.inc("nx_ctx_overflow_total", (group_name,))
                log.info("[ctx-overflow] %s: ctx=%d oltre il max_input di "
                         "tutti i %d deployment del gruppo (max=%d): "
                         "fail-fast senza catena", group_name, ctx,
                         len(_grp_all), max(_tok))
                return None
        if not deps and not live_only:
            # Nessun vivo: ULTIMA SPIAGGIA con PROBE PASSIVO. Se esistono
            # dormienti "maturi" (>= cooldown_probe_after_ratio del cooldown
            # trascorso) si provano SOLO quelli: se rispondono clear_cooldown
            # li resuscita; se falliscono il cooldown raddoppia (forwarder ->
            # mark_failed_double_residual). Senza probe maturi resta la vecchia
            # ultima spiaggia (ignora il cooldown), rispettando exclude/need.
            _cand = [d for d in self.config.groups.get(group_name, [])
                     if _ok(d, allow_retired=True)]
            _ret_n = sum(1 for d in _cand if self.is_retired(d["unique"]))
            if _ret_n:
                log.warning("[ladder] ULTIMA SPIAGGIA %s: uso anche %d "
                            "ritirati non-permanenti", group_name, _ret_n)
            _ripe = [d for d in _cand if self.probe_ready(d["unique"])]
            if _ripe:
                log.info("[probe] %s: %d dormienti maturi (>=%.0f%% del "
                         "cooldown) -> probe passivo", group_name, len(_ripe),
                         float(getattr(self.policy,
                                       "cooldown_probe_after_ratio", 0.5)
                               or 0.5) * 100)
                deps = _ripe
            else:
                deps = _cand
        deps, _ = self._defer_media(group_name, need, deps)
        # INFLIGHT-GUARD (budget predittivo): salta chi ha superato l'80%
        # (configurabile) del cap appreso PRIMA del 429, deviando sul fratello.
        deps = self._apply_inflight_guard(deps)
        # CONCURRENCY LIMIT dinamico (per-deployment): salta chi ha raggiunto
        # il limite di connessioni concorrenti (le in volo proseguono). Se
        # tutto il gruppo e' saturo il pick procede comunque (edge estremo).
        # SOLO mondo TESTO: i gruppi capacita' (gen/stt/video) hanno la loro
        # model-stickiness e non devono essere disturbati dalla saturazione.
        if self.config.group_caps.get(group_name) is None:
            deps = self._apply_concurrency_limit(deps, ctx)
        # LEASE PER CHIAVE (P2-8, opt-in): depriorizza le api_key con troppe
        # richieste in volo (soft: se tutte sono al cap, lista invariata).
        deps = self._lease_filter(deps)
        # ORDINAMENTO DETERMINISTICO per -go/-fallback: niente metriche
        # (reputation, latenza, recency, _score). Solo `data` (giorno rinnovo
        # -> sort_key) e `model_preference`. Le metriche restano SOLO come
        # filtri di eleggibilita' (cooldown, _ok, inflight-guard, capacita').
        # Entro il tier (stesso data+pref) si pesca a caso: non si surriscalda
        # sempre la stessa chiave, ma la famiglia giusta resta davanti.
        if deps and self.config.group_caps.get(group_name) is None \
                and (group_name.endswith(self.config.go_suffix or "-go")
                     or group_name.endswith(self.config.fallback_suffix
                                            or "-fallback")):
            # RICHIESTA ESPLICITA sul bucket: il detentore cache della
            # sessione vince sul tier (stessa chiave = KV-cache calda, i
            # crediti si "sommano" un account alla volta: al 429 il holder
            # si esclude da solo e il pick prosegue nell'ordine normale).
            # MODELLI PREFERITI (regola utente: -go sempre deepseek-v4.1-flash
            # se disponibile): se nel bucket ci sono chiavi VIVE del modello
            # preferito, restringi a loro — il holder non scavalca la scelta.
            deps = self._filter_go_preferred(deps)
            if prefer_holder:
                _ch = self.cache_holder(need=need, ctx=ctx)
                if _ch and any(_ch["unique"] == d["unique"] for d in deps):
                    log.info("[pick-final] %s chosen=%s (esplicito: detentore "
                             "cache, tier scavalcato)", group_name,
                             _ch["unique"])
                    return _ch
            _key = lambda d: (float(d.get("sort_key", float("inf"))),
                              -int(d.get("model_preference", 0) or 0))
            best_key = min(_key(d) for d in deps)
            best = [d for d in deps if _key(d) == best_key]
            # Entro il tier: privilegia SEMPRE il detentore cache della
            # sessione (stessa chiave = cache calda); altrimenti random.
            chosen = None
            _ch = self.cache_holder(need=need, ctx=ctx)
            if _ch and any(_ch["unique"] == d["unique"] for d in best):
                chosen = _ch
                log.info("[pick-final] %s chosen=%s (go/fallback: data+pref, "
                         "cache-holder)", group_name, chosen["unique"])
            else:
                chosen = random.choice(best)
                log.info("[pick-final] %s chosen=%s (go/fallback: solo data+"
                         "pref, tier=%s, %d chiavi)", group_name,
                         chosen["unique"], best_key, len(best))
            return chosen
        # COLD SPREAD: nasconde il 20% piu' usato (finestra 24h) PRIMA del
        # filtro `order`, cosi' il carico si distribuisce anche su order
        # diversi senza dipendere dalla gerarchia. Solo mondo testo dims.
        if deps and self.config.group_caps.get(group_name) is None \
                and self.DIM_SUFFIX_RE.search(group_name):
            deps = self._spread_hide(deps)
        # TIER esplicito (colonna `order`): nei gruppi TESTO dims si prova
        # prima il tier col valore minimo tra i vivi; se e' tutto in cooldown
        # si scende automaticamente al tier successivo. Dentro il tier resta
        # il pick adattivo (reputation/latenza/priority). Solo mondo testo:
        # le catene capacita' (-vision, -audio, ...) non sono toccate.
        if deps and self.config.group_caps.get(group_name) is None \
                and self.DIM_SUFFIX_RE.search(group_name):
            min_order = min(int(d.get("order", ORDER_LAST)) for d in deps)
            deps = [d for d in deps
                    if int(d.get("order", ORDER_LAST)) == min_order]
        # STICKINESS gen/stt: attacca le richieste consecutive allo stesso
        # modello upstream (voce/stile coerenti); le chiavi gemelle continuano
        # a ruotare per recency/EMA dentro il sottoinsieme. Nessuno vivo ->
        # pool completo (e un eventuale cross-model in fallback lo aggiornerà).
        cap_g = self.config.group_caps.get(group_name)
        if cap_g is not None and self.policy.gen_same_model_failover \
                and cap_g in self.SAME_MODEL_PRIORITY_CAPS:
            last = self._gen_last_model.get(group_name)
            if last:
                same = [d for d in deps if d.get("model") == last]
                if same:
                    log.debug("[sticky-model] %s: vincolo a %s (%d/%d "
                              "chiavi vive)", group_name, last, len(same),
                              len(deps))
                    deps = same
        if not deps:
            return None
        now = time.time()
        if self.policy.adaptive_pick:
            # [Blocco 1] Reputation scoring: calcola punteggio per ogni deployment
            rep_scores = [(self._reputation_score(d["unique"], d, ctx), d)
                          for d in deps]
            # Trova il punteggio MINIMO (lower is better)
            min_rep = min(s for s, _ in rep_scores)
            # Filtra solo i candidati con punteggio minimo
            candidates = [d for s, d in rep_scores if s == min_rep]
            if len(candidates) == 1:
                return candidates[0]
            # Tie-breaker: model_preference più alto, poi media storica latenza
            if len(candidates) > 1:
                def _lat(_d):
                    return self._get_avg_latency(_d["unique"]) or float("inf")
                lat_sorted = sorted(
                    candidates,
                    key=lambda d: (-d.get("model_preference", 0), _lat(d)))
                top = lat_sorted[0]
                # Raggruppa quelli con stessa preferenza e stessa latenza migliore
                tie = [d for d in lat_sorted
                       if d.get("model_preference", 0) == top.get("model_preference", 0)
                       and _lat(d) == _lat(top)]
                if len(tie) == 1:
                    log.debug("[pick] %s rep=%.1f tie-break-pref/lat -> %s",
                              group_name, min_rep, tie[0]["unique"])
                    return tie[0]
                # Ancora parità: usa legacy score come ultimo tie-breaker
                weights = [self._score(d, now) for d in tie]
                chosen = random.choices(tie, weights=weights, k=1)[0]
                log.debug("[pick] %s rep=%.1f tie-break-legacy -> %s",
                          group_name, min_rep, chosen["unique"])
                log.info("[pick-final] %s chosen=%s min_rep=%.1f (%d candidates)", group_name, chosen["unique"], min_rep, len(candidates))
                return chosen
            log.debug("[pick] %s rep=%.1f", group_name, min_rep)
        else:
            weights = [d.get("priority", 0) + 1 for d in deps]
            return random.choices(deps, weights=weights, k=1)[0]

    def _walk_chain(self, chain: list[str], failed_unique: str | None,
                    need: frozenset[str] | None = None,
                    ctx: int | None = None,
                    prefer_model: str | None = None, *,
                    ignore_cooldown: bool = False,
                    min_cooldown_age: float | None = None,
                     limit: int = 0,
                     tried: set[str] | None = None,
                     allow_slow: bool = False) -> dict | None:
        """Cammina una catena piatta di univoci saltando cooled-down,
        deployment senza le capacità `need`, (se ctx) sopra max_input e —
        per richieste pure-testo su catene dims — i multimodali finché
        esiste un text-only più avanti (multimodal_last_resort).

        Con `prefer_model` (failover same-model nei gruppi gen/stt) prova
        PRIMA i candidati con quello stesso modello upstream; solo se non
        ce ne sono di vivi attraversa su modelli diversi, loggando.

        Rilassamento cooldown (usato da _walk_ladder_resilient):
        - ignore_cooldown=True: il cooldown non conta;
        - min_cooldown_age=N: un cooled-down torna candidato SE e' in pausa da
          piu' di N secondi (forse la chiave si e' svegliata).

        `tried`: set di deployment già tentati in questa richiesta — vengono
        saltati a prescindere. Se fornito, ha priorità su `failed_unique`."""
        start = 0
        if failed_unique and failed_unique in chain:
            start = chain.index(failed_unique) + 1

        # "skip after N" REALE: quando `ladder_skip_after` membri di uno stesso
        # gruppo sono già stati tentati in QUESTA richiesta, il resto del gruppo
        # si salta — si sale di dim invece di rovistare decine di key free dello
        # stesso bucket finché scade stream_total_deadline_ms (il pool gratuito
        # di un profilo può avere >30 deployment per dim: senza questo taglio la
        # scala non raggiunge mai -go/-fallback dentro la deadline).
        # SOLO sul walk "vivo" (step 1 della ladder): gli step di riesumazione
        # cooldown (min_cooldown_age / ignore_cooldown: stantii, ultima
        # spiaggia, -fallback) NON sono gated — lì le key untried di un gruppo
        # già battuto vanno comunque riprovate prima di sforare sul pagato.
        # La soglia è fissa (`ladder_skip_after`), non il `limit` variabile
        # del singolo step, per non escludere un gruppo a soglie incoerenti.
        exhausted_groups: set[str] = set()
        _live_walk = (min_cooldown_age is None and not ignore_cooldown)
        # COLD SPREAD: sul solo walk VIVO nascondi il 20% piu' usato (24h)
        # per-gruppo, cosi' anche la scala piatta spalma il carico.
        _hidden = self._spread_hidden_chain(chain) if _live_walk else set()
        if tried and limit > 0 and _live_walk:
            _skip_after = max(1, int(getattr(self.policy,
                                             "ladder_skip_after", 4) or 4))
            _gc: dict[str, int] = {}
            for _u in tried:
                _d = self.config.deployment_by_unique(_u)
                if _d:
                    _g = _d.get("group", "")
                    _gc[_g] = _gc.get(_g, 0) + 1
            exhausted_groups = {g for g, n in _gc.items() if n >= _skip_after}

        def _eligible(u: str) -> dict | None:
            if tried and u in tried:
                return None
            dep = self.config.deployment_by_unique(u)
            if not dep:
                return None
            if exhausted_groups and dep.get("group") in exhausted_groups:
                return None
            if not ignore_cooldown and self.is_cooled_down(u):
                if min_cooldown_age is None:
                    return None
                age = self.cooldown_age(u)
                if age is None or age < min_cooldown_age:
                    return None
                # cooldown "stantio": lo ri-consideriamo
            if self.other_session_recent(u):
                return None
            # F25: modello in breaker aperto -> skip (anche nel walk della scala)
            if self._model_blocked(dep):
                return None
            if not allow_slow and self.is_slow_for_session(u, ctx=ctx):
                return None
            if u in _hidden:
                return None
            if self.is_retired(u):
                return None
            if self._endpoint_quarantined(dep):
                return None
            if self._gemini_blocked(dep):
                return None
            if need and not self._dep_supports(dep, need):
                return None
            # guardia universale (vedi pick_deployment): _cap_fits e' no-op
            # sicura quando ctx e' None o max_input<=0.
            if not self._cap_fits(dep, ctx):
                return None
            return dep

        if limit > 0:
            eligible = []
            for u in chain[start:]:
                d = _eligible(u)
                if d is not None:
                    eligible.append((u, d))
                    if len(eligible) >= limit:
                        break
        else:
            eligible = [(u, d) for u in chain[start:]
                        if (d := _eligible(u)) is not None]
        if not eligible:
            return None
        # protezione quota: preferisci i text-only PRESERVANDO l'ordine
        gname = eligible[0][1]["group"]
        pool = [d for _, d in eligible]
        preferred, _ = self._defer_media(gname, need, pool)
        # LEASE (P2-8, opt-in): depriorizza le chiavi con troppe richieste in
        # volo (soft: se tutte sono al cap la lista resta intera).
        preferred = self._lease_filter(preferred)

        # failover same-model (gruppi gen/stt): prima le chiavi gemelle
        if prefer_model:
            same = [d for d in preferred
                    if d.get("model") == prefer_model]
            if same:
                return same[0]
            if preferred and preferred[0].get("model") != prefer_model:
                self._note_cross(gname, prefer_model,
                                 preferred[0].get("model", "?"))
            elif not preferred:
                return None

        if preferred:
            # MODELLI PREFERITI nei bucket -go/-fallback (regola utente): se
            # tra i candidati vivi del bucket c'e' il modello preferito,
            # prendi il PRIMO quello (l'ordine della catena fa comunque
            # ruotare le chiavi gemelle del modello).
            _pm = self._go_pref()
            if _pm:
                _hit = [d for d in preferred
                        if self._is_go_bucket(d.get("group") or "")
                        and self._go_pref_hit(d)]
                if _hit:
                    return _hit[0]
            return preferred[0]
        return None

    def note_warm_owner(self, session_id: str | None, unique: str) -> None:
        """Registra la PROVA DI FUNZIONAMENTO di un deployment per la sessione
        (solo ownership warm, NIENTE holder/reputazione/latency).

        Usata per i canary che hanno PRODOTTO contenuto ma hanno perso la gara:
        il deploy e' buono (l'abbiamo visto rispondere), quindi entra subito
        nella lista warm del profilo. Cosi' il prossimo giro la warm sa gia'
        dove andare e non serve andare a caccia. Non diventa holder: il primo
        posto resta di chi ha servito davvero la risposta."""
        if not session_id or not unique:
            return
        try:
            self._note_dep_session(session_id, unique)
        except Exception:                      # mai rompere la risposta
            log.debug("[warm] note_warm_owner %s fallito", unique,
                      exc_info=True)

    def _is_known_nonstream(self, unique: str) -> bool:
        """True se il dep e' noto per IGNORARE stream:true (risponde JSON e
        viene adattato). Un canary cosi' non puo' vincere la gara: lo si
        esclude dal pool dei sostituti."""
        s = self._stats.get(unique)
        return bool(s and getattr(s, "json_fallback", 0) >= 2)

    def _nonstream_blocked(self, unique: str) -> bool:
        """True se il dep va ESCLUSO dai pool sostituti perche' noto
        non-stream e la policy non lo ammette. Con
        `nonstream_canary_allowed` (default True) un dep che ignora
        stream:true viene ADATTATO a SSE e CONSEGNATO: vale la consegna
        finale, non la forma del transport (regola utente: "sia stream che
        non stream indistintamente"). False storica = solo SSE nativo."""
        if not self._is_known_nonstream(unique):
            return False
        try:
            allowed = bool(getattr(self.policy, "nonstream_canary_allowed",
                                   True))
        except Exception:                          # noqa: BLE001
            allowed = True
        return not allowed

    def hedge_canaries(self, profile: str | None, dep: dict,
                       need: frozenset[str] | None, ctx: int | None,
                       tried: set[str] | None, requested_group: str | None,
                       k: int = 2, exclude: set[str] | None = None,
                       fresh_only: bool = False) -> list[dict]:
        """Fino a `k` candidati NUOVI per la gara sul primo contenuto.

        Regole: mai A ne' i gia' provati; mai bucket pagati (-go/-fallback);
        mai ritirati permanenti; mai sotto il floor di dim richiesto dal
        client; mai dep noti non-streaming. Tier CRESCENTI: prima i tier
        diversi da quello di A, poi (se serve) quello di A. Con
        `fresh_only=True` (eletto warm lento) si esclude TUTTO il warm della
        sessione (vogliamo candidati nuovi) e si preferiscono i MENO USATI
        nelle 24h."""
        k = max(1, int(k))
        a_u = dep.get("unique")
        ex = set(tried or ())
        if a_u:
            ex.add(a_u)
        for u in (exclude or ()):
            if u:
                ex.add(u)
        if fresh_only:
            try:
                ex |= set(self._sess_deps().get(current_session(), ()))
            except Exception:                  # noqa: BLE001
                pass
        floor = 0
        if requested_group:
            try:
                floor = int(self._group_min_dim(requested_group) or 0)
            except Exception:                  # noqa: BLE001
                floor = 0
        a_group = dep.get("group")
        ladder = self.config.chains.get(profile or "", []) or []
        tiers: list[tuple[int, str, list[str]]] = []
        cur_g = None
        cur: list[str] = []
        cur_mxi = 0
        for u in ladder:
            d = self.config.deployment_by_unique(u)
            if not d:
                continue
            g = str(d.get("group") or "")
            if not self._free_group(g):
                continue                       # solo dim gratis
            if self._endpoint_quarantined(d):
                continue                       # ban/ToS: host in quarantena
            if self.is_retired(u):
                if not self._retired_usable(u):
                    continue
                continue                       # i ritirati non sono canary
            mxi = int(d.get("max_input_tokens") or 0)
            if floor and mxi and mxi < floor * 1000:
                continue                       # mai sotto la dim richiesta
            if self._nonstream_blocked(u):
                continue
            if g != cur_g:
                if cur:
                    tiers.append((cur_mxi, cur_g, cur))
                cur_g, cur, cur_mxi = g, [], mxi
            cur.append(u)
        if cur:
            tiers.append((cur_mxi, cur_g, cur))
        tiers.sort(key=lambda t: (t[1] == a_group, t[0]))
        out: list[dict] = []
        taken: set[str] = set()
        for _mxi, _g, us in tiers:
            if len(out) >= k:
                break
            if fresh_only:
                us = sorted(us, key=lambda u: self.usage_weight_24h(u))
            got = self._walk_chain(us, None, need, ctx, tried=ex)
            if got is not None and got["unique"] not in taken:
                taken.add(got["unique"])
                ex.add(got["unique"])
                out.append(got)
        return out[:k]

    def prelast_shared(self, uniques: list[str], failed_unique: str | None,
                       need: frozenset[str] | None = None,
                       ctx: int | None = None,
                       tried: set[str] | None = None) -> dict | None:
        """TIER PRE-ULTIMA-SPIAGGIA (prima del -fallback a pagamento): tra i
        free-dims VIVI occupati da un'ALTRA sessione negli ultimi
        `session_dep_guard_sec`, sceglie quello col max_input piu' piccolo che
        regge la richiesta (fit migliore). Nessun cooldown viene toccato: e'
        una condivisione temporanea; appena la sessione occupante smette o
        scade la finestra il deployment torna normale. Ritorna None se non ce
        ne sono. La guardia e' direzionale (ultimo successo)."""
        if not getattr(self.policy, "session_dep_guard_enabled", True):
            return None
        tried = tried or set()
        seen: set[str] = set()
        by_group: dict[str, list[dict]] = {}
        for u in uniques:
            if not u or u in seen:
                continue
            seen.add(u)
            if u == failed_unique or u in tried:
                continue
            if not self.other_session_recent(u):
                continue
            if self.is_slow_for_session(u, ctx=ctx):
                continue
            dep = self.config.deployment_by_unique(u)
            if dep is None or self.is_retired(u) or self.is_cooled_down(u):
                continue
            if self._endpoint_quarantined(dep):
                continue
            if self._gemini_blocked(dep):
                continue
            if need and not self._dep_supports(dep, need):
                continue
            if not self._cap_fits(dep, ctx):
                continue
            by_group.setdefault(dep.get("group", ""), []).append(dep)
        if not by_group:
            return None
        cands: list[dict] = []
        for g, ds in by_group.items():
            kept, _ = self._defer_media(g, need, ds)
            cands.extend(kept or ds)
        if not cands:
            return None
        cands.sort(key=lambda d: (int(d.get("max_input_tokens") or 0)
                                  or (1 << 62)))
        dep = cands[0]
        ent = self._dep_sess().get(dep["unique"]) or (None, 0.0)
        log.info("[prelast] sessione condivisa: %s (max_in=%s, eta=%.0fs) -> "
                 "usato prima di scendere al -fallback",
                 dep["unique"], int(dep.get("max_input_tokens") or 0),
                 max(0.0, time.time() - (ent[1] or 0.0)))
        return dep

    # ---------------------------------------------------- warm pool (caldi)
    def _group_profile(self, group_name: str) -> str | None:
        """Nome PROFILO (modello base) di un gruppo TESTO: da `<prefix>pname`
        o `<prefix>pname-Nk`/`-go`/`-fallback`. None per gruppi capacita' o
        profili ignoti (il pool caldi e' solo free-dims testo)."""
        cfg = self.config
        for suf in (cfg.fallback_suffix, cfg.go_suffix):
            if suf and group_name.endswith(suf):
                base = group_name[:-len(suf)]
                if base.startswith(cfg.proxy_prefix):
                    p = base[len(cfg.proxy_prefix):]
                    return p if p in cfg.profile_dims else None
        m = self.DIM_SUFFIX_RE.search(group_name)
        if m:
            base = group_name[:m.start()]
            if base.startswith(cfg.proxy_prefix):
                p = base[len(cfg.proxy_prefix):]
                return p if p in cfg.profile_dims else None
        return None

    def _warm_ttl(self) -> float:
        """Finestra di validita' del pool caldi (0 = session_dep_guard_sec)."""
        try:
            v = float(getattr(self.policy, "warm_pool_ttl_sec", 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        return v if v > 0 else self._guard_sec()

    def _group_min_dim(self, group_name: str | None) -> int:
        """Dim minima (in k) richiesta da un gruppo TESTO.

        `-Nk` -> N (SOGLIA MINIMA: il client non vuole dim inferiori); per i
        bucket -go/-fallback o gruppi senza dim ritorna 0 (nessun floor)."""
        if not group_name:
            return 0
        m = self.DIM_SUFFIX_RE.search(group_name)
        return int(m.group(1)) if m else 0

    def _warm_allowed(self, pname: str, group_name: str | None) -> set[str]:
        """Univoci ammessi nel pool caldi per `pname` IMPONENDO la dim minima
        del gruppo di partenza: mai dim < dim(group_name), anche se la
        sessione le ha gia' servite con successo (es. richiesta esplicita
        `...-200k` -> il caldo `-64k` della stessa sessione NON va pescato).

        I bucket -go/-fallback sono inclusi ma il pool li ignora comunque
        (l'ownership caldi traccia solo i free-dims)."""
        floor = self._group_min_dim(group_name)
        return set(self._text_ladder(pname, start_dim=floor))

    def _warm_pool(self, session_id: str | None, allowed: set[str] | None,
                   need: frozenset[str] | None = None,
                   ctx: int | None = None,
                   tried: set[str] | None = None,
                   failed_unique: str | None = None) -> list[dict]:
        """Tier "caldi": free-dims che QUESTA sessione ha gia' servito con
        SUCCESSO entro la finestra warm, ancora vivi (no cooldown/retired/
        draining) e compatibili con `need` + `max_input`/contesto (`_cap_fits`).

        Lista ORDINATA: cache-holder di sessione, poi MRU (`last_used`), poi
        `order`, poi `max_input` crescente. Vuota se disabilitato, senza
        sessione, o senza candidati. `allowed` limita al MONDO richiesto
        (catena del profilo); None = nessun filtro di mondo."""
        if not getattr(self.policy, "warm_pool_enabled", True):
            return []
        sid = session_id or current_session()
        if not sid:
            return []
        owned = self._sess_deps().get(sid)
        if not owned:
            return []
        d = self._dep_sess()
        ttl = self._warm_ttl()
        now = time.time()
        skip = tried or set()
        holder = self.session_holder(sid)
        out: list[dict] = []
        for u in owned:
            if u in skip or u == failed_unique:
                continue
            if allowed is not None and u not in allowed:
                continue
            ent = d.get(u)
            if not ent or ent[0] != sid:
                continue
            if now - ent[1] > ttl:
                continue
            dep = self.config.deployment_by_unique(u)
            if dep is None or self.is_retired(u) or self.is_draining(u):
                continue
            if self._endpoint_quarantined(dep):
                continue
            if self.is_cooled_down(u) or self._gemini_blocked(dep):
                continue
            if self._is_demoted_dep(u, sid, ctx,
                                    allow_slow=self._warm_allow_slow()):
                log.debug("[warm] skip (chiave satura o demote): %s", u)
                continue
            if need and not self._dep_supports(dep, need):
                continue
            if not self._cap_fits(dep, ctx):
                continue
            out.append(dep)
        if not out:
            return []
        out.sort(key=lambda dep: (
            0 if holder and dep["unique"] == holder else 1,
            -(self.stats_for(dep["unique"]).last_used or 0.0),
            int(dep.get("order", ORDER_LAST)),
            int(dep.get("max_input_tokens") or 0),
        ))
        max_n = max(0, int(getattr(self.policy, "warm_pool_max_attempts", 0) or 0))
        if max_n > 0:
            out = out[:max_n]
        log.debug("[warm] pool=%d sid=%s: %s", len(out), sid,
                  ",".join(d["unique"] for d in out[:6]))
        return out

    # ---------------------------------------------------- WARM REFILL (cascata)
    def _reasoning_reserve_frac(self) -> float:
        """Frazione di finestra riservate al reasoning dei modelli
        effort_capable: stesso numero usato dal clamp (1 - headroom_ratio)."""
        try:
            return max(0.0, 1.0 - float(getattr(
                self.policy, "cache_ctx_reasoning_headroom_ratio", 0.7)))
        except Exception:                              # noqa: BLE001
            return 0.30

    def dep_deliverable(self, dep: dict, need: frozenset[str] | None,
                        ctx: int | None, out_tokens: int | None) -> bool:
        """Il dep puo' DAVVERO consegnare la risposta con QUESTI token:
        capacita' `need` + `ctx + safety(5%) + output(+riserva reasoning se
        effort_capable)` dentro la finestra. E' il criterio di validita' del
        warm-refill: '3 in warm' conta solo candidati che possono servire, non
        semplici reduci. max_input<=0 = nessun guardo (come _cap_fits)."""
        if need and not self._dep_supports(dep, need):
            return False
        mi = self._eff_max_input(dep)
        if mi <= 0:
            return True
        room = mi - int(ctx or 0) - int(mi * 0.05)
        try:
            out = int(out_tokens or 0)
        except (TypeError, ValueError):
            out = 0
        if out > 0 and dep.get("effort_capable"):
            frac = self._reasoning_reserve_frac()
            if frac > 0:
                room -= int(mi * frac)
        return room >= max(0, out)

    # ------------------------------- MAX_INPUT SCOPERTO DAL PROVIDER (413)
    # Se il CSV mente (dichiara 32k ma il provider taglia a 16k) il primo 413
    # rivela il vero limite nel body ("maximum context length is 16384").
    # Registriamo il limite scoperto e lo usiamo come cap EFFETTIVO: solo
    # RESTRINGERE (mai oltre il dichiarato), persistito nel routing_state.
    def _discovered(self) -> dict:
        d = getattr(self, "_discovered_max_input", None)
        if d is None:
            d = self._discovered_max_input = {}
        return d

    def note_discovered_max_input(self, unique: str | None,
                                  limit: int | None) -> None:
        """Il provider ha rivelato il VERO limite di input: ridimensiona il
        deployment cosi' il router non gli rimanda payload troppo grossi."""
        if not unique or not limit:
            return
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            return
        if lim <= 0:
            return
        dep = self.config.deployment_by_unique(unique) or {}
        mi = int(dep.get("max_input_tokens") or 0)
        if mi > 0:
            lim = min(lim, mi)          # mai OLTRE il dichiarato
        cur = self._discovered().get(unique)
        if cur and cur <= lim:
            return                      # gia' noto un limite piu' stretto
        self._discovered()[unique] = lim
        log.warning("[max-input] %s: limite reale scoperto dal provider = %d "
                    "token (csv=%s) -> ridimensionato", unique, lim,
                    mi or "0")

    def _eff_max_input(self, dep: dict) -> int:
        """max_input EFFETTIVO: min(dichiarato, scoperto dal provider)."""
        mi = int(dep.get("max_input_tokens") or 0)
        try:
            dis = self._discovered().get(dep.get("unique"))
        except Exception:                              # noqa: BLE001
            dis = None
        if dis and (mi <= 0 or dis < mi):
            return int(dis)
        return mi

    def _owned_by_any_session(self, unique: str) -> bool:
        """True se il dep ha un OWNER vivo (UNA qualsivoglia sessione): un
        candidato del refill deve essere LIBERO, non rubato a chi lo usa gia'."""
        ent = self._dep_sess().get(unique)
        if not ent:
            return False
        try:
            return (time.time() - ent[1]) < self._guard_sec()
        except Exception:                              # noqa: BLE001
            return False

    # ------------------------------------------- PROBES IN VOLO (tetto 4/sess)
    # Contiamo TUTTO lo speculativo ancora in corsa per la sessione (canari
    # refill e legacy, A/loser staccati come probe): il gate del refill si
    # ferma a `warm_refill_max_inflight` in volo per non riaccendere a ogni
    # turno una tempesta di chiamate che poi prende 429. Il cleanup VERO
    # avviene nel finally di ogni probe (che e' bounded: drain cap / wait_for
    # 900s); il TTL qui sotto e' solo la rete di sicurezza.
    def _probes(self) -> dict:
        d = getattr(self, "_probes_flight", None)
        if d is None:
            d = self._probes_flight = {}
        return d

    def note_probe_started(self, session_id: str | None,
                           unique: str | None) -> None:
        if not session_id or not unique:
            return
        now = time.time()
        m = self._probes().setdefault(session_id, {})
        m[unique] = now
        if len(m) > 64:                       # rete: spazza i dimenticati
            for u, ts in list(m.items()):
                if now - ts > 950:
                    m.pop(u, None)

    def note_probe_done(self, session_id: str | None,
                        unique: str | None) -> None:
        if not session_id or not unique:
            return
        m = self._probes().get(session_id)
        if m:
            m.pop(unique, None)
            if not m:
                self._probes().pop(session_id, None)

    def probes_in_flight(self, session_id: str | None = None) -> int:
        sid = session_id or current_session()
        if not sid:
            return 0
        m = self._probes().get(sid)
        if not m:
            return 0
        now = time.time()
        for u, ts in list(m.items()):
            if now - ts > 950:
                m.pop(u, None)
        return len(m)

    # ------------------------------------- QUARANTENA ENDPOINT (ban / ToS IP)
    # Se un provider risponde "Access from this IP ... ip_banned" o
    # "policy_review_required" il problema NON e' la singola chiave: e'
    # l'INTERO endpoint per il NOSTRO IP. Mettiamo in quarantena l'HOST per
    # `seconds` (default 24h): nessun deployment su quell'host e' eleggibile
    # (rotazione, warm, canary, risvegli, ultima spiaggia) e la quarantena
    # SCDE da sola, senza ri-provare a martellate (i 253 hit llm7.io di
    # scalifai nascevano proprio dal bruciare una chiave dopo l'altra).
    def _ep_quar(self) -> dict:
        d = getattr(self, "_endpoint_quarantine", None)
        if d is None:
            d = self._endpoint_quarantine = {}
        return d

    def quarantine_endpoint(self, host: str, seconds: float = 86400.0,
                            reason: str = "ban/ToS") -> None:
        if not host:
            return
        q = self._ep_quar()
        until = time.time() + max(0.0, float(seconds))
        if q.get(host, 0.0) >= until:
            return
        q[host] = until
        log.warning("[quarantina] endpoint %s fuori gioco per %.0fs (%s)",
                    host, max(0.0, float(seconds)), reason)

    def _endpoint_quarantined(self, dep: dict | None) -> bool:
        """True se l'HOST dell'endpoint del dep e' in quarantena ban/ToS."""
        if not dep:
            return False
        q = self._ep_quar()
        if not q:
            return False
        url = str(dep.get("api_base") or dep.get("endpoint") or "")
        if not url:
            return False
        try:
            host = urllib.parse.urlparse(url).hostname or ""
        except Exception:                              # noqa: BLE001
            return False
        if not host:
            return False
        until = q.get(host, 0.0)
        if until <= 0:
            return False
        if time.time() >= until:
            q.pop(host, None)                          # scaduta: pulisci
            return False
        return True

    def endpoint_quarantine_view(self) -> dict:
        now = time.time()
        q = self._ep_quar()
        out = {}
        for h, until in list(q.items()):
            if now >= until:
                q.pop(h, None)
            else:
                out[h] = round(until - now)
        return out

    def warm_valid_for(self, session_id: str | None, profile: str | None,
                       group_name: str | None,
                       need: frozenset[str] | None, ctx: int | None,
                       out_tokens: int | None,
                       tried: set[str] | None = None,
                       failed_unique: str | None = None) -> list[dict]:
        """I caldi della sessione che possono EFFETTIVAMENTE servire questa
        richiesta (need + ctx + output assicurato): e' COSI' che si contano i
        "3 pronti-caldi" del refill, non il numero grezzo del pool."""
        if not profile:
            return []
        allowed = self._warm_allowed(profile, group_name)
        pool = self._warm_pool(session_id, allowed, need, ctx, tried,
                               failed_unique)
        return [d for d in pool
                if self.dep_deliverable(d, need, ctx, out_tokens)]

    def warm_api_keys(self, session_id: str | None, pname: str | None,
                      group_name: str | None) -> set[str]:
        """Chiavi api gia' rappresentate (stessa api_key) dai deployment nel
        warm della
        sessione: un probe di refill NON deve testare una chiave che abbiamo
        gia' nel parco dei caldi."""
        if not pname:
            return set()
        try:
            pool = self._warm_pool(session_id,
                                   self._warm_allowed(pname, group_name))
        except Exception:                          # noqa: BLE001
            return set()
        return {str(d.get("api_key") or "") for d in pool
                if d.get("api_key")}

    def _tiers_of(self, uniqs, group: str | None = None) -> set[int]:
        """Tier `order` dei deployment indicati (per il round-robin canary).
        Con `group` filtra solo quel gruppo: i tier sono PER-GRUPPO, quindi
        un tier sondato in un altro -dim non deve marcare anche questo."""
        out: set[int] = set()
        for u in uniqs or ():
            d = self.config.deployment_by_unique(u)
            if not d:
                continue
            if group is not None and str(d.get("group") or "") != str(group):
                continue
            try:
                out.add(int(d.get("order", ORDER_LAST)))
            except Exception:                          # noqa: BLE001
                continue
        return out

    def _group_dim_order_key(self, group: str) -> tuple[int, int]:
        """Chiave di ordinamento DIM-MAJOR (regola utente): prima TUTTE le
        -dim, dalla piu' bassa (quella richiesta) in su, poi i gruppi non-dim.
        Serve a far SCAVARE al canary/sveglia la -dim richiesta fino
        all'esaurimento prima di salire a quella superiore."""
        m = self.DIM_SUFFIX_RE.search(str(group or ""))
        if not m:
            return (1, 0)
        return (0, int(m.group(1)))

    def _canary_cold_pick(self, cands: list[dict], ctx: int | None,
                          sampled_tiers: set[int] | None = None
                          ) -> dict | None:
        """Scelta del candidato canary/sveglia "come una chiamata a freddo"
        (regola utente), con ROUND-ROBIN SUI TIER `order`:

        - si parte sempre dal tier `order` MINIMO NON ancora sondato in questo
          giro (1 probe per tier, in ordine crescente);
        - quando tutti i tier sono stati sondati si RICICLA dal piu' basso
          (le chiavi gia' provate restano escluse a monte, quindi si prende
          una chiave diversa);
        - dentro il tier si applica il cold spread (nasconde il 20% piu'
          usato) e il reputation scoring adattivo (fallback legacy: priority
          + model_preference).

        Cosi' la cascata scopre in pochi probe QUALE tier e' vivo invece di
        bruciare tutti i tentativi su un tier morto."""
        if not cands:
            return None
        _kept = self._spread_hide(cands)
        if _kept:
            cands = _kept
        _by_tier: dict[int, list[dict]] = {}
        for d in cands:
            try:
                _t = int(d.get("order", ORDER_LAST))
            except Exception:                          # noqa: BLE001
                _t = ORDER_LAST
            _by_tier.setdefault(_t, []).append(d)
        _tiers = sorted(_by_tier)
        _sampled = {int(t) for t in (sampled_tiers or ())}
        _fresh = [t for t in _tiers if t not in _sampled]
        _t = _fresh[0] if _fresh else _tiers[0]
        _tier = _by_tier.get(_t) or cands
        try:
            if getattr(self.policy, "adaptive_pick", True):
                return min(_tier, key=lambda d: (
                    self._reputation_score(d["unique"], d, ctx),
                    self.usage_weight_24h(d["unique"])))
            return min(_tier, key=lambda d: (
                int(d.get("priority", 0) or 0),
                -int(d.get("model_preference", 0) or 0),
                self.usage_weight_24h(d["unique"])))
        except Exception:                              # noqa: BLE001
            return _tier[0]

    # ---------------------------------- MODELLI PREFERITI nei bucket -go/-fb
    def _go_pref(self) -> list[str]:
        raw = str(getattr(self.policy, "go_preferred_models", "") or "")
        return [s.strip().lower() for s in raw.split(",") if s.strip()]

    def _is_go_bucket(self, group: str) -> bool:
        gs = self.config.go_suffix or "-go"
        fs = self.config.fallback_suffix or "-fallback"
        g = str(group or "")
        return g.endswith(gs) or g.endswith(fs)

    def _go_pref_hit(self, dep: dict) -> bool:
        mod = str(dep.get("model") or "").lower()
        return bool(mod) and any(p in mod for p in self._go_pref())

    def _filter_go_preferred(self, deps: list[dict]) -> list[dict]:
        """Nei bucket -go/-fallback: se tra i candidati vivi ce n'e' ALMENO
        uno col modello preferito (regola utente: -go -> sempre
        deepseek-v4.1-flash se disponibile), RESTRINGI a quelli; altrimenti
        comportamento normale (il preferito non c'e', non si blocca nulla)."""
        if not self._go_pref() or not deps:
            return deps
        same = [d for d in deps if self._go_pref_hit(d)]
        return same or deps

    def warm_fill_canary(self, profile: str | None, cur_dep: dict,
                         need: frozenset[str] | None, ctx: int | None,
                         out_tokens: int | None,
                         tried: set[str] | None = None,
                         requested_group: str | None = None,
                         exclude_keys: set[str] | None = None,
                         exclude_uniq: set[str] | None = None,
                         sampled_tiers: set[int] | None = None) -> dict | None:
        """UN candidato probe per il warm-refill: percorre il ladder -dim
        ASCENDENTE partendo dal gruppo corrente (se nel -dim corrente non c'e'
        niente di buono si sale al -dim superiore) fermandosi ai FREE: i bucket
        -go/-fallback NON sono mai candidati. Esclusioni: api_key di dep gia'
        in warm, dep assegnati a UNA qualsivoglia sessione (owner vivo), dep
        gia' tentati/sondati in QUESTA richiesta, dep che non possono
        effettivamente consegnare (need + ctx + output assicurato).
        Nel -dim corrente la scelta e' "come a freddo" (_canary_cold_pick:
        cold-spread dei piu' usati + tier order + reputation): si SCAVA il
        -dim (gli tentativi successivi escludono i gia' provati) e solo a
        esaurimento si sale al -dim superiore.
        Ritorna il miglior candidato del primo -dim utile; None = esauriti."""
        cur = cur_dep.get("unique")
        ex: set[str] = set(tried or ())
        if cur:
            ex.add(cur)
        for u in (exclude_uniq or ()):
            if u:
                ex.add(u)
        keys = {str(k) for k in (exclude_keys or ()) if k}
        floor = 0
        if requested_group:
            try:
                floor = int(self._group_min_dim(requested_group) or 0)
            except Exception:                          # noqa: BLE001
                floor = 0
        go_suf = self.config.go_suffix or "-go"
        fb_suf = self.config.fallback_suffix or "-fallback"
        # "SCAVA IL -DIM" (regola utente): si raccolgono TUTTI i candidati
        # liberi del -dim corrente, si sceglie il migliore "come a freddo"
        # e solo quando quel -dim e' esaurito si sale al successivo. Il ladder
        # resta ASCENDENTE dal gruppo corrente; i bucket -go/-fallback non
        # sono mai candidati (FREE only).
        _by: dict[str, list[dict]] = {}
        _order: list[str] = []
        # FISSATO (regola utente "scava il -dim ESPPLICITO"): il ladder parte
        # dalla dim RICHIESTA, non dal gruppo della holder (che puo' essere
        # piu' alta: una sessione ancorata a -1000k non vedrebbe mai le free
        # -200k). Se la dim richiesta non da' ladder, ripiega su quello corrente.
        _lad = self._ladder_for_group(requested_group
                                      or cur_dep.get("group") or "")
        if not _lad:
            _lad = self._ladder_for_group(cur_dep.get("group") or "")
        for u in _lad:
            if u in ex:
                continue
            d = self.config.deployment_by_unique(u)
            if not d:
                continue
            g = str(d.get("group") or "")
            if g.endswith(go_suf) or g.endswith(fb_suf):
                continue                               # FREE only: qui si ferma
            if self.is_retired(u) or self.is_draining(u):
                continue
            if self._endpoint_quarantined(d):
                continue
            if self.is_cooled_down(u) or self._gemini_blocked(d):
                continue
            if self._nonstream_blocked(u):
                continue
            if self._owned_by_any_session(u):
                continue                               # occupato da qualcuno
            mxi = int(d.get("max_input_tokens") or 0)
            if floor and mxi and mxi < floor * 1000:
                continue
            if not self.dep_deliverable(d, need, ctx, out_tokens):
                continue
            k = str(d.get("api_key") or "")
            if k and k in keys:
                continue                               # chiave gia' in warm
            if g not in _by:
                _by[g] = []
                _order.append(g)
            _by[g].append(d)
        _sam0 = {int(t) for t in (sampled_tiers or ())}
        # DIM-MAJOR (regola utente): si SCAVA la -dim richiesta fino
        # all'esaurimento e solo dopo si sale alla successiva; dentro la -dim
        # la scelta resta "come a freddo" (tier round-robin + spread dei piu'
        # usati). L'ordine del ladder (tier-major) non decide piu' il salto di
        # dimensione: senza questo, una -dim piu' profonda con `order` basso
        # veniva sondata prima di finire quella richiesta.
        _order.sort(key=self._group_dim_order_key)
        for g in _order:
            _pick = self._canary_cold_pick(
                _by[g], ctx, _sam0 | self._tiers_of(ex, g))
            if _pick is not None:
                return _pick
        return None

    def session_api_keys(self) -> set[str]:
        """api_key dei dep posseduti da UNA QUALSIASI sessione viva.
        Regola utente: la SVEglia deve provare chiavi DIVERSE da tutte le
        sessioni (mai rubare/riprovare la chiave di qualcun altro)."""
        out: set[str] = set()
        now = time.time()
        try:
            guard = self._guard_sec()
        except Exception:                              # noqa: BLE001
            guard = 900.0
        for u, ent in list(self._dep_sess().items()):
            if not ent:
                continue
            try:
                if (now - float(ent[1])) >= guard:
                    continue
            except Exception:                          # noqa: BLE001
                continue
            d = self.config.deployment_by_unique(u)
            if d and d.get("api_key"):
                out.add(str(d["api_key"]))
        return out

    def warm_wake_canary(self, profile: str | None, cur_dep: dict,
                         need: frozenset[str] | None, ctx: int | None,
                         out_tokens: int | None,
                         tried: set[str] | None = None,
                         requested_group: str | None = None,
                         exclude_keys: set[str] | None = None,
                         exclude_uniq: set[str] | None = None,
                         min_age_sec: float = 3600.0,
                         sampled_tiers: set[int] | None = None) -> dict | None:
        """TERZO canario del warm-refill: non cerca un dep fresco ma un
        DORMIENTE da almeno `min_age_sec` (default 1h) messo in cooldown da
        un 429/quota — un vero e proprio SVEglia. Se risponde, il probe lo
        riporta caldo (clear_cooldown + warm owner): cosi' la capacita' che
        era stata messa in pausa torna utile senza aspettare l'autoprobe.
        Percorre lo stesso ladder -dim del refill (free only) con le stesse
        esclusioni, scegliendo "come a freddo" nello stesso modo; in piu' NON
        tocca i dep che non hanno un cooldown 429 maturo."""
        now = time.time()
        cur = cur_dep.get("unique")
        ex: set[str] = set(tried or ())
        if cur:
            ex.add(cur)
        for u in (exclude_uniq or ()):
            if u:
                ex.add(u)
        keys = {str(k) for k in (exclude_keys or ()) if k}
        # Chiave DIVERSA da TUTTE LE SESSIONI (regola utente): la Sveglia non
        # tocca mai una api_key gia' impegnata da sessione alcuna.
        keys |= self.session_api_keys()
        floor = 0
        if requested_group:
            try:
                floor = int(self._group_min_dim(requested_group) or 0)
            except Exception:                          # noqa: BLE001
                floor = 0
        go_suf = self.config.go_suffix or "-go"
        fb_suf = self.config.fallback_suffix or "-fallback"
        _by: dict[str, list[dict]] = {}
        _order: list[str] = []
        # FISSATO: anche la Sveglia scava dalla dim RICHIESTA (vedi refill).
        _lad = self._ladder_for_group(requested_group
                                      or cur_dep.get("group") or "")
        if not _lad:
            _lad = self._ladder_for_group(cur_dep.get("group") or "")
        for u in _lad:
            if u in ex:
                continue
            d = self.config.deployment_by_unique(u)
            if not d:
                continue
            g = str(d.get("group") or "")
            if g.endswith(go_suf) or g.endswith(fb_suf):
                continue                               # FREE only
            if not self.is_cooled_down(u):
                continue                               # cerchiamo dormienti
            since = float(self._cooldown_since.get(u) or 0.0)
            if since and (now - since) < max(0.0, float(min_age_sec)):
                continue                # troppo fresco: non e' un 429 maturo
            try:
                _reason = str(getattr(self.stats_for(u), "last_reason", "")
                              or "")
            except Exception:                          # noqa: BLE001
                _reason = ""
            if _reason not in ("http_429", "quota_exhausted",
                               "quota_exhausted_account"):
                continue             # solo 429/quota: mai svegliare un 403/ban
            # PROVENIENZA (P0): si sveglia SOLO un cooldown 'heuristic' (nostra
            # stima). Se il provider ha DICHIARATO quando torna (Retry-After /
            # reset quota = 'authoritative'), o e' credito/tier, ritentare
            # prima scadenza e' solo rumore e rischio ban.
            if not self.cooldown_probeable(u):
                continue
            if self.is_retired(u) or self.is_draining(u):
                continue
            if self._endpoint_quarantined(d) or self._gemini_blocked(d):
                continue
            if self._nonstream_blocked(u):
                continue
            if self._owned_by_any_session(u):
                continue
            mxi = int(d.get("max_input_tokens") or 0)
            if floor and mxi and mxi < floor * 1000:
                continue
            if not self.dep_deliverable(d, need, ctx, out_tokens):
                continue
            k = str(d.get("api_key") or "")
            if k and k in keys:
                continue
            if g not in _by:
                _by[g] = []
                _order.append(g)
            _by[g].append(d)
        _sam0 = {int(t) for t in (sampled_tiers or ())}
        # DIM-MAJOR (regola utente): si SCAVA la -dim richiesta fino
        # all'esaurimento e solo dopo si sale alla successiva; dentro la -dim
        # la scelta resta "come a freddo" (tier round-robin + spread dei piu'
        # usati). L'ordine del ladder (tier-major) non decide piu' il salto di
        # dimensione: senza questo, una -dim piu' profonda con `order` basso
        # veniva sondata prima di finire quella richiesta.
        _order.sort(key=self._group_dim_order_key)
        for g in _order:
            _pick = self._canary_cold_pick(
                _by[g], ctx, _sam0 | self._tiers_of(ex, g))
            if _pick is not None:
                return _pick
        return None

    def initial_pick(self, profile: str | None, group_name: str,
                     need: frozenset[str] | None = None,
                     ctx: int | None = None,
                     session_id: str | None = None,
                     warm: bool = True,
                     prefer_holder: bool = False,
                     prefer_fast: bool = False) -> dict | None:
        """Prima selezione dentro un gruppo; nessun candidato vivo ->
        cammina la catena DEL MONDO del gruppo (cap-chain per -C, testo
        per dims/-go/-fallback). Sostituisce pick+fallback_after in main.

        Con `session_id` e `deployment_sticky=True`: nei bucket FREE (dims
        -Nk, cap groups primari) la sessione resta INCOLLATA alla stessa key
        finché è viva (cache calda). Se la richiesta cresce e supera il
        max_input dello sticky, si cerca lo STESSO modello+chiave nel nuovo
        gruppo dim (crescita cache-preserving).

        Con `warm=True` (default), PRIMA dello sticky/del pick si esaurisce
        il pool "caldi" del profilo (free-dims gia' serviti con successo da
        questa sessione, vivi e compatibili ctx/need). Vale anche per le
        richieste esplicite su un -Nk; NON per -go/-fallback (escalation
        deliberata: warm=False lì)."""
        # --- WARM POOL (caldi propri): PRIMA del -dim e della scala -------
        if warm and self.config.group_caps.get(group_name) is None:
            _pname = self._group_profile(group_name)
            if _pname:
                _warm = self._warm_pool(
                    session_id, self._warm_allowed(_pname, group_name),
                    need=need, ctx=ctx)
                if _warm:
                    _dep = _warm[0]
                    # P3 (non-stream): se l'eletto e' LENTO e c'e' un caldo
                    # non-lento della stessa sessione, prendi quello (niente
                    # gara in non-stream: meglio non pagare un prefill lento).
                    if prefer_fast and self._is_demoted_dep(
                            _dep["unique"], session_id, ctx):
                        for _alt in _warm[1:]:
                            if not self._is_demoted_dep(
                                    _alt["unique"], session_id, ctx):
                                log.info("[warm] %s: eletto lento -> prendo "
                                         "il caldo non-lento %s",
                                         _dep["unique"], _alt["unique"])
                                _dep = _alt
                                break
                    log.info("[warm] initial_pick %s -> %s (caldo proprio: "
                             "my_success, max_in=%s)", group_name, _dep["unique"],
                             int(_dep.get("max_input_tokens") or 0))
                    if session_id and not self._is_renewal_bucket(group_name):
                        if getattr(self.policy,
                                   "deployment_sticky_per_capability", False):
                            self.dep_cap_sticky_set(session_id, need,
                                                    _dep["unique"])
                        elif self.policy.deployment_sticky:
                            self.dep_sticky_set(session_id, _dep["unique"])
                    return _dep
        # --- STICKY per-deployment (SOLO FREE, mai renewal/paid) ---------
        sticky_dep = None
        # Prima prova lo sticky per-capability se abilitato
        if session_id and not self._is_renewal_bucket(group_name):
            sticky_dep = self.dep_cap_sticky_get(session_id, need)
        # Fallback allo sticky classico (per-sessione)
        if not sticky_dep:
            sticky_dep = (session_id
                          and self.policy.deployment_sticky
                          and not self._is_renewal_bucket(group_name)
                          and self.dep_sticky_get(session_id))
        if sticky_dep:
            sd = self.config.deployment_by_unique(sticky_dep)
            # Validità dello sticky:
            # 1) esiste ancora nel CSV (hot-reload: deployment rimosso)
            # 2) non è in cooldown (errore/429 -> release automatico)
            # 3) il contesto stimato sta nel suo max_input (usa _cap_fits
            #    che gestisce sia max_input_tokens che fallback ctx_k*1000)
            # 4) soddisfa le capacità richieste (vision, audio, ...)
            if sd and sd.get("group") == group_name \
                    and not self.is_cooled_down(sticky_dep) \
                    and not self._gemini_blocked(sd) \
                    and not self.other_session_recent(sticky_dep) \
                    and not self._is_demoted_dep(
                        sticky_dep, session_id, ctx,
                        allow_slow=self._warm_allow_slow()) \
                    and self._cap_fits(sd, ctx) \
                    and (need is None or self._dep_supports(sd, need)):
                log.debug("[dep-sticky] %s riuso key %s (ctx≈%s)",
                          session_id, sticky_dep, ctx or "?")
                return sd
            # Sticky non valido: release e pesca normale
            self.dep_sticky_release(session_id)

        # --- PESCA NORMALE (adaptive_pick + recency) ---------------------
        dep = self.pick_deployment(group_name, need=need, ctx=ctx,
                                   prefer_holder=prefer_holder)
        # Se lo sticky era su un gruppo dim più piccolo e ora serve un gruppo
        # più grande: stesso provider+modello nella stessa sessione (crescita
        # cache-preserving). Il pick normale ha già scelto; se matcha
        # modello+key dello sticky, riagganciamo lo sticky al nuovo gruppo.
        if dep is not None:
            # --- SET STICKY: free bucket, sessione non anonima -------------
            if session_id and not self._is_renewal_bucket(group_name):
                if getattr(self.policy, "deployment_sticky_per_capability", False):
                    self.dep_cap_sticky_set(session_id, need, dep["unique"])
                elif self.policy.deployment_sticky:
                    self.dep_sticky_set(session_id, dep["unique"])
            return dep

        # Cooldown-wakeup: se pick_deployment non ha trovato nulla, prova il
        # deployment RAFFREDDATO da più tempo (stantio) e con cooldown residuo
        # minore nel dim corrente, PRIMA di escalationare a -go/fallback. Stessa
        # soglia degli "stantii" della scala: un cooldown fresco non si tocca.
        _chronic_thr = max(1, int(getattr(self.policy,
                                          "cooldown_retry_max_fail_24h", 10)
                                  or 10))
        _stale_age = float(getattr(self.policy, "stale_cooldown_retry_sec",
                                   300) or 300)
        _cooled = [d for d in self.config.groups.get(group_name, [])
                   if getattr(self.policy, "initial_pick_cooldown_wakeup", True)
                   and self.is_cooled_down(d["unique"])
                   and not self._gemini_blocked(d)
                   and not self.is_slow_for_session(d["unique"], ctx=ctx)
                   and self.stats_for(d["unique"]).fail_count_24h < _chronic_thr
                   and (self.cooldown_age(d["unique"]) or 0) >= _stale_age
                   and (need is None or self._dep_supports(d, need))
                   and self._cap_fits(d, ctx)]
        if _cooled:
            _cooled.sort(
                key=lambda d: self.cooldown_residual(d["unique"]))
            _wake = _cooled[0]
            _rem = int(self.cooldown_residual(_wake["unique"]))
            log.info("[cooldown-wakeup] %s: provo lo stantio meno raffreddato: "
                     "%s (residuo %ds)", group_name, _wake["unique"], _rem)
            return _wake

        # Nessuna pesca riuscita nel bucket richiesto: prima di rivisitare la
        # scala (che puo' costare una catena lunghissima), se c'e' un escalation
        # winner fresco/sano/sufficiente per QUESTO contesto, saltaci diretto.
        _ew = self._try_esc_win(group_name, need, ctx)
        if _ew is not None:
            # RICAMPIONAMENTO: se il bucket richiesto e' morto, NON saltare
            # subito al winner: sonda prima le dim intermedie (solo quelle,
            # niente retry del bucket morto). Se ne trovi una viva, parti da
            # lì; il resto della scala (2a intermedia -> winner) lo gestisce
            # fallback_next con requested_group.
            _p = self._esc_pin_probe(group_name, _ew, need, ctx, tried=None,
                                     allow_retry=False)
            _use = _p if _p is not None else _ew
            if session_id and not self._is_renewal_bucket(group_name):
                if getattr(self.policy, "deployment_sticky_per_capability", False):
                    self.dep_cap_sticky_set(session_id, need, _use["unique"])
                elif self.policy.deployment_sticky:
                    self.dep_sticky_set(session_id, _use["unique"])
            return _use
        # Nessuna pesca riuscita: catena/ladder come prima
        cap = self.config.group_caps.get(group_name)
        if cap is not None:
            chain = self.config.chains_cap.get(profile or "", {}).get(cap, [])
            return self._walk_chain(chain, None, need, ctx)
        if self.policy.dims_ladder_floor:
            chain = self._ladder_for_group(group_name)
            if chain:
                _go_suf = self.config.go_suffix or "-go"
                _fb_suf = self.config.fallback_suffix or "-fallback"

                def _is_rb(u: str) -> bool:
                    d = self.config.deployment_by_unique(u)
                    g = str(d.get("group", "")) if d else ""
                    return g.endswith(_go_suf) or g.endswith(_fb_suf)

                _dims = [u for u in chain if not _is_rb(u)]
                dep = self._walk_chain(_dims, None, need, ctx)
                if dep is not None:
                    log.info("[ladder] initial_pick: %s senza candidati "
                             "vivi -> scala (%d univoci) -> %s",
                             group_name, len(chain), dep["unique"])
                    return dep
                # PRE-ULTIMA SPIAGGIA (fra dims e -go): dims vivi occupati da
                # un'ALTRA sessione negli ultimi session_dep_guard_sec.
                dep = self.prelast_shared(_dims, None, need, ctx, None)
                if dep is not None:
                    return dep
                # -go/-fallback vivi
                dep = self._walk_chain([u for u in chain if _is_rb(u)],
                                       None, need, ctx)
                if dep is not None:
                    return dep
        chain = self.config.chains.get(profile or "", [])
        dep = self._walk_chain(chain, None, need, ctx)
        if dep is None:
            # ULTIMO SCAGLIONE: riammetti i free-dims 'lenti per la sessione'
            # (demoted) solo ora che tutto il resto e' esaurito.
            dep = self._walk_chain(chain, None, need, ctx, allow_slow=True)
        return dep

    def _walk_ladder_resilient(self, ladder: list[str],
                               failed_unique: str | None,
                               need: frozenset[str] | None = None,
                               ctx: int | None = None,
                               tried: set[str] | None = None) -> dict | None:
        """Cammina la scala testo con early-escalation e cooldown lineare.

        Sequenza (8 step):
          1) dims vivi — max ladder_skip_after candidati
          1bis) dims cooldown-wakeup (stantii, residuo minore)
          1ter) PRE-ULTIMA SPIAGGIA: dims vivi usati di recente da un'ALTRA
                sessione (session_dep_guard) — condivisi QUI, prima di -go
          2) -go vivi
          3) dims stantii (cooldown > stale_cooldown_retry_sec) — max ladder_stale_max
          4) -go stantii (dormiente, potrebbe essersi svegliato)
          4bis) PARACADUTE CRONICI (fail_24h >= soglia): riprovati qui, dal
                meno fallimentare al più fallimentare (a parità: cooldown
                residuo più breve), max ladder_chronic_max, PRIMA del -fallback
                a pagamento. Fallendo di nuovo: pausa minima 2h.
          5) -fallback INTERO (ignore cooldown — deve sempre rispondere)
          6) ULTIMA SPIAGGIA: non-cronici in cooldown, per residuo crescente

        Ritorna None solo se non esiste piu' niente."""
        if not ladder:
            return None
        cfg = self.config
        fb_suf = cfg.fallback_suffix or "-fallback"
        go_suf = cfg.go_suffix or "-go"
        pol = self.policy

        def _is_fb(u: str) -> bool:
            d = cfg.deployment_by_unique(u)
            return bool(d) and str(d.get("group", "")).endswith(fb_suf)

        def _is_go(u: str) -> bool:
            d = cfg.deployment_by_unique(u)
            return bool(d) and str(d.get("group", "")).endswith(go_suf)

        dims = [u for u in ladder if not _is_fb(u) and not _is_go(u)]
        go   = [u for u in ladder if _is_go(u)]
        fb   = [u for u in ladder if _is_fb(u)]
        skip = max(1, int(getattr(pol, "ladder_skip_after", 4) or 4))
        stale_max = max(0, int(getattr(pol, "ladder_stale_max", 3) or 0))
        age = float(getattr(pol, "stale_cooldown_retry_sec", 300) or 300)
        # Leva B: un deployment "cronico" (tanti fallimenti nelle ultime 24h)
        # NON va riesumato dagli step di ri-tentativo ordinari (stantii/ultima
        # spiaggia): è statisticamente rotto, riprovarlo ogni 5 minuti rallenta
        # la catena senza utilità. Viene però provato in un PARACADUTE dedicato
        # (step 4bis) PRIMA del -fallback a pagamento, e se fallisce lì va in
        # pausa minima chronic_fail_cooldown_sec (2h).
        chronic_thr = max(1, int(getattr(pol, "cooldown_retry_max_fail_24h", 10)
                                  or 10))
        # max deployment cronici provati per richiesta nel paracadute gratuito
        ladder_chronic_max = max(0, int(getattr(pol, "ladder_chronic_max", 3)
                                        or 3))

        def _is_chronic(u: str) -> bool:
            s = self.stats_for(u)
            return s.fail_count_24h >= chronic_thr

        def _chronic_filter(uniqs: list[str],
                            exclude_chronic: bool) -> list[str]:
            if not exclude_chronic:
                return uniqs
            return [u for u in uniqs if not _is_chronic(u)]

        def _chronic_context(ccfg, uniqs: list[str],
                             cneed, cctx) -> list[str]:
            """Candidati cronici che possono PROPRIO gestire la richiesta:
            capacità `cneed` e contesto/max_input compatibili (`_cap_fits`)."""
            out = []
            for u in uniqs:
                d = ccfg.deployment_by_unique(u)
                if not d or self.is_retired(u):
                    continue
                if self._endpoint_quarantined(d):
                    continue
                if cneed and not self._dep_supports(d, cneed):
                    continue
                if not self._cap_fits(d, cctx):
                    continue
                out.append(u)
            return out

        def _chronic_media_defer(ccfg, uniqs: list[str],
                                 cneed, cctx) -> list[str]:
            """Opzione A — MEDIA DEFER anche nel paracadute cronico: per
            richieste PURE-TESTO, se nel pool cronico esiste almeno un
            text-only (capace e cap-fits), i cronici media-capable vengono
            rimandati (non si 'spreca' il paracadute su un modello vision/
            audio se c'è un text-only che può rispondere). Richieste media e
            pool tutto-multimediale restano invariati (coerente con
            _defer_media)."""
            if cneed and not cneed.isdisjoint(self.MEDIA_TOKENS):
                return uniqs
            deps = [d for d in (ccfg.deployment_by_unique(u) for u in uniqs)
                    if d is not None]
            text_only = [d for d in deps if not self._is_deferrable(d)]
            if text_only and len(text_only) < len(deps):
                kept = {d["unique"] for d in text_only}
                return [u for u in uniqs if u in kept]
            return uniqs

        # 0) WARM POOL (caldi propri): esaurisci i free-dims GIA' serviti con
        #    successo da QUESTA sessione (vivi, non in cooldown, compatibili
        #    ctx/need) PRIMA di toccare qualsiasi altro deployment. `allowed`
        #    = univoci della scala corrente: in `force_escalation` (solo
        #    -go/-fallback) il pool e' vuoto di fatto -> nessun effetto.
        _warm = self._warm_pool(current_session(), set(ladder), need, ctx,
                                tried, failed_unique)
        if _warm:
            _dep = _warm[0]
            log.info("[warm] ladder -> %s (caldo proprio, max_in=%s)",
                     _dep["unique"], int(_dep.get("max_input_tokens") or 0))
            return _dep

        # 1) dims vivi (max skip)
        nxt = self._walk_chain(dims, failed_unique, need, ctx,
                               limit=skip, tried=tried)
        if nxt is not None:
            return nxt

        # 1ter) PRE-ULTIMA SPIAGGIA (fra l'ultimo -dim e -go): dims VIVI che
        #    un'ALTRA sessione ha servito con successo negli ultimi
        #    session_dep_guard_sec. Sono occupati -> la sessione corrente li
        #    condivide solo ORA (prima del -go a pagamento), scegliendo il
        #    max_input piu' piccolo che regge. Gratis e senza toccare cooldown.
        _shared = self.prelast_shared(dims, failed_unique, need, ctx, tried)
        if _shared is not None:
            return _shared

        # 1bis) dims cooldown-wakeup: SOLO cooldown nati da QUOTA/429 (una
        #    chiave satura e' VIVA: il cooldown puo' essere piu' lungo della
        #    finestra di quota) e con cooldown_age >= stale_cooldown_retry_sec.
        #    Budget A FINESTRA (`ladder_cooldown_wakeups` per
        #    `ladder_cooldown_wakeup_window_sec`): niente riesumazioni ripetute
        #    degli stessi. Scelta: residuo minore (il piu' vicino a scadere).
        #    Filtri allineati a _walk_chain (other_session_recent, retired,
        #    gemini/model breaker, need, cap_fits, slow-sessione/soft-429).
        _now_w = time.time()
        _win = max(1.0, float(getattr(
            pol, "ladder_cooldown_wakeup_window_sec", 3600) or 3600))
        _max_wake = max(0, int(getattr(pol, "ladder_cooldown_wakeups", 20) or 0))
        if _max_wake > 0:
            _tried_w = tried or set()
            _cooled_dims = []
            for u in _chronic_filter(dims, True):
                if u in _tried_w or u == failed_unique:
                    continue
                if not self.is_cooled_down(u):
                    continue
                _cage = self.cooldown_age(u)
                if _cage is None or _cage < age:
                    continue                       # cooldown fresco
                if not _is_quota_evidence(
                        getattr(self.stats_for(u), "last_reason", None)):
                    continue                       # solo nati da 429/quota
                _du = cfg.deployment_by_unique(u)
                if _du is None or self.is_retired(u) \
                        or self._endpoint_quarantined(_du) \
                        or self._gemini_blocked(_du) or self._model_blocked(_du):
                    continue
                if self.other_session_recent(u):
                    continue
                if self.is_slow_for_session(u, ctx=ctx):
                    continue
                if need and not self._dep_supports(_du, need):
                    continue
                if not self._cap_fits(_du, ctx):
                    continue
                _dq = self._wake_times.get(u)
                if _dq:
                    while _dq and _now_w - _dq[0] > _win:
                        _dq.popleft()
                    if len(_dq) >= _max_wake:
                        continue                   # budget finestra esaurito
                _cooled_dims.append(u)
            if _cooled_dims:
                _cooled_dims.sort(key=lambda u: self.cooldown_residual(u))
                _wake_u = _cooled_dims[0]
                _wake = cfg.deployment_by_unique(_wake_u)
                if _wake is not None:
                    self._wake_times.setdefault(_wake_u, deque()).append(_now_w)
                    log.info("[ladder] dims cooldown-wakeup 429 (budget %d/%ds):"
                             " residuo %ds -> %s", _max_wake, int(_win),
                             int(self.cooldown_residual(_wake_u)), _wake_u)
                    return _wake

        # 1quater) PROSEGUI NELLA -DIM SUCCESSIVA (prima di -go): la camminata
        #    e' ancorata a `failed_unique` e non torna mai indietro, quindi i
        #    deployment delle dim SUPERIORI che la scala (order, dim) mette
        #    PRIMA del fallito sono irraggiungibili. Se nella -dim corrente non
        #    e' rimasto nulla di vivo, la ricerca RIPARTE dalla -dim successiva
        #    (camminata fresca, dims > dim del fallito): si sale di -dim in
        #    -dim e solo alla fine si tocca -go (regola utente: "se la -dim non
        #    ne ha abbastanza prosegue semplicemente la sua ricerca nella -dim
        #    successiva prima di andare a -go"). Solo dims, mai -go/-fallback.
        _nextd = self._dims_above(dims, failed_unique)
        if _nextd:
            nxt = self._walk_chain(_nextd, None, need, ctx, limit=skip,
                                   tried=tried)
            if nxt is not None:
                log.info("[ladder] -dim successiva -> %s", nxt["unique"])
                return nxt

        # 2) -go vivi
        nxt = self._walk_chain(go, failed_unique, need, ctx, tried=tried)
        if nxt is not None:
            log.info("[ladder] escalation a -go -> %s", nxt["unique"])
            return nxt

        # 3) dims stantii (max stale_max) — mai i cronici (Leva B).
        #    Opzionale: `ladder_stale_max=0` disattiva (l'autoprobe risveglia).
        if stale_max > 0:
            _q_dims = [u for u in _chronic_filter(dims, True)
                       if _is_quota_evidence(
                           getattr(self.stats_for(u), "last_reason", None))]
            nxt = self._walk_chain(_q_dims, failed_unique,
                                   need, ctx,
                                   min_cooldown_age=age, limit=stale_max,
                                   tried=tried)
            if nxt is not None:
                log.info("[ladder] dims stantio (>%ds) -> %s",
                         int(age), nxt["unique"])
                return nxt

        # 4) -go stantii — mai i cronici (Leva B)
        nxt = self._walk_chain(_chronic_filter(go, True), failed_unique,
                               need, ctx,
                               min_cooldown_age=age, tried=tried)
        if nxt is not None:
            log.info("[ladder] go stantio (>%ds) -> %s",
                     int(age), nxt["unique"])
            return nxt

        # 4bis) PARACADUTE CRONICI (gratis, PRIMA del -fallback a pagamento):
        # i deployment con molti fallimenti/24h NON provati ancora in questa
        # richiesta vengono riprovati qui — sia in cooldown (svegliati) sia
        # no — ordinati dal MENO fallimentare al più fallimentare; a parità
        # vince il cooldown residuo più breve. Massimo `ladder_chronic_max`
        # per richiesta. Se falliscono di nuovo vanno in pausa minima
        # `chronic_fail_cooldown_sec` (2h, vedi mark_failed/
        # mark_failed_double_residual).
        if ladder_chronic_max > 0:
            # Paracadute cronici: chi ha fallito molto/24h ma NON è ancora
            # stato provato in questa richiesta. Sono quelli che i passi
            # normali ignorano (vivi = pescati; in cooldown = esclusi perché
            # cronici anche da stantii/ultima spiaggia): li riproviamo qui,
            # PRIMA del -fallback a pagamento, dal meno fallimentare al più
            # fallimentare (a parità: cooldown residuo più breve).
            chronic_pool = [u for u in ladder
                            if u != failed_unique
                            and not (tried and u in tried)
                            and _is_chronic(u)
                            and not self.is_slow_for_session(u, ctx=ctx)
                            and not self.is_retired(u)]
            # contesto/capacità compatibili e (Opzione A) testo puro -> text-only
            chronic_pool = _chronic_context(cfg, chronic_pool, need, ctx)
            chronic_pool = _chronic_media_defer(cfg, chronic_pool, need, ctx)
            if chronic_pool:
                chronic_pool.sort(
                    key=lambda u: (self.stats_for(u).fail_count_24h,
                                   self.cooldown_residual(u)))
                for u in chronic_pool[:ladder_chronic_max]:
                    d = cfg.deployment_by_unique(u)
                    if d is None:
                        continue
                    log.warning("[ladder] paracadute cronico (fail_24h=%d, "
                                "in_cooldown=%s) -> %s",
                                self.stats_for(u).fail_count_24h,
                                self.is_cooled_down(u), u)
                    return d

        # 5) -fallback INTERO: ignore cooldown, il servizio deve rispondere
        if fb:
            nxt = self._walk_chain(fb, failed_unique, need, ctx,
                                   ignore_cooldown=True, tried=tried,
                                   allow_slow=True)
            if nxt is not None:
                log.info("[ladder] escalation a -fallback (no cooldown) "
                         "-> %s", nxt["unique"])
                return nxt

        # 6) ULTIMA SPIAGGIA: tutti in cooldown, ordinati per residuo crescente
        #    — MAI i cronici (Leva B): chi fallisce da decine di volte/24h
        #    non va riesumato qui; la catena si esaurisce senza rovistare.
        now = time.time()
        cooled = []
        for u in ladder:
            if u == failed_unique:
                continue
            if tried and u in tried:
                continue
            if _is_chronic(u):
                log.debug("[ladder] %s cronico (fail_24h>=%d): saltato "
                          "da ULTIMA SPIAGGIA", u, chronic_thr)
                continue
            if not self.is_cooled_down(u):
                continue
            d = cfg.deployment_by_unique(u)
            if not d or self.is_retired(u):
                continue
            if self._endpoint_quarantined(d):
                continue
            if need and not self._dep_supports(d, need):
                continue
            if not self._cap_fits(d, ctx):
                continue
            remaining = self.cooldown_residual(u)
            cooled.append((remaining, u, d))
        cooled.sort(key=lambda x: x[0])
        if cooled:
            dep = cooled[0][2]
            log.warning("[ladder] ULTIMA SPIAGGIA (cooldown ignorato, "
                        "residuo %ds) -> %s",
                        int(cooled[0][0]), dep["unique"])
            return dep
        # 6bis) ULTIMA SPIAGGIA ESTREMA: anche i RITIRATI, ma solo se non
        #       permanenti (quota/probe-cap) e solo quando non e' rimasto
        #       NIENTE altro. Un successo li ripulisce dal lifecycle: e' il
        #       modo di "tirarli su" senza spendere probe a vuoto.
        _ret = []
        for u in ladder:
            if u == failed_unique or (tried and u in tried):
                continue
            if not self._retired_usable(u):
                continue
            _d = cfg.deployment_by_unique(u)
            if not _d:
                continue
            if self._endpoint_quarantined(_d):
                continue
            if need and not self._dep_supports(_d, need):
                continue
            if not self._cap_fits(_d, ctx):
                continue
            _ret.append((self.cooldown_residual(u), u, _d))
        _ret.sort(key=lambda x: x[0])
        if _ret:
            dep = _ret[0][2]
            log.warning("[ladder] ULTIMA SPIAGGIA ESTREMA (ritirato "
                        "non-permanente) -> %s", dep["unique"])
            return dep
        return None

    def fallback_after(self, profile: str, failed_unique: str | None,
                       need: frozenset[str] | None = None,
                       ctx: int | None = None) -> dict | None:
        """Prossimo deployment vivo nella catena TESTO piatta del profilo
        (dims crescenti -> -go -> -fallback), filtrata da need. Con
        escalation graduale del rilassamento cooldown."""
        return self._walk_ladder_resilient(self.config.chains.get(profile, []),
                                           failed_unique, need, ctx)

    def force_escalation(self, cur_dep: dict,
                         need: frozenset[str] | None = None,
                         ctx: int | None = None,
                         tried: set[str] | None = None) -> dict | None:
        """Salta DIRETTAMENTE ai gradini -go/-fallback del ladder.

        Usato dal rilevamento fake tool-call: niente scala dims, si va
        subito ai bucket a pagamento. Ritorna None se non c'e' alcun
        gradino di escalation disponibile.
        """
        cfg = self.config
        ladder = self._ladder_for_group(cur_dep.get("group", ""))
        esc = [g for g in ladder
               if (cfg.go_suffix and g.endswith(cfg.go_suffix))
               or (cfg.fallback_suffix and g.endswith(cfg.fallback_suffix))]
        if not esc:
            return None
        return self._walk_ladder_resilient(esc, cur_dep["unique"], need,
                                           ctx, tried=tried)

    def _capable_first(self, ladder: list[str], cur_dep: dict) -> list[str]:
        """Riordina il ladder (lista di UNIQUE, vedi _ladder_for_group) per la
        rotazione DOPO troncatura/risposta-vuota: tra i candidati SUCCESSIVI al
        deployment corrente (la camminata non torna mai indietro) passano
        avanti quelli con finestra di input MAGGIORE (il problema e'
        fisicamente lo spazio: -4096 token non li fa pensare nessuno), a
        parita' la `intelligence` piu' alta e la finestra piu' grande; la coda
        -go/-fallback resta fissa in fondo (che all'occorrenza ignora gia' i
        cooldown). Il prefisso fino al corrente e' invariato (gia' tentato o
        mai raggiungibile dalla walk)."""
        cur_mi = int(cur_dep.get("max_input_tokens") or 0)
        cur_u = cur_dep.get("unique")
        go_suf = self.config.go_suffix or "-go"
        fb_suf = self.config.fallback_suffix or "-fallback"
        power: dict[str, tuple[int, int]] = {}
        dims: list[str] = []
        tail: list[str] = []
        for u in ladder:
            d = self.config.deployment_by_unique(u) or {}
            g = d.get("group") or ""
            intel = int(d.get("intelligence") or 0)
            mi = int(d.get("max_input_tokens") or 0)
            power[u] = (intel, mi)
            (tail if (g.endswith(go_suf) or g.endswith(fb_suf))
             else dims).append(u)
        idx = (dims.index(cur_u) + 1) if cur_u in dims else 0

        def _key(u: str):
            intel, mi = power[u]
            return (0 if mi > cur_mi else 1, -intel, -mi)
        return dims[:idx] + sorted(dims[idx:], key=_key) + tail

    def _dims_above(self, dims: list[str],
                    failed_unique: str | None) -> list[str]:
        """Deployment delle -dim SUPERIORI a quella del fallito.

        La scala testo e' ordinata per `order` (tier) e poi dim; dopo un
        fallimento la camminata riprende DOPO il fallito e non torna mai
        indietro: i dep delle dim superiori con tier piu' basso restano
        dietro e non vengono mai raggiunti. Questo helper li recupera cosi'
        la rotazione sale di -dim in -dim prima di passare a -go.

        Ritorna [] se il fallito non appartiene a una -dim (bucket -go/
        -fallback o group sconosciuto): in quel caso comportamento invariato.
        """
        if not failed_unique:
            return []
        cur = self.config.deployment_by_unique(failed_unique)
        if not cur:
            return []
        m = self.DIM_SUFFIX_RE.search(str(cur.get("group") or ""))
        if not m:
            return []
        cur_dim = int(m.group(1))
        out: list[str] = []
        for u in dims:
            d = self.config.deployment_by_unique(u)
            if not d:
                continue
            mm = self.DIM_SUFFIX_RE.search(str(d.get("group") or ""))
            if mm and int(mm.group(1)) > cur_dim:
                out.append(u)
        return out

    def fallback_next(self, profile: str | None, cur_dep: dict,
                      need: frozenset[str] | None = None,
                      scope: str = "chain",
                      ctx: int | None = None,
                      tried: set[str] | None = None,
                      requested_group: str | None = None,
                      prefer_capable: bool = False) -> dict | None:
        """Prossimo tentativo DOPO un fallimento, con regole di SCOPO:

        - scope="chain": catena DEL MONDO del deployment corrente — cap-group
          -> chains_cap[cap] (con guardie need+max_input); dims -> catena testo.
          Mai text-only per una richiesta vision, e viceversa. Nei gruppi
          gen/stt (SAME_MODEL_PRIORITY_CAPS) con gen_same_model_failover,
          PRIMA i deployment dello stesso modello upstream, cross-model solo
          a esaurimento (loggato + contato).
        - scope="group" (richieste ESPLICITE): rotazione SOLO nello stesso
          gruppo, senza filtro capacità né sconfinamenti; stessa priorità
          same-model nei gruppi gen/stt.

        `requested_group`: gruppo ORIGINARIO della richiesta (es. -200k). Serve
        al pin escalation-winner per continuare a valere anche dopo che la
        richiesta e' salita su altre dim (cur_dep["group"] cambia). None =
        usa cur_dep["group"] (comportamento storico).

        `prefer_capable`: rotazione DOPO troncatura/risposta-vuota: le dim con
        finestra di output maggiore e `intelligence` piu' alta passano avanti
        (_capable_first); coda -go/-fallback e resilienza cooldown invariate.
        """
        req_grp = requested_group or cur_dep["group"]
        # --- WARM POOL: failover verso i "caldi" propri (free-dims stesso
        # mondo). Generalizza il vecchio cache-holder: esaurisce tutti i
        # deployment gia' serviti con successo da questa sessione (il
        # cache-holder e' il primo per costruzione). Esclusi i bucket
        # rinnovo/pagato: li' l'escalation e' deliberata.
        if not self._is_renewal_bucket(cur_dep.get("group", "")):
            _pname = self._group_profile(cur_dep["group"])
            if _pname:
                _warm = self._warm_pool(
                    None, self._warm_allowed(_pname, req_grp),
                    need=need, ctx=ctx, tried=tried,
                    failed_unique=cur_dep.get("unique"))
                if _warm:
                    _wd = _warm[0]
                    log.info("[warm] fallback -> %s (caldo proprio, max_in=%s)",
                             _wd["unique"],
                             int(_wd.get("max_input_tokens") or 0))
                    return _wd
        # --- CACHE HOLDER: fallback cache-preserving (solo bucket FREE) --
        # Rete di sicurezza storica quando il pool caldi non si applica (profilo
        # non risolvibile / nessun caldo del mondo): riusa il detentore cache
        # della sessione, se free e non gia' tentato.
        if (getattr(self.policy, "cache_aware_enabled", True)
                and getattr(self.policy, "cache_prefer_last_success", True)):
            _holder = self.cache_holder(None, need, ctx)
            if (_holder is not None
                    and _holder["unique"] != cur_dep.get("unique")
                    and (not tried or _holder["unique"] not in tried)
                    and self._free_group(_holder.get("group", ""))
                    and self._free_group(cur_dep.get("group", ""))):
                log.info("[cache] fallback -> detentore %s (cache calda)",
                         _holder["unique"])
                return _holder
        if scope == "group":
            cap_cur = self.config.group_caps.get(cur_dep["group"])
            if cap_cur is None and self.policy.dims_ladder_floor:
                # dims esplicito: SCALA (mai dim inferiori; coda -go/-fallback)
                # con escalation graduale del rilassamento cooldown.
                # Scorciatoia escalation-winner: il bucket richiesto ha gia'
                # fallito (siamo qui), salta direttamente al winner ricordato.
                _ew = self._try_esc_win(req_grp, need, ctx, tried=tried)
                if _ew is not None:
                    _p = self._esc_pin_probe(req_grp, _ew, need, ctx, tried)
                    return _p if _p is not None else _ew
                _lad = self._ladder_for_group(cur_dep["group"])
                if prefer_capable:
                    _lad = self._capable_first(_lad, cur_dep)
                nxt = self._walk_ladder_resilient(
                    _lad,
                    cur_dep["unique"], need, ctx, tried=tried)
                if nxt is not None and nxt["group"] != cur_dep["group"]:
                    log.info("[ladder] rotazione %s -> %s",
                             cur_dep["group"], nxt["group"])
                return nxt
            if self._prefer_same_model(cap_cur, cur_dep.get("model", "")):
                nxt = self.pick_deployment(cur_dep["group"],
                                           exclude=cur_dep["unique"],
                                           restrict_model=cur_dep.get("model"))
                if nxt is not None:
                    log.debug("[restrict] %s: failover same-model -> %s",
                              cur_dep["group"], nxt["unique"])
                    return nxt
                nxt = self.pick_deployment(cur_dep["group"],
                                           exclude=cur_dep["unique"])
                if nxt is not None and nxt.get("model") != cur_dep.get("model"):
                    self._note_cross(cur_dep["group"], cur_dep.get("model", "?"),
                                     nxt.get("model", "?"))
                return nxt
            return self.pick_deployment(cur_dep["group"], exclude=cur_dep["unique"])
        cap = self.config.group_caps.get(cur_dep["group"])
        if cap is not None:
            # Scorciatoia escalation-winner anche per i cap-group (primario ->
            # -go/-fallback): se il primario ha gia' fallito e c'e' un winner,
            # salta lì invece di rivisitare la cap-chain.
            _ew = self._try_esc_win(cur_dep["group"], need, ctx, tried=tried)
            if _ew is not None:
                return _ew
            chain = self.config.chains_cap.get(profile or "", {}).get(cap, [])
            prefer = cur_dep.get("model") \
                if self._prefer_same_model(cap, cur_dep.get("model", "")) else None
            if chain:
                return self._walk_chain(chain, cur_dep["unique"], need, ctx,
                                        prefer_model=prefer, tried=tried)
            # cap senza catena registrata: ripiega sulla catena testo filtrata
            return self.fallback_after(profile or "", cur_dep["unique"], need, ctx)
        if self.policy.dims_ladder_floor:
            # auto: stessa scala unica, partendo dalla dim corrente (mai giù),
            # con escalation graduale del rilassamento cooldown.
            # Scorciatoia escalation-winner: bucket richiesto già fallito ->
            # salta al winner ricordato invece di rivisitare la scala morta.
            _ew = self._try_esc_win(req_grp, need, ctx, tried=tried)
            if _ew is not None:
                _p = self._esc_pin_probe(req_grp, _ew, need, ctx, tried)
                return _p if _p is not None else _ew
            _lad = self._ladder_for_group(cur_dep["group"])
            if prefer_capable:
                _lad = self._capable_first(_lad, cur_dep)
            nxt = self._walk_ladder_resilient(
                _lad,
                cur_dep["unique"], need, ctx, tried=tried)
            if nxt is not None:
                return nxt
            return None                     # scala finita: errore a monte
        return self.fallback_after(profile or "", cur_dep["unique"], need, ctx)

    def capability_chains(self, profile: str) -> dict[str, list[str]]:
        """Capacità -> catena completa dei univoci (primario → go → fallback).
        Fonte per /admin/state e schermo TUI 'M'."""
        return {c: list(ch) for c, ch in
                self.config.chains_cap.get(profile, {}).items()}

    def capability_groups_counts(self, profile: str) -> dict[str, dict[str, int]]:
        """Capacità -> {primary, go, fallback} costruiti per quel profilo."""
        return {c: dict(cnt) for c, cnt in
                self.config.cap_counts.get(profile, {}).items()}

    def video_gen_candidates(self, model: str) -> list[dict]:
        """Deployment candidati (chiavi diverse) del gruppo video_gen per un
        nome richiesto: serve al poll/download STATELESS dei job video — i job
        vivono sull'account OR del deployment che ha fatto la submit, quindi
        si prova ogni chiave del gruppo finché qualcuno trova il job."""
        cfg = self.config
        pname = cfg.profile_of_base(self.policy.canonicalize(model)) \
            if hasattr(self.policy, "canonicalize") else None
        if not pname:
            base = model.split("__")[0]
            pname = cfg.profile_of_base(base) or cfg.profile_of_base(model)
        if not pname:
            # nome grezzo (es. 'bytedance/seedance-...') senza profilo
            # risolvibile: NON un 404 — fallback su tutti i gruppi
            # video_gen di tutti i profili.
            return self._video_gen_candidates_all()
        out, seen_keys = [], set()
        for cap_chain in [cfg.chains_cap.get(pname, {}).get("video_gen", [])]:
            for u in cap_chain:
                dep = cfg.deployment_by_unique(u)
                if not dep or dep["api_key"] in seen_keys:
                    continue
                seen_keys.add(dep["api_key"])
                out.append(dep)
        return out

    def _video_gen_candidates_all(self) -> list[dict]:
        """Il nome passato a ?model= spesso e' il MODELLO GREZZO (es.
        'bytedance/seedance-2.0-mini'), non un alias di profilo: in quel caso
        la risoluzione profilo fallisce e il recupero stateless dei job video
        andrebbe in 404. Fallback: interroga TUTTI i gruppi video_gen di
        tutti i profili (il job vive su UN account: si prova ogni chiave
        finche' qualcuno lo trova)."""
        cfg = self.config
        out, seen_keys = [], set()
        for chain_map in cfg.chains_cap.values():
            for u in chain_map.get("video_gen", []):
                dep = cfg.deployment_by_unique(u)
                if not dep or dep["api_key"] in seen_keys:
                    continue
                seen_keys.add(dep["api_key"])
                out.append(dep)
        return out