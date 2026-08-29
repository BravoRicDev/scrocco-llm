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

import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config import GatewayConfig, CAP_PRIORITY_ORDER
from .policy import Policy
from .capabilities import required_caps, count_image_parts

log = logging.getLogger("nx.router")

CHARS_PER_TOKEN = 4
STICKY_TTL_SECONDS = 3600
COOLDOWN_SECONDS = 600


@dataclass
class DepStats:
    """Statistiche runtime per deployment (rotazione adattiva).

    I campi budget_* (Feature no-spreco) NON sono persistiti: le finestre
    ripartono pulite al restart e i cap si ri-apprendono al primo 429.
    """
    last_used: float = 0.0
    ema_latency_ms: float | None = None
    inflight: int = 0
    fail_streak: int = 0              # fallimenti consecutivi (escalation cooldown)
    success_ema: float | None = None  # tasso successo stimato (penalità dolce)
    last_reason: str | None = None    # ultimo motivo di fallimento (402, 429...)
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


class Router:
    def __init__(self, config: GatewayConfig, policy: Policy | None = None):
        self.config = config
        self.policy = policy or Policy.default()
        self._sticky: dict[str, tuple[str, float]] = {}
        self._cooldown: dict[str, float] = {}   # unique -> expiry epoch
        self._cooldown_since: dict[str, float] = {}  # unique -> quando fu messo
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

    # -------------------------------------------------------------- sticky
    def sticky_get(self, session_id: str) -> str | None:
        entry = self._sticky.get(session_id)
        if not entry:
            return None
        target, ts = entry
        if time.time() - ts > self.policy.sticky_ttl_sec:
            self._sticky.pop(session_id, None)
            return None
        return target

    def sticky_set(self, session_id: str, target: str) -> None:
        self._sticky[session_id] = (target, time.time())

    def sticky_release(self, session_id: str) -> None:
        self._sticky.pop(session_id, None)

    # ------------------------------------------------------------ cooldown
    def mark_failed(self, unique: str, seconds: float | None = None,
                    reason: str | None = None) -> float:
        """Marca il deployment fallito con cooldown.

        - `seconds` esplicito vince SEMPRE (es. Retry-After su 429)
        - default: dipende da cooldown_mode:
          * "linear": BASE + MULT*(fail_count_24h - 1) minuti
          * "exponential": cooldown_sec * 2^(streak-1) (legacy)
        - `reason`: classificazione dell'errore (http_429, no_credits,
          not_found...). Un 429 alimenta il BUDGET GUARD.
        Ritorna i secondi applicati.
        """
        pol = self.policy
        s = self.stats_for(unique)
        if reason:
            s.last_reason = reason
        # --- fail_count_24h: contatore giornaliero (mai azzerato da successi)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if s.fail_day_key != today:
            s.fail_count_24h = 0
            s.fail_day_key = today
        s.fail_count_24h += 1
        # --- budget guard: apprendimento del limite dal 429 --------------
        bg_cfg = pol.budget_guard or {}
        if reason == "http_429" and bg_cfg.get("enabled"):
            floor_min = max(1.0, float(bg_cfg.get("min_per_min", 10)))
            floor_day = max(1.0, float(bg_cfg.get("min_per_day", 200)))
            learned_min = max(floor_min, s.minute_calls * 1.2)
            learned_day = max(floor_day, s.day_calls * 1.2)
            s.min_cap_learned = max(s.min_cap_learned, learned_min)
            s.day_cap_learned = max(s.day_cap_learned, learned_day)
            log.info("[budget] %s: cap appresi da 429 -> ~%.0f/min, "
                     "~%.0f/giorno", unique, s.min_cap_learned,
                     s.day_cap_learned)
        s.fail_streak += 1
        prev = 1.0 if s.success_ema is None else s.success_ema
        s.success_ema = max(0.0, 0.8 * prev)      # EMA verso lo 0 (α=0.2)
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
        self._cooldown[unique] = time.time() + seconds
        self._cooldown_since[unique] = time.time()
        log.warning("[cooldown] %s inattivo per %ds (streak=%d, "
                    "fail_24h=%d)",
                    unique, int(seconds), s.fail_streak, s.fail_count_24h)
        return seconds

    def mark_failed_double_residual(self, unique: str,
                                     reason: str | None = None) -> float:
        """Raddoppia il cooldown residuo quando un deployment dormiente fallisce
        di nuovo. Usato al posto di mark_failed per i retry di deployment
        in cooldown (stale/ultima spiaggia)."""
        now = time.time()
        remaining = max(1.0, self._cooldown.get(unique, now) - now)
        new_cd = remaining * 2.0
        new_cd = min(new_cd, float(self.policy.max_cooldown_sec))
        s = self.stats_for(unique)
        if reason:
            s.last_reason = reason
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if s.fail_day_key != today:
            s.fail_count_24h = 0
            s.fail_day_key = today
        s.fail_count_24h += 1
        s.fail_streak += 1
        prev = 1.0 if s.success_ema is None else s.success_ema
        s.success_ema = max(0.0, 0.8 * prev)
        self._cooldown[unique] = now + new_cd
        self._cooldown_since[unique] = now
        log.warning("[cooldown] %s dormiente ri-fallito -> cooldown "
                    "raddoppiato a %ds (residuo era %ds, fail_24h=%d)",
                    unique, int(new_cd), int(remaining), s.fail_count_24h)
        return new_cd

    def clear_cooldown(self, unique: str) -> None:
        """Rimuove cooldown e azzera contatori fail per un deployment che ha
        risposto con successo dopo essere stato in cooldown."""
        self._cooldown.pop(unique, None)
        self._cooldown_since.pop(unique, None)
        s = self.stats_for(unique)
        s.fail_streak = 0
        s.fail_count_24h = 0
        s.fail_day_key = ""
        log.info("[cooldown] %s riabilitato (successo da dormiente)", unique)

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

    def is_cooled_down(self, unique: str) -> bool:
        exp = self._cooldown.get(unique)
        if exp is None:
            return False
        if time.time() > exp:
            self._cooldown.pop(unique, None)
            self._cooldown_since.pop(unique, None)
            return False
        return True

    def cooldown_age(self, unique: str) -> float | None:
        """Da quanti secondi e' in cooldown questo deployment (None se non lo e'
        o se non lo sappiamo)."""
        since = self._cooldown_since.get(unique)
        return (time.time() - since) if since is not None else None

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

    def purge_expired(self) -> tuple[int, int]:
        """Rimuove sticky scadute e cooldown espirati (chiamato dal watcher).

        Senza purge, con tanti session_id unici, i dict crescerebbero senza
        limite sugli uptime lunghi. Ritorna (sticky_rimosse, cooldown_rimossi).
        """
        now = time.time()
        dead_sessions = [s for s, (_t, ts) in self._sticky.items()
                         if now - ts > self.policy.sticky_ttl_sec]
        for s in dead_sessions:
            self._sticky.pop(s, None)
        dead_sg = [s for s, (_g, ts) in self._session_group.items()
                   if now - ts > self.policy.sticky_ttl_sec]
        for s in dead_sg:
            self._session_group.pop(s, None)
        dead_cd = [u for u, exp in self._cooldown.items() if now > exp]
        for u in dead_cd:
            self._cooldown.pop(u, None)
            self._cooldown_since.pop(u, None)
        if dead_sessions or dead_cd or dead_sg:
            log.debug("[purge] sticky=%d cooldown=%d sessioni=%d",
                      len(dead_sessions), len(dead_cd), len(dead_sg))
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
        text_only = [d for d in deps if not self._is_media_capable(d)]
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

    def note_start(self, unique: str) -> None:
        """Richiesta inviata: tocca last_used (penalità anti rate-limit),
        incrementa inflight e le FINESTRE budget (minuto/giorno). Nei gruppi
        gen/stt registra anche l'ultimo modello per la stickiness."""
        s = self.stats_for(unique)
        s.last_used = time.time()
        s.inflight += 1
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

    def note_result(self, unique: str, latency_ms: float) -> None:
        """Risposta ricevuta: aggiorna l'EMA di latenza (non tocca inflight:
        per lo streaming chiude la nota_end al termine del flusso).
        Resetta anche lo streak di fallimenti e aggiorna il tasso successo."""
        s = self.stats_for(unique)
        s.ema_latency_ms = (latency_ms if s.ema_latency_ms is None
                            else s.ema_latency_ms * 0.7 + latency_ms * 0.3)
        s.fail_streak = 0
        prev = 1.0 if s.success_ema is None else s.success_ema
        s.success_ema = min(1.0, 0.8 * prev + 0.2)

    def note_end(self, unique: str) -> None:
        self.stats_for(unique).inflight = max(
            0, self.stats_for(unique).inflight - 1)

    # ------------------------------------------------ persistenza (F4)
    def dump_stats(self) -> dict:
        """Snapshot serializzabile: EMA+last_used e scadenze cooldown."""
        return {
            "stats": {u: {"ema_latency_ms": s.ema_latency_ms,
                          "last_used": s.last_used,
                          "fail_streak": s.fail_streak,
                          "success_ema": s.success_ema,
                          "fail_count_24h": s.fail_count_24h,
                          "fail_day_key": s.fail_day_key}
                      for u, s in self._stats.items()},
            "cooldown": dict(self._cooldown),
            "cap_strikes": [{"key": k, **v} for k, v in
                            self._cap_strikes.items()],
            "saved_at": time.time(),
        }

    def load_stats(self, data: dict) -> None:
        """Ricarica lo snapshot dopo un restart. File corrotto/campi strani ->
        si riparte puliti (mai crash all'avvio). inflight NON si persiste."""
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
            for u, exp in (data.get("cooldown") or {}).items():
                self._cooldown[str(u)] = float(exp)
                # dopo un restart non sappiamo quando fu messo: lo trattiamo
                # come "appena impostato" (niente retry-stantio finche' non
                # passa la soglia).
                self._cooldown_since.setdefault(str(u), time.time())
            for st in (data.get("cap_strikes") or []):
                if not isinstance(st, dict) or "|" not in str(st.get("key", "")):
                    continue
                self._cap_strikes[str(st["key"])] = {
                    "count": int(st.get("count") or 0),
                    "first": float(st.get("first") or 0),
                    "last": float(st.get("last") or 0),
                    "evidence": str(st.get("evidence") or "")[:200],
                }
        except Exception as exc:             # noqa: BLE001
            log.warning("[stats] load fallito (%s): riparto pulito", exc)

    def _score(self, dep: dict, now: float | None = None) -> float:
        """Punteggio adattivo: base priority × freschezza × velocità ÷ saturazione.
        A freddo (nessuna statistica) tutti i fattori sono neutri (=1)."""
        pol = self.policy
        now = now if now is not None else time.time()
        s = self._stats.get(dep["unique"])
        priority = max(0, int(dep.get("priority", 0) or 0)) + 1
        if s is None:
            return float(priority)
        freshness = 1.0 if s.last_used <= 0 else max(
            0.05, math.exp(-(now - s.last_used) / max(0.001,
                                                      pol.recency_halflife_sec)))
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
        'fallback' taglia la parte sopra: -go esplicito non tocca i primari;
        -fallback resta solo fallback. I bucket go/fallback compaiono UNA
        volta sola in coda (sono la coda della dim massima)."""
        cfg = self.config
        base = f"{cfg.proxy_prefix}{pname}"
        chain: list[str] = []
        if start_tier == "primary":
            for d in sorted(cfg.profile_dims.get(pname, [])):
                if d < (start_dim or 0):
                    continue
                for u in cfg.groups.get(f"{base}-{d}k", []):
                    chain.append(u["unique"])
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
        mi = dep.get("max_input_tokens") or 0
        return mi <= 0 or ctx <= mi

    def pick_deployment(self, group_name: str, need: frozenset[str] | None = None,
                        exclude: str | None = None,
                        ctx: int | None = None,
                        restrict_model: str | None = None) -> dict | None:
        """Selezione pesata dentro un gruppo, saltando i cooled-down.

        Con policy.adaptive_pick (default True): punteggio dinamico che
        penalizza l'ultimo usato, premia la velocità (EMA) ed evita chi ha
        richieste in corso. Disattivato: comportamento legacy priority-only.

        `need` filtra per capacità dichiarate; `exclude` esclude un unique
        (rotazione post-fallimento); `ctx` attiva la guardia soft max_input
        nei gruppi capacità; `restrict_model` limita a un solo modello
        upstream (failover same-model dei gruppi gen/stt).
        """
        def _ok(d: dict) -> bool:
            if d["unique"] == exclude:
                return False
            if self.is_retired(d["unique"]):
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
        if not deps:
            # tutti in cooldown (o nessun capace) -> riprova ignorando il cooldown
            # (ultima spiaggia), rispettando exclude/need/guardia
            deps = [d for d in self.config.groups.get(group_name, []) if _ok(d)]
        deps, _ = self._defer_media(group_name, need, deps)
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
            weights = [self._score(d, now) for d in deps]
            if len(deps) <= 4:
                order = sorted(
                    ((w, d["unique"]) for w, d in zip(weights, deps)),
                    reverse=True)
                log.debug("[pick] %s top: %s", group_name,
                          ", ".join(f"{u}={sw:.2f}"
                                    for sw, u in order[:3]))
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
                    tried: set[str] | None = None) -> dict | None:
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

        def _eligible(u: str) -> dict | None:
            if tried and u in tried:
                return None
            dep = self.config.deployment_by_unique(u)
            if not dep:
                return None
            if not ignore_cooldown and self.is_cooled_down(u):
                if min_cooldown_age is None:
                    return None
                age = self.cooldown_age(u)
                if age is None or age < min_cooldown_age:
                    return None
                # cooldown "stantio": lo ri-consideriamo
            if self.is_retired(u):
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
            return preferred[0]
        return None

    def initial_pick(self, profile: str | None, group_name: str,
                     need: frozenset[str] | None = None,
                     ctx: int | None = None) -> dict | None:
        """Prima selezione dentro un gruppo; nessun candidato vivo ->
        cammina la catena DEL MONDO del gruppo (cap-chain per -C, testo
        per dims/-go/-fallback). Sostituisce pick+fallback_after in main."""
        dep = self.pick_deployment(group_name, need=need, ctx=ctx)
        if dep is not None:
            return dep
        cap = self.config.group_caps.get(group_name)
        if cap is not None:
            chain = self.config.chains_cap.get(profile or "", {}).get(cap, [])
            return self._walk_chain(chain, None, need, ctx)
        if self.policy.dims_ladder_floor:
            chain = self._ladder_for_group(group_name)
            if chain:
                dep = self._walk_chain(chain, None, need, ctx)
                if dep is not None:
                    log.info("[ladder] initial_pick: %s senza candidati "
                             "vivi -> scala (%d univoci) -> %s",
                             group_name, len(chain), dep["unique"])
                    return dep
        chain = self.config.chains.get(profile or "", [])
        return self._walk_chain(chain, None, need, ctx)

    def _walk_ladder_resilient(self, ladder: list[str],
                               failed_unique: str | None,
                               need: frozenset[str] | None = None,
                               ctx: int | None = None,
                               tried: set[str] | None = None) -> dict | None:
        """Cammina la scala testo con early-escalation e cooldown lineare.

        Sequenza (7 step):
          1) dims vivi — max ladder_skip_after candidati
          2) -go vivi
          3) dims stantii (cooldown > stale_cooldown_retry_sec) — max ladder_stale_max
          4) -go stantii (dormiente, potrebbe essersi svegliato)
          5) -fallback INTERO (ignore cooldown — deve sempre rispondere)
          6) ULTIMA SPIAGGIA: tutti in cooldown, ordinati per residuo crescente

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
        stale_max = max(1, int(getattr(pol, "ladder_stale_max", 3) or 3))
        age = float(getattr(pol, "stale_cooldown_retry_sec", 300) or 300)

        # 1) dims vivi (max skip)
        nxt = self._walk_chain(dims, failed_unique, need, ctx,
                               limit=skip, tried=tried)
        if nxt is not None:
            return nxt

        # 2) -go vivi
        nxt = self._walk_chain(go, failed_unique, need, ctx, tried=tried)
        if nxt is not None:
            log.info("[ladder] escalation a -go -> %s", nxt["unique"])
            return nxt

        # 3) dims stantii (max stale_max)
        nxt = self._walk_chain(dims, failed_unique, need, ctx,
                               min_cooldown_age=age, limit=stale_max,
                               tried=tried)
        if nxt is not None:
            log.info("[ladder] dims stantio (>%ds) -> %s",
                     int(age), nxt["unique"])
            return nxt

        # 4) -go stantii
        nxt = self._walk_chain(go, failed_unique, need, ctx,
                               min_cooldown_age=age, tried=tried)
        if nxt is not None:
            log.info("[ladder] go stantio (>%ds) -> %s",
                     int(age), nxt["unique"])
            return nxt

        # 5) -fallback INTERO: ignore cooldown, il servizio deve rispondere
        if fb:
            nxt = self._walk_chain(fb, failed_unique, need, ctx,
                                   ignore_cooldown=True, tried=tried)
            if nxt is not None:
                log.info("[ladder] escalation a -fallback (no cooldown) "
                         "-> %s", nxt["unique"])
                return nxt

        # 6) ULTIMA SPIAGGIA: tutti in cooldown, ordinati per residuo crescente
        now = time.time()
        cooled = []
        for u in ladder:
            if tried and u in tried:
                continue
            if not self.is_cooled_down(u):
                continue
            d = cfg.deployment_by_unique(u)
            if not d or self.is_retired(u):
                continue
            if need and not self._dep_supports(d, need):
                continue
            if not self._cap_fits(d, ctx):
                continue
            remaining = self._cooldown.get(u, now) - now
            cooled.append((remaining, u, d))
        cooled.sort(key=lambda x: x[0])
        if cooled:
            dep = cooled[0][2]
            log.warning("[ladder] ULTIMA SPIAGGIA (cooldown ignorato, "
                        "residuo %ds) -> %s",
                        int(cooled[0][0]), dep["unique"])
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

    def fallback_next(self, profile: str | None, cur_dep: dict,
                      need: frozenset[str] | None = None,
                      scope: str = "chain",
                      ctx: int | None = None,
                      tried: set[str] | None = None) -> dict | None:
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
        """
        if scope == "group":
            cap_cur = self.config.group_caps.get(cur_dep["group"])
            if cap_cur is None and self.policy.dims_ladder_floor:
                # dims esplicito: SCALA (mai dim inferiori; coda -go/-fallback)
                # con escalation graduale del rilassamento cooldown.
                nxt = self._walk_ladder_resilient(
                    self._ladder_for_group(cur_dep["group"]),
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
            nxt = self._walk_ladder_resilient(
                self._ladder_for_group(cur_dep["group"]),
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