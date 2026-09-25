"""Valorizza la colonna `image_via` del CSV (intervento #57).

`image_via` dichiara COME l'upstream espone i modelli immagine, cosi' il
gateway sa se adattare una chat su /images/* (o viceversa) senza sprecare
una chiamata e mettere un deployment in cooldown per un errore di schema.

Uso (su un host, fuori dal container, con la master key):
    K=$(docker exec scrocco-llm printenv GATEWAY_MASTER_KEY)
    GATEWAY_MASTER_KEY="$K" python3 scripts/set_image_via.py

Default misurati con un probe sugli upstream (una chiamata per direzione):
  - antigravity gemini-3.1-flash-image : images=400 "not supported on
    /v1/images", chat=OK   -> chat
  - codex-chatgpt gpt-image-2.5        : images=OK, chat=503 "only supported
    on /v1/images"        -> images
  - google gemini-*-image              : images=404, chat=endpoint esiste
                                        -> chat
  - openrouter google/gemini-*-image   : images=OK, chat=OK -> both
  - default (modelli ignoti)           -> both (il gateway adatta su errore)

NON stampa chiavi. GET /admin/csv -> modifica header+righe -> PUT /admin/csv.
"""
import csv
import io
import json
import os
import urllib.request

BASE = os.environ.get("NX_BASE", "http://127.0.0.1:4001")
KEY = os.environ["GATEWAY_MASTER_KEY"]
COL = "image_via"


def _req(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Authorization": "Bearer " + KEY,
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return resp.read().decode()


def decide(provider, model):
    p = (provider or "").strip().lower()
    if p == "antigravity" and "gemini" in model:
        return "chat"
    if p == "codex-chatgpt" and model.startswith("gpt-image"):
        return "images"
    if p == "google" and "image" in model:
        return "chat"
    return "both"


raw = json.loads(_req("/admin/csv")).get("raw") or ""
rows = [r for r in csv.reader(io.StringIO(raw))]
header, body = rows[0], rows[1:]
created = COL not in header
if created:
    ci = len(header)
    header.append(COL)
else:
    ci = header.index(COL)

prov = header.index("provider")
modl = header.index("modello")

changed = 0
for r in body:
    while len(r) <= ci:
        r.append("")
    want = decide(r[prov] if prov < len(r) else "",
                  r[modl] if modl < len(r) else "")
    if r[ci] != want:
        r[ci] = want
        changed += 1

out = io.StringIO()
w = csv.writer(out, lineterminator="\n")
w.writerow(header)
for r in body:
    w.writerow(r)

print("colonna image_via: %s, righe toccate=%d, totale=%d"
      % ("creata" if created else "gia presente", changed, len(body)))
if changed:
    _req("/admin/csv", "PUT", {"raw": out.getvalue()})
    print("PUT /admin/csv eseguito")
else:
    print("nessuna modifica necessaria")
