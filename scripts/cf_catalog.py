#!/usr/bin/env python3
"""Catalogo Cloudflare Workers AI (free, non-deprecati) -> keys_rotation.csv.

- Individua gli account CF univoci gia' presenti nel CSV (chiave + endpoint +
  commento/email).
- Rimuove TUTTE le righe provider=cloudflare.
- Reinserisce ogni modello del catalogo per OGNI account.
- context = contesto dichiarato da Cloudflare (in k, arrotondato);
  max_input = 50% del contesto in token.
- data=free, priority=0, caps da catalogo (text / text,vision).

Default: dry-run. Usare --apply per scrivere (backup + validazione + atomic).
"""
from __future__ import annotations

import csv
import os
import shutil
import sys
from datetime import datetime

CSV = "var/keys_rotation.csv"
PROFILE_COL = "scrocco-llm-mioaruba"
PROVIDER = "cloudflare"

# (modello, context_k, context_tokens, caps)
CATALOG: list[tuple[str, int, int, str]] = [
    ("@cf/qwen/qwen3.8-27b",                         262, 262144, "text,vision"),
    ("@cf/google/gemma-4-26b-a4b-it",                256, 256000, "text,vision"),
    ("@cf/nvidia/nemotron-3-120b-a12b",              256, 256000, "text"),
    ("@cf/zai-org/glm-4.7-flash",                    131, 131072, "text"),
    ("@cf/meta/llama-4-scout-17b-16e-instruct",      131, 131000, "text,vision"),
    ("@cf/ibm-granite/granite-4.0-h-micro",          131, 131000, "text"),
    ("@cf/openai/gpt-oss-120b",                      128, 128000, "text"),
    ("@cf/openai/gpt-oss-20b",                       128, 128000, "text"),
    ("@cf/meta/llama-3.1-8b-instruct-fast",          128, 128000, "text"),
    ("@cf/meta/llama-3.2-11b-vision-instruct",       128, 128000, "text,vision"),
    ("@cf/aisingapore/gemma-sea-lion-v4-27b-it",     128, 128000, "text"),
    ("@cf/mistralai/mistral-small-3.1-24b-instruct", 128, 128000, "text"),
    ("@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",  80,  80000, "text"),
    ("@cf/meta/llama-3.2-3b-instruct",                80,  80000, "text"),
    ("@cf/meta/llama-3.2-1b-instruct",                60,  60000, "text"),
    ("@cf/qwen/qwen2.5-coder-32b-instruct",           32,  32768, "text"),
    ("@cf/qwen/qwen3-30b-a3b-fp8",                    32,  32768, "text"),
    ("@cf/meta/llama-3.1-8b-instruct-fp8",            32,  32000, "text"),
    ("@cf/meta/llama-3.3-70b-instruct-fp8-fast",      24,  24000, "text"),
    ("@cf/qwen/qwq-32b",                              24,  24000, "text"),
]


def load_rows(path: str):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        all_rows = list(reader)
    hdr = all_rows[0]
    rows = [dict(zip(hdr, r + [""] * (len(hdr) - len(r)))) for r in all_rows[1:]]
    return hdr, rows


def save_rows(path: str, hdr: list[str], rows: list[dict]) -> None:
    tmp = path + ".tmp." + datetime.now().strftime("%Y%m%d%H%M%S")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=hdr)
        w.writeheader()
        w.writerows(rows)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.config import GatewayConfig
    GatewayConfig(tmp)
    os.replace(tmp, path)


def make_row(hdr, **kw) -> dict:
    row = {h: "" for h in hdr}
    row.update(kw)
    return row


def main() -> None:
    apply = "--apply" in sys.argv
    hdr, rows = load_rows(CSV)

    cf = [r for r in rows if r["provider"].strip().lower() == PROVIDER]
    accounts: dict[str, dict] = {}
    for r in cf:
        key = r[PROFILE_COL].strip()
        if not key:
            continue
        accounts.setdefault(key, {
            "endpoint": r["endpoint"].strip(),
            "commento": r.get("commento", "").strip(),
        })
    if not accounts:
        print("[ERR] nessun account Cloudflare nel CSV")
        sys.exit(1)

    print(f"[INFO] account CF univoci: {len(accounts)}")
    print(f"[INFO] modelli catalogo: {len(CATALOG)}")
    print(f"[INFO] righe da inserire: {len(accounts) * len(CATALOG)}")
    print(f"[INFO] righe CF esistenti da rimuovere: {len(cf)}")

    remaining = [r for r in rows if r["provider"].strip().lower() != PROVIDER]
    new_rows: list[dict] = []
    for key, acc in accounts.items():
        for model, ctx_k, ctx_tok, caps in CATALOG:
            new_rows.append(make_row(
                hdr,
                commento=acc["commento"],
                modello=model,
                provider=PROVIDER,
                endpoint=acc["endpoint"],
                data="free",
                context=str(ctx_k),
                max_input=str(ctx_tok // 2),
                priority="0",
                caps=caps,
                **{PROFILE_COL: key},
            ))

    print("\n--- anteprima (primo account) ---")
    for r in new_rows[:len(CATALOG)]:
        print(f"  {r['modello']:48s} ctx={r['context']:>4s} max_in={r['max_input']:>7s} caps={r['caps']}")

    if not apply:
        print("\n[DRY-RUN] nessuna modifica. Usa --apply per scrivere.")
        return

    bak = CSV + f".bak.cf-catalog.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(CSV, bak)
    print(f"\n[BACKUP] {bak}")
    save_rows(CSV, hdr, remaining + new_rows)
    print(f"[OK] CSV: {len(rows)} -> {len(remaining) + len(new_rows)} righe "
          f"({len(remaining)} non-CF + {len(new_rows)} CF)")


if __name__ == "__main__":
    main()
