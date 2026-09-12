"""Normalizzazione STRUTTURALE della history (#1 del piano L1).

[IT] Rende la COPIA dei `messages` inviata all'upstream strutturalmente
coerente, senza mai cambiare il significato e senza riscrivere il prefisso
stabile (prompt cache). Per default si opera SOLO sulla coda (dall'ultimo
messaggio `user` in poi), che e' la parte nuova non ancora in cache.

Mosse ammesse (tutte strutturali):
 - rimozione dei messaggi `tool` orfani (tool_call_id senza chiamata);
 - tool_calls pendenti senza risultato -> si tiene il content, si toglie la
   chiamata (o si scarta il messaggio se vuoto);
 - scarto dei messaggi assistant vuoti (senza contenuto e senza tool_calls);
 - collasso di messaggi `system` consecutivi identici.

Niente mosse semantiche: non si sintetizzano risultati, non si fondono
system diversi.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field

log = logging.getLogger("nx.histnorm")


@dataclass
class HistNormConfig:
    enabled: bool = True
    tail_only: bool = True
    drop_orphan_tool: bool = True
    drop_dangling_tool_calls: bool = True
    drop_empty_assistant: bool = True
    dedupe_system: bool = True


def create_hist_config(policy_dict: dict | None = None) -> HistNormConfig:
    cfg = HistNormConfig()
    if not isinstance(policy_dict, dict):
        return cfg
    blk = policy_dict.get("history_normalize") or {}
    if not isinstance(blk, dict):
        return cfg
    for key, attr in (("enabled", "enabled"), ("tail_only", "tail_only"),
                      ("drop_orphan_tool", "drop_orphan_tool"),
                      ("drop_dangling_tool_calls", "drop_dangling_tool_calls"),
                      ("drop_empty_assistant", "drop_empty_assistant"),
                      ("dedupe_system", "dedupe_system")):
        if key in blk:
            setattr(cfg, attr, bool(blk[key]))
    return cfg


def hist_config_from_policy(policy) -> HistNormConfig:
    if policy is None:
        return HistNormConfig()
    return HistNormConfig(
        enabled=bool(getattr(policy, "history_normalize_enabled", True)),
        tail_only=bool(getattr(policy, "history_normalize_tail_only", True)),
        drop_orphan_tool=bool(getattr(policy, "history_normalize_drop_orphan_tool", True)),
        drop_dangling_tool_calls=bool(
            getattr(policy, "history_normalize_drop_dangling_tool_calls", True)),
        drop_empty_assistant=bool(
            getattr(policy, "history_normalize_drop_empty_assistant", True)),
        dedupe_system=bool(getattr(policy, "history_normalize_dedupe_system", True)),
    )


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _assistant_tool_ids(msgs) -> set[str]:
    ids: set[str] = set()
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict) and tc.get("id"):
                    ids.add(tc["id"])
    return ids


def normalize_messages(messages, cfg: HistNormConfig | None = None):
    """Ritorna (nuova_lista, report). La COPIA e' modificata; l'input resta intatto."""
    cfg = cfg or HistNormConfig()
    if not cfg.enabled or not isinstance(messages, list):
        return messages, {"enabled": False}
    msgs = copy.deepcopy(messages)

    tail_start = 0
    if cfg.tail_only:
        for i in range(len(msgs) - 1, -1, -1):
            m = msgs[i]
            if isinstance(m, dict) and m.get("role") == "user":
                tail_start = i
                break
    else:
        tail_start = 0                    # noqa: F841  (esplicito)

    head = msgs[:tail_start]
    tail = msgs[tail_start:]
    report = {"shown_orphan_tool": 0, "dangling_tool_calls": 0,
              "empty_assistant": 0, "dup_system": 0,
              "tail_start": tail_start, "changed": False}

    if cfg.drop_orphan_tool:
        valid_ids = _assistant_tool_ids(msgs)
        new_tail = []
        for m in tail:
            if (isinstance(m, dict) and m.get("role") == "tool"
                    and m.get("tool_call_id")
                    and m["tool_call_id"] not in valid_ids):
                report["shown_orphan_tool"] += 1
                continue
            new_tail.append(m)
        tail = new_tail

    if cfg.drop_dangling_tool_calls:
        new_tail = []
        for idx, m in enumerate(tail):
            if (isinstance(m, dict) and m.get("role") == "assistant"
                    and m.get("tool_calls")):
                ids = [tc.get("id") for tc in m["tool_calls"]
                       if isinstance(tc, dict)]
                has_result = any(
                    isinstance(rest, dict) and rest.get("role") == "tool"
                    and rest.get("tool_call_id") in ids
                    for rest in tail[idx + 1:])
                if not has_result:
                    report["dangling_tool_calls"] += 1
                    if _text_of(m.get("content")).strip():
                        m = dict(m)
                        m.pop("tool_calls", None)
                        new_tail.append(m)
                        continue
                    continue                # assistant vuoto con call pendente
            new_tail.append(m)
        tail = new_tail

    if cfg.drop_empty_assistant:
        new_tail = []
        for m in tail:
            if (isinstance(m, dict) and m.get("role") == "assistant"
                    and not m.get("tool_calls")
                    and not _text_of(m.get("content")).strip()):
                report["empty_assistant"] += 1
                continue
            new_tail.append(m)
        tail = new_tail

    if cfg.dedupe_system:
        new_tail = []
        prev_sys = None
        for m in tail:
            if isinstance(m, dict) and m.get("role") == "system":
                key = _text_of(m.get("content"))
                if prev_sys is not None and key == prev_sys:
                    report["dup_system"] += 1
                    continue
                prev_sys = key
            new_tail.append(m)
        tail = new_tail

    out = head + tail
    report["changed"] = (
        report["shown_orphan_tool"] or report["dangling_tool_calls"]
        or report["empty_assistant"] or report["dup_system"])
    return out, report
