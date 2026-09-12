"""Troncamento cache-aware del contesto: stub dei tool output VECCHI.

[IT] COSA: quando la cache del provider e' FREDDA (nessun detentore sano e
raggiungibile per la sessione), possiamo riscrivere il prefisso senza perdere
nulla: sostituiamo il `content` dei messaggi `role=="tool"` piu' vecchi degli
ultimi `keep_turns` turni utente con uno stub compatto. Quando la cache e'
CALDA NON si tocca nulla (si preserva il prefisso byte-per-byte).

VINCOLI: non si rimuovono MAI messaggi (l'accoppiata assistant.tool_calls /
tool.tool_call_id resta valida); si tocca SOLO il content dei tool vecchi;
soglia `min_saved_tokens` per evitare churn inutile.

[EN] WHAT: cache-aware context trimming. When the provider cache is cold,
replace the content of tool messages older than the last `keep_turns` user
turns with a compact stub. Cold only; never removes messages; tool-result
pairing is preserved.
"""

from __future__ import annotations

import logging

log = logging.getLogger("nx.ctxcompact")

DEFAULT_STUB = "[tool output omesso: {n} caratteri]"


class CtxCompactConfig:
    def __init__(self, enabled: bool = True, keep_turns: int = 4,
                 max_tool_output_chars: int = 2000,
                 min_saved_tokens: int = 500,
                 stub_text: str = DEFAULT_STUB):
        self.enabled = bool(enabled)
        self.keep_turns = int(keep_turns)
        self.max_tool_output_chars = int(max_tool_output_chars)
        self.min_saved_tokens = int(min_saved_tokens)
        self.stub_text = stub_text or DEFAULT_STUB


def create_ctxcompact_config(policy_dict: dict | None = None) -> CtxCompactConfig:
    """Costruisce la config dal blocco `cache_aware.context_truncation`."""
    cfg = CtxCompactConfig()
    if not isinstance(policy_dict, dict):
        return cfg
    ca = policy_dict.get("cache_aware") or {}
    if not isinstance(ca, dict):
        return cfg
    ct = ca.get("context_truncation")
    if ct is None:
        ct = ca                     # consenti forma piatta (retro-compat)
    if not isinstance(ct, dict):
        return cfg
    if "enabled" in ct:
        cfg.enabled = bool(ct["enabled"])
    for src, attr in (("keep_turns", "keep_turns"),
                      ("max_tool_output_chars", "max_tool_output_chars"),
                      ("min_saved_tokens", "min_saved_tokens")):
        if ct.get(src) is not None:
            setattr(cfg, attr, int(ct[src]))
    if ct.get("stub_text"):
        cfg.stub_text = str(ct["stub_text"])
    return cfg


def ctxcompact_config_from_policy(policy) -> CtxCompactConfig:
    """Costruisce la config dai campi gia' parsati in `Policy`."""
    if policy is None:
        return CtxCompactConfig()
    return CtxCompactConfig(
        enabled=bool(getattr(policy, "cache_ctx_truncation_enabled", True)),
        keep_turns=int(getattr(policy, "cache_ctx_keep_turns", 4) or 4),
        max_tool_output_chars=int(
            getattr(policy, "cache_ctx_max_tool_output_chars", 2000) or 2000),
        min_saved_tokens=int(
            getattr(policy, "cache_ctx_min_saved_tokens", 500) or 500),
        stub_text=str(getattr(policy, "cache_ctx_stub_text", DEFAULT_STUB)
                      or DEFAULT_STUB),
    )


def _content_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(p.get("text", "")) for p in content
                   if isinstance(p, dict))
    return 0


def _already_stub(content, cfg) -> bool:
    return isinstance(content, str) and content.startswith(
        cfg.stub_text.split("{n}", 1)[0])


def compact_tool_outputs(messages, cfg: CtxCompactConfig):
    """Ritorna (nuova_lista, report). Non muta l'input.

    report: {stubbed, saved_chars, saved_tokens_est, boundary, changed}.
    Se il risparmio stimato < min_saved_tokens la lista originale e' ritornata
    invariata (changed=False) per non alterare la cache per nulla.
    """
    rep = {"stubbed": 0, "saved_chars": 0, "saved_tokens_est": 0,
           "boundary": None, "changed": False}
    if not cfg.enabled or not messages:
        return messages, rep

    user_idx = [i for i, m in enumerate(messages)
                if isinstance(m, dict) and m.get("role") == "user"]
    if not user_idx:
        return messages, rep               # niente turni utente: non toccare
    keep_n = max(0, cfg.keep_turns)
    if keep_n <= 0:
        boundary = len(messages)
    else:
        boundary = user_idx[-keep_n] if len(user_idx) >= keep_n else user_idx[0]
    rep["boundary"] = boundary

    new = list(messages)
    saved = 0
    stubbed = 0
    for i, m in enumerate(messages):
        if not (isinstance(m, dict) and m.get("role") == "tool" and i < boundary):
            continue
        content = m.get("content")
        n = _content_len(content)
        if n <= cfg.max_tool_output_chars:
            continue                       # output gia' piccolo: lascialo
        if _already_stub(content, cfg):
            continue                       # idempotenza
        stub = cfg.stub_text.replace("{n}", str(n))
        new[i] = {**m, "content": stub}
        stubbed += 1
        saved += n - len(stub)
    rep["stubbed"] = stubbed
    rep["saved_chars"] = saved
    rep["saved_tokens_est"] = saved // 4
    if stubbed == 0 or rep["saved_tokens_est"] < cfg.min_saved_tokens:
        return messages, {**rep, "changed": False}
    rep["changed"] = True
    return new, rep
