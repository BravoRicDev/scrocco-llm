"""Aggiunge le righe Jev / TypeSafe System One (`cap decision`) al CSV.

Jev non e' un LLM generativo: risponde su `POST /v1/systemone` con body/risposta
nativi `{state, questions}` -> `{model, answers, usage}`. Va quindi instradato
con `api_style=jev` sulla capacita' `decision` (endpoint dedicato, NON
OpenAI-chat). OpenRouter espone lo stesso endpoint dedicato, quindi si usano le
chiavi OpenRouter GIA' presenti nel CSV (una riga per chiave, come per gli altri
provider).

  endpoint : https://openrouter.ai/api/v1/systemone
  modello  : $JEV_MODEL (default `typesafe/jev-1.13`; verificato 200 su OpenRouter)
             NB: `typesafe/jev-latest` NON esiste su OpenRouter (400); `typesafe/jev-router`
             e' un'altra cosa (router testuale, non il modello decisionale).
  caps     : decision          -> gruppi -decision / -decision-go / -decision-fallback
  data     : free              -> bucket primario (priority)
  context  : 32 (k)            -> posizionamento dims
  max_input: 32000
  context  : sempre testo; `state` puo' essere string|object|array

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
NON stampa chiavi. Idempotente su (provider, modello, endpoint).
"""
import csv
import io
import json
import os
import sys
import urllib.request

BASE = os.environ.get("NX_BASE", "http://127.0.0.1:4001")
KEY = os.environ["GATEWAY_MASTER_KEY"]
MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
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
if pcol is None or ecol is None or mcol is None:
    raise SystemExit("header CSV senza provider/endpoint/modello")

# colonna profilo locale (header "scrocco-llm-<profilo>")
prof_cols = [h for h in header if h.startswith("scrocco-llm-")]
if len(prof_cols) != 1:
    raise SystemExit("colonna profilo non univoca: %r" % prof_cols)
prof_col = prof_cols[0]

# chiavi OpenRouter distinte gia' presenti -> una riga Jev per chiave
seen: list[str] = []
for r in body:
    p = r[pcol].strip().lower() if pcol < len(r) else ""
    if p != PROVIDER:
        continue
    k = r[header.index(prof_col)].strip() if header.index(prof_col) < len(r) else ""
    if k and k not in seen:
        seen.append(k)
if not seen:
    raise SystemExit("nessuna chiave OpenRouter nel CSV: niente da fare")
print("chiavi OpenRouter distinte=%d, modello=%s" % (len(seen), MODEL))

existing = {(r[pcol].strip().lower(),
             r[mcol].strip(),
             r[ecol].strip().rstrip("/"))
            for r in body if ecol < len(r) and mcol < len(r) and pcol < len(r)}


def _row(key: str) -> list[str]:
    row = [""] * len(header)
    row[header.index("commento")] = "jev-systemone"
    row[mcol] = MODEL
    row[pcol] = PROVIDER
    row[ecol] = ENDPOINT
    row[header.index("data")] = "free"
    row[header.index("context")] = "32"
    row[header.index("max_input")] = "32000"
    row[header.index("priority")] = "0"
    row[header.index("intelligence_score")] = "8"
    row[header.index("model_preference")] = "50"
    row[header.index("caps")] = "decision"
    row[header.index("api_style")] = "jev"
    row[header.index("enabled")] = "true"
    row[header.index("hold_until_finish")] = "true"
    row[header.index(prof_col)] = key
    return row


added = []
for key in seen:
    if (PROVIDER, MODEL, ENDPOINT.rstrip("/")) in existing:
        continue
    added.append(_row(key))

if not added:
    print("righe Jev gia' presenti: nessuna modifica")
    raise SystemExit(0)

out = io.StringIO()
w = csv.writer(out, lineterminator="\n")
w.writerow(header)
for r in body:
    w.writerow(r)
for r in added:
    w.writerow(r)

print("righe da aggiungere=%d, totale=%d" % (len(added), len(body) + len(added)))
if not APPLY:
    print("dry-run: rilancia con --apply per scrivere (PUT /admin/csv)")
    raise SystemExit(0)
_req("/admin/csv", "PUT", {"raw": out.getvalue()})
print("PUT /admin/csv eseguito")
