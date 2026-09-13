#!/usr/bin/env python3
"""Audit modelli: confronta i deployment del CSV con i /models REALI dei provider.

Per ogni combinazione (endpoint, chiave) fa UNA chiamata GET {endpoint}/models
e verifica che il modello EFFETTIVO (post infer_model_prefix) esista.

Uso:
  .venv/bin/python scripts/audit_models.py            # solo report
  .venv/bin/python scripts/audit_models.py --fix      # corregge via admin API
                                                      # i mismatch con 1 solo
                                                      # candidato chiaro

Mai chiavi in output (solo mascherate).
"""
from __future__ import annotations

import asyncio
import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import GatewayConfig, infer_model_prefix  # noqa: E402
from app import provider_models  # noqa: E402
from tui.gateway_client import GatewayClient, load_master_key  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
CSV_PATH = Path(__file__).resolve().parent.parent / "var" / "keys_rotation.csv"

PREFIXES = ("openai/", "mistral/", "nvidia/", "cloudflare/", "meta-llama/")


def _slug(name: str) -> str:
    """Normalizza per confronto fuzzy: minuscolo, senza prefissi noti e
    senza punteggiatura."""
    s = name.strip().lower()
    for p in PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    return re.sub(r"[^a-z0-9]+", "", s)


def _candidates(configured: str, available: list[str]) -> list[str]:
    """Candidati plausibili per un modello configurato assente."""
    cs = _slug(configured)
    out = []
    for av in available:
        a = _slug(av)
        if not a or not cs:
            continue
        if a == cs or a.endswith(cs) or cs.endswith(a) or a in cs or cs in a:
            out.append(av)
    return sorted(set(out))


async def main(fix: bool) -> int:
    cfg = GatewayConfig(CSV_PATH)
    # Una sola GET /models per ENDPOINT (prima chiave valida, fallback sulle
    # successive): le chiavi gemelle condividono la stessa lista.
    deps_by: dict[str, list[dict]] = defaultdict(list)
    keys_by: dict[str, list[str]] = defaultdict(list)
    seen_keys: dict[str, set[str]] = defaultdict(set)
    for gname, deps in cfg.groups.items():
        for d in deps:
            base = (d["api_base"] or "").rstrip("/")
            deps_by[base].append(d)
            s = seen_keys[base]
            if d["api_key"] and d["api_key"] not in s:
                s.add(d["api_key"])
                keys_by[base].append(d["api_key"])

    print(f"deployment totali: {sum(len(v) for v in cfg.groups.values())} "
          f"in {len(deps_by)} endpoint(s)\n")

    problems: list[tuple[dict, list[str]]] = []
    async with httpx.AsyncClient(timeout=20.0) as http:
        for i, base in enumerate(sorted(deps_by), start=1):
            deps = deps_by[base]
            res = await provider_models.fetch_provider_models(
                http, base, keys_by[base], ttl_sec=0)
            masked = res.key_masked or "—"
            if not res.ok:
                print(f"[{i:>2}] {base} ({masked}) -> {res.error}: "
                      f"TUTTI i {len(deps)} dep non verificabili")
                continue
            ids = [m.get("id", "") for m in (res.items or [])]
            idset = set(ids)

            missing = []
            for d in deps:
                eff, _needs = infer_model_prefix(d["model"], d["api_base"])
                if eff in idset:
                    continue
                # match ESATTO col prefisso della lista (es. Google "models/")
                if f"models/{eff}" in idset:
                    missing.append((d, [f"models/{eff}"]))
                    continue
                cand = _candidates(eff, ids)
                missing.append((d, eff, cand))
            if missing:
                print(f"[{i:>2}] {base} ({masked}) -> {len(missing)}/"
                      f"{len(deps)} modelli ASSENTI:")
                for item in missing:
                    if len(item) == 2:
                        d, cand = item
                        print(f"     · [{d['unique']}] config='{d['model']}' "
                              f"-> ESATTO {cand[0]}")
                    else:
                        d, eff, cand = item
                        print(f"     · [{d['unique']}] config='{d['model']}' "
                              f"effettivo='{eff}'"
                              + (f"  candidati={cand[:4]}" if cand else
                                 "  nessun candidato"))
                for item in missing:
                    problems.append((item[0], item[1]))
            else:
                print(f"[{i:>2}] {base} ({masked}) -> tutti OK "
                      f"({len(deps)} dep)")
            await asyncio.sleep(0.2)                    # gentile coi provider

    print(f"\n=== RIEPILOGO: {len(problems)} deployment da sistemare ===")
    if fix and problems:
        cli = GatewayClient()
        # L'id admin (drow_*) non coincide con l'unique: lo abbiniamo con
        # (modello, endpoint, chiave mascherata) — formato identico a csv_store
        from app.csv_store import mask_key  # noqa: E402
        deps_admin = await cli.deployments()
        lookup = {(d["modello"], (d["endpoint"] or "").rstrip("/"),
                   d["key_masked"]): d["id"]
                  for d in deps_admin}
        fixed = 0
        for d, cand in problems:
            if len(cand) != 1:
                continue                     # solo match univoco: sicuro
            rid = lookup.get((d["model"], d["api_base"].rstrip("/"),
                              mask_key(d["api_key"])))
            if not rid:
                print(f"SKIP  {d['unique']}: nessun id admin abbinato")
                continue
            new_model = cand[0]
            try:
                await cli.update_deployment(rid, {"modello": new_model})
            except Exception as exc:         # noqa: BLE001
                print(f"FAIL  {d['unique']}: {exc}")
                continue
            fixed += 1
            print(f"FIXED {d['unique']}: '{d['model']}' -> '{new_model}'")
        print(f"\ncorrezioni applicate: {fixed}/{len(problems)}")
    elif problems:
        print("(usa --fix per correggere automaticamente i match univoci)")
    await asyncio.get_event_loop().run_in_executor(None, lambda: None)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="corregge via API i mismatch con candidato univoco")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.fix)))
