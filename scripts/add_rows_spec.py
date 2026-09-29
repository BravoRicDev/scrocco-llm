#!/usr/bin/env python3
"""Aggiunge a QUESTO host le righe di deployment descritte da uno spec SENZA chiavi.

Perche' esiste (misurato il 2026-09-29):
  i CSV della flotta devono essere identici riga per riga a parte la colonna
  profilo, che contiene le API key ed e' per-host. `sync_csv_from.py` allinea
  l'intero CSV, ma mappa le chiavi PER INDICE DI RIGA: per le righe che la
  sorgente ha in piu' (i modelli nuovi) non esiste una riga locale, quindi
  lascerebbe le chiavi della SORGENTE. Verificato con un test isolato
  (gateway finto + CSV sintetici): le righe nuove uscivano con SRCKEY-3/4, cioe'
  le chiavi di cubotto finite nei CSV degli altri host. Questo script fa il
  pezzo mancante: prende lo spec (modello, provider, endpoint, campi di routing
  e `count`) e per ogni riga usa una chiave LOCALE dello stesso provider,
  ciclata. Nessuna chiave entra nella riga di comando, nel file o nel log.

Spec (JSON, nessun segreto):
  {
    "defaults": {"data": "free", "priority": "0", "enabled": "true", ...},
    "rows": [
      {"modello": "...", "provider": "google", "endpoint": "https://...",
       "context": "1000", "max_input": "1000000", "caps": "text,vision",
       "alias": "gemini-3-8-flash", "count": 9},
      ...
    ]
  }
  `count` = quante righe creare per quel modello (una per chiave locale).
  I campi assenti nella riga si prendono da `defaults`.

Uso (sull'host, FUORI dal container; dry-run di default):
    K=$(docker exec scrocco-llm printenv GATEWAY_MASTER_KEY)
    GATEWAY_MASTER_KEY=$K python3 scripts/add_rows_spec.py --spec <file.json>
    GATEWAY_MASTER_KEY=$K python3 scripts/add_rows_spec.py --spec <file.json> --apply

Nel primo passaggio il tempo impiega pochi secondi: il batch e' ATOMICO per
provider, cioe' se una op e' invalida non viene applicata nessuna di quel
provider (il gateway risponde 400 con il dettaglio).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("NX_BASE", "http://127.0.0.1:4001")
# Colonne del CSV che lo spec puo' impostare (la chiave NON e' configurabile:
# arriva dalle chiavi locali dell'host).
PAYLOAD_FIELDS = (
    "modello", "provider", "endpoint", "data", "context", "max_input", "priority",
    "caps", "effort_capable", "intelligence_score", "model_preference", "media_defer",
    "order", "enabled", "hold_until_finish", "api_style", "thinking_replay",
    "strip_reasoning", "content_string", "alias", "image_via",
)


def _api(path: str, method: str = "GET", body=None, key: str = "") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode() or "{}")
        except Exception:                                   # noqa: BLE001
            return {"ok": False, "error": {"message": "HTTP %s" % exc.code}}


def _tabelle(raw: str) -> tuple[list[str], list[list[str]]]:
    righe = [r for r in csv.reader(io.StringIO(raw)) if r and any(c.strip() for c in r)]
    return righe[0], righe[1:]


def profilo_locale(head: list[str]) -> str:
    """Nome della colonna profilo di questo host (es. scrocco-llm-lumon).

    Serve nel payload di create: il gateway scrive la chiave in QUESTA colonna,
    che e' per-host (`campi obbligatori mancanti: ['profile']` se assente).
    """
    for h in head:
        if h.startswith("scrocco-llm-"):
            return h
    raise SystemExit("nessuna colonna profilo nell'header locale")


def chiavi_locali(head: list[str], rows: list[list[str]]) -> tuple[dict[str, list[str]], str]:
    """(provider -> chiavi locali dedup in ordine, nome colonna profilo)."""
    prov_i = head.index("provider")
    prof = profilo_locale(head)
    prof_i = head.index(prof)
    out: dict[str, list[str]] = {}
    for r in rows:
        p = r[prov_i] if prov_i < len(r) else ""
        k = r[prof_i] if prof_i < len(r) else ""
        if p and k and k not in out.setdefault(p, []):
            out[p].append(k)
    return out, prof


def esistenti(head: list[str], rows: list[list[str]]) -> set[tuple[str, str, str]]:
    """(provider, modello, endpoint) gia' presenti: rende lo script idempotente."""
    pi, mi, ei = head.index("provider"), head.index("modello"), head.index("endpoint")
    return {
        ((r[pi] if pi < len(r) else ""), (r[mi] if mi < len(r) else ""), (r[ei] if ei < len(r) else ""))
        for r in rows
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="file JSON dello spec (senza chiavi)")
    ap.add_argument("--apply", action="store_true", help="scrive davvero (default: dry-run)")
    args = ap.parse_args()
    key = os.environ["GATEWAY_MASTER_KEY"]

    spec = json.load(open(args.spec, encoding="utf-8"))
    defaults = spec.get("defaults") or {}
    righe_spec = spec.get("rows") or []
    if not righe_spec:
        raise SystemExit("spec senza 'rows'")

    locale = _api("/admin/csv", key=key)
    head, rows = _tabelle(locale.get("raw") or "")
    chiavi, profilo = chiavi_locali(head, rows)
    print("host     : righe=%d colonne=%d profilo=%s" % (len(rows), len(head), profilo))
    chiavi = chiavi_locali(head, rows)[0]
    print("chiavi locali per provider: %s"
          % {p: len(v) for p, v in sorted(chiavi.items())})
    gia = esistenti(head, rows)

    piani: dict[str, list[dict]] = {}
    saltate_senza_chiave: list[tuple[str, str]] = []
    saltate_gia_presenti: list[tuple[str, str]] = []
    for spec_row in righe_spec:
        modello = str(spec_row.get("modello") or "").strip()
        provider = str(spec_row.get("provider") or "").strip()
        endpoint = str(spec_row.get("endpoint") or defaults.get("endpoint") or "").strip()
        n = int(spec_row.get("count") or 1)
        if not modello or not provider:
            raise SystemExit("riga di spec senza modello/provider: %r" % spec_row)
        if (provider, modello, endpoint) in gia:
            saltate_gia_presenti.append((provider, modello))
            continue
        pool = chiavi.get(provider) or []
        if not pool:
            saltate_senza_chiave.append((provider, modello))
            continue
        for i in range(n):
            op: dict[str, object] = {"action": "create"}
            for campo in PAYLOAD_FIELDS:
                if campo in spec_row:
                    op[campo] = spec_row[campo]
                elif campo in defaults:
                    op[campo] = defaults[campo]
            op.setdefault("modello", modello)
            op.setdefault("provider", provider)
            op.setdefault("endpoint", endpoint)
            op["profile"] = profilo             # colonna profilo di QUESTO host
            op["key"] = pool[i % len(pool)]          # chiave LOCALE, mai stampata
            piani.setdefault(provider, []).append(op)

    tot = sum(len(v) for v in piani.values())
    print("da creare: %d righe in %d provider %s"
          % (tot, len(piani), {p: len(v) for p, v in sorted(piani.items())}))
    if saltate_gia_presenti:
        print("gia' presenti, saltate: %d %s" % (len(saltate_gia_presenti),
                                                 sorted(set(saltate_gia_presenti))[:5]))
    if saltate_senza_chiave:
        print("SENZA CHIAVE LOCALE, saltate: %d %s"
              % (len(saltate_senza_chiave), sorted(set(saltate_senza_chiave))[:5]))

    if not args.apply:
        print("\nDRY-RUN: nessuna scrittura. Ripetere con --apply.")
        return
    if not tot:
        print("\nniente da applicare.")
        return

    esiti = []
    for provider, ops in sorted(piani.items()):
        res = _api("/admin/deployments/bulk", "POST", {"operations": ops}, key=key)
        ko = [r for r in (res.get("results") or []) if not r.get("ok")]
        esiti.append((provider, len(ops), res.get("ok"), res.get("applied"), len(ko)))
        print("  %-16s %3d ops -> ok=%s applied=%s errori=%d"
              % (provider, len(ops), res.get("ok"), res.get("applied"), len(ko)))
        for r in ko[:3]:
            print("      %s" % json.dumps(r)[:160])
    falliti = [e for e in esiti if not e[2]]
    print("\nRISULTATO: %d provider su %d applicati%s"
          % (len(esiti) - len(falliti), len(esiti),
             "" if not falliti else " — FALLITI: %s" % [e[0] for e in falliti]))


if __name__ == "__main__":
    main()
