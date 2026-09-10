"""Append a single OpenRouter API key to every free + fallback OpenRouter
deployment in var/keys_rotation.csv.

Why: an OpenRouter key with credit must be pooled into every free model row
(and the -fallback bucket) so the router rotates it like the other keys.
Hand-editing the CSV is allowed ONLY via this validate-before-swap script
(AGENT.md invariant #2: atomic validated write, never a half-written file).

Idempotent: skips any (model, key) pair already present.

Usage:  python3 scripts/add_openrouter_key.py KEY  [--profile myteam]
Run with --dry-run first to preview without touching the file.
"""
from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CSV_PATH = REPO / "var" / "keys_rotation.csv"
ENDPOINT = "https://openrouter.ai/api/v1"
PROVIDER = "openrouter"

# models that must exist as :free rows even if absent from the current CSV
NEW_FREE = {
    # model -> (context_k, max_input, priority, caps)
    "nex-agi/nex-n2.5-mini:free": (262, 50000, 0, "text,vision"),
    "nex-agi/nex-n2.5-pro:free":  (262, 50000, 0, "text,vision"),
}


def load(path: Path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        raw = list(csv.reader(f))
    header = raw[0]
    rows = raw[1:]
    return header, rows


def col(header, name):
    return header.index(name)


def pick_metadata(rows, ci, pi, di, ctxi, mi, pri, capi, model, want_data):
    """Return the metadata of a representative EXISTING row for this model.
    Prefer a row of the wanted data category that already carries a key."""
    best = None
    for r in rows:
        if len(r) <= ci or r[pi].strip().lower() != PROVIDER:
            continue
        if r[ci].strip() != model:
            continue
        if r[di].strip().lower() != want_data:
            continue
        has_key = any(v.strip() for v in r[9:]) if len(r) > 9 else False
        cand = (r[ctxi], r[mi], r[pri], r[capi])
        if has_key:
            return cand          # keyed row wins (authoritative metadata)
        if best is None:
            best = cand
    return best


def build_new_rows(header, rows, key, profile_col):
    ci = col(header, "modello")
    pi = col(header, "provider")
    ei = col(header, "endpoint")
    di = col(header, "data")
    ctxi = col(header, "context")
    mi = col(header, "max_input")
    pri = col(header, "priority")
    capi = col(header, "caps")
    ki = col(header, profile_col)

    # existing (model, data) -> have this exact key?
    present = set()
    for r in rows:
        if len(r) > ki and r[ki].strip() == key:
            present.add((r[ci].strip(), r[di].strip().lower()))

    models_free = sorted({
        r[ci].strip() for r in rows
        if len(r) > di and r[pi].strip().lower() == PROVIDER
        and r[di].strip().lower() == "free"
    } | set(NEW_FREE))

    models_fb = sorted({
        r[ci].strip() for r in rows
        if len(r) > di and r[pi].strip().lower() == PROVIDER
        and r[di].strip().lower() == "fallback"
    })

    out = []
    for model, want in [(m, "free") for m in models_free] + \
                       [(m, "fallback") for m in models_fb]:
        if (model, want) in present:
            continue
        meta = pick_metadata(rows, ci, pi, di, ctxi, mi, pri, capi, model, want)
        if meta is None:
            if want == "free" and model in NEW_FREE:
                ctx, maxin, prio, caps = NEW_FREE[model]
            else:
                print(f"WARN: nessun template per {model!r} ({want})", file=sys.stderr)
                continue
        else:
            ctx, maxin, prio, caps = meta
        new = ["" for _ in header]
        new[ci] = model
        new[pi] = PROVIDER
        new[ei] = ENDPOINT
        new[di] = want
        new[ctxi] = str(ctx).strip()
        new[mi] = str(maxin).strip()
        new[pri] = str(prio).strip()
        new[capi] = caps.strip()
        new[ki] = key
        out.append(new)
    return out, len(models_free), len(models_fb)


def write_atomic(path: Path, header, rows, extra_rows):
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp.csv")
    import os
    os.close(tmp_fd)
    tmp = Path(tmp_name)
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
            w.writerows(extra_rows)
        # VALIDATE like live: must instantiate without error
        sys.path.insert(0, str(REPO))
        from app.config import GatewayConfig
        GatewayConfig(tmp)                      # raises on any structural break
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key")
    ap.add_argument("--profile", default="myteam")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    key = a.key.strip()
    if not key.startswith("sk-or-"):
        print("ERRORE: la chiave non sembra un token OpenRouter (sk-or-…)",
              file=sys.stderr)
        return 2
    profile_col = "scrocco-llm-" + a.profile
    header, rows = load(CSV_PATH)
    if profile_col not in header:
        print(f"ERRORE: colonna profilo {profile_col!r} non presente",
              file=sys.stderr)
        return 2

    new_rows, nf, nfallback = build_new_rows(header, rows, key, profile_col)
    print(f"Modelli free pool: {nf} | fallback: {nfallback}")
    print(f"Righe nuove da aggiungere: {len(new_rows)}")
    for r in new_rows:
        ci = col(header, "modello"); di = col(header, "data")
        capi = col(header, "caps")
        print(f"  + {r[ci]:55s} data={r[di]:9s} caps={r[capi]!r}")

    if a.dry_run:
        print("\n[dry-run] nessun file toccato")
        return 0

    write_atomic(CSV_PATH, header, rows, new_rows)
    print(f"\nOK: {len(new_rows)} righe aggiunte e CSV validato->swap atomico")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
