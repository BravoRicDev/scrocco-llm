#!/usr/bin/env python3
"""Catalogo Cloudflare Workers AI -> keys_rotation.csv. AGGIORNA, non rigenera.

CONTRATTO CAMBIATO (2026-10-11), dopo un incidente reale su cubotto.

La versione precedente cancellava TUTTE le righe provider=cloudflare e le
riscriveva con le sole colonne del catalogo (modello, endpoint, data, context,
max_input, priority, caps). Eseguirla su un catalogo curato a mano distrugge
tutto il resto: alias, effort_capable, intelligence_score, model_preference,
media_defer, order, hold_until_finish, image_via. Misurato: da 9343 a 8251
deployment (-1092, tutte le alias) e 204 alias su 204 spariti.

REGOLA NUOVA - solo aggiunte e, su richiesta, aggiornamenti:
  - riga ESISTENTE     -> NON toccata (nemmeno context/max_input), salvo
                          --update-context: allora aggiorna SOLO
                          context/max_input/caps stampando ogni differenza;
  - riga MANCANTE      -> creata CLONANDO una riga CF sorella dello stesso
                          account (stesso caps, la piu' capiente), cosi' tutti i
                          campi curati restano coerenti. L'alias resta VUOTO:
                          un alias inventato instraderebbe male, e chi cura il
                          catalogo decide se e come esporlo;
  - riga FUORI catalogo-> lasciata intatta (prima veniva cancellata).

GATE prima di scrivere: nessuna identita' di deployment puo' sparire e il numero
di deployment non puo' calare. Se accade, la scrittura e' annullata.

Nota: il valore `context` nel CSV e' in k e `max_input` NON e' sempre il 50% del
contesto dichiarato dallo script: sul catalogo vivo divergono in 5 casi su 17,
segno di tarature successive. Per questo gli aggiornamenti sono opt-in.
"""
from __future__ import annotations

import csv
import os
import shutil
import sys
from datetime import datetime

CSV = os.environ.get("SCROCCO_CSV", "var/keys_rotation.csv")
PROFILE_COL = os.environ.get("SCROCCO_PROFILE_COL", "scrocco-llm-example")
PROVIDER = "cloudflare"

# Campi di cui questo script e' proprietario (tutti gli altri sono intoccabili).
OWNED = ("context", "max_input", "caps")

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


def load_rows(path: str = CSV):
    with open(path, newline="", encoding="utf-8-sig") as f:
        all_rows = list(csv.reader(f))
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


def _ctx(r: dict) -> int:
    try:
        return int((r.get("context") or "0").strip() or 0)
    except ValueError:
        return 0


def _n_deployments(path: str) -> tuple[int, set]:
    """(deployment, identita' semantiche) di un CSV, via il codice dell'app.

    Le identita' sono il contenuto dei deployment SENZA `unique`/`sort_key`:
    l'`unique` e' funzione del contenuto del CSV, quindi togliendo o aggiungendo
    righe si RINUMERA (config.py:447) e un confronto per `unique` darebbe falsi
    allarmi. 9343 deployment corrispondono a soli 1876 unique distinti perche'
    lo stesso unique vive in piu' gruppi dimensionali (config.py:565).
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.config import GatewayConfig
    cfg = GatewayConfig(path)
    ident = set()
    for gname, lst in cfg.groups.items():
        for d in lst:
            sig = tuple(sorted((k, str(v)) for k, v in d.items()
                               if k not in ("unique", "sort_key")))
            ident.add((gname, sig))
    return sum(len(v) for v in cfg.groups.values()), ident


def pick_template(by_model: dict[str, dict], caps: str) -> dict | None:
    """Riga sorella da clonare: stesso caps se esiste, altrimenti la piu' capiente."""
    if not by_model:
        return None
    same = [r for r in by_model.values() if (r.get("caps") or "").strip() == caps]
    pool = same or list(by_model.values())
    return max(pool, key=_ctx)


def main() -> None:
    apply = "--apply" in sys.argv
    update_ctx = "--update-context" in sys.argv
    hdr, rows = load_rows(CSV)

    if PROFILE_COL not in hdr:
        cands = [c for c in hdr if c.startswith("scrocco-llm-")]
        print(f"[ERR] colonna profilo '{PROFILE_COL}' assente nel CSV.")
        print(f"      Nel file ci sono: {cands}")
        print(f"      Esporta SCROCCO_PROFILE_COL={cands[0] if cands else '<colonna>'} e riprova.")
        sys.exit(2)

    cf_rows = [r for r in rows if (r.get("provider") or "").strip().lower() == PROVIDER]
    by_account: dict[str, dict[str, dict]] = {}
    accounts: dict[str, dict] = {}
    for r in cf_rows:
        key = (r.get(PROFILE_COL) or "").strip()
        if not key:
            continue
        accounts.setdefault(key, {"endpoint": (r.get("endpoint") or "").strip(),
                                  "commento": (r.get("commento") or "").strip()})
        by_account.setdefault(key, {}).setdefault((r.get("modello") or "").strip(), r)

    if not accounts:
        print("[ERR] nessun account Cloudflare nel CSV")
        sys.exit(1)

    print(f"[INFO] account CF: {len(accounts)} | modelli a catalogo: {len(CATALOG)}")
    print(f"[INFO] righe CF esistenti: {len(cf_rows)}"
          f" | aggiornamento contesti: {'SI (--update-context)' if update_ctx else 'no'}")

    added = updated = 0
    for key, acc in accounts.items():
        by_model = by_account.setdefault(key, {})
        for model, ctx_k, ctx_tok, caps in CATALOG:
            want = {"context": str(ctx_k), "max_input": str(ctx_tok // 2), "caps": caps}
            ex = by_model.get(model)
            if ex is not None:
                if not update_ctx:
                    continue
                diff = {f: ((ex.get(f) or "").strip(), v) for f, v in want.items()
                        if (ex.get(f) or "").strip() != v}
                if diff:
                    for f, (_old, new) in diff.items():
                        ex[f] = new
                    updated += 1
                    print(f"  ~ {model:46} {key[:6]}… " +
                          " ".join(f"{f}:{o}->{n}" for f, (o, n) in diff.items()))
                continue
            tpl = pick_template(by_model, caps)
            row = dict(tpl) if tpl else make_row(hdr)
            row.update(want)
            row["modello"] = model
            row["provider"] = PROVIDER
            row["endpoint"] = acc["endpoint"]
            row["commento"] = acc["commento"]
            row[PROFILE_COL] = key
            row["alias"] = ""          # mai inventare un alias
            if not tpl:
                row["data"] = "free"
                row["priority"] = "0"
            rows.append(row)
            by_model[model] = row
            added += 1
            print(f"  + {model:46} {key[:6]}… clonata da "
                  f"{tpl['modello'] if tpl else 'SCHEMA VUOTO'}")

    fuori = sorted({m for acc in by_account.values() for m in acc}
                   - {m for m, _, _, _ in CATALOG})
    if fuori:
        print(f"[INFO] {len(fuori)} righe CF fuori catalogo lasciate intatte: {fuori[:4]}")

    print(f"\n[ESITO] da aggiungere: {added} | da aggiornare: {updated}")

    if not apply:
        print("[DRY-RUN] nessuna modifica. Usa --apply per scrivere.")
        return
    if not added and not updated:
        print("[SKIP] nulla da fare: il catalogo e' gia' allineato.")
        return

    n0, i0 = _n_deployments(CSV)
    tmp = CSV + ".gate." + datetime.now().strftime("%Y%m%d%H%M%S")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=hdr)
        w.writeheader()
        w.writerows(rows)
    n1, i1 = _n_deployments(tmp)
    perse = i0 - i1
    print(f"[GATE] deployment {n0} -> {n1} | identita' perse: {len(perse)}")
    if n1 < n0 or perse:
        print("[GATE FALLITO] si perderebbe qualcosa: scrittura ANNULLATA.")
        for g, s in list(perse)[:5]:
            print("   -", dict(s).get("model"), "|", g[:34])
        os.unlink(tmp)
        sys.exit(3)
    os.unlink(tmp)

    bak = CSV + f".bak.cf-catalog.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(CSV, bak)
    save_rows(CSV, hdr, rows)
    print(f"[BACKUP] {bak}")
    print(f"[OK] CSV: {len(rows)} righe (+{added}, ~{updated})")


if __name__ == "__main__":
    main()
