"""Default di sampling (#2A) e loop detector (#2B) — piano L1.

[IT] #2A: applica parametri di sampling a basso rischio SOLO se il client non
li ha inviati e SOLO per i provider in allow-list (il client vince sempre).
#2B: rileva loop degenerativi (n-gram ripetuti / tool-call ripetuta) cosi' il
chiamante puo' scartare il deployment e passare alla dim successiva.

[EN] #2A: low-risk sampling defaults, applied only when the client did not
send them and only for allow-listed providers (client always wins).
#2B: degenerate loop detector (repeated n-grams / repeated tool-call).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger("nx.sampling")

# parametri applicabili come default (basso rischio)
_ALLOWED_KEYS = ("top_p", "presence_penalty", "frequency_penalty",
                 "repetition_penalty")


@dataclass
class LoopConfig:
    enabled: bool = True
    ngram_size: int = 8
    repeats: int = 3
    toolcall_repeat: int = 2
    min_tokens: int = 16


@dataclass
class SamplingConfig:
    enabled: bool = True
    provider_params: dict = field(
        default_factory=lambda: {"*": {"top_p": 0.95}})
    allow_providers: tuple = ("*",)
    loop: LoopConfig = field(default_factory=LoopConfig)


def _provider_key(dep: dict) -> str:
    prov = (dep or {}).get("provider") or ""
    if prov:
        return str(prov).lower()
    base = (dep or {}).get("api_base") or ""
    try:
        from urllib.parse import urlparse
        host = urlparse(base).hostname or base
    except Exception:
        host = base
    return str(host).lower()


def _defaults_table() -> dict:
    # default prudente: solo top_p per tutti; override per provider.
    return {"*": {"top_p": 0.95}}


def create_sampling_config(policy_dict: dict | None = None) -> SamplingConfig:
    cfg = SamplingConfig(provider_params=_defaults_table())
    if not isinstance(policy_dict, dict):
        return cfg
    blk = policy_dict.get("sampling_defaults") or {}
    if not isinstance(blk, dict):
        return cfg
    if "enabled" in blk:
        cfg.enabled = bool(blk["enabled"])
    pp = blk.get("provider_params")
    if isinstance(pp, dict):
        merged = _defaults_table()
        for prov, params in pp.items():
            if isinstance(params, dict):
                merged.setdefault(str(prov).lower(), {}).update(params)
        cfg.provider_params = merged
    if blk.get("allow_providers") is not None:
        cfg.allow_providers = tuple(str(x).lower()
                                    for x in blk["allow_providers"])
    lb = blk.get("loop") or {}
    if isinstance(lb, dict):
        if "enabled" in lb:
            cfg.loop.enabled = bool(lb["enabled"])
        for key in ("ngram_size", "repeats", "toolcall_repeat", "min_tokens"):
            if lb.get(key) is not None:
                setattr(cfg.loop, key, int(lb[key]))
    return cfg


def sampling_config_from_policy(policy) -> SamplingConfig:
    if policy is None:
        return create_sampling_config(None)
    raw = {}
    pp = getattr(policy, "sampling_provider_params", None)
    ap = getattr(policy, "sampling_allow_providers", None)
    if pp is not None or ap is not None:
        raw = {"sampling_defaults": {
            "enabled": getattr(policy, "sampling_enabled", True),
            "provider_params": pp or {},
            "allow_providers": list(ap) if ap is not None else None,
            "loop": {
                "enabled": getattr(policy, "loop_detector_enabled", True),
                "ngram_size": getattr(policy, "loop_ngram_size", 8),
                "repeats": getattr(policy, "loop_repeats", 3),
                "toolcall_repeat": getattr(policy, "loop_toolcall_repeat", 2),
                "min_tokens": getattr(policy, "loop_min_tokens", 16),
            },
        }}
    return create_sampling_config(raw)


def apply_sampling_defaults(body: dict, dep: dict,
                            cfg: SamplingConfig | None = None) -> list[str]:
    """Applica i default mancanti. Ritorna la lista di chiavi applicate."""
    cfg = cfg or SamplingConfig()
    if not cfg.enabled or not isinstance(body, dict):
        return []
    prov = _provider_key(dep)
    allow = cfg.allow_providers or ()
    if "*" not in allow and prov not in allow:
        return []
    params: dict = {}
    params.update(cfg.provider_params.get("*", {}) or {})
    params.update(cfg.provider_params.get(prov, {}) or {})
    applied = []
    for key in _ALLOWED_KEYS:
        if key in params and key not in body:
            body[key] = params[key]
            applied.append(key)
    return applied


def _loop_reason_text(text: str, lc: LoopConfig) -> str | None:
    tokens = (text or "").split()
    if len(tokens) < max(lc.min_tokens, lc.ngram_size * lc.repeats):
        return None
    n = lc.ngram_size
    k = lc.repeats
    # ricerca di k n-gram consecutivi identici
    i = 0
    run = 1
    while i + n <= len(tokens):
        gram = tokens[i:i + n]
        nxt = tokens[i + n:i + 2 * n]
        if len(nxt) == n and nxt == gram:
            run += 1
            if run >= k:
                return "repeated_ngram"
        else:
            run = 1
        i += 1
    return None


def _loop_reason_toolcalls(tool_calls, lc: LoopConfig) -> str | None:
    if not tool_calls or lc.toolcall_repeat < 2:
        return None
    seen: dict[tuple, int] = {}
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        key = (str(fn.get("name")), str(fn.get("arguments")))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] >= lc.toolcall_repeat:
            return "repeated_toolcall"
    return None


def detect_loop(text, tool_calls, cfg: SamplingConfig | None = None) -> str | None:
    cfg = cfg or SamplingConfig()
    if not cfg.enabled or not cfg.loop.enabled:
        return None
    return _loop_reason_toolcalls(tool_calls, cfg.loop) \
        or _loop_reason_text(text if isinstance(text, str) else "", cfg.loop)


def response_loop_reason(data: dict, cfg: SamplingConfig | None = None) -> str | None:
    """Loop detector applicato a una risposta chat non-streaming."""
    if not isinstance(data, dict):
        return None
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content
                          if isinstance(p, dict))
    return detect_loop(content, message.get("tool_calls"), cfg)
