"""Policy runtime (var/gateway.yaml): comportamento senza riavvii.

[IT] COSA: tutte le manopole (alias profili, hotwords, step_up, QC,
cooldown_sec, capability_routing.model_capabilities, client_keys...).
HOW: dataclass validata + load_or_default; PATCH /admin/policy VALIDA
prima di scrivere (niente policy mezza applicata). WHY:
  - hot-reload ovunque: l'agente gestisce tutto via API, mai restart.
  - model_capabilities a PATTERN (*seedance*): il CSV dice DOVE, la policy
    dice COSA; separazione dati/comportamento.
  - default conservativi: qc_sanity min_chars=1, watchdog_mark_no_done
    False (solo log) -- prima osservare, poi punire.

[EN] WHAT: hot-reloadable behaviour knobs. WHY: agents drive everything
via validated PATCHes; capabilities as patterns keep CSV data-only;
defaults observe first, punish later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("nx.policy")

DEFAULT_HOTWORDS = [
    r"pensaci\s+bene",
    r"pensa\s+a\s+fondo",
    r"\bragiona\b",
    r"deep\s*think",
]

DEFAULT_SPEED_HOTWORDS = [
    r"\bveloce\b",
    r"fai\s+in\s+fretta",
    r"\bin\s+fretta\b",
]

# Default SOLO SE gateway.yaml non configura altro (contratto documentato).
DEFAULT_SERVICE_NAME = "scrocco-llm"
DEFAULT_PROXY_PREFIX = "scrocco-llm-"


def _coerce_bool(value: Any, ctx: str) -> bool:
    """Bool nativo O stringhe leggibili (true/on/sì/1…). Solleva ValueError."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "on", "sì", "si", "1", "yes"):
            return True
        if low in ("false", "off", "no", "0"):
            return False
    raise ValueError(f"{ctx} non valido: {value!r}")


@dataclass
class QcSanity:
    """QC generico contenuti non-streaming: scarta SOLO risposte vuote/triviali
    (i JSON rotti restano a QcJson). Zero rilevamento rifiuti (falsi positivi)."""
    enabled: bool = True
    min_chars: int = 1                # sotto questa lunghezza (strip) -> scarto
    rotate_on_length_empty: bool = False  # False: output vuoto+finish_reason=
                                          # length -> risposta "notice" subito
                                          # (niente giro di catena). True: ruota
                                          # come qualsiasi altro output vuoto.


@dataclass
class QcJson:
    """QC contenuto JSON: vedi app/qc.py."""
    enabled: bool = True             # interruttore generale (anche trigger esplicito)
    strip_fences: bool = True        # unwrappa ```json ... ``` prima del parse
    max_attempts: int = 3            # deployment da provare OLTRE il primo
    annotate_reasoning: bool = True  # nota nel reasoning della risposta finale
    retry_provider_4xx: bool = True  # 400 firma openai_error -> ritriabile
    watchdog_mark_no_done: bool = False  # [DONE] mancante -> cooldown (tier 2)
    watchdog_cooldown_sec: int = 90    # cooldown CORTO FISSO per i fallimenti
                                       # "soft" dello streaming (vuoto/troncato/
                                       # zero-answer): il modello ha risposto,
                                       # solo male -> 90s, non l'escalation da
                                       # 10min+ che spegnerebbe il pool
    # STREAMING anti-stallo: lo stream verso il client NON parte finche' non
    # arriva contenuto di RISPOSTA reale da un deployment. Entro questa finestra
    # un upstream vuoto/errore/lento viene ruotato in modo TRASPARENTE (nessun
    # byte inviato). Esaurita la catena -> risposta "notice" non vuota.
    stream_first_content_ms: int = 20000   # attesa max del primo contenuto
                                           # (clamp >= 2000)
    stream_commit_min_chars: int = 40      # caratteri di RISPOSTA minimi per
                                           # impegnare lo stream (evita di
                                           # committare su 1 token poi morto);
                                           # un finish_reason con >0 char
                                           # committa comunque
    stream_total_deadline_ms: int = 90000  # tetto wall-clock su tutto il giro
    stream_commit_include_reasoning: bool = False  # True: bastano i reasoning
                                           # token per impegnare lo stream
    # PARACADUTE: sulla catena -go/-fallback (ULTIMO scaglione del ladder) il
    # timeout sul primo contenuto NON deve produrre un 503: la catena e' il
    # paracadute finale, non c'e' dove ruotare. True = lo stream parte comunque
    # (si trasmette quel che arriva). False = comportamento legacy (timeout ->
    # rotazione/503), utile solo se le catene hanno molti account validi.
    stream_parachute_no_timeout: bool = True


@dataclass
class Policy:
    """Tutto ciò che un giorno era una costante nel router, ora vive qui."""
    service_name: str = DEFAULT_SERVICE_NAME   # usato in API/log, configurabile
    proxy_prefix: str = DEFAULT_PROXY_PREFIX
    go_suffix: str = "-go"
    fallback_suffix: str = "-fallback"

    # Prefissi STORICI riconosciuti come compatibili (OPZIONALI, default NESSUNO):
    # se valorizzati, le colonne del CSV che li usano vengono lette normalmente
    # e i nomi richiesti dai client vengono riscritti al prefisso corrente.
    # Vuoto = si accettano SOLO i nomi col prefisso attuale.
    legacy_prefixes: list[str] = field(default_factory=list)

    estimate_divisor: int = 4
    sticky_ttl_sec: int = 3600
    cooldown_sec: int = 600
    hotwords_window: int = 3
    hotwords: list[str] = field(default_factory=lambda: list(DEFAULT_HOTWORDS))

    # hot-word di VELOCITÀ ("veloce", "fai in fretta"...): non forzano il
    # gruppo massimo ma scelgono il gruppo PIÙ RAPIDO (EMA latenza) tra
    # quelli che ospitano la richiesta con contesto >= speed_min_dim_k.
    # Se ragione e fretta compaiono insieme, LA FRETTA VINCE.
    speed_hotwords: list[str] = field(
        default_factory=lambda: list(DEFAULT_SPEED_HOTWORDS))
    speed_min_dim_k: int = 200        # tetto minimo di contesto (k token)
    speed_qualify_pct: int = 70       # margine fit: stima <= dim*70%
    profile_speed_min_dim_k: dict[str, int] = field(default_factory=dict)
    profile_speed_qualify_pct: dict[str, int] = field(default_factory=dict)

    step_up_pct: int = 100                     # default globale (legacy)
    profile_step_up_pct: dict[str, int] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    # chiave CUSTOM opzionale per alias GENERICI (target = nome base):
    # sostituisce dep["api_key"] SOLO al primo tentativo; ignorata per
    # alias verso gruppi/unique espliciti.
    alias_keys: dict[str, str] = field(default_factory=dict)

    # override OPZIONALE delle chiavi client deterministiche sk-<profilo>:
    # se un profilo è qui, per autenticarsi vale SOLO questa chiave
    # (la deterministica viene disattivata: più sicuro se esposto).
    client_keys: dict[str, str] = field(default_factory=dict)

    # catalogo prezzi per la STIMA dei costi nel ledger (/admin/insights):
    # pattern glob sul nome modello upstream -> USD per MILIONE di token.
    # Esempio: {"openai/gpt-oss-120b": {"prompt_per_1m": 0.1,
    #                                    "completion_per_1m": 0.5}}
    # Default vuoto: senza catalogo si vedono i token e i soli costi che i
    # provider riportano da soli (es. OpenRouter). WHY pattern e non tabella:
    # un provider ha decine di varianti con lo stesso prezzo.
    pricing: dict[str, dict] = field(default_factory=dict)

    # cosa scrivere nel campo "model" delle risposte NON-streaming:
    #   upstream (default)   -> nome scritto dal PROVIDER nella sua risposta
    #                           (fallback al nome che noi inviamo se assente)
    #   deployment           -> unique scelto, es. <prefix>collego-200k__mod__3
    #   requested            -> nome richiesto dal client (compatibilità storica)
    response_model: str = "upstream"

    # rotazione adattiva preventiva dentro ogni gruppo: penalizza l'ultimo
    # usato (anti rate-limit), premia la velocità (EMA latenza), evita chi
    # ha già richieste in corso.
    adaptive_pick: bool = True
    recency_halflife_sec: float = 20.0
    latency_ref_ms: float = 1500.0

    # PROTEZIONE FREE-TIER: nei gruppi DIMS i modelli con input media
    # (vision/video/audio) sono ULTIMA SPIAGGIA per le richieste di testo
    # puro — non vengono scelti finché esiste almeno un text-only vivo nel
    # gruppo (hard rule, tutti i tier). I gruppi cap (-vision ecc.) e le
    # richieste media non sono toccate: lì il multimodale È lo scopo.
    multimodal_last_resort: bool = True

    # FAILOVER SAME-MODEL nei gruppi gen/stt (image_gen, video_gen, tts,
    # stt): al fallimento si riprova PRIMA su altre chiavi dello stesso
    # modello upstream (output identico); attraversare verso un modello
    # diverso è ammesso solo a esaurimento, con log + contatore.
    # Cambiare voce o stile immagine/video inatteso rompe la coerenza.
    gen_same_model_failover: bool = True

    # SCALA UNICA dims (testo): al fallimento si sale SEMPRE di dimensione
    # (primari asc) e solo in cima si usano -go/-fallback; i suffissi
    # espliciti -Nk diventano SOGLIA MINIMA (mai dim < N, neanche in
    # rotazione); alias senza suffisso = "0k" completamente automatico.
    # False = legacy (esplicito puntamento esatto, catena solo primari).
    dims_ladder_floor: bool = True

    # QC del contenuto JSON (non-streaming) + retry 400 provider-side +
    # watchdog streaming passivo (vedi app/qc.py)
    qc_json: "QcJson" = field(default_factory=lambda: QcJson())
    # sanity QC generica: scarta contenuti vuoti/triviali (non-streaming)
    qc_sanity: "QcSanity" = field(default_factory=lambda: QcSanity())

    # escalation del cooldown: fallimenti ripetuti allungano l'esclusione
    # (cooldown_sec * 2^streak) fino a max_cooldown_sec — una chiave morta
    # non viene ri-provata ogni 10 minuti per sempre.
    cooldown_escalation: bool = True

    # modalità cooldown: "linear" (BASE + MULT*(fail_24h-1) minuti) o "exponential" (legacy)
    cooldown_mode: str = "linear"
    cooldown_base_min: int = 30
    cooldown_linear_mult_min: int = 30

    # Prima di scomodare il tier -fallback (a pagamento), la scala ri-prova i
    # deployment free+go il cui cooldown e' stato messo PIU' di questi secondi
    # fa: forse la chiave si e' svegliata. Cosi' non sprechiamo tentativi su
    # qualcosa appena messo in pausa, ma sfruttiamo quello che e' tornato su.
    stale_cooldown_retry_sec: int = 300

    # early escalation: dopo N fallimenti dims, salta a -go/-fallback
    ladder_skip_after: int = 4
    # max tentativi stale dims prima di passare a -fallback
    ladder_stale_max: int = 3

    # Tetto ai tentativi di fallback interni per una singola richiesta (prima
    # di arrendersi con 503). Ogni tentativo = 1 chiamata upstream reale.
    max_fallback_tries: int = 128

    # Budget guard proattivo (Feature no-spreco): dosa i deployment PRIMA
    # che prendano 429. I cap NON si indovinano: si APPRENDONO dal primo 429
    # osservato (uso fatto nella finestra x1.2, floor min_per_*). Finche'
    # non c'e' evidenza la guardia non tocca nulla -> le chiavi sane/pagate
    # non vengono frenate. La penalita' e' SOLO sul punteggio: mai esclusione
    # dura (quella resta il cooldown); a quota esaurita peso residuo 5%.
    budget_guard: dict = field(default_factory=lambda: {
        "enabled": True, "soft_factor": 0.8,
        "min_per_min": 10, "min_per_day": 200})
    # cap a 5 ORE: i free-tier si rinnovano su finestre giornaliere/orarie,
    # seppellire una chiave per un intero giorno la toglie dal giro anche
    # quando il limite era solo orario. Il budget_guard (router) dosa PRIMA
    # del muro, quindi il cooldown resta per i casi veramente rotti, non
    # per quota.
    max_cooldown_sec: int = 18000

    # Lifecycle chiavi (keyhealth): dopo N giorni consecutivi "dead_suspect"
    # la chiave viene marcata RETIRED ed esclusa dal routing. MAI cancellata
    # dal CSV; sblocco via POST /admin/deployments/unretire o probe riuscito.
    retire_after_days: int = 7

    # health proattivo (F6): verifica periodica via GET /models (zero token)
    proactive_health: bool = False
    health_interval_sec: int = 1800

    # ----------------------------------------------------------- capacità (semver)
    # abilitato il routing consapevole del modalità input (vision, video, audio, image_gen)
    capability_routing_enabled: bool = True
    # mappa pattern->capacità (exact/globs -> list di stringhe canoniche)
    model_capabilities: dict[str, list[str]] = field(default_factory=dict)
    # capacità di fallback per modelli non elencati
    capabilities_default: frozenset[str] = frozenset({"text"})
    # token fittizi per parte immagine nella stima contesto (0 = comportamento attuale)
    image_token_estimate: int = 800
    # per /v1/images/generations: tenta via chat se /images/generations fallisce
    images_chat_fallback: bool = True

    # AUTO-LEARN capacità: quando un provider rifiuta una modalità (400 firma
    # provider-side su richiesta instradata PER quella capacità) si conta uno
    # strike sul modello; al superamento della soglia la capacità viene rimossa
    #   off     -> nessuno strike registrato
    #   suggest -> strike+suggerimento nel journal, NESSUNA modifica mappa
    #   auto    -> rimozione automatica (entry esplicita in model_capabilities,
    #              glob preservate) con journal + [caps][auto-learn] revertibile
    cap_auto_learn: str = "suggest"
    cap_auto_learn_threshold: int = 3

    # GRUPPI DI CAPACITÀ STRUTTURALI: membri dichiarati dalla colonna CSV
    # "caps" (token text,vision,video,audio,image_gen,tts,stt). Quando
    # abilitato, le richieste media instradano verso il gruppo dedicato
    # -C / -C-go / -C-fallback invece del filtro dinamico sui nomi.
    # DEFAULT OFF: si attiva SOLO dopo il seed della colonna caps (rollout).
    cap_groups_enabled: bool = False
    # se la cap richiesta NON ha alcun gruppo nel profilo:
    #   dynamic -> filtro dinamico legacy sull'intero profilo (migrazione)
    #   error   -> 400 rigoroso
    cap_groups_on_missing: str = "dynamic"

    # ------------------------------------------------------------- accessors
    def step_up_for(self, profile: str | None) -> int:
        """Soglia di salita (%) per un profilo, o quella globale."""
        if profile and profile in self.profile_step_up_pct:
            return self.profile_step_up_pct[profile]
        return self.step_up_pct

    def speed_min_for(self, profile: str | None) -> int:
        """Contesto minimo (k) del gruppo scelto 'veloce', per profilo."""
        if profile and profile in self.profile_speed_min_dim_k:
            return self.profile_speed_min_dim_k[profile]
        return self.speed_min_dim_k

    def speed_qualify_for(self, profile: str | None) -> int:
        """Margine fit (%) della scelta veloce, per profilo."""
        if profile and profile in self.profile_speed_qualify_pct:
            return self.profile_speed_qualify_pct[profile]
        return self.speed_qualify_pct

    # ---------------------------------------------------------- ability accessors
    def routing_active(self) -> bool:
        """True se il routing consapevole del modalità input è abilitato."""
        return self.capability_routing_enabled

    def caps_for(self, model_name: str) -> frozenset[str]:
        """Risolve le capacità per un modello (match exact -> glob più lungo -> default)."""
        from .capabilities import normalize_caps
        import fnmatch
        # 1) exact
        if model_name in self.model_capabilities:
            return normalize_caps(self.model_capabilities[model_name], f"model_capabilities[{model_name}]")
        # 2) glob - pattern più lungo vince
        best_pat = None
        best_len = -1
        for pat, caps in self.model_capabilities.items():
            if fnmatch.fnmatch(model_name, pat):
                if len(pat) > best_len:
                    best_len = len(pat)
                    best_pat = pat
        if best_pat is not None:
            return normalize_caps(self.model_capabilities[best_pat], f"model_capabilities[{best_pat}]")
        # 3) default
        return self.capabilities_default

    def from_legacy(self, name: str) -> str:
        """Riscrive un nome con prefisso STORICO al prefisso corrente.

        Es. con legacy_prefixes=["vecchio-"]: 'vecchio-collego-32k' ->
        '<proxy_prefix>collego-32k'. I nomi già col prefisso corrente
        passano indenni.
        """
        for lp in self.legacy_prefixes:
            if lp and lp != self.proxy_prefix and name.startswith(lp):
                return self.proxy_prefix + name[len(lp):]
        return name

    # ---------------------------------------------------------- ability accessors
    def routing_active(self) -> bool:
        """True se il routing consapevole del modalità input è abilitato."""
        return self.capability_routing_enabled

    def caps_for(self, model_name: str) -> frozenset[str]:
        """Risolve le capacità per un modello (match exact -> glob più lungo -> default)."""
        from .capabilities import normalize_caps
        import fnmatch
        # 1) exact
        if model_name in self.model_capabilities:
            return normalize_caps(self.model_capabilities[model_name], f"model_capabilities[{model_name}]")
        # 2) glob - pattern più lungo vince
        best_pat = None
        best_len = -1
        for pat, caps in self.model_capabilities.items():
            if fnmatch.fnmatch(model_name, pat):
                if len(pat) > best_len:
                    best_len = len(pat)
                    best_pat = pat
        if best_pat is not None:
            return normalize_caps(self.model_capabilities[best_pat], f"model_capabilities[{best_pat}]")
        # 3) default
        return self.capabilities_default

    def resolve_alias(self, requested: str) -> str:
        """Alias -> nome canonico; nomi ignoti passano indenni."""
        return self.aliases.get(requested, requested)

    def canonicalize(self, requested: str) -> str:
        """Ordine completo di normalizzazione di un nome richiesto:
        1) riscrittura da prefisso legacy (compatibilità) 2) alias."""
        return self.resolve_alias(self.from_legacy(requested))

    @classmethod
    def default(cls) -> "Policy":
        """Policy storica: identica alle costanti pre-parametrizzazione."""
        return cls()

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "Policy":
        """Costruisce la policy da un dict YAML validandone i tipi."""
        p = cls()
        if not raw:
            return p
        _set_int(p, raw, "estimate_divisor", minimum=1)
        _set_int(p, raw, "sticky_ttl_sec", minimum=1)
        _set_int(p, raw, "cooldown_sec", minimum=0)
        _set_int(p, raw, "stale_cooldown_retry_sec", minimum=0)
        _set_int(p, raw, "max_fallback_tries", minimum=1)
        _set_int(p, raw, "hotwords_window", minimum=1)
        _set_int(p, raw, "step_up_pct", minimum=1, maximum=200)

        for key, attr in (("proxy_prefix", "proxy_prefix"),
                          ("go_suffix", "go_suffix"),
                          ("fallback_suffix", "fallback_suffix"),
                          ("service_name", "service_name")):
            if key in raw:
                if not isinstance(raw[key], str) or not raw[key]:
                    raise ValueError(f"{key} deve essere una stringa non vuota")
                setattr(p, attr, raw[key])

        if "legacy_prefixes" in raw:
            lp = raw["legacy_prefixes"]
            if not isinstance(lp, list) or \
                    not all(isinstance(x, str) and x for x in lp):
                raise ValueError(
                    "legacy_prefixes deve essere una lista di stringhe non vuote")
            p.legacy_prefixes = lp

        if "hotwords" in raw:
            hw = raw["hotwords"]
            if not isinstance(hw, list) or \
                    not all(isinstance(x, str) for x in hw):
                raise ValueError("hotwords deve essere una lista di regex")
            p.hotwords = hw

        if "speed_hotwords" in raw:
            shw = raw["speed_hotwords"]
            if not isinstance(shw, list) or \
                    not all(isinstance(x, str) for x in shw):
                raise ValueError(
                    "speed_hotwords deve essere una lista di regex")
            p.speed_hotwords = shw
        _set_int(p, raw, "speed_min_dim_k", minimum=0)
        if "speed_qualify_pct" in raw:
            p.speed_qualify_pct = _valid_pct(raw["speed_qualify_pct"],
                                             "speed_qualify_pct")

        profs = raw.get("profiles")
        if profs is not None:
            if not isinstance(profs, dict):
                raise ValueError("profiles deve essere una mappa profilo->opzioni")
            for pname, opts in profs.items():
                if not isinstance(opts, dict):
                    raise ValueError(f"profiles.{pname}: deve essere una mappa")
                if "step_up_pct" in opts:
                    p.profile_step_up_pct[pname] = _valid_pct(
                        opts["step_up_pct"], f"profiles.{pname}.step_up_pct")
                if "speed_min_dim_k" in opts:
                    v = opts["speed_min_dim_k"]
                    if isinstance(v, bool) or not isinstance(v, (int, float)) \
                            or v < 0:
                        raise ValueError(
                            f"profiles.{pname}.speed_min_dim_k non valido: {v!r}")
                    p.profile_speed_min_dim_k[pname] = int(v)
                if "speed_qualify_pct" in opts:
                    p.profile_speed_qualify_pct[pname] = _valid_pct(
                        opts["speed_qualify_pct"],
                        f"profiles.{pname}.speed_qualify_pct")

        als = raw.get("aliases")
        if als is not None:
            if not isinstance(als, dict):
                raise ValueError("aliases deve essere una mappa nome->nome")
            for k, v in als.items():
                if not isinstance(v, str) or not v:
                    raise ValueError(f"aliases.{k}: target deve essere una stringa")
                p.aliases[str(k)] = v

        aks = raw.get("alias_keys")
        if aks is not None:
            if not isinstance(aks, dict):
                raise ValueError("alias_keys deve essere una mappa alias->chiave")
            for k, v in aks.items():
                if str(k) not in p.aliases:
                    raise ValueError(
                        f"alias_keys.{k}: l'alias '{k}' non esiste in aliases")
                if not isinstance(v, str) or len(v.strip()) < 8:
                    raise ValueError(
                        f"alias_keys.{k}: la chiave deve avere almeno 8 caratteri")
                p.alias_keys[str(k)] = v.strip()

        cks = raw.get("client_keys")
        if cks is not None:
            if not isinstance(cks, dict):
                raise ValueError("client_keys deve essere una mappa profilo->chiave")
            for k, v in cks.items():
                if not isinstance(k, str) or not k.strip():
                    raise ValueError("client_keys: nome profilo non valido")
                if not isinstance(v, str) or len(v.strip()) < 8:
                    raise ValueError(
                        f"client_keys.{k}: la chiave deve avere almeno 8 caratteri")
                p.client_keys[k.strip()] = v.strip()

        # pricing per la stima costi del ledger (pattern glob -> USD/1M tok)
        pr = raw.get("pricing")
        if pr is not None:
            if not isinstance(pr, dict):
                raise ValueError("pricing deve essere una mappa pattern->costi")
            for pat, cfgp in pr.items():
                if not isinstance(pat, str) or not pat.strip():
                    raise ValueError("pricing: pattern non valido")
                if not isinstance(cfgp, dict) or (
                        "prompt_per_1m" not in cfgp
                        and "completion_per_1m" not in cfgp):
                    raise ValueError(
                        f"pricing.{pat}: servono prompt_per_1m e/o "
                        "completion_per_1m (USD per milione di token)")
                try:
                    pp = float(cfgp.get("prompt_per_1m") or 0)
                    cp = float(cfgp.get("completion_per_1m") or 0)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"pricing.{pat}: valori numerici richiesti") from None
                if pp < 0 or cp < 0:
                    raise ValueError(f"pricing.{pat}: valori >= 0")
                p.pricing[pat.strip()] = {"prompt_per_1m": pp,
                                          "completion_per_1m": cp}

        rm = raw.get("response_model")
        if rm is not None:
            if rm not in ("requested", "deployment", "upstream"):
                raise ValueError(
                    f"response_model non valido: {rm!r} "
                    "(ammessi: requested|deployment|upstream)")
            p.response_model = str(rm)

        ap = raw.get("adaptive_pick")
        if ap is not None:
            p.adaptive_pick = _coerce_bool(ap, "adaptive_pick")
        for num_key, attr in (("recency_halflife_sec", "recency_halflife_sec"),
                              ("latency_ref_ms", "latency_ref_ms")):
            nv = raw.get(num_key)
            if nv is not None:
                if isinstance(nv, bool) or not isinstance(nv, (int, float)) \
                        or nv <= 0:
                    raise ValueError(f"{num_key} non valido: {nv!r} "
                                     "(numero > 0 richiesto)")
                setattr(p, attr, float(nv))

        qj = raw.get("qc_json")
        if qj is not None:
            if not isinstance(qj, dict):
                raise ValueError("qc_json deve essere una mappa")
            # chiavi sconosciute IGNORATE (validazione soft);
            # max_attempts clampata 1..8; bool coerenti anche da stringa.
            if "enabled" in qj:
                p.qc_json.enabled = _coerce_bool(qj["enabled"],
                                                 "qc_json.enabled")
            if "strip_fences" in qj:
                p.qc_json.strip_fences = _coerce_bool(
                    qj["strip_fences"], "qc_json.strip_fences")
            if "annotate_reasoning" in qj:
                p.qc_json.annotate_reasoning = _coerce_bool(
                    qj["annotate_reasoning"], "qc_json.annotate_reasoning")
            if "retry_provider_4xx" in qj:
                p.qc_json.retry_provider_4xx = _coerce_bool(
                    qj["retry_provider_4xx"], "qc_json.retry_provider_4xx")
            if "watchdog_mark_no_done" in qj:
                p.qc_json.watchdog_mark_no_done = _coerce_bool(
                    qj["watchdog_mark_no_done"],
                    "qc_json.watchdog_mark_no_done")
            if "watchdog_cooldown_sec" in qj:
                v = qj["watchdog_cooldown_sec"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0 <= int(v) <= 3600):
                    raise ValueError("qc_json.watchdog_cooldown_sec deve "
                                     "essere 0..3600")
                p.qc_json.watchdog_cooldown_sec = int(v)
            if "max_attempts" in qj:
                v = qj["max_attempts"]
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError("qc_json.max_attempts deve essere un intero")
                p.qc_json.max_attempts = max(1, min(8, int(v)))
            if "stream_first_content_ms" in qj:
                v = qj["stream_first_content_ms"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (2000 <= int(v) <= 120000):
                    raise ValueError("qc_json.stream_first_content_ms deve "
                                     "essere 2000..120000")
                p.qc_json.stream_first_content_ms = int(v)
            if "stream_commit_min_chars" in qj:
                v = qj["stream_commit_min_chars"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0 <= int(v) <= 2000):
                    raise ValueError("qc_json.stream_commit_min_chars deve "
                                     "essere 0..2000")
                p.qc_json.stream_commit_min_chars = int(v)
            if "stream_total_deadline_ms" in qj:
                v = qj["stream_total_deadline_ms"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (5000 <= int(v) <= 600000):
                    raise ValueError("qc_json.stream_total_deadline_ms deve "
                                     "essere 5000..600000")
                p.qc_json.stream_total_deadline_ms = int(v)
            if "stream_commit_include_reasoning" in qj:
                p.qc_json.stream_commit_include_reasoning = _coerce_bool(
                    qj["stream_commit_include_reasoning"],
                    "qc_json.stream_commit_include_reasoning")
            if "stream_parachute_no_timeout" in qj:
                p.qc_json.stream_parachute_no_timeout = _coerce_bool(
                    qj["stream_parachute_no_timeout"],
                    "qc_json.stream_parachute_no_timeout")
            # stream_buffer_ms / stream_emit_error_tail / on_empty_response:
            # rimossi. Catena esaurita -> sempre 503 retryable, mai un turno
            # finto. Chiavi ignorate se presenti in un vecchio gateway.yaml.
        ph = raw.get("proactive_health")
        if ph is not None:
            p.proactive_health = _coerce_bool(ph, "proactive_health")
        hi = raw.get("health_interval_sec")
        if hi is not None:
            if isinstance(hi, bool) or not isinstance(hi, (int, float)) \
                    or hi < 60:
                raise ValueError("health_interval_sec deve essere >= 60")
            p.health_interval_sec = int(hi)

        # capacità (capability_routing)
        cr = raw.get("capability_routing")
        if cr is not None:
            if not isinstance(cr, dict):
                raise ValueError("capability_routing deve essere una mappa")
            if "enabled" in cr:
                p.capability_routing_enabled = _coerce_bool(cr["enabled"], "capability_routing.enabled")
            mc = cr.get("model_capabilities")
            if mc is not None:
                if not isinstance(mc, dict):
                    raise ValueError("capability_routing.model_capabilities deve essere una mappa")
                from .capabilities import CapabilitiesError, normalize_caps
                for k, v in mc.items():
                    if not isinstance(k, str) or not k:
                        raise ValueError("capability_routing.model_capabilities: chiavi non valide")
                    try:
                        normalize_caps(v, f"capability_routing.model_capabilities[{k}]")
                    except CapabilitiesError as exc:
                        raise ValueError(str(exc)) from exc
                p.model_capabilities = {str(k): list(v) for k, v in mc.items()}
            if "capabilities_default" in cr:
                try:
                    from .capabilities import normalize_caps, CapabilitiesError
                    p.capabilities_default = normalize_caps(
                        cr["capabilities_default"], "capability_routing.capabilities_default")
                except CapabilitiesError as exc:
                    raise ValueError(str(exc)) from exc
            if "image_token_estimate" in cr:
                v = cr["image_token_estimate"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    raise ValueError("capability_routing.image_token_estimate deve essere int >= 0")
                p.image_token_estimate = int(v)
            if "images_chat_fallback" in cr:
                p.images_chat_fallback = _coerce_bool(cr["images_chat_fallback"], "capability_routing.images_chat_fallback")
            mlr = cr.get("multimodal_last_resort")
            if mlr is not None:
                p.multimodal_last_resort = _coerce_bool(
                    mlr, "capability_routing.multimodal_last_resort")
            gsf = cr.get("gen_same_model_failover")
            if gsf is not None:
                p.gen_same_model_failover = _coerce_bool(
                    gsf, "capability_routing.gen_same_model_failover")
            dlf = cr.get("dims_ladder_floor")
            if dlf is not None:
                p.dims_ladder_floor = _coerce_bool(
                    dlf, "capability_routing.dims_ladder_floor")
            al = cr.get("auto_learn")
            if al is not None:
                if str(al) not in ("off", "suggest", "auto"):
                    raise ValueError("capability_routing.auto_learn non valido: "
                                     "ammessi off|suggest|auto")
                p.cap_auto_learn = str(al)
            alt = cr.get("auto_learn_threshold")
            if alt is not None:
                if isinstance(alt, bool) or not isinstance(alt, (int, float)) \
                        or not (1 <= int(alt) <= 50):
                    raise ValueError("capability_routing.auto_learn_threshold "
                                     "deve essere 1..50")
                p.cap_auto_learn_threshold = int(alt)

        # cooldown escalation
        ce = raw.get("cooldown_escalation")
        if ce is not None:
            p.cooldown_escalation = _coerce_bool(ce, "cooldown_escalation")
        _set_int(p, raw, "max_cooldown_sec", minimum=10)

        cm = raw.get("cooldown_mode")
        if cm is not None:
            if str(cm) not in ("linear", "exponential"):
                raise ValueError("cooldown_mode non valido: ammessi linear|exponential")
            p.cooldown_mode = str(cm)
        _set_int(p, raw, "cooldown_base_min", minimum=1)
        _set_int(p, raw, "cooldown_linear_mult_min", minimum=0)
        _set_int(p, raw, "ladder_skip_after", minimum=1)
        _set_int(p, raw, "ladder_stale_max", minimum=1)

        # budget guard (dict con chiavi note; sconosciute ignorate)
        bg = raw.get("budget_guard")
        if bg is not None:
            if not isinstance(bg, dict):
                raise ValueError("budget_guard deve essere una mappa")
            merged = dict(p.budget_guard)
            if "enabled" in bg:
                merged["enabled"] = _coerce_bool(bg["enabled"],
                                                 "budget_guard.enabled")
            for k in ("soft_factor", "min_per_min", "min_per_day"):
                if k in bg:
                    try:
                        v = float(bg[k])
                    except (TypeError, ValueError):
                        raise ValueError(
                            f"budget_guard.{k}: numero richiesto") from None
                    if v <= 0:
                        raise ValueError(f"budget_guard.{k}: > 0 richiesto")
                    merged[k] = v
            p.budget_guard = merged

        # gruppi capacità strutturali
        cg = raw.get("capability_groups")
        if cg is not None:
            if not isinstance(cg, dict):
                raise ValueError("capability_groups deve essere una mappa")
            if "enabled" in cg:
                p.cap_groups_enabled = _coerce_bool(cg["enabled"],
                                                    "capability_groups.enabled")
            om = cg.get("on_missing")
            if om is not None:
                if str(om) not in ("dynamic", "error"):
                    raise ValueError("capability_groups.on_missing non valido: "
                                     "ammessi dynamic|error")
                p.cap_groups_on_missing = str(om)

        # sanity QC
        qs = raw.get("qc_sanity")
        if qs is not None:
            if not isinstance(qs, dict):
                raise ValueError("qc_sanity deve essere una mappa")
            if "enabled" in qs:
                p.qc_sanity.enabled = _coerce_bool(qs["enabled"], "qc_sanity.enabled")
            if "min_chars" in qs:
                v = qs["min_chars"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0 <= int(v) <= 1000):
                    raise ValueError("qc_sanity.min_chars deve essere 0..1000")
                p.qc_sanity.min_chars = int(v)
            if "rotate_on_length_empty" in qs:
                p.qc_sanity.rotate_on_length_empty = _coerce_bool(
                    qs["rotate_on_length_empty"],
                    "qc_sanity.rotate_on_length_empty")

        return p

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        """Carica gateway.yaml; solleva eccezione chiara se invalido."""
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is not None and not isinstance(data, dict):
            raise ValueError(f"{path}: il YAML di root deve essere una mappa")
        return cls.from_dict(data)

    @classmethod
    def load_or_default(cls, path: str | Path) -> "Policy":
        """Come load(), ma file assente/corrotto -> policy di default (mai crash)."""
        path = Path(path)
        if not path.exists():
            log.info("[policy] %s assente: uso i default (comportamento legacy)",
                     path.name)
            return cls.default()
        try:
            pol = cls.load(path)
            log.info("[policy] %s caricata: step_up=%s%% aliases=%d "
                     "alias_keys=%d adaptive=%s speed_min=%dk "
                     "cap_routing=%s cap_patterns=%d",
                     path.name, pol.step_up_pct, len(pol.aliases),
                     len(pol.alias_keys), pol.adaptive_pick,
                     pol.speed_min_dim_k,
                     pol.capability_routing_enabled, len(pol.model_capabilities))
            return pol
        except Exception as exc:
            log.warning("[policy] %s INVALIDO (%s): uso i default",
                        path.name, exc)
            return cls.default()


# ------------------------------------------------------------------ helpers
def _valid_pct(value: Any, ctx: str) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not (1 <= value <= 200):
        raise ValueError(f"{ctx} deve essere un numero tra 1 e 200")
    return int(value)


def _set_int(obj: Policy, raw: dict, key: str, minimum: int = 0,
             maximum: int | None = None) -> None:
    if key not in raw:
        return
    v = raw[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < minimum \
            or (maximum is not None and v > maximum):
        raise ValueError(f"{key} non valido: {v!r}")
    setattr(obj, key, int(v))
