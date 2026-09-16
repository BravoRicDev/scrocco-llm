"""Complete the OpenRouter free-model × key rotation matrix.

Rules (from user):
  1. ALL 8 keys must cover ALL 18 catalog :free models.
  2. Video models (seedance-2.0-mini, veo-3.1-lite) only on funded keys.
  3. Remove dead models (stealth/ox-alpha, minimax m2.7, minimax m3 = 404).
  4. Fallback rows: already complete, no changes.

Usage:  python3 scripts/complete_matrix.py [--dry-run] [--profile mioaruba]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CSV_PATH = REPO / "var" / "keys_rotation.csv"
PROVIDER = "openrouter"
EP = "https://openrouter.ai/api/v1"

# ── Discovered from OpenRouter GET /api/v1/models ────────────────────────────
CATALOG_FREE = [
    "cohere/north-mini-code:free",
    "dots-studio/dots-3-note-preview:free",
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.0-flash-fin:free",
    "inclusionai/ling-3.0-flash-sante:free",
    "liquid/lfm-2.5-2.6b:free",
    "nex-agi/nex-n2.5-mini:free",
    "nex-agi/nex-n2.5-pro:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-content-safety:free",
    "nvidia/nemotron-3.5-lightning:free",
    "poolside/laguna-s-2.1:free",
    "poolside/laguna-xs-2.1:free",
    "thinkingmachines/inkling-small:free",
    "thinkingmachines/inkling:free",
]

# ── Video models: FUNDED keys only (free-tier skip – no credit) ─────────────
VIDEO_FUNDED_ONLY = [
    "bytedance/seedance-2.0-mini",
    "google/veo-3.1-lite",
]

# ── Dead models (404 from OpenRouter): remove ALL rows ───────────────────────
DEAD_MODELS = {
    "stealth/ox-alpha",
    "minimax/minimax-m2.7:free",
    "minimax/minimax-m3:free",
}

# ── Funded keys are DETECTED at runtime (never hardcoded) ───────────────────
# Segnale stabile nei dati: le chiavi con credito hanno righe `data=fallback`
# nei modelli openrouter (le free-tier non possono servire fallback a pagamento).
# Diamo la lista esplicita solo via --funded (o lo rileviamo dal CSV).
FUNDED_KEYS: frozenset[str] = frozenset()


def load(path: Path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        raw = list(csv.reader(f))
    return raw[0], raw[1:]


def col_idx(header, name):
    return header.index(name)


def pick_metadata(rows, key_col_i, ci, pi, di, ctxi, mi, pri, capi,
                  model, want_data):
    """Copy metadata from a representative existing row for this model."""
    for r in rows:
        if len(r) <= ci:
            continue
        if r[pi].strip().lower() != PROVIDER:
            continue
        if r[ci].strip() != model:
            continue
        if r[di].strip().lower() != want_data:
            continue
        has_key = (len(r) > key_col_i and (r[key_col_i] or "").strip().startswith("sk-or-"))
        if has_key:
            return r[ctxi], r[mi], r[pri], r[capi]
    # fallback: any row
    for r in rows:
        if len(r) <= ci:
            continue
        if r[pi].strip().lower() != PROVIDER:
            continue
        if r[ci].strip() != model:
            continue
        if r[di].strip().lower() == want_data:
            return r[ctxi], r[mi], r[pri], r[capi]
    return None


def run(dry_run: bool, profile: str):
    prefix = "scrocco-llm-"
    profile_col = prefix + profile
    header, rows = load(CSV_PATH)
    ci = col_idx(header, "modello")
    pi = col_idx(header, "provider")
    ei = col_idx(header, "endpoint")
    di = col_idx(header, "data")
    ctxi = col_idx(header, "context")
    mi = col_idx(header, "max_input")
    pri = col_idx(header, "priority")
    capi = col_idx(header, "caps")
    ki = col_idx(header, profile_col)

    # ── Discover all OpenRouter keys in profile column ────────────────────
    all_keys = sorted({
        (r[ki] or "").strip()
        for r in rows
        if len(r) > ki and (r[ki] or "").strip().startswith("sk-or-")
    })
    print(f"Chiavi OpenRouter nel profilo {profile!r}: {len(all_keys)}")

    # ── Detect FUNDED keys: those with >=1 openrouter row with data=fallback/paid ─
    global FUNDED_KEYS
    FUNDED_KEYS = frozenset({
        (r[ki] or "").strip()
        for r in rows
        if len(r) > ki and (r[ki] or "").strip().startswith("sk-or-")
        and (r[pi] or "").strip().lower() == PROVIDER
        and (r[di] or "").strip().lower() in ("fallback", "paid")
    })
    print(f"Chiavi con credito (rilevate da righe fallback): {len(FUNDED_KEYS)}")

    # ── Build current matrix state ────────────────────────────────────────
    have: dict[str, set[str]] = {k: set() for k in all_keys}
    for r in rows:
        if len(r) <= ki:
            continue
        k = (r[ki] or "").strip()
        m = (r[ci] or "").strip()
        d = (r[di] or "").strip().lower()
        p = (r[pi] or "").strip().lower()
        if k in have and p == PROVIDER and d in ("free", "priority"):
            have[k].add(m)

    # ── Step 1: Remove dead models (all keys) ────────────────────────────
    remove_count = 0
    new_rows = []
    for r in rows:
        if len(r) <= ci:
            new_rows.append(r)
            continue
        m = (r[ci] or "").strip()
        p = (r[pi] or "").strip().lower()
        k = (r[ki] or "").strip() if len(r) > ki else ""
        if p == PROVIDER and m in DEAD_MODELS:
            remove_count += 1
            continue  # drop
        new_rows.append(r)
    print(f"Step 1 — Rimossi {remove_count} righe (modelli morti)")

    # ── Step 2: Remove video rows from free-tier keys ─────────────────────
    vid_remove = 0
    filtered = []
    for r in new_rows:
        if len(r) <= ci:
            filtered.append(r)
            continue
        m = (r[ci] or "").strip()
        p = (r[pi] or "").strip().lower()
        k = (r[ki] or "").strip() if len(r) > ki else ""
        d = (r[di] or "").strip().lower()
        if (p == PROVIDER and m in VIDEO_FUNDED_ONLY
                and k in all_keys and k not in FUNDED_KEYS
                and d in ("free", "priority")):
            vid_remove += 1
            continue
        filtered.append(r)
    new_rows = filtered
    print(f"Step 2 — Rimossi {vid_remove} righe video su chiavi free-tier")

    # ── Step 3: Complete free matrix ──────────────────────────────────────
    # Target: every funded key gets catalog_free + video_free_funded_only
    #         every free key   gets catalog_free only
    need_add: dict[str, list[str]] = {}  # key -> [models to add]
    for k in all_keys:
        want = list(CATALOG_FREE)
        if k in FUNDED_KEYS:
            want += list(VIDEO_FUNDED_ONLY)
        missing = [m for m in want if m not in have[k]]
        if missing:
            need_add[k] = missing

    total_add = sum(len(v) for v in need_add.values())
    print(f"Step 3 — {total_add} righe da aggiungere per completare la matrice")

    add_count = 0
    for k in sorted(need_add):
        for model in need_add[k]:
            # Determine data column
            data_val = "free"
            meta = pick_metadata(new_rows, ki, ci, pi, di, ctxi, mi, pri, capi,
                                 model, "free")
            if meta is None:
                print(f"  WARN: nessun template per {model}, skip")
                continue
            ctx, maxin, prio, caps = meta
            new = ["" for _ in header]
            new[ci] = model
            new[pi] = PROVIDER
            new[ei] = EP
            new[di] = data_val
            new[ctxi] = str(ctx).strip()
            new[mi] = str(maxin).strip()
            new[pri] = str(prio).strip()
            new[capi] = caps.strip()
            new[ki] = k
            new_rows.append(new)
            add_count += 1
    print(f"  Aggiunte effettive: {add_count}")

    # ── Summary ───────────────────────────────────────────────────────────
    # Recount final state
    final_have: dict[str, set[str]] = {k: set() for k in all_keys}
    for r in new_rows:
        if len(r) <= ki:
            continue
        k = (r[ki] or "").strip()
        m = (r[ci] or "").strip()
        d = (r[di] or "").strip().lower()
        p = (r[pi] or "").strip().lower()
        if k in final_have and p == PROVIDER and d in ("free", "priority"):
            final_have[k].add(m)

    print()
    print("=== Matrice finale ===")
    for k in all_keys:
        tag = "💰" if k in FUNDED_KEYS else "  "
        models = sorted(final_have[k])
        print(f"  {tag} {k[:16]}… {len(models)} modelli free")
        for m in models:
            extra = " 🎬" if m in VIDEO_FUNDED_ONLY else ""
            print(f"      {m}{extra}")

    # Check completeness
    expected_free = set(CATALOG_FREE)
    expected_funded = expected_free | set(VIDEO_FUNDED_ONLY)
    ok = True
    for k in all_keys:
        want = expected_funded if k in FUNDED_KEYS else expected_free
        actual = final_have[k]
        missing = want - actual
        extra = actual - want
        if missing:
            print(f"  ⚠️  {k[:16]}… MANCANO: {sorted(missing)}")
            ok = False
        if extra:
            print(f"  ⚠️  {k[:16]}… IN ECCESSO: {sorted(extra)}")
    if ok:
        print("\n✅ Matrice completa e coerente")
    else:
        print("\n❌ Matrice INCOMPLETA")

    if dry_run:
        print("\n[dry-run] nessun file toccato")
        return 0

    # ── Atomic write + validate ───────────────────────────────────────────
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(CSV_PATH.parent), suffix=".tmp.csv")
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(new_rows)
        sys.path.insert(0, str(REPO))
        from app.config import GatewayConfig
        GatewayConfig(tmp)
        os.replace(tmp, CSV_PATH)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    print(f"\nOK: CSV validato, {remove_count+vid_remove} rimosse + {add_count} aggiunte")
    print(f"    righe totali: {len(rows)} → {len(new_rows)}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--profile", default="mioaruba")
    ap.add_argument("--funded",
                    help="override funded keys (comma-separated sk-or-… prefixes). "
                         "Default: auto-detected from fallback rows in CSV.")
    a = ap.parse_args()
    if a.funded:
        prefixes = [x.strip() for x in a.funded.split(",") if x.strip()]
        # Resolve full keys from CSV
        header, rows = load(CSV_PATH)
        ki = col_idx(header, f"scrocco-llm-{a.profile}")
        all_k = {(r[ki] or "").strip() for r in rows
                 if len(r) > ki and (r[ki] or "").strip().startswith("sk-or-")}
        global FUNDED_KEYS
        FUNDED_KEYS = frozenset(
            k for k in all_k
            if any(k.startswith(p) for p in prefixes))
    return run(a.dry_run, a.profile)


if __name__ == "__main__":
    raise SystemExit(main())
