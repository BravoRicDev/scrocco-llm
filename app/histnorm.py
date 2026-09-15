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
  - collasso di messaggi `system` consecutivi identici;
  - la frontiera LAZY del reasoning: `reasoning_content` rimosso/troncato nei
    turni assistant vecchi (fuori dai `reasoning_keep_recent` piu' recenti).

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
    # REASONING LAZY: il thinking dei turni vecchi e' PROCESSO, non
    # informazione: l'upstream non ne ha bisogno e pesa migliaia di token per
    # turno (R1/Qwen-thinking). -1 = mai toccare (comportamento storico);
    # 0 = rimosso del tutto dai turni "vecchi"; >0 = troncato a N char (head,
    # con marker deterministico). "Vecchio" = fuori dai ultimi
    # `reasoning_keep_recent` messaggi assistant con reasoning: la frontiera
    # avanza monotona col dialogo (stessa lista -> stessi byte), esattamente
    # come lo stub del ctxcompact.
    reasoning_content_max_chars: int = 0
    reasoning_keep_recent: int = 1


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
    _rmax = blk.get("reasoning_content_max_chars")
    if _rmax is not None:
        try:
            cfg.reasoning_content_max_chars = int(_rmax)
        except (TypeError, ValueError):
            pass
    _rkeep = blk.get("reasoning_keep_recent")
    if _rkeep is not None:
        try:
            cfg.reasoning_keep_recent = max(0, int(_rkeep))
        except (TypeError, ValueError):
            pass
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
        reasoning_content_max_chars=int(
            getattr(policy, "history_normalize_reasoning_content_max_chars", 0)),
        reasoning_keep_recent=max(0, int(
            getattr(policy, "history_normalize_reasoning_keep_recent", 1))),
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


_RC_MARKER = "…[troncato "          # sentinella di idempotenza del troncamento


def _trim_reasoning(msgs, cfg: HistNormConfig, report: dict):
    """Frontiera LAZY del reasoning: fuori dagli ultimi
    `reasoning_keep_recent` assistant con reasoning, il `reasoning_content`
    viene rimosso (max_chars=0) o troncato head+marker (>0). Funzione PURA
    della lista: a parita' di history byte identici, e la frontiera avanza
    monotona col dialogo (mai rigressioni di prefisso)."""
    maxc = int(cfg.reasoning_content_max_chars)
    if maxc < 0:
        return msgs
    idxs = [i for i, m in enumerate(msgs)
            if isinstance(m, dict) and m.get("role") == "assistant"
            and isinstance(m.get("reasoning_content"), str)
            and m.get("reasoning_content")]
    if not idxs:
        return msgs
    keep_n = max(0, int(cfg.reasoning_keep_recent))
    keep = set(idxs[len(idxs) - keep_n:]) if keep_n else set()
    cut = [i for i in idxs if i not in keep]
    if not cut:
        return msgs
    out = list(msgs)
    for i in cut:
        m = dict(out[i])
        rc = m.get("reasoning_content") or ""
        if maxc > 0 and _RC_MARKER in rc:
            continue                          # gia' troncato da noi: byte-stabile
        if maxc == 0:
            m.pop("reasoning_content", None)
        elif len(rc) > maxc:
            m["reasoning_content"] = (
                rc[:maxc] + "\n" + _RC_MARKER + "{:,} char di reasoning]".format(
                    len(rc) - maxc))
        else:
            continue
        out[i] = m
        report["reasoning_trimmed"] = report.get("reasoning_trimmed", 0) + 1
    return out


def normalize_messages(messages, cfg: HistNormConfig | None = None,
                       tail_floor: int = 0):
    """Ritorna (nuova_lista, report). La COPIA e' modificata; l'input resta intatto.

    `tail_floor` = frontiera ctxcompact della sessione (watermark): in un tool
    loop senza user intermedi (assistant/tool ripetuti) l'ultimo `user` puo'
    stare molti turni indietro e la normalizzazione finirebbe dentro il
    prefisso GIA' IN CACHE, invalidandola. Con il floor la coda protetta non
    risale mai oltre cio' che e' stato davvero stubbato.
    """
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
        if tail_floor > 0:
            # F29: mai tornare indietro oltre la frontiera della sessione
            tail_start = max(tail_start, min(int(tail_floor), len(msgs)))
    else:
        tail_start = 0                    # noqa: F841  (esplicito)

    head = msgs[:tail_start]
    tail = msgs[tail_start:]
    report = {"shown_orphan_tool": 0, "dangling_tool_calls": 0,
              "dropped_tool_calls": 0,
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
        # ORFANO INVERSO (anche PARZIALE): un assistant dichiara tool_calls ma
        # manca il messaggio `tool` col risultato per uno o piu' id (history
        # troncata/rilavorata). I provider severi (OpenAI/Anthropic recenti)
        # rispondono 400 bloccante e la richiesta muore. Qui togliamo SOLO le
        # call senza risultato (quelle con risultato restano), cosi' la catena
        # torna strutturalmente valida; se non resta nessuna call si conserva
        # il content (o si scarta l'assistant se vuoto). Nessuna sintesi di
        # risultati: nessuna mossa semantica.
        result_ids = {m.get("tool_call_id") for m in msgs
                      if isinstance(m, dict) and m.get("role") == "tool"
                      and m.get("tool_call_id")}
        new_tail = []
        for m in tail:
            if (isinstance(m, dict) and m.get("role") == "assistant"
                    and m.get("tool_calls")):
                calls = [tc for tc in m["tool_calls"]
                         if isinstance(tc, dict)]
                kept = [tc for tc in calls if tc.get("id") in result_ids]
                missing = len(calls) - len(kept)
                if missing:
                    report["dangling_tool_calls"] += 1
                    report["dropped_tool_calls"] += missing
                    if kept:
                        m = dict(m)
                        m["tool_calls"] = kept
                        new_tail.append(m)
                        continue
                    if _text_of(m.get("content")).strip():
                        m = dict(m)
                        m.pop("tool_calls", None)
                        new_tail.append(m)
                        continue
                    continue                # assistant vuoto con call pendente
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
    if cfg.drop_empty_assistant:
        # Passata GLOBALE (testa inclusa): un assistant senza contenuto ne'
        # tool_calls e' INVALIDO per i provider severi ("Assistant message
        # must have either content or tool_calls" -> 400 bloccante l'intera
        # richiesta, cache o non cache). La validita' batte la protezione del
        # prefisso: un turno del genere non ha mai servito nulla.
        new_out = []
        for m in out:
            if (isinstance(m, dict) and m.get("role") == "assistant"
                    and not m.get("tool_calls")
                    and not _text_of(m.get("content")).strip()):
                report["empty_assistant"] += 1
                continue
            new_out.append(m)
        out = new_out
    out = _trim_reasoning(out, cfg, report)
    report["changed"] = (
        report["shown_orphan_tool"] or report["dangling_tool_calls"]
        or report["empty_assistant"] or report["dup_system"]
        or report.get("reasoning_trimmed", 0))
    return out, report
