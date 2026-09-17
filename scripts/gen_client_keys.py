#!/usr/bin/env python3
"""Genera chiavi client casuali per la policy (produzione).

In sviluppo le chiavi deterministiche `sk-<profilo>` funzionano da sole; in
produzione (`GATEWAY_ENV=production`) sono DISATTIVATE e servono chiavi
esplicite in `client_keys` della policy. Questo script le genera.

Esempi:
    # stampa un blocco YAML con le chiavi mancanti (nessuna scrittura)
    python scripts/gen_client_keys.py

    # profili espliciti, rigenera anche quelli gia' presenti
    python scripts/gen_client_keys.py --profiles alice,bob --force

    # aggiorna la policy sul posto (backup automatico .bak.<ts>)
    python scripts/gen_client_keys.py --write

# [EN] Generates random client keys for the policy. DEV uses deterministic
# sk-<profile> keys; PRODUCTION disables them and requires explicit
# `client_keys`. Prints YAML by default; `--write` updates the file (backup).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth import generate_client_key, gateway_env  # noqa: E402


def _resolve_policy(arg: str | None) -> Path:
    return Path(arg or os.environ.get("GATEWAY_POLICY") or "./var/gateway.yaml")


def _resolve_csv(arg: str | None) -> Path:
    return Path(arg or os.environ.get("GATEWAY_CSV") or "./var/keys_rotation.csv")


def _profiles_from_csv(csv_path: Path) -> list[str]:
    try:
        from app.config import GatewayConfig

        cfg = GatewayConfig(csv_path)
        return list(cfg.profiles)
    except Exception as exc:                        # noqa: BLE001
        print(f"[warn] impossibile leggere i profili da {csv_path}: {exc}",
              file=sys.stderr)
        return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Genera client_keys casuali per la policy.")
    ap.add_argument("--policy", help="path gateway.yaml (default $GATEWAY_POLICY)")
    ap.add_argument("--csv", help="path keys_rotation.csv per elencare i profili")
    ap.add_argument("--profiles", help="lista profili separati da virgola")
    ap.add_argument("--force", action="store_true",
                    help="rigenera anche le chiavi gia' presenti")
    ap.add_argument("--write", action="store_true",
                    help="scrive la policy (backup .bak.<ts>)")
    args = ap.parse_args(argv)

    policy_path = _resolve_policy(args.policy)

    doc: dict = {}
    if policy_path.exists():
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            doc = loaded
    else:
        print(f"[warn] policy non trovata: {policy_path} (ne creo una nuova)",
              file=sys.stderr)

    existing = doc.get("client_keys")
    existing = dict(existing) if isinstance(existing, dict) else {}

    profiles: list[str] = []
    if args.profiles:
        profiles = [p.strip() for p in args.profiles.split(",") if p.strip()]
    elif existing:
        profiles = list(existing)
    else:
        profiles = _profiles_from_csv(_resolve_csv(args.csv))

    if not profiles:
        print("[error] nessun profilo: usa --profiles o fornisci un CSV valido",
              file=sys.stderr)
        return 2

    new_keys: dict[str, str] = dict(existing)
    changed: list[str] = []
    for p in profiles:
        if p in existing and not args.force:
            continue
        new_keys[p] = generate_client_key()
        changed.append(p)

    block = yaml.safe_dump({"client_keys": new_keys},
                           sort_keys=False, allow_unicode=True)
    print(f"# GATEWAY_ENV={gateway_env()} · policy={policy_path}")
    print(block, end="")

    if not changed:
        print("# nessuna modifica (usa --force per rigenerare)", file=sys.stderr)
    if args.write and changed:
        backup = policy_path.with_suffix(
            policy_path.suffix + f".bak.{time.strftime('%Y%m%d-%H%M%S')}")
        if policy_path.exists():
            backup.write_bytes(policy_path.read_bytes())
        doc["client_keys"] = new_keys
        policy_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
            encoding="utf-8")
        print(f"# scritte {len(changed)} chiavi in {policy_path} "
              f"(backup: {backup})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
