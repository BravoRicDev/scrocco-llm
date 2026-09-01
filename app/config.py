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
from datetime import date
from pathlib import Path
from typing import Any
from .capabilities import ROUTING_CAPS, GEN_CAPS

log = logging.getLogger("nx.config")

ENDPOINT_HEADERS = {"endpoint", "endppoint", "end point", "endpoint_url"}
MODEL_HEADER = "modello"
PROVIDER_HEADER = "provider"
DATA_HEADER = "data"
CONTEXT_HEADER = "context"
MAX_INPUT_HEADER = "max_input"
PRIORITY_HEADER = "priority"
CAPS_HEADER = "caps"

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

    raw_max = (row.get(MAX_INPUT_HEADER) or "").strip()
    max_from_csv = 0
    if raw_max:
        try:
            max_from_csv = int(float(raw_max))
        except ValueError:
            max_from_csv = 0
    max_input = max_from_csv if max_from_csv > 0 else ((ctx_k or 0) * 1000)

    try:
        priority = int(float((row.get(PRIORITY_HEADER) or "").strip()))
    except ValueError:
        priority = 0

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
                })
            self.groups[gname] = lst
            self.group_caps[gname] = cap
            if cap is None:
                flat_uniques.extend(d["unique"] for d in lst)
            else:
                chains_cap[cap].extend(d["unique"] for d in lst)

        self.chains[pname] = flat_uniques
        self.chains_cap[pname] = chains_cap
        self.cap_counts[pname] = cap_counts
        self.profile_dims[pname] = dims_sorted
        self.profile_caps[pname] = sorted(cap_world)

    # ------------------------------------------------------------- accessors
    def profile_of_base(self, base_name: str) -> str | None:
        """'<proxy_prefix>collego' -> 'collego', solo se il profilo esiste."""
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
        """Rilegge il CSV (hot reload senza riavvio).

        ATOMICO: se il nuovo CSV è rotto, lo stato precedente resta integro
        (nessun downtime per un file scritto a metà).
        """
        saved = (self.profiles, dict(self.groups), dict(self.group_caps),
                 dict(self.profile_dims), dict(self.profile_caps),
                 dict(self.chains), {k: dict(v) for k, v in self.chains_cap.items()},
                 {k: {c: dict(x) for c, x in v.items()}
                  for k, v in self.cap_counts.items()})
        try:
            (self.profiles, self.groups, self.group_caps, self.profile_dims,
             self.profile_caps, self.chains, self.chains_cap,
             self.cap_counts) = {}, {}, {}, {}, {}, {}, {}, {}
            self._load()
        except Exception:
            (self.profiles, self.groups, self.group_caps, self.profile_dims,
             self.profile_caps, self.chains, self.chains_cap,
             self.cap_counts) = saved
            raise


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
    except Exception:
        # CSV temporaneamente corrotto (scrittura parziale ecc.): riproverà al giro dopo
        return last_mtime

