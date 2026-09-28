"""Auto-learn delle capacita': un modello che rifiuta una modalita' la perde.

[IT] Dopo `cap_auto_learn_threshold` rifiuti (strike) di una capacita'
(vision, audio, ...) da parte di un modello:
- mode=suggest (default): si registra nel journal quali righe del CSV
  andrebbero corrette (`membership_removal_candidates`), senza scrivere;
- mode=auto: si toglie la capacita' dal modello in
  capability_routing.model_capabilities (`remove_cap_for_model`), sotto il
  lock delle scritture di config (app/config_writes.py).
Prima viveva in app/admin.py: il percorso di una richiesta chat importava
l'intero modulo admin solo per questo.

[EN] Capability auto-learn (suggest / auto) from repeated modality refusals.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from . import config_writes, csv_store, journal
from . import state as gw_state
from .config import MODEL_HEADER
from .policy_store import persist_merged

log = logging.getLogger("nx.admin")


def strip_cap_from_map(map_: dict[str, list[str]], model: str,
                       cap: str, floor: tuple[str, ...] = ("text",)) -> dict[str, list[str]]:
    """PURa: mappa aggiornata con le capacità di `model` ridotte di `cap`.

    Inserisce/aggiorna una entry ESPLICITA per il modello (vince già alla
    risoluzione) preservando le glob per gli altri modelli. Se la rimozione
    svuota tutto, applica un floor minimo (default ["text"]) per non rendere
    il modello instradabile a nulla."""
    import fnmatch as _fn
    resolved: set[str] = set()
    if model in map_:
        resolved = set(map_[model])
    else:
        best_len = -1
        for pat, caps in map_.items():
            if _fn.fnmatch(model, pat) and len(pat) > best_len:
                best_len = len(pat)
                resolved = set(caps)
    if not resolved:
        resolved = {"text"}
    newcaps = sorted(resolved - {cap})
    if not newcaps:
        newcaps = list(floor)
    out = dict(map_)
    out[model] = newcaps
    return out


def remove_cap_for_model(model: str, cap: str, evidence: str = "",
                         count: int = 0) -> dict | None:
    """AUTO-LEARN (mode=auto): rimuove `cap` da `model` nella mappa con
    scrittura atomica validata + journal. Ritorna il report o None su errore.
    Lettura-modifica-scrittura sotto il lock delle scritture di config: va
    chiamata FUORI dall'event loop (vedi chat_helpers._auto_learn_apply)."""
    with config_writes.locked():
        report = _remove_cap_for_model_locked(model, cap, evidence, count)
    if report is not None:
        config_writes.changed()
    return report


def _remove_cap_for_model_locked(model: str, cap: str, evidence: str,
                                 count: int) -> dict | None:
    try:
        current: dict = {}
        if Path(gw_state.POLICY_PATH).exists():
            with open(gw_state.POLICY_PATH, encoding="utf-8") as f:
                current = yaml.safe_load(f) or {}
        cr = dict(current.get("capability_routing") or {})
        mc = dict(cr.get("model_capabilities") or {})
        before = sorted(gw_state.policy.caps_for(model))
        mc2 = strip_cap_from_map(mc, model, cap)
        cr["model_capabilities"] = mc2
        nxt = dict(current)
        nxt["capability_routing"] = cr
        fresh = persist_merged(nxt)
        if fresh is None:
            log.error("[caps][auto-learn] persist fallita per %s/%s", model, cap)
            return None
        after = sorted(fresh.caps_for(model))
        report = {"model": model, "cap": cap, "count": count,
                  "evidence": (evidence or "")[:200],
                  "before": before, "after": after,
                  "pattern_edited": model}
        journal.record(gw_state.VAR_DIR, "cap_auto_learn", report)
        log.warning("[caps][auto-learn] rimossa '%s' da %s dopo %d strike "
                    "(%s -> %s). Revert: PATCH capability_routing."
                    "model_capabilities", cap, model, count,
                    before, after)
        return report
    except Exception as exc:                 # noqa: BLE001
        log.error("[caps][auto-learn] errore su %s/%s: %s", model, cap, exc)
        return None


def _row_caps_of(row: dict) -> list[str]:
    from .csv_store import CAPS_TOKENS
    return sorted({t.strip().lower() for t in
                   (row.get("caps") or "").split(",") if t.strip()}
                  & CAPS_TOKENS)


def membership_removal_candidates(modello: str, cap: str) -> list[dict]:
    """AUTO-LEARN suggest: righe candidate alla rimozione del token `cap`
    (modello combacia e cap presente nella colonna caps). Solo lettura."""
    try:
        header, rows = csv_store.load_table(gw_state.CSV_PATH)
    except Exception:
        return []
    out: list[dict] = []
    prefix = gw_state.config.proxy_prefix
    for row in rows:
        if (row.get(MODEL_HEADER) or "").strip().lower() != modello.lower():
            continue
        caps_list = _row_caps_of(row)
        if cap not in caps_list:
            continue
        profile = ""
        for h in header:
            if h.startswith(prefix) and (row.get(h) or "").strip():
                profile = h[len(prefix):]
                break
        out.append({"id": csv_store.row_id(row, csv_store.endpoint_of(header, row)),
                    "profile": profile, "caps": caps_list})
    return out
