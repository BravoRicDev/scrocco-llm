"""Aggiunge/corregge le righe Jev / TypeSafe System One (`cap decision`).

Jev non e' un LLM generativo: risponde su `POST /v1/systemone` con body/risposta
nativi `{state, questions}` -> `{model, answers, usage}`. Va quindi instradato
con `api_style=jev` sulla capacita' `decision` (endpoint dedicato, NON
OpenAI-chat).

Provider Jev-compatibili verificati (endpoint `/v1/systemone` nativo):
  - **bynara**        modello `jev`            -> GRATIS (free tier 7M tok/g,
                       15 req/min). `data=free` -> gruppo `-decision` primario.
  - **opencode-zen**  modello `jev-1.13-free`  -> GRATIS (tier "within OpenCode",
                       a tempo limitato; richiede UA `opencode/*`, che il
                       gateway manda gia' per gli upstream opencode.ai).
                       `data=free` -> `-decision` primario.
  - **openrouter**    modelli `typesafe/jev-1.13` e `~typesafe/jev-latest`
                       -> A PAGAMENTO (input $0.042/Mtok, output $0).
                       `data=fallback` -> `-decision-fallback`.

NB modello: `typesafe/jev-latest` senza `~` NON esiste (400); l'alias e'
`~typesafe/jev-latest`. `typesafe/jev-router` e' un router testuale, non Jev.

Lo script e' idempotente: una riga per (provider, modello, endpoint, chiave),
e riallinea il bucket `data` (free/fallback) delle righe Jev esistenti.

!!!! ORDINE OBBLIGATORIO !!!!
Le righe vanno scritte DOPO aver deployato il codice che conosce `decision` e
`jev`. Sul codice vecchio `parse_caps` scarta `decision` (la riga cadrebbe nel
MONDO TESTO) e `normalize_style("jev")` degrada a `chat`.

Uso (su un host, fuori dal container, con la master key):
    K=$(docker exec scrocco-llm printenv GATEWAY_MASTER_KEY)
    GATEWAY_MASTER_KEY="$K" python3 scripts/add_jev.py            # dry-run
    GATEWAY_MASTER_KEY="$K" python3 scripts/add_jev.py --apply
NON stampa chiavi.
"""
import csv
import io
import json
import os
import sys
import urllib.request

BASE = os.environ.get("NX_BASE", "http://127.0.0.1:4001")
KEY = os.environ["GATEWAY_MASTER_KEY"]
APPLY = "--apply" in sys.argv

# (provider, endpoint nativo, bucket data, modelli)
TARGETS = [
    ("bynara", "https://router.bynara.id/v1/systemone", "free", ["jev"]),
    ("opencode-zen", "https://opencode.ai/zen/v1/systemone", "free",
     ["jev-1.13-free"]),
    ("openrouter", "https://openrouter.ai/api/v1/systemone", "fallback",
     ["typesafe/jev-1.13", "~typesafe/jev-latest"]),
]


def _req(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Authorization": "Bearer " + KEY,
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.read().decode()


raw = json.loads(_req("/admin/csv")).get("raw") or ""
rows = list(csv.reader(io.StringIO(raw)))
header, body = rows[0], rows[1:]
for need in ("provider", "endpoint", "modello", "data", "api_style", "caps"):
    if need not in header:
        raise SystemExit("header CSV senza colonna %r" % need)
prof_cols = [h for h in header if h.startswith("scrocco-llm-")]
if len(prof_cols) != 1:
    raise SystemExit("colonna profilo non univoca: %r" % prof_cols)
kcol = header.index(prof_cols[0])
pcol, ecol, mcol = header.index("provider"), header.index("endpoint"), header.index("modello")
dcol, scol = header.index("data"), header.index("api_style")


def _get(r, i):
    return r[i].strip() if i < len(r) else ""


# bucket atteso per provider (per la normalizzazione delle righe esistenti)
_bucket = {p: b for p, _e, b, _m in TARGETS}
fixed = 0
for r in body:
    if _get(r, scol).lower() != "jev":
        continue
    want = _bucket.get(_get(r, pcol).lower())
    if want and _get(r, dcol).lower() != want:
        while len(r) <= dcol:
            r.append("")
        r[dcol] = want
        fixed += 1

existing = {(_get(r, pcol).lower(), _get(r, mcol), _get(r, ecol).rstrip("/"),
             _get(r, kcol)) for r in body}

added = []
for provider, endpoint, bucket, models in TARGETS:
    keys = []
    for r in body:
        if _get(r, pcol).lower() != provider:
            continue
        k = _get(r, kcol)
        if k and k not in keys:
            keys.append(k)
    if not keys:
        print("[skip] %s: nessuna chiave nel CSV" % provider)
        continue
    for model in models:
        for k in keys:
            if (provider, model, endpoint.rstrip("/"), k) in existing:
                continue
            row = [""] * len(header)
            row[header.index("commento")] = "jev-systemone"
            row[mcol] = model
            row[pcol] = provider
            row[ecol] = endpoint
            row[dcol] = bucket
            row[header.index("context")] = "32"
            row[header.index("max_input")] = "32000"
            row[header.index("priority")] = "0"
            row[header.index("intelligence_score")] = "8"
            row[header.index("model_preference")] = "50"
            row[header.index("caps")] = "decision"
            row[scol] = "jev"
            row[header.index("enabled")] = "true"
            row[header.index("hold_until_finish")] = "true"
            row[kcol] = k
            added.append(row)
    print("[%s] chiavi=%d modelli=%s bucket=%s"
          % (provider, len(keys), models, bucket))

print("corrette a bucket=%d, da aggiungere=%d" % (fixed, len(added)))
if fixed == 0 and not added:
    print("nessuna modifica necessaria")
    raise SystemExit(0)

out = io.StringIO()
w = csv.writer(out, lineterminator="\n")
w.writerow(header)
for r in body:
    w.writerow(r)
for r in added:
    w.writerow(r)

if not APPLY:
    print("dry-run: rilancia con --apply per scrivere (PUT /admin/csv)")
    raise SystemExit(0)
_req("/admin/csv", "PUT", {"raw": out.getvalue()})
print("PUT /admin/csv eseguito")
