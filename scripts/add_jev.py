"""Aggiunge/corregge le righe Jev / TypeSafe System One (`cap decision`).

Jev non e' un LLM generativo: risponde su `POST /v1/systemone` con body/risposta
nativi `{state, questions}` -> `{model, answers, usage}`. Va quindi instradato
con `api_style=jev` sulla capacita' `decision` (endpoint dedicato, NON
OpenAI-chat). OpenRouter espone lo stesso endpoint dedicato, quindi si usano le
chiavi OpenRouter GIA' presenti nel CSV (una riga per chiave, come per gli altri
provider).

  endpoint : https://openrouter.ai/api/v1/systemone
  modelli  : $JEV_MODELS (default `typesafe/jev-1.13,~typesafe/jev-latest`)
  caps     : decision          -> gruppi -decision / -decision-go / -decision-fallback
  data     : fallback          -> Jev e' A PAGAMENTO (input $0.042/Mtok, output
                                  $0): per convenzione resta SEMPRE nel bucket
                                  `-decision-fallback`, mai come prima scelta.
                                  Lo script CORREGGE a `fallback` anche le righe
                                  Jev esistenti eventualmente rimaste `free`.
  context  : 32 (k)            -> posizionamento dims
  max_input: 32000

NB: `typesafe/jev-latest` senza `~` NON esiste (400). L'alias version-latest e'
`~typesafe/jev-latest` (col `~`). `typesafe/jev-router` e' un router testuale,
non il modello decisionale.

!!!! ORDINE OBBLIGATORIO !!!!
Va eseguito DOPO aver deployato il codice che conosce `decision` e `jev`. Sul
codice VECCHIO `parse_caps` scarterebbe il token `decision` (caps vuoto -> la
riga cadrebbe nel MONDO TESTO) e `normalize_style("jev")` degraderebbe a `chat`
(build_url -> `.../systemone/chat/completions`, 404). Quindi: deploy prima, poi
questo script.

Uso (su un host, fuori dal container, con la master key):
    K=$(docker exec scrocco-llm printenv GATEWAY_MASTER_KEY)
    GATEWAY_MASTER_KEY="$K" python3 scripts/add_jev.py            # dry-run
    GATEWAY_MASTER_KEY="$K" python3 scripts/add_jev.py --apply
NON stampa chiavi. Idempotente su (provider, modello, endpoint) + data=fallback.
"""
import csv
import io
import json
import os
import sys
import urllib.request

BASE = os.environ.get("NX_BASE", "http://127.0.0.1:4001")
KEY = os.environ["GATEWAY_MASTER_KEY"]
MODELS = [m.strip() for m in os.environ.get(
    "JEV_MODELS", "typesafe/jev-1.13,~typesafe/jev-latest").split(",") if m.strip()]
ENDPOINT = os.environ.get("JEV_ENDPOINT",
                          "https://openrouter.ai/api/v1/systemone")
PROVIDER = "openrouter"
APPLY = "--apply" in sys.argv


def _req(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Authorization": "Bearer " + KEY,
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.read().decode()


def _col(header, name):
    return header.index(name) if name in header else None


raw = json.loads(_req("/admin/csv")).get("raw") or ""
rows = list(csv.reader(io.StringIO(raw)))
header, body = rows[0], rows[1:]

pcol = _col(header, "provider")
ecol = _col(header, "endpoint")
mcol = _col(header, "modello")
dcol = _col(header, "data")
scol = _col(header, "api_style")
if None in (pcol, ecol, mcol, dcol, scol):
    raise SystemExit("header CSV senza provider/endpoint/modello/data/api_style")

# colonna profilo locale (header "scrocco-llm-<profilo>")
prof_cols = [h for h in header if h.startswith("scrocco-llm-")]
if len(prof_cols) != 1:
    raise SystemExit("colonna profilo non univoca: %r" % prof_cols)
prof_col = prof_cols[0]
kcol = header.index(prof_col)


def _get(r, i):
    return r[i].strip() if i < len(r) else ""


# 1) CORREGGI: ogni riga Jev gia' presente deve stare in `fallback` (pagamento).
fixed = 0
for r in body:
    if _get(r, scol).lower() == "jev" and _get(r, dcol).lower() != "fallback":
        while len(r) <= dcol:
            r.append("")
        r[dcol] = "fallback"
        fixed += 1

# 2) AGGIUNGI: una riga per (chiave, modello) mancante, sempre fallback.
openrouter_keys: list[str] = []
for r in body:
    if _get(r, pcol).lower() != PROVIDER:
        continue
    k = _get(r, kcol)
    if k and k not in openrouter_keys:
        openrouter_keys.append(k)
if not openrouter_keys:
    raise SystemExit("nessuna chiave OpenRouter nel CSV: niente da fare")

existing = {(_get(r, pcol).lower(), _get(r, mcol), _get(r, ecol).rstrip("/"),
             _get(r, kcol)) for r in body}

added = []
for model in MODELS:
    for k in openrouter_keys:
        if (PROVIDER, model, ENDPOINT.rstrip("/"), k) in existing:
            continue
        row = [""] * len(header)
        row[header.index("commento")] = "jev-systemone"
        row[mcol] = model
        row[pcol] = PROVIDER
        row[ecol] = ENDPOINT
        row[dcol] = "fallback"
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

print("modelli=%s chiavi OpenRouter=%d" % (MODELS, len(openrouter_keys)))
print("corrette a fallback=%d, da aggiungere=%d" % (fixed, len(added)))
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
