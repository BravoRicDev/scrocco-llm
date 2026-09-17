"""Caricamento CSV credenziali -> gruppi deployment (hot-reload atomico).

[IT] COSA: trasforma var/keys_rotation.csv (riga = modello x chiave) in
strutture di routing. HOW: parsing colonne -> _classify (bucket dalla
colonna data: free/priority/paid/fallback/giorno-rinnovo) ->
_build_profile partiziona le righe. WHY le regole:
  - ANTI-CONTAMINAZIONE: una riga con token *_gen entra SOLO nei gruppi di
    generazione (mai nei dims ne nelle catene ingest): i generatori non
    ricevono traffico chat, e viceversa.
  - GRUPPI DIMS (-Nk) + CAPS (-vision/-tts/-go/-fallback): terna identica
    al testo, cosi fallback/cooldown sono UNA meccanica sola.
  - HOT-RELOAD ATOMICO: reload() salva lo stato, rilegge; se fallisce
    ripristina -- zero downtime su CSV scritti male.
  - Fresh install SENZA file: boot con 0 deployment (guida /bootstrap);
    niente crash del container.

[EN] WHAT: turns the credential CSV into routing structures. WHY:
generation tokens are quarantined into gen-only groups; every capability
mirrors the text bucket trio; reload is atomic; missing CSV boots empty.
"""

from __future__ import annotations

import calendar
import csv
import logging
import random
import re
import threading
from datetime import date
from pathlib import Path
from typing import Any
from .capabilities import ROUTING_CAPS, GEN_CAPS, canonical_family
from .protocols import normalize_style

log = logging.getLogger("nx.config")

# Lock che serializza reload() ovunque venga chiamato (watcher async task,
# PUT /admin/csv in threadpool, test): due reload sovrapposti non applicano
# mai stati misti. threading.Lock (non asyncio) perche' reload() e' sincrono e
# puo' girare in thread diversi; la sezione critica e' breve.
_RELOAD_LOCK = threading.Lock()

ENDPOINT_HEADERS = {"endpoint", "endppoint", "end point", "endpoint_url"}
MODEL_HEADER = "modello"
PROVIDER_HEADER = "provider"
DATA_HEADER = "data"
CONTEXT_HEADER = "context"
MAX_INPUT_HEADER = "max_input"
# Tetto della dimensione di contesto: un `context` >1000 nel CSV (es. 1049,
# nato per errore) viene NORMALIZZATO a 1000k in fase di parsing:
# nessun gruppo -Nk sopra 1000k esiste, e max_input non supera mai 1.000.000.
MAX_CONTEXT_DIM_K = 1000
MAX_CONTEXT_INPUT = MAX_CONTEXT_DIM_K * 1000
PRIORITY_HEADER = "priority"
CAPS_HEADER = "caps"
EFFORT_CAPABLE_HEADER = "effort_capable"
INTELLIGENCE_HEADER = "intelligence_score"
TOOL_REPAIR_HEADER = "tool_repair"
MODEL_PREFERENCE_HEADER = "model_preference"
# media_defer=false: il deployment (anche multimodale) NON viene rimandato dal
# multimodal_last_resort nelle richieste di testo puro: resta eleggibile nelle
# chat senza dover svuotare la colonna `caps` (capacità reali intatte).
MEDIA_DEFER_HEADER = "media_defer"
# order: chiave di ordinamento esplicita per deployment. Valore PIU' BASSO =
# prima; deployment con lo stesso valore formano un "tier" (aggregabile anche
# tra provider diversi). Vuoto/assente = ORDER_LAST (neutro, in coda).
ORDER_HEADER = "order"
ORDER_LAST = 1_000_000_000
# enabled: disabilitazione DICHIARATIVA di un deployment dal CSV. Default true
# (vuoto/assente = attivo); false/0/no/off -> la riga RESTA nel CSV (chiave e
# coppia provider/chiave conservate) ma e' esclusa da tutti i bucket di routing.
ENABLED_HEADER = "enabled"
# hold_until_finish: attesa di una chiusura PULITA dello stream prima di
# consegnare al client (niente risposte a metà). Opt-in per deployment (CSV),
# default false; true/1/yes/on = attivo.
HOLD_UNTIL_HEADER = "hold_until_finish"
# thinking_replay: il provider esige che i turni assistant con tool_calls
# riportino il campo `reasoning_content` (modalita' thinking). true/1/yes/on =
# attivo: prima di OGNI invio il gateway rimette il reasoning (quello VERO se
# disponibile, altrimenti un segnaposto) cosi' il primo tentativo e' gia'
# corretto. Viene scritta in automatico quando un deployment lo "impara".
THINKING_REPLAY_HEADER = "thinking_replay"
# strip_reasoning: il provider RIFIUTA i campi reasoning nella history
# ("property 'reasoning_content' is unsupported"). true/1/yes/on = attivo:
# prima di ogni invio il gateway TOGLIE `reasoning_content`/`reasoning` dai
# turni assistant (il contenuto resta). Imparata in automatico quando un
# deployment lo scopre da un 400 del provider.
STRIP_REASONING_HEADER = "strip_reasoning"
# no_thinking: il provider non accetta il thinking su history costruite dal
# gateway (blocchi thinking incoerenti con `content`+`tool_calls`). true =
# per questo deployment NON si iniettano `reasoning_effort`/`thinking`.
# Imparata in automatico quando il downgrade risolve un 400 del provider.
NO_THINKING_HEADER = "no_thinking"
# content_string: il provider ha schema JSON stretto e pretende
# `messages[].content` come STRINGA (rifiuta gli array di blocchi OpenAI:
# "'array' not in 'string'") e la proprieta' `content` sempre presente (es.
# assistant con soli tool_calls). true/1/yes/on = attivo: prima di ogni invio
# il gateway appiattisce gli array di SOLO testo a stringa (media-safe: i
# blocchi con immagini/audio restano intatti) e aggiunge `content:""` dove
# manca. Imparata in automatico quando un deployment lo scopre da un 400.
CONTENT_STRING_HEADER = "content_string"
# api_style: protocollo nativo dell'upstream per questo deployment. Default
# "chat" (OpenAI Chat Completions). Altri valori gestiti da app/protocols.py:
# "responses" (OpenAI Responses /res/v1), "messages" (Anthropic), "google"
# (Gemini generateContent). scrocco-llm traduce da/verso Chat Completions.
API_STYLE_HEADER = "api_style"

# ordine di specificità per il dispatcher base: i GENERATORI prima degli
# ingest, così una richiesta i2i/i2v (input+output) cade nel gruppo _gen
CAP_PRIORITY_ORDER = ("image_gen", "video_gen", "tts", "stt",
                      "video", "audio", "vision")


def parse_caps(raw: str | None) -> frozenset[str]:
    """Parsing della colonna caps: token separati da virgola/spazi.

    Token ammessi: "text" + ROUTING_CAPS. Ignoti -> warning e scarto
    (il CSV resta caricabile anche con refusi).
    """
    out: set[str] = set()
    for tok in re.split(r"[,\s]+", (raw or "").strip().lower()):
        tok = tok.strip()
        if not tok:
            continue
        if tok == "text" or tok in ROUTING_CAPS:
            out.add(tok)
        else:
            log.warning("[config] token caps ignoto %r: ignorato", tok)
    return frozenset(out)


def _naming_defaults() -> tuple[str, str, str]:
    """Default di naming dalla policy (import lazy: evita cicli)."""
    from .policy import Policy
    p = Policy.default()
    return p.proxy_prefix, p.go_suffix, p.fallback_suffix


def slugify_model(name: str) -> str:
    """Slug sicuro per i nomi univoci (es. nvidia/nemotron:free -> nvidia-nemotron-free)."""
    return re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip()).strip("-").lower()


def infer_model_prefix(model_name: str, endpoint: str) -> tuple[str, bool]:
    """Modello upstream + flag provider esplicito.

    Il forwarder chiama gli upstream DIRETTAMENTE via httpx (niente
    litellm): un prefisso "mistral/" o "cloudflare/" aggiunto qui finirebbe
    NEL BODY upstream e genererebbe 400 "No such model" su tutti i
    deployment mistral e cloudflare del CSV. Il modello viaggia SEMPRE
    com'è nel CSV; openrouter/vendor-prefixed inclusi (il loro namespace
    "vendor/model" è atteso dall'upstream).
      - nvidia NIM: needs_openai=True (riservato, nessuna trasformazione)
    """
    model = (model_name or "").strip()
    ep = (endpoint or "").lower()
    if not model:
        return model, False
    if "integrate.api.nvidia.com" in ep:
        return model, True
    return model, False


def parse_renewal(raw: str, today: date) -> dict[str, Any]:
    """Parsing della colonna data (rinnovo/priorità/fallback/free).

    Valori ammessi nella colonna 'data':
      - "priority"/"free"        -> bucket priority/free
      - "fallback"/"paid"        -> bucket fallback
      - giorno del mese (1..31)  -> rinnovo MENSILE ricorrente: sort_key =
        giorni mancanti al prossimo rinnovo. Serve SOLO a ordinare i gruppi
        "-go"/"-fallback" mettendo PRIMA il deployment che si rinnova prima.
    """
    s = (raw or "").strip().lower()
    if not s:
        return {"category": None, "sort_key": float("inf")}
    if s in ("priority", "free"):
        return {"category": "priority", "sort_key": 0}
    if s in ("fallback", "paid"):
        return {"category": "fallback", "sort_key": 0}
    if re.fullmatch(r"\d{1,2}", s):
        day = int(s)
        if not 1 <= day <= 31:
            return {"category": None, "sort_key": float("inf")}
        if day >= today.day:
            days = day - today.day          # rinnovo in questo mese (0 = oggi)
        else:
            month_days = calendar.monthrange(today.year, today.month)[1]
            days = (month_days - today.day) + day   # rinnovo nel prossimo mese
        return {"category": "future", "sort_key": days}
    return {"category": None, "sort_key": float("inf")}


def _classify(row: dict[str, str], today: date) -> dict[str, Any]:
    """Classificazione di una riga del CSV in metadati deployment.

    La categoria determina il bucket di routing (free/priority/go/fallback/zen).
    Le regole sono applicati nell'ordine seguente:
      1. La colonna 'data' definisce la categoria base (priority/free/fallback/paid).
      2. Se la categoria e' "future" (giorno rinnovo mese), si usa il provider
         per determinare se e' "zen" (solo provider esplicito opencode-zen) o "go".
      3. Altrimenti, la categoria e' quella definita da 'data', oppure 'free'
         se il modello contiene "free", altrimenti 'go'.
    I token 'zen' nei provider nomi sono riconosciuti esplicitamente;
    non sono presenti heuristiche "opencode-zen" obscure ne' endpoint
    speciali (es. NVIDIA NIM NON e' "zen": e' un provider normale).
    """
    modello = (row.get(MODEL_HEADER) or "").strip()
    provider = (row.get(PROVIDER_HEADER) or "").strip().lower()
    endpoint = ""
    for h, v in row.items():
        if h and h.strip().lower() in ENDPOINT_HEADERS:
            endpoint = (v or "").strip()
            break
    ren = parse_renewal(row.get(DATA_HEADER) or "", today)

    category = ren["category"]
    # Se la renewal ha dato "future", risolviamo in base al provider.
    # "zen" e' SOLO il provider esplicito opencode-zen (niente endpoint
    # speciali): la detection e' conservativa, basata unicamente sulla
    # colonna provider. Un normale provider (es. NVIDIA NIM) va nel bucket
    # "go" come tutti gli altri.
    if category == "future":
        if "zen" in provider:
            category = "zen"
        else:
            category = "go"
    # Se la renewal e' priority o fallback, mantieni quella categoria.
    # Altrimenti (free o None), determina in base a provider/modello.
    if category not in ("priority", "fallback"):
        # Provider esplicito zen
        if "zen" in provider:
            category = "zen"
        # Modello contiene "free" -> bucket free
        elif "free" in modello:
            category = "free"
        # Altrimenti default a go
        else:
            category = "go"

    ctx_k = None
    raw_ctx = (row.get(CONTEXT_HEADER) or "").strip()
    if raw_ctx:
        try:
            ctx_k = int(float(raw_ctx))
        except ValueError:
            ctx_k = None
    if ctx_k is not None:
        # Normalizzazione voluta (regola dell'utente): >1000 diventa 1000k.
        # Il clamp avviene QUI, unico choke point: nomi dei gruppi, ladder,
        # regex -Nk e confronti d*1000 derivano tutti da context_k.
        ctx_k = min(ctx_k, MAX_CONTEXT_DIM_K)

    raw_max = (row.get(MAX_INPUT_HEADER) or "").strip()
    max_from_csv = 0
    if raw_max:
        try:
            max_from_csv = int(float(raw_max))
        except ValueError:
            max_from_csv = 0
    max_input = max_from_csv if max_from_csv > 0 else ((ctx_k or 0) * 1000)
    if max_input > MAX_CONTEXT_INPUT:
        max_input = MAX_CONTEXT_INPUT

    try:
        priority = int(float((row.get(PRIORITY_HEADER) or "").strip()))
    except ValueError:
        priority = 0

    # effort_capable: il modello accetta `reasoning_effort` upstream.
    raw_eff = (row.get(EFFORT_CAPABLE_HEADER) or "").strip().lower()
    effort_capable = raw_eff in ("1", "true", "yes", "si", "sì", "y", "t")

    # intelligence_score 1-10 (default 5 neutro).
    try:
        intelligence = int(float((row.get(INTELLIGENCE_HEADER) or "").strip()))
    except ValueError:
        intelligence = 5
    intelligence = max(1, min(10, intelligence))

    # model_preference: preferenza utente (intero, default 0 neutro).
    # 0 = neutro, >0 = mi piace, <0 = non mi piace.
    # Formula: score -= preference * abs(score) / 100
    try:
        model_preference = int((row.get(MODEL_PREFERENCE_HEADER) or "0").strip())
    except ValueError:
        model_preference = 0

    # tool_repair: livello di riparazione tool-call (vuoto=aggressive, safe, off).
    raw_tr = (row.get(TOOL_REPAIR_HEADER) or "").strip().lower()
    if raw_tr not in ("", "off", "safe", "aggressive"):
        raw_tr = ""

    # media_defer: partecipa al MEDIA DEFER per le richieste testo?
    # default/vuoto = true (comportamento storico); false = esente.
    raw_md = (row.get(MEDIA_DEFER_HEADER) or "").strip().lower()
    media_defer = raw_md not in ("0", "false", "no", "n", "off")

    # hold_until_finish: attendere la chiusura pulita dello stream prima di
    # consegnare (nessuna risposta a metà). Opt-in: default false.
    raw_hu = (row.get(HOLD_UNTIL_HEADER) or "").strip().lower()
    hold_until_finish = raw_hu in ("1", "true", "yes", "on")

    # thinking_replay: il provider esige il replay del reasoning_content nei
    # turni assistant con tool_calls (modalita' thinking). Opt-in: default off.
    raw_tp = (row.get(THINKING_REPLAY_HEADER) or "").strip().lower()
    thinking_replay = raw_tp in ("1", "true", "yes", "on")

    # strip_reasoning / no_thinking: rimedi appresi sulla history thinking
    # (provider che RIFIUTA i campi reasoning / non accetta il thinking).
    strip_reasoning = (row.get(STRIP_REASONING_HEADER) or "").strip().lower() \
        in ("1", "true", "yes", "on")
    no_thinking = (row.get(NO_THINKING_HEADER) or "").strip().lower() \
        in ("1", "true", "yes", "on")
    # content_string: schema stretto (content array -> string), appresa.
    content_string = (row.get(CONTENT_STRING_HEADER) or "").strip().lower() \
        in ("1", "true", "yes", "on")

    # api_style: protocollo nativo upstream (chat/responses/messages/google).
    api_style = normalize_style(row.get(API_STYLE_HEADER))

    # order: chiave di ordinamento esplicita (tier). Piu' basso = prima;
    # vuoto/assente/non numerico = ORDER_LAST (neutro, in coda).
    raw_order = (row.get(ORDER_HEADER) or "").strip()
    if raw_order:
        try:
            order = int(float(raw_order))
        except ValueError:
            order = ORDER_LAST
    else:
        order = ORDER_LAST

    # enabled: disabilitazione dichiarativa (default true). La riga disabilitata
    # resta nel CSV ma viene saltata in _load (nessun bucket la vede).
    raw_en = (row.get(ENABLED_HEADER) or "").strip().lower()
    enabled = raw_en not in ("0", "false", "no", "n", "off")

    return {
        "modello": modello,
        "provider": provider,
        "endpoint": endpoint,
        "data_raw": row.get(DATA_HEADER) or "",
        "category": category,
        "sort_key": ren["sort_key"],
        "context_k": ctx_k,
        "max_input": max_input,
        "priority": priority,
        "caps": parse_caps(row.get(CAPS_HEADER)),
        "effort_capable": effort_capable,
        "intelligence": intelligence,
        "tool_repair": raw_tr,
        "model_preference": model_preference,
        "media_defer": media_defer,
        "order": order,
        "enabled": enabled,
        # limite FISSO di connessioni concorrenti (CSV, opzionale): vince
        # sempre sul limite dinamico appreso dal router.
        "concurrent_limit": _int_or_none(row.get("concurrent_limit")),
        "hold_until_finish": hold_until_finish,
        "thinking_replay": thinking_replay,
        "strip_reasoning": strip_reasoning,
        "no_thinking": no_thinking,
        "content_string": content_string,
        "api_style": api_style,
    }


def _shuffle_bucket(deps: list[dict]) -> list[dict]:
    """Replica _shuffle_bucket(): shuffle dentro ogni modello, modelli ordinati
    per priority massima decrescente, deployment dello stesso modello consecutivi."""
    by_model: dict[str, list[dict]] = {}
    for d in deps:
        by_model.setdefault(d["meta"]["modello"], []).append(d)
    model_groups = []
    for _model, grp in by_model.items():
        random.shuffle(grp)
        model_groups.append((max(g["meta"]["priority"] for g in grp), grp))
    model_groups.sort(key=lambda x: -x[0])
    out: list[dict] = []
    for _, grp in model_groups:
        out.extend(grp)
    return out


class ConfigValidationError(ValueError):
    """Il CSV non ha superato il lint: lo stato precedente resta intatto."""


# colonne che DEVONO essere numeriche quando valorizzate (lint di fase 1)
_NUMERIC_COLUMNS = ("priority", "max_input", "intelligence_score",
                    "order", "model_preference")


def _is_number(v: str) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _int_or_none(v: object) -> int | None:
    """Parsa un intero positivo; None se assente o non numerico."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or not _is_number(s):
        return None
    try:
        return max(1, int(float(s)))
    except (TypeError, ValueError):
        return None


def validate_csv(path: str | Path) -> list[str]:
    """Lint del CSV PRIMA dello swap. Ritorna i problemi con riga/colonna.

    Non solleva mai: un CSV assente/vuoto e' gestito dal loader (fresh
    install), quindi qui non e' un errore.
    """
    issues: list[str] = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = list(csv.reader(f))
    except (FileNotFoundError, OSError):
        return issues
    start = 0
    while start < len(reader):
        first = reader[start]
        if not first or not any(c.strip() for c in first) \
                or first[0].lstrip().startswith("#"):
            start += 1
        else:
            break
    if start >= len(reader):
        return issues
    header = [h.strip() for h in reader[start]]
    cols = {name: i for i, name in enumerate(header)}
    numeric = [(name, cols[name]) for name in _NUMERIC_COLUMNS if name in cols]
    for i, row in enumerate(reader[start + 1:], start=start + 2):
        if not row or not any(c.strip() for c in row):
            continue
        for name, idx in numeric:
            val = (row[idx] if idx < len(row) else "").strip()
            if val and not _is_number(val):
                issues.append(
                    f"riga {i}: colonna '{name}' non numerica: {val!r}")
        if len(row) > len(header):
            issues.append(f"riga {i}: {len(row)} colonne (attese "
                          f"{len(header)})")
    return issues


def self_check(cfg: "GatewayConfig") -> list[str]:
    """Validazione STRUTTURALE dell'istanza ombra (fase 1)."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for gname, deps in cfg.groups.items():
        if not deps:
            problems.append(f"gruppo '{gname}' senza deployment")
            continue
        for d in deps:
            u = d.get("unique", "")
            if u in seen:
                problems.append(f"unique duplicato '{u}' (in '{gname}' e "
                                f"'{seen[u]}')")
            else:
                seen[u] = gname
    return problems


# --------------------------------------------------------------------------
# Diff strutturato per l'hot-reload: cosa e' cambiato tra lo stato APPLICATO
# e il nuovo file CSV. Identita' per riga = (modello, endpoint); la chiave
# (colonne non-standard) e' un campo MODIFICABILE (mai stampata in chiaro).
# --------------------------------------------------------------------------

_STANDARD_COLS = frozenset({
    "commento", "modello", "provider", "endpoint", "data", "context",
    "max_input", "priority", "caps", "effort_capable", "intelligence_score",
    "model_preference", "media_defer", "order", "enabled",
    "hold_until_finish", "api_style", "thinking_replay",
    "strip_reasoning", "no_thinking", "content_string",
})


def _csv_field_rows(path: str | Path) -> list[dict]:
    """Righe CSV normalizzate (header-aware) per il diff strutturato.
    Ogni elemento: {model, endpoint, key, fields:{col->val}, line}."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = list(csv.reader(f))
    except (FileNotFoundError, OSError):
        return []
    start = 0
    while start < len(reader) and (
            not reader[start] or not any(c.strip() for c in reader[start])
            or reader[start][0].lstrip().startswith("#")):
        start += 1
    if start >= len(reader):
        return []
    header = [h.strip() for h in reader[start]]
    out: list[dict] = []
    for i, r in enumerate(reader[start + 1:], start=start + 2):
        if not r or not any(c.strip() for c in r):
            continue
        cells = {header[j]: (r[j] if j < len(r) else "").strip()
                 for j in range(len(header))}
        key = "|".join(v for c, v in cells.items()
                       if c not in _STANDARD_COLS and v) or ""
        out.append({
            "line": i,
            "model": cells.get("modello", ""),
            "endpoint": cells.get("endpoint", ""),
            "key": key,
            "fields": cells,
        })
    return out


def _config_diff(old_rows: list[dict], new_rows: list[dict]) -> dict:
    """Classifica ogni riga: added / removed / modified / renamed / unchanged.
    Una riga e' identica se modello+endpoint+key+tutti i campi coincidono.
    MODIFIED: stessa identita' (modello, endpoint), qualche campo cambiato
    (es. key revocata). RENAME: i campi coincidono tranne il modello."""
    def _ident(r):
        return (r["model"], r["endpoint"])

    old_by = {_ident(r): r for r in old_rows}
    new_by = {_ident(r): r for r in new_rows}
    added: list[dict] = []
    removed: list[dict] = []
    modified: list[dict] = []
    unchanged = 0
    for r in new_rows:
        o = old_by.get(_ident(r))
        if o is None:
            added.append(r)
        elif o["key"] == r["key"] and o["fields"] == r["fields"]:
            unchanged += 1
        else:
            modified.append({"model": r["model"], "endpoint": r["endpoint"],
                             "old": o["fields"], "new": r["fields"]})
    for r in old_rows:
        if _ident(r) not in new_by:
            removed.append(r)
    # rename modello: una REMOVED che combacia con una ADDED su tutto tranne
    # modello -> riportata come MODIFIED "model changed" (niente churn).
    renamed: list[tuple[dict, dict]] = []
    keep_r, keep_a = [], []
    for r in removed:
        _m = [a for a in added
              if a["key"] == r["key"]
              and a["endpoint"] == r["endpoint"]
              and {k: v for k, v in a["fields"].items() if k != "modello"}
              == {k: v for k, v in r["fields"].items() if k != "modello"}]
        if _m:
            renamed.append((r, _m[0]))
        else:
            keep_r.append(r)
    for a in added:
        if not any(_m is a for _r, _m in renamed):
            keep_a.append(a)
    removed, added = keep_r, keep_a
    return {"added": added, "removed": removed, "modified": modified,
            "renamed": renamed, "unchanged": unchanged}


def _emit_config_diff(diff: dict) -> None:
    log.info("[CONFIG_DIFF] added=%d removed=%d modified=%d "
             "unchanged=%d", len(diff["added"]), len(diff["removed"]),
             len(diff["modified"]), diff["unchanged"])
    if not (diff["added"] or diff["removed"] or diff["modified"]
            or diff["renamed"]):
        return
    for r in diff["added"]:
        log.info("[CONFIG_DIFF] + deployment=%s endpoint=%s",
                 r["model"] or "?", r["endpoint"] or "?")
    for r in diff["removed"]:
        log.info("[CONFIG_DIFF] - deployment=%s endpoint=%s",
                 r["model"] or "?", r["endpoint"] or "?")
    for m in diff["modified"]:
        _changes = []
        for name, v in m["new"].items():
            ov = m["old"].get(name, "")
            if ov == v:
                continue
            if name in _STANDARD_COLS:
                if name == "modello":
                    _changes.append(f"model changed: {ov or '∅'} -> {v or '∅'}")
                elif name == "endpoint":
                    _changes.append(f"endpoint changed: {ov or '∅'} -> {v or '∅'}")
                else:
                    _changes.append(f"{name} changed: {ov or '∅'} -> {v or '∅'}")
            else:
                _changes.append("key changed")   # mai la key in chiaro
        log.info("[CONFIG_DIFF] ~ deployment=%s %s",
                 m["model"] or "?", "; ".join(_changes))
    for r, a in diff["renamed"]:
        log.info("[CONFIG_DIFF] ~ deployment=%s model changed: %s -> %s",
                 a["model"] or "?", r["model"] or "?", a["model"] or "?")


class GatewayConfig:
    """Stato runtime completo derivato dal CSV."""

    def __init__(self, csv_path: str | Path, today: date | None = None,
                 seed: int | None = None,
                 proxy_prefix: str | None = None,
                 go_suffix: str | None = None,
                 fallback_suffix: str | None = None,
                 extra_prefixes: tuple[str, ...] | list[str] = ()):
        d_prefix, d_go, d_fb = _naming_defaults()
        self.csv_path = Path(csv_path)
        self.proxy_prefix = proxy_prefix or d_prefix
        self.go_suffix = go_suffix or d_go
        self.fallback_suffix = fallback_suffix or d_fb
        # prefissi STORICI accettati nell'header del CSV oltre al corrente
        # (opzionali: default NESSUNO, vedi Policy.legacy_prefixes)
        self.extra_prefixes = tuple(
            p for p in (extra_prefixes or ()) if p and p != self.proxy_prefix)
        self.loaded_at = today or date.today()
        if seed is not None:
            random.seed(seed)  # usato SOLO nei test per riproducibilità
        self.profiles: list[str] = []
        self.groups: dict[str, list[dict]] = {}       # group_name -> deployments
        self.group_caps: dict[str, str | None] = {}   # group_name -> cap|None(testo)
        self.profile_dims: dict[str, list[int]] = {}  # profilo -> dims ordinate
        self.profile_caps: dict[str, list[str]] = {}  # profilo -> caps con gruppi
        self.chains: dict[str, list[str]] = {}        # profilo -> univoci TESTO
        self.chains_cap: dict[str, dict[str, list[str]]] = {}   # profilo->{cap:[uniques]}
        self.cap_counts: dict[str, dict[str, dict[str, int]]] = {}  # profilo->{cap:{primary,go,fallback}}
        self._load()
        # snapshot dello stato APPLICATO, per il diff strutturato al reload
        self._last_rows = _csv_field_rows(self.csv_path)

    # ------------------------------------------------------------------ load
    def _match_column_prefix(self, header: str) -> str | None:
        """Ritorna il prefisso (corrente o legacy) con cui l'header combacia.
        Il prefisso PIÙ LUNGO vince per evitare ambiguità di prefissi annidati."""
        candidates = [self.proxy_prefix, *self.extra_prefixes]
        for p in sorted(candidates, key=len, reverse=True):
            if header.startswith(p):
                return p
        return None

    def _load(self) -> None:
        # FIX bootstrap: fresh install SENZA CSV (repo clonato, var/ vuota)
        # non deve crashare il container: parte con 0 deployment e il
        # playbook /bootstrap guida l'agente fino al primo bulk-insert.
        try:
            with open(self.csv_path, newline="", encoding="utf-8-sig") as f:
                reader = list(csv.reader(f))
        except FileNotFoundError:
            reader = None
        # CSV assente OPPURE presente ma vuoto (0 byte / solo whitespace, es.
        # dopo un PUT /admin/csv sbagliato o un ripristino incompleto): NON deve
        # brickare il gateway -> stessa via del fresh install (0 deployment, il
        # playbook /bootstrap guida fino al primo bulk-insert).
        if not reader or not any(any(c.strip() for c in row) for row in reader):
            log.warning("[config] CSV assente/vuoto (%s): avvio con 0 "
                        "deployment (fresh install: segui GET /bootstrap)",
                        self.csv_path)
            self.profiles = []
            self.groups, self.group_caps = {}, {}
            self.profile_dims, self.profile_caps = {}, {}
            self.chains, self.chains_cap, self.cap_counts = {}, {}, {}
            return
        # salta le righe di commento (# ...) e vuote PRIMA dell'header:
        # l'example pubblicato nel repo le ha, e il quickstart
        # (cp example -> up) non deve crashare. Dopo l'header nessun
        # comportamento cambia.
        start = 0
        while start < len(reader):
            first = reader[start]
            if not first or not any(c.strip() for c in first) \
                    or first[0].lstrip().startswith("#"):
                start += 1
            else:
                break
        if start >= len(reader):
            raise ValueError("Nessuna colonna profilo nel CSV")
        header = [h.strip() for h in reader[start]]
        prof_cols = []
        for i, h in enumerate(header):
            prefix = self._match_column_prefix(h)
            if prefix:
                prof_cols.append((i, h[len(prefix):].strip()))
        if not prof_cols:
            raise ValueError("Nessuna colonna profilo nel CSV")
        self.profiles = [name for _, name in prof_cols]
        # reset strutture (idempotenza: reload chiama già un reset, qui si
        # difende da chiamate dirette a _load)
        self.groups, self.group_caps = {}, {}
        self.profile_dims, self.profile_caps = {}, {}
        self.chains, self.chains_cap, self.cap_counts = {}, {}, {}

        rows: list[tuple[dict, dict[str, str]]] = []
        for r in reader[start + 1:]:
            if not r or not any(c.strip() for c in r):
                continue
            row = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
            meta = _classify(row, self.loaded_at)
            # riga disabilitata dal CSV: resta nel file (chiave conservata) ma
            # non entra in nessun bucket di routing.
            if not meta.get("enabled", True):
                continue
            assign = {}
            for (i, _col), pname in zip(prof_cols, self.profiles):
                val = (r[i] if i < len(r) else "").strip()
                if val:
                    assign[pname] = val
            if assign:
                rows.append((meta, assign))

        by_profile: dict[str, list[dict]] = {p: [] for p in self.profiles}
        for meta, assign in rows:
            for pname, key in assign.items():
                by_profile[pname].append({"key": key, "meta": meta})

        for pname in self.profiles:
            self._build_profile(pname, by_profile[pname])

    def _build_profile(self, pname: str, deps: list[dict]) -> None:
        # ---- partizione dei MONDI: testo/ingest vs GENERAZIONE ----
        # REGOLA ANTI-CONTAMINAZIONE: una riga con token *_gen (image_gen/
        # video_gen) partecipa SOLO ai bucket del/dai domini di generazione.
        # Non entra nei dims né nelle catene di ingest (vision/video/audio):
        # i generatori ricevono traffico SOLO dagli endpoint dedicati. Se ha
        # PIÙ token gen sta in ENTRAMBI i gruppi (es. image_gen+video_gen).
        # I suoi ingest-token restano dichiarati sulla riga per l'inner-filter
        # dell'endpoint gen (i2i/i2v).
        cap_world: dict[str, list[dict]] = {}
        text_deps: list[dict] = []
        for d in deps:
            cs = d["meta"].get("caps") or frozenset()
            gen_tokens = cs & GEN_CAPS
            if gen_tokens:
                for t in sorted(gen_tokens):
                    cap_world.setdefault(t, []).append(d)
                continue
            if not cs or "text" in cs:
                text_deps.append(d)
            for c in sorted(cs - {"text"}):
                cap_world.setdefault(c, []).append(d)

        free: list[dict] = []
        go: list[dict] = []
        fb: list[dict] = []
        for d in text_deps:
            cat = d["meta"]["category"]
            if cat == "fallback":
                fb.append(d)
            elif cat == "go":
                go.append(d)
            else:
                free.append(d)

        by_dim: dict[int, list[dict]] = {}
        for d in free:
            by_dim.setdefault(d["meta"]["context_k"] or 0, []).append(d)

        built: list[tuple[str, list[dict], str | None]] = []
        dims_sorted = sorted(k for k in by_dim if k > 0)
        for dim in dims_sorted:
            built.append((f"{self.proxy_prefix}{pname}-{dim}k",
                          _shuffle_bucket(by_dim[dim]), None))
        if go:
            built.append((f"{self.proxy_prefix}{pname}{self.go_suffix}",
                          sorted(go, key=lambda d: d["meta"]["sort_key"]), None))
        if fb:
            built.append((f"{self.proxy_prefix}{pname}{self.fallback_suffix}",
                          sorted(fb, key=lambda d: d["meta"]["sort_key"]), None))

        # ---- gruppi capacità: terna primario/-go/-fallback per ogni cap ----
        # stessa semantica data del mondo testo, nessuna dimensione -Nk
        chains_cap: dict[str, list[str]] = {}
        cap_counts: dict[str, dict[str, int]] = {}
        for cap in sorted(cap_world):
            c_free: list[dict] = []
            c_go: list[dict] = []
            c_fb: list[dict] = []
            for d in cap_world[cap]:
                cat = d["meta"]["category"]
                if cat == "fallback":
                    c_fb.append(d)
                elif cat == "go":
                    c_go.append(d)
                else:
                    c_free.append(d)
            base_g = f"{self.proxy_prefix}{pname}-{cap}"
            if c_free:
                built.append((base_g, _shuffle_bucket(c_free), cap))
            if c_go:
                built.append((f"{base_g}{self.go_suffix}",
                              sorted(c_go, key=lambda d: d["meta"]["sort_key"]),
                              cap))
            if c_fb:
                built.append((f"{base_g}{self.fallback_suffix}",
                              sorted(c_fb, key=lambda d: d["meta"]["sort_key"]),
                              cap))
            cap_counts[cap] = {"primary": len(c_free), "go": len(c_go),
                               "fallback": len(c_fb)}
        chains_cap = {cap: [] for cap in cap_world}

        flat_uniques: list[str] = []
        dims_ranked: list[tuple[int, str]] = []
        tail_go: list[str] = []
        tail_fb: list[str] = []
        for gname, gdeps, cap in built:
            lst = []
            for idx, d in enumerate(gdeps):
                meta = d["meta"]
                model_final, needs_openai = infer_model_prefix(
                    meta["modello"], meta["endpoint"])
                tier = "free" if meta["category"] in ("priority", "zen") else "paid"
                unique = f"{gname}__{slugify_model(model_final)}__{idx}"
                lst.append({
                    "unique": unique,
                    "group": gname,
                    "model": model_final,
                    "api_base": meta["endpoint"].rstrip("/"),
                    "api_key": d["key"],
                    "tier": tier,
                    "max_input_tokens": meta["max_input"],
                    "needs_openai_provider": needs_openai,
                    "priority": meta["priority"],
                    "caps": frozenset(meta.get("caps") or ()),
                    "provider": meta.get("provider") or "",
                    "effort_capable": bool(meta.get("effort_capable")),
                    "intelligence": int(meta.get("intelligence") or 5),
                    "tool_repair": meta.get("tool_repair", ""),
                    "model_preference": int(meta.get("model_preference") or 0),
                    "sort_key": float(meta.get("sort_key") or float("inf")),
                    "concurrent_limit": meta.get("concurrent_limit"),
                    "media_defer": bool(meta.get("media_defer", True)),
                    "hold_until_finish": bool(meta.get("hold_until_finish")),
                    "thinking_replay": bool(meta.get("thinking_replay")),
                    "strip_reasoning": bool(meta.get("strip_reasoning")),
                    "no_thinking": bool(meta.get("no_thinking")),
                    "content_string": bool(meta.get("content_string")),
                    "order": int(meta.get("order", ORDER_LAST)),
                    "family": canonical_family(model_final),
                    "api_style": normalize_style(meta.get("api_style")),
                })
            self.groups[gname] = lst
            self.group_caps[gname] = cap
            if cap is None:
                # Mondo testo: i dims vanno ordinati per tier (colonna
                # `order`) e poi per dim crescente; -go/-fallback restano in
                # coda nell'ordine di costruzione (invariati).
                if gname.endswith(self.fallback_suffix):
                    tail_fb.extend(dep["unique"] for dep in lst)
                elif gname.endswith(self.go_suffix):
                    tail_go.extend(dep["unique"] for dep in lst)
                else:
                    dims_ranked.extend(
                        (int(dep.get("order", ORDER_LAST)), dep["unique"])
                        for dep in lst)
            else:
                chains_cap[cap].extend(d["unique"] for d in lst)

        # tier (`order`) primario, poi dim crescente: i dims sono raccolti in
        # ordine dim-ascendente, quindi uno stable sort per `order` produce
        # esattamente (order, dim) mantenendo l'ordine interno del gruppo.
        dims_ranked.sort(key=lambda t: t[0])
        flat_uniques = [u for _, u in dims_ranked] + tail_go + tail_fb
        self.chains[pname] = flat_uniques
        self.chains_cap[pname] = chains_cap
        self.cap_counts[pname] = cap_counts
        self.profile_dims[pname] = dims_sorted
        self.profile_caps[pname] = sorted(cap_world)

    # ------------------------------------------------------------- accessors
    def profile_of_base(self, base_name: str) -> str | None:
        """'<proxy_prefix>example' -> 'example', solo se il profilo esiste."""
        if not base_name.startswith(self.proxy_prefix):
            return None
        p = base_name[len(self.proxy_prefix):]
        return p if p in self.profile_dims else None

    def all_dims(self) -> set[int]:
        dims: set[int] = set()
        for v in self.profile_dims.values():
            dims.update(v)
        return dims

    def known_suffixes(self) -> list[str]:
        """Suffissi espliciti instradabili: -fallback, -go, ogni -Nk noto
        e i gruppi capacità -C / -C-go / -C-fallback."""
        sufs = [self.fallback_suffix, self.go_suffix]
        sufs += [f"-{d}k" for d in sorted(self.all_dims())]
        for caps in self.profile_caps.values():
            for c in caps:
                sufs += [f"-{c}", f"-{c}{self.go_suffix}",
                         f"-{c}{self.fallback_suffix}"]
        return sufs

    def deployment_by_unique(self, unique: str) -> dict | None:
        for lst in self.groups.values():
            for dep in lst:
                if dep["unique"] == unique:
                    return dep
        return None

    def whitelist_for(self, pname: str) -> list[str]:
        """Whitelist a tre livelli: base + gruppi + univoci."""
        base = self.proxy_prefix + pname
        groups = sorted(g for g in self.groups if g.startswith(base + "-"))
        uniques: list[str] = []
        for g in groups:
            uniques.extend(d["unique"] for d in self.groups[g])
        return [base] + groups + uniques

    def reload(self) -> None:
        """Rilegge il CSV in DUE FASI (hot reload senza riavvio).

        Fase 1 (lint + shadow): valida il file (riga/colonna), poi costruisce
        un'istanza OMBRA completa e la valida strutturalmente (gruppi non
        vuoti, unique non duplicati).
        Fase 2 (atomic swap): solo se TUTTO passa, i riferimenti in memoria
        vengono sostituiti in un colpo solo. Se la validazione fallisce, lo
        stato precedente resta INTATTO e il problema esatto va nei log.
        """
        issues = validate_csv(self.csv_path)
        if issues:
            for it in issues:
                log.warning("[config] CSV INVALIDO: %s", it)
            raise ConfigValidationError(
                f"{len(issues)} problemi nel CSV (primo: {issues[0]})")
        # Lock seriale: un reload gia' in corso (watcher + PUT /admin/csv)
        # viene serializzato, mai sovrapposto -> niente stati misti.
        with _RELOAD_LOCK:
            old_rows = getattr(self, "_last_rows", None)
            new_rows = _csv_field_rows(self.csv_path)
            shadow = GatewayConfig(
                self.csv_path, today=self.loaded_at,
                proxy_prefix=self.proxy_prefix, go_suffix=self.go_suffix,
                fallback_suffix=self.fallback_suffix,
                extra_prefixes=self.extra_prefixes)
            problems = self_check(shadow)
            if problems:
                for it in problems:
                    log.warning("[config] CSV INVALIDO (struttura): %s", it)
                raise ConfigValidationError(problems[0])
            self.profiles = shadow.profiles
            self.groups = shadow.groups
            self.group_caps = shadow.group_caps
            self.profile_dims = shadow.profile_dims
            self.profile_caps = shadow.profile_caps
            self.chains = shadow.chains
            self.chains_cap = shadow.chains_cap
            self.cap_counts = shadow.cap_counts
            # diff strutturato: cosa e' cambiato in questo reload (log CLOG)
            if old_rows is not None:
                _emit_config_diff(_config_diff(old_rows, new_rows))
            else:
                log.info("[CONFIG_DIFF] primo caricamento, %d righe "
                         "(no diff)", len(new_rows))
            self._last_rows = new_rows


# --------------------------------------------------------------------------
# Hot reload del CSV (usato dal watcher in main e testato direttamente)
# --------------------------------------------------------------------------

def csv_mtime_ns(path: str | Path) -> int | None:
    """mtime in nanosecondi, o None se il file manca."""
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return None


def maybe_reload(cfg: GatewayConfig, last_mtime: int | None) -> int | None:
    """Ricarica il CSV se l'mtime è cambiato. Ritorna il nuovo mtime di riferimento.

    - primo avvio (last_mtime=None): registra il mtime SENZA ricaricare
      (la config è appena stata costruita dal file);
      NOTA: qui si sceglie di ricaricare comunque una volta per semplicità?
      No: ritorna il mtime corrente senza toccare nulla.
    - mtime invariato -> nessuna azione.
    - CSV mancante o corrotto -> resta lo stato precedente (mai downtime).
    """
    m = csv_mtime_ns(cfg.csv_path)
    if m is None:
        return last_mtime
    if last_mtime is not None and m == last_mtime:
        return last_mtime
    if last_mtime is None:
        return m                      # solo baseline al primo avvio
    try:
        cfg.reload()
        return m
    except Exception as exc:
        # CSV temporalmente invalido (lint/struttura o scrittura parziale):
        # lo stato precedente resta attivo, si riprovera' al giro successivo.
        log.warning("[config] reload rifiutato, resta la config precedente: "
                    "%s", exc)
        return last_mtime

