"""Allinea il CSV di questo host a quello di un host sorgente, sostituendo
SOLO la colonna profilo.

Regola: i CSV della flotta devono essere identici riga per riga e colonna per
colonna, a parte il nome della colonna profilo (che e' diverso per host e
finisce nei nomi modello esposti da /v1/models: `scrocco-llm-fissone` su
cubotto, `scrocco-llm-lumon` su lumon, ecc.).

Il profilo viene ri-mappato in TRE passaggi, perche' il CSV puo' avere piu'
colonne profilo storiche:
  1. riga per riga   -> la colonna profilo di SOURCE prende il valore
                        corrispondente di DEST;
  2. intestazione    -> l'header di SOURCE viene adottato cosi' com'e'
     (aggiunge le colonne mancanti, es. `alias`, e riordino il resto);
  3. rimozione       -> le colonne profilo residue di DEST (che non esistono
                        piu' nel nuovo header) vengono eliminate, cosi' non
     restano colonne profilo "fantasma".

Sicurezza:
  - NESSUN segreto dentro questo file: ne' chiavi API ne' master key ne' IP.
    La chiave del gateway si legge dall'ambiente (`GATEWAY_MASTER_KEY`, come
    in `set_image_via.py`).
  - le chiavi API dei deployment arrivano via STDIN (o via `ssh <host> cat`,
    se gli host si vedono): stanno solo in memoria e nel PUT locale, mai in
    un argomento della riga di comando ne' in un file.
  - il gateway fa gia' il backup rotato a ogni PUT; in piu' qui copiamo il
    CSV locale in /tmp prima di scrivere.
  - dry-run di default: stampa il diff e non scrive nulla senza --apply.

Uso (su un host di destinazione, fuori dal container):
    K=$(docker exec scrocco-llm printenv GATEWAY_MASTER_KEY)
    # dry-run, il CSV sorgente arriva su stdin (mai come argomento)
    ssh cubotto 'cat ~/scrocco-llm/var/keys_rotation.csv' \
      | GATEWAY_MASTER_KEY="$K" python3 scripts/sync_csv_from.py
    # applica
    ... | GATEWAY_MASTER_KEY="$K" python3 scripts/sync_csv_from.py --apply
"""
import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request

BASE = os.environ.get("NX_BASE", "http://127.0.0.1:4001")
CSV_REL = "var/keys_rotation.csv"


def _local_csv() -> str:
    """Path del CSV di questo host (lo working dir del compose)."""
    return os.path.join(os.environ.get("SYNC_CSV", ""), CSV_REL) \
        if os.environ.get("SYNC_CSV") else CSV_REL


def _read_source(args):
    """CSV sorgente: da stdin (default, mai come argomento) o via ssh."""
    if not args.ssh_host:
        data = sys.stdin.buffer.read()
        label = "stdin"
    else:
        rel = CSV_REL
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
               "-o", "StrictHostKeyChecking=accept-new", args.ssh_host,
               "cat ~/%s" % rel]
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            raise SystemExit("ssh non riuscito: %s" % r.stderr.decode()[:200])
        data, label = r.stdout, args.ssh_host
    if not data.strip():
        raise SystemExit("CSV sorgente vuoto")
    return data.decode(), label


def _api(path, method="GET", body=None, key=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Authorization": "Bearer " + key,
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=300) as resp:
        return resp.read().decode()


def _tables(raw):
    rows = list(csv.reader(io.StringIO(raw)))
    rows = [r for r in rows if r and any(c.strip() for c in r)]
    return rows[0], rows[1:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default="sorgente",
                    help="etichetta host sorgente (solo per il log)")
    ap.add_argument("--ssh-host", default=os.environ.get("SYNC_SSH_HOST", ""),
                    help="leggi il CSV da qui con ssh; vuoto = da stdin")
    ap.add_argument("--apply", action="store_true",
                    help="applica davvero (default: solo dry-run)")
    args = ap.parse_args()
    key = os.environ["GATEWAY_MASTER_KEY"]

    # --- profilo di DEST (dal CSV locale) ---
    local_raw = json.loads(_api("/admin/csv", key=key))["raw"]
    lhead, lrows = _tables(local_raw)
    lprof = [h for h in lhead if h.startswith("scrocco-llm-")]
    if len(lprof) != 1:
        raise SystemExit("attesa 1 colonna profilo, trovate: %r" % lprof)
    lprof = lprof[0]

    # --- CSV sorgente (chiavi solo in memoria, mai in un argomento) ---
    src_raw, label = _read_source(args)
    shead, srows = _tables(src_raw)
    sprof = [h for h in shead if h.startswith("scrocco-llm-")]
    if len(sprof) != 1:
        raise SystemExit("sorgente: attesa 1 colonna profilo, trovate: %r"
                         % sprof)
    sprof = sprof[0]

    print("sorgente : %s (%s) righe=%d colonne=%d profilo=%s"
          % (args.src, label, len(srows), len(shead), sprof))
    print("dest     : righe=%d colonne=%d profilo=%s"
          % (len(lrows), len(lhead), lprof))

    si, li = shead.index(sprof), lhead.index(lprof)
    ncols = max(len(r) for r in srows)
    moved = 0
    for s in srows:
        while len(s) < ncols:
            s.append("")
        d = lrows[moved] if moved < len(lrows) else None
        if d is not None:
            while len(d) < ncols:
                d.append("")
            s[si] = d[li]          # 1. valore della chiave -> profilo locale
            moved += 1
    out_head, out_rows = shead, srows      # 2. header della sorgente

    # diff riassuntivo (identita' logica: profilo escluso, provider/modello/
    # endpoint/caps confrontati -> dice SE le righe sono le stesse)
    def ident(rows, prof_i, head):
        cols = ["provider", "modello", "endpoint", "caps"]
        idx = [head.index(c) for c in cols]
        return {"|".join([r[prof_i] if prof_i < len(r) else ""]
                         + [r[i] if i < len(r) else "" for i in idx])
                for r in rows}

    lset = ident(lrows, li, lhead)
    sset = ident(out_rows, si, out_head)
    added = sorted(set(out_head) - set(lhead))
    removed = sorted(set(lhead) - set(out_head))
    print("righe    : %d -> %d" % (len(lrows), len(out_rows)))
    print("colonne  : %d -> %d  aggiunte: %s / rimosse: %s"
          % (len(lhead), len(out_head), added or "-", removed or "-"))
    print("identita : %d comuni, %d solo-locali, %d nuove-dalla-sorgente"
          % (len(lset & sset), len(lset - sset), len(sset - lset)))

    if not args.apply:
        print("\nDRY-RUN: nessuna scrittura. Ripetere con --apply.")
        return

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(out_head)
    for r in out_rows:
        w.writerow(r)
    body = out.getvalue()
    # 3. verifica che il risultato sia valido per il parser del gateway
    check = list(csv.reader(io.StringIO(body)))
    if not check or len(check[0]) != len(check[1]):
        raise SystemExit("CSV risultato non valido: header/righe disallineati")
    shutil.copy(_local_csv(), "/tmp/keys_rotation.pre-sync.bak.csv")
    res = json.loads(_api("/admin/csv", "PUT", {"raw": body}, key=key))
    print("PUT /admin/csv -> %s (backup: %s)" % (res.get("ok"), res.get("backup")))


if __name__ == "__main__":
    main()
