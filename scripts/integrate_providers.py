#!/usr/bin/env python3
"""
Idempotent provider-key integration tool.

Reads intake file from var/intake.json (gitignored, contains secrets).
Writes to keys_rotation.csv atomically with GatewayConfig validation.

Intake format (var/intake.json):
{
  "remove_llm7_dead_models": true,
  "opencode_new_keys": ["sk-xxx", "sk-yyy"],
  "opencode_go_endpoint": "https://opencode.ai/zen/go/v1",
  "opencode_zen_endpoint": "https://opencode.ai/zen/v1",
  "opencode_go_models": {"model-name": ["data","ctx","maxin","prio","caps"], ...},
  "opencode_zen_models": {"model-name": ["data","ctx","maxin","prio","caps"], ...},
  "google_bare_endpoint": "...",
  "google_bare_model": "gemini-3.5-flash",
  "google_bare_meta": ["free","1000","1000000","0","text,vision"],
  "google_bare_keys": ["AQ.xxx", ...]
}
"""
import csv, json, os, shutil, sys
from datetime import datetime

CSV = "var/keys_rotation.csv"
PROFILE_COL = os.environ.get("SCROCCO_PROFILE_COL", "scrocco-llm-example")
INTAKE = "var/intake.json"
LLM7_DEAD = ["deepseek-v4-flash", "gpt-oss", "meta-Llama-3.1-8B-Instruct-Turbo"]
LLM7_PROV = "llm7"
LLM7_EP = "https://api.llm7.io/v1"

def load_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        hdr = list(csv.reader(f))[0]
        f.seek(0); reader = csv.DictReader(f)
        return hdr, list(reader)

def make_row(hdr, **kw):
    row = {h: "" for h in hdr}
    for k, v in kw.items():
        row[k] = v
    return row

def save_rows(path, hdr, rows):
    tmp = path + ".tmp." + datetime.now().strftime("%Y%m%d%H%M%S")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=hdr)
        w.writeheader()
        w.writerows(rows)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.config import GatewayConfig
    GatewayConfig(tmp)
    os.replace(tmp, path)

def main():
    dry = "--dry-run" in sys.argv

    with open(INTAKE) as f:
        intake = json.load(f)

    hdr, rows = load_rows(CSV)

    # --- 1. REMOVE llm7 dead models ---
    to_remove = {id(r) for r in rows
                 if r["provider"].strip().lower() == LLM7_PROV and r["modello"] in LLM7_DEAD}
    if not dry:
        rows = [r for r in rows if id(r) not in to_remove]
    print(f"[REMOVE] llm7 dead: {len(to_remove)} rows")

    # --- 2. Build dedup set ---
    existing = {(r["provider"].strip().lower(), r["modello"],
                 (r.get(PROFILE_COL) or "").strip()) for r in rows
                if (r.get(PROFILE_COL) or "").strip()}

    new_rows = []
    def add(model, prov, ep, meta, key):
        trip = (prov, model, key)
        if trip in existing:
            return False
        data, ctx, mx, pri, caps = meta
        new_rows.append(make_row(hdr, modello=model, provider=prov, endpoint=ep,
                                 data=data, context=ctx, max_input=mx,
                                 priority=pri, caps=caps, **{PROFILE_COL: key}))
        existing.add(trip)
        return True

    # --- 3. opencode go + zen ---
    go_m = intake.get("opencode_go_models", {})
    zen_m = intake.get("opencode_zen_models", {})
    new_keys = intake.get("opencode_new_keys", [])
    for k in new_keys:
        for m, mt in go_m.items():
            add(m, "opencode-go", intake["opencode_go_endpoint"], mt, k)
        for m, mt in zen_m.items():
            add(m, "opencode-zen", intake["opencode_zen_endpoint"], mt, k)
    n_go_zen = len(new_keys) * (len(go_m) + len(zen_m))
    print(f"[ADD] opencode go+zen: {len(new_keys)} keys × ({len(go_m)}+{len(zen_m)}) models")

    # --- 4. google bare ---
    gm = intake.get("google_bare_model")
    gmeta = tuple(intake.get("google_bare_meta", []))
    gk = intake.get("google_bare_keys", [])
    for k in gk:
        add(gm, "google", intake.get("google_bare_endpoint", ""), gmeta, k)
    print(f"[ADD] google bare {gm}: {len(gk)} keys")

    print(f"\nRimosse: {len(to_remove)} | Aggiunte: {len(new_rows)} | Rete: +{len(new_rows)-len(to_remove)}")

    if dry:
        print("[DRY-RUN] nessun file modificato")
        return

    bak = CSV + f".bak.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(CSV, bak)
    print(f"[BACKUP] {bak}")
    rows.extend(new_rows)
    save_rows(CSV, hdr, rows)
    print("[OK] CSV aggiornato e validato")

if __name__ == "__main__":
    main()
