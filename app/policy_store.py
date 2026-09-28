"""gateway.yaml: scritture atomiche e validate, poi policy attiva subito.

[IT] Ogni scrittura passa da un file temporaneo validato con `Policy.load`:
un yaml invalido non tocca mai il file live. A scrittura riuscita la nuova
policy diventa quella del router e dei moduli (`apply_policy`), senza
aspettare il giro del watcher. Merge dei PATCH: scalari sostituiti, dict
fusi in profondita', alcune chiavi (aliases, pricing, ...) sostituite
intere; `profiles`/`alias_keys`/`client_keys` uniti per chiave (valore
vuoto = cancella). Usato dall'admin API e dall'auto-learn delle capacita'.

[EN] Atomic, validated gateway.yaml writes + immediate policy swap.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

import yaml

from . import state as gw_state
from .policy import Policy
from .runtime_persistence import apply_policy

log = logging.getLogger("nx.admin")


POLICY_REPLACE_KEYS = frozenset({
    "aliases", "pricing", "scoring_weights", "effort_temperature_overrides",
    "profiles", "alias_keys", "client_keys",
})


def deep_merge(base: dict, patch: dict) -> dict:
    """Merge ricorsivo: dict+dict fusi in profondita'; ogni altro tipo
    (liste incluse) SOSTITUITO."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_policy_patch(current: dict, patch: dict) -> dict:
    """Merge del patch sul yaml corrente: scalari sostituiti; 'profiles'
    unito per-profilo; 'alias_keys' unito per-alias (valore vuoto/null =
    cancella l'override, torna al pool); liste/aliases sostituiti se forniti."""
    merged = dict(current)
    for k, v in patch.items():
        if k == "profiles" and isinstance(v, dict) \
                and isinstance(merged.get("profiles"), dict):
            merged["profiles"] = {**merged["profiles"], **v}
        elif k == "alias_keys" and isinstance(v, dict):
            existing = dict(merged.get("alias_keys") or {})
            for ak, av in v.items():
                if av in (None, ""):
                    existing.pop(ak, None)
                else:
                    existing[ak] = av
            merged["alias_keys"] = existing
        elif k == "client_keys" and isinstance(v, dict):
            existing = dict(merged.get("client_keys") or {})
            for ak, av in v.items():
                if av in (None, ""):
                    existing.pop(ak, None)
                else:
                    existing[ak] = av
            merged["client_keys"] = existing
        else:
            if (k not in POLICY_REPLACE_KEYS
                    and isinstance(v, dict)
                    and isinstance(merged.get(k), dict)):
                merged[k] = deep_merge(merged[k], v)
            else:
                merged[k] = v
    return merged


def persist_merged(merged: dict) -> Policy | None:
    """Scrittura atomica+validata del yaml unito e swap dei riferimenti runtime.
    Ritorna la Policy fresca o None se invalida (file intatto)."""
    policy_path = Path(gw_state.POLICY_PATH)
    fd, tmp_name = tempfile.mkstemp(dir=str(policy_path.parent),
                                    suffix=".tmp.yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
        fresh = Policy.load(tmp_name)          # validazione preventiva
        os.replace(tmp_name, policy_path)      # atomico
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        return None
    # swap immediato dei riferimenti (stessa manovra del watcher)
    gw_state.router.policy = fresh
    gw_state.policy = fresh
    apply_policy(fresh)        # subito anche forwarder/stime/cooldown, non al giro del watcher
    return fresh


def persist_raw(raw_text: str):
    """Sostituisce l'INTERO gateway.yaml col testo fornito (nessun merge).
    Valida su tmp con Policy.load; su OK: backup best-effort del file corrente
    in var/backups/gateway.yaml-<ts>.yaml, os.replace atomico, swap dei
    riferimenti runtime. Ritorna la Policy fresca, o None se invalida
    (file live INTATTO)."""
    policy_path = Path(gw_state.POLICY_PATH)
    fd, tmp_name = tempfile.mkstemp(dir=str(policy_path.parent),
                                    suffix=".tmp.yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(raw_text)
        fresh = Policy.load(tmp_name)             # validazione preventiva
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        return None
    # backup del file corrente (best-effort, non blocca)
    try:
        if policy_path.exists():
            bdir = Path(gw_state.VAR_DIR) / "backups"
            bdir.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            (bdir / f"gateway.yaml-{ts}.yaml").write_text(
                policy_path.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        log.warning("[policy] backup pre-raw fallito: %s", exc)
    try:
        os.replace(tmp_name, policy_path)          # atomico
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        return None
    gw_state.router.policy = fresh
    gw_state.policy = fresh
    apply_policy(fresh)        # subito anche forwarder/stime/cooldown, non al giro del watcher
    return fresh
