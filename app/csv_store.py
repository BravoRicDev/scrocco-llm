"""Persistenza CSV: id stabili drow_*, scritture atomiche validate-live.

[IT] COSA: lettura/scrittura di keys_rotation.csv per l admin API (regola
AGENT.md: MAI toccare il file a mano). WHY/HOW:
  - row_id = md5(modello|endpoint|chiave)[:10]: ID STABILE che sopravvive
    ai reload -> PUT/DELETE idempotenti per gli agenti. Cambia se cambia
    la chiave (rotazione): documentato, ri-leggere dopo.
  - save_table: scrive su .tmp, VALIDA istanziando un GatewayConfig di
    controllo sul tmp, poi os.replace atomico -- un CSV rotto non arriva
    mai in produzione.
  - mask_key: nelle risposte admin le chiavi sono sempre mascherate.

[EN] WHAT: CSV persistence for the admin API. WHY: stable drow ids make
agent-driven CRUD idempotent; validate-before-swap means broken CSV never
ships; keys are always masked in responses.
"""

from __future__ import annotations

import csv
import hashlib
import os
import tempfile
from datetime import date
from pathlib import Path

from .config import (ENDPOINT_HEADERS, MODEL_HEADER, PROVIDER_HEADER,
                     DATA_HEADER, CONTEXT_HEADER, MAX_INPUT_HEADER,
                     PRIORITY_HEADER, CAPS_HEADER, GatewayConfig)

# campi gestiti dall'API (il resto delle colonne passa trasparente)
PAYLOAD_FIELDS = {
    "modello": MODEL_HEADER,
    "provider": PROVIDER_HEADER,
    "data": DATA_HEADER,
    "context": CONTEXT_HEADER,
    "max_input": MAX_INPUT_HEADER,
    "priority": PRIORITY_HEADER,
    "caps": CAPS_HEADER,
}

# token ammessi nella colonna caps (speculare a ROUTING_CAPS + text)
CAPS_TOKENS = frozenset({"text", "vision", "video", "audio", "image_gen",
                         "tts", "stt", "video_gen"})


def validate_caps(value) -> str:
    """Normalizza il valore caps (lista o stringa comma/virgola) in una
    stringa canonica 'a,b'. Token ignoti -> CsvStoreError."""
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        items = [x.strip().lower() for x in value.replace(",", " ").split()
                 if x.strip()]
    elif isinstance(value, (list, tuple, set)):
        items = [str(x).strip().lower() for x in value if str(x).strip()]
    else:
        raise CsvStoreError("'caps' deve essere una lista o stringa")
    bad = [t for t in items if t not in CAPS_TOKENS]
    if bad:
        raise CsvStoreError(
            f"caps non validi: {sorted(set(bad))}; ammessi: {sorted(CAPS_TOKENS)}")
    return ",".join(t for t in items if t)


class CsvStoreError(ValueError):
    """Errore di validazione/uso del CSV (-> HTTP 400)."""


# ------------------------------------------------------------------- lettura
def load_table(path: str | Path) -> tuple[list[str], list[dict]]:
    """Ritorna (header, righe-come-dict). Righe vuote scartate."""
    path = Path(path)
    with open(path, newline="", encoding="utf-8-sig") as f:
        raw = list(csv.reader(f))
    if not raw:
        raise CsvStoreError(f"CSV vuoto: {path}")
    header = [h.strip() for h in raw[0]]
    rows: list[dict] = []
    for line in raw[1:]:
        if not line or not any(c.strip() for c in line):
            continue
        rows.append({header[i]: (line[i] if i < len(line) else "")
                     for i in range(len(header))})
    return header, rows


def endpoint_of(header: list[str], row: dict) -> str:
    """Valore dell'endpoint gestendo le varianti note dell'intestazione."""
    for h in header:
        if h and h.strip().lower() in ENDPOINT_HEADERS:
            return (row.get(h) or "").strip()
    return ""


def row_id(row: dict, endpoint: str) -> str:
    """Id STABILE della riga: md5 di modello|endpoint|chiave.

    L'ID deve rimanere STABILE anche se vengono aggiunte colonne di metadati
    al CSV (es. 'note', 'scadenza'). Per questo si includono SOLO i VALORI
    delle colonne profilo (quelle che contengono la chiave API), NON i nomi
    delle colonne: aggiungendo una colonna metadata con valore vuoto non si
    altera l'ID.

    Le colonne note (modello, provider, data, context, max_input, priority,
    caps, endpoint) sono escluse. Tutto il resto con valore non-vuoto
    (tipicamente le colonne profilo scrocco-llm-<profilo>) contribuisce
    con il solo valore.
    """
    basis = "|".join(((row.get(MODEL_HEADER) or "").strip(),
                       (endpoint or "").strip(),
                       (row.get("chiave") or "").strip()))
    known = {MODEL_HEADER, PROVIDER_HEADER, DATA_HEADER, CONTEXT_HEADER,
             MAX_INPUT_HEADER, PRIORITY_HEADER, CAPS_HEADER} | ENDPOINT_HEADERS
    # Stabilita' su colonne: includi SOLO i valori (non i nomi), ordinati,
    # in modo che aggiungere una colonna metadata non cambi l'ID.
    extra_vals = sorted(v.strip() for v in row.values()
                        if v and v.strip()
                        and any(k not in known for k in row
                                if row.get(k) and row.get(k).strip() == v))
    # Più robusto: raccogli i valori di tutte le colonne non-note
    extra_vals = sorted(v.strip() for k, v in row.items()
                        if k not in known and v and v.strip())
    if extra_vals:
        basis += "|" + "|".join(extra_vals)
    digest = hashlib.md5(basis.encode()).hexdigest()[:10]
    return f"drow_{digest}"


def find_row(header: list[str], rows: list[dict],
             row_hash: str) -> tuple[int, dict] | tuple[None, None]:
    """Cerca una riga per id stabile. Ritorna (indice, riga) o (None, None)."""
    for i, r in enumerate(rows):
        if row_id(r, endpoint_of(header, r)) == row_hash:
            return i, r
    return None, None


# ------------------------------------------------------------------ scrittura
def _validate_like_live(tmp_path: Path, like: GatewayConfig) -> None:
    """Istanzia un GatewayConfig di controllo sul contenuto nuovo."""
    GatewayConfig(tmp_path,
                  proxy_prefix=like.proxy_prefix,
                  go_suffix=like.go_suffix,
                  fallback_suffix=like.fallback_suffix,
                  extra_prefixes=like.extra_prefixes)


def ensure_profile_column(header: list[str], profile: str,
                          prefix: str) -> list[str]:
    """Garantisce la colonna '<prefix><profilo>' (crea on-demand se manca).

    Ritorna l'header eventualmente esteso. NOTA: le nuove celle vanno
    riempite dal chiamante quando serializza (save_table usa r.get(h,"")).
    """
    col = prefix + profile
    if col not in header:
        header.append(col)
    return header


def ensure_caps_column(header: list[str]) -> list[str]:
    """Garantisce la colonna 'caps' (membership gruppi capacità).

    Da chiamare nei percorsi create/update/bulk PRIMA di apply_payload
    quando il payload contiene 'caps': senza header la serializzazione
    scarterebbe il valore (save_table itera l'header)."""
    if CAPS_HEADER not in header:
        header.append(CAPS_HEADER)
    return header


def apply_payload(row: dict, payload: dict, prefix: str,
                  current_profile: str | None = None) -> str:
    """Applica i campi del payload a una riga (merge parziale, in place).

    - 'profile': sposta l'assegnazione alla colonna del nuovo profilo
      (svuotando quella vecchia). La CREAZIONE dell'eventuale nuova colonna
      è a carico del chiamante (ensure_profile_column sull'header vero).
    - 'key': viene scritta nella COLONNA DEL PROFILO (semantica CSV).
    - campi ignoti -> CsvStoreError.

    Ritorna il profilo effettivo dopo l'operazione.
    """
    unknown = set(payload) - set(PAYLOAD_FIELDS) - {"profile", "key", "endpoint"}
    if unknown:
        raise CsvStoreError(f"campi non gestiti: {sorted(unknown)}")

    new_profile = payload.get("profile", current_profile)
    if not isinstance(new_profile, str) or not new_profile.strip():
        raise CsvStoreError("'profile' mancante o non valido")
    new_profile = new_profile.strip()
    new_col = prefix + new_profile

    if "key" in payload and (not isinstance(payload["key"], str)
                             or len(payload["key"].strip()) < 8):
        raise CsvStoreError("'key' deve essere una stringa di almeno 8 caratteri")
    for field_name in ("context", "max_input", "priority"):
        if field_name in payload and payload[field_name] is not None:
            try:
                int(payload[field_name])
            except (TypeError, ValueError):
                raise CsvStoreError(
                    f"'{field_name}' deve essere un intero") from None
    if "endpoint" in payload and not str(payload["endpoint"] or "").strip():
        raise CsvStoreError("'endpoint' non valido")
    if "modello" in payload and not str(payload["modello"] or "").strip():
        raise CsvStoreError("'modello' non valido")
    if "data" in payload and not str(payload["data"] or "").strip():
        raise CsvStoreError("'data' (categoria/rinnovo) non può essere vuota")

    # spostamento di profilo: svuota la vecchia colonna
    if current_profile is not None and new_profile != current_profile:
        old_col = prefix + current_profile
        if old_col in row:
            row[old_col] = ""

    # campi semplici
    for payload_key, header_name in PAYLOAD_FIELDS.items():
        if payload_key in payload:
            if payload_key == "caps":
                row[header_name] = validate_caps(payload["caps"])
                continue
            v = payload[payload_key]
            row[header_name] = "" if v is None else str(v)

    # chiave -> colonna del profilo (nuovo o corrente)
    if "key" in payload:
        row[new_col] = payload["key"].strip()
    elif new_col not in row:
        row[new_col] = ""

    # NOTA: 'endpoint' è scritto dal chiamante via write_endpoint()
    # (serve conoscere la variante di intestazione presente).

    return new_profile


def write_endpoint(row: dict, header: list[str], value: str) -> None:
    """Scrive l'endpoint nella colonna corretta gestendo le varianti note
    dell'intestazione (ENDPOINT_HEADERS: 'endpoint', 'endppoint', ...).

    Se il CSV non ha ancora nessuna colonna endpoint, la CREA usando il
    nome canonico 'endpoint': la nuova intestazione viene APPENDATA alla
    lista header IN PLACE, così il chiamante vede l'header esteso.
    """
    for h in header:
        if h and h.strip().lower() in ENDPOINT_HEADERS:
            row[h] = str(value or "").strip()
            return
    col = "endpoint"                    # nome canonico per CSV nuovi
    header.append(col)
    row[col] = str(value or "").strip()


def save_table(path: str | Path, header: list[str], rows: list[dict],
               like: GatewayConfig) -> None:
    """Scrittura ATOMICA con validazione preventiva sul file temporaneo."""
    path = Path(path)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp.csv")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in rows:
                w.writerow([r.get(h, "") for h in header])
        _validate_like_live(tmp_path, like)
        os.replace(tmp_path, path)          # atomico sullo stesso filesystem
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def mask_key(key: str) -> str:
    """Maschera una chiave per le risposte API: mai in chiaro."""
    key = (key or "").strip()
    if len(key) > 8:
        return f"{key[:4]}…{key[-2:]}"
    return "***"


# ------------------------------------------------------------------ report
def expiring(rows: list[dict], header: list[str], days: int,
             today: date | None = None) -> list[dict]:
    """Righe con rinnovo entro N giorni (colonna 'data': giorno del mese 1-31)."""
    from .config import parse_renewal
    today = today or date.today()
    out = []
    for r in rows:
        ren = parse_renewal(r.get(DATA_HEADER) or "", today)
        if ren["category"] == "future" and ren["sort_key"] <= days:
            out.append({
                "id": row_id(r, endpoint_of(header, r)),
                "modello": (r.get(MODEL_HEADER) or "").strip(),
                "in_days": ren["sort_key"],
                "data_raw": (r.get(DATA_HEADER) or "").strip(),
            })
    out.sort(key=lambda x: x["in_days"])
    return out
