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

from .constants import SCORING_WEIGHTS as DEFAULT_SCORING_WEIGHTS

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
    rotate_on_length_truncated: bool = False  # True: risposta CON contenuto ma
                                              # finish_reason=length -> cooldown
                                              # del dep + fallback alle richieste
                                              # successive. Se il client ha
                                              # chiesto max_tokens e il modello
                                              # si e' fermato Esattamente lì, e'
                                              # il cap del client: nessun cooldown.


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
    # ADATTIVO: la finestra sul primo contenuto non e' piu' fissa ma legata
    # alla latenza storica (EMA, `_avg_latencies`) del deployment scelto:
    #   deadline = min(stream_first_content_ms, max(floor, mult * EMA))
    # Un dep che normalmente risponde in pochi secondi non blocca 180s se
    # stalla; un dep strutturalmente lento mantiene un margine proporzionato
    # (comunque <= stream_first_content_ms). Se EMA ignota -> si usa il cap.
    stream_first_content_adaptive: bool = True
    stream_first_content_mult: float = 3.0   # deadline = mult * EMA del dep
    stream_first_content_floor_ms: int = 20000  # pavimento (>=2000)
    # HEDGE sul primo contenuto (SOLO stream, SOLO catena FREDDA, solo 1
    # volta, mai verso bucket pagati -go/-fallback): se dopo questo ritardo
    # l'upstream scelto non ha ancora dato contenuto, si lancia in parallelo
    # un canary sul candidato successivo e impegna chi dei DUE produce
    # contenuto per primo (l'altro viene cancellato pre-byte, nessuna quota
    # di risposta sprecata oltre l'avvio). 0 = spento.
    stream_hedge_delay_ms: int = 1500
    # Calibrazione del hedge sul bucket: clamp(TTFT_bucket * frac, min, max).
    stream_hedge_ttft_frac: float = 0.6
    stream_hedge_min_ms: int = 800
    stream_hedge_max_ms: int = 2500
    # HEDGE cross-tier: canary su TIER diversi (fino a `tiers`, max 2 -> 3
    # richieste in volo con A) e gara a ogni rotazione finche' la warm non
    # aiuta. `max_races=0` = illimitato (limitato da deadline e max_tries).
    stream_hedge_cross_tier: bool = True
    stream_hedge_tiers: int = 2
    stream_hedge_max_races: int = 0
    stream_commit_min_chars: int = 40      # caratteri di RISPOSTA minimi per
                                           # impegnare lo stream (evita di
                                           # committare su 1 token poi morto);
                                           # un finish_reason con >0 char
                                           # committa comunque
    stream_total_deadline_ms: int = 180000  # tetto wall-clock su tutto il giro
    stream_commit_include_reasoning: bool = False  # True: bastano i reasoning
                                           # token per impegnare lo stream
    # PARACADUTE: sulla catena -go/-fallback (ULTIMO scaglione del ladder) il
    # timeout sul primo contenuto NON deve produrre un 503: la catena e' il
    # paracadute finale, non c'e' dove ruotare. True = lo stream parte comunque
    # (si trasmette quel che arriva). False = comportamento legacy (timeout ->
    # rotazione/503), utile solo se le catene hanno molti account validi.
    stream_parachute_no_timeout: bool = True
    # HOLD-UNTIL-FINISH: attivo di default (decisione post-incidente viemmegi
    # 2026-09-15): il gateway NON consegna al client finche' lo stream upstream
    # non e' chiuso PULITAMENTE (finish_reason stop/tool_calls o [DONE]):
    # niente risposte a meta'. Chiusure sporche o finish_reason=length (anche
    # a 0 caratteri) -> rotazione PRE-BYTE su un candidato piu' capace, senza
    # penalizzare il deployment troncato dal budget. Costo: si perde lo
    # streaming incrementale (i byte partono a risposta completa); la gara
    # hedge resta attiva ma hold-aware (solo upstream MUTI). La colonna CSV
    # `hold_until_finish` resta opt-in per-deployment (OR con questo valore:
    # con default True il hold non si puo' spegnere dal CSV).
    stream_hold_until_finish: bool = True
    stream_hold_idle_ms: int = 120000
    stream_hold_max_buffer_bytes: int = 50 * 1024 * 1024
    # --- STRUCT-OUT (#5): enforcement output strutturato ---
    struct_out_enabled: bool = True
    rewrite_content: bool = True      # pulisci fence/prosa dal JSON consegnato
    strict_schema: bool = False        # valida il content contro il JSON Schema
    repair_content: bool = True        # ripara schema-driven (mosse tool_repair)
    inject_response_format: bool = False  # inietta response_format (allow-list)
    inject_allow_providers: tuple[str, ...] = ()
    # degradazione gentile json_schema: rimuove response_format e inietta lo
    # schema nel prompt per i provider NON in native_schema_providers.
    downgrade_response_format: bool = True
    native_schema_providers: tuple[str, ...] = ("openai", "azure")


@dataclass
class Policy:
    """Tutto ciò che un giorno era una costante nel router, ora vive qui."""
    service_name: str = DEFAULT_SERVICE_NAME   # usato in API/log, configurabile
    proxy_prefix: str = DEFAULT_PROXY_PREFIX
    go_suffix: str = "-go"
    fallback_suffix: str = "-fallback"

    # Attribuzione app verso OpenRouter (header HTTP-Referer + X-Title):
    # OpenRouter serve i modelli :free SOLO se la richiesta arriva da un
    # "agentic harness" riconosciuto (app elencata su openrouter.ai/apps),
    # identificato tramite questi header. Il gateway è usato da opencode
    # (harness riconosciuto): valore di default adeguato, sovrapponibile in
    # gateway.yaml e/o via env OPENROUTER_APP_REFERER / OPENROUTER_APP_TITLE.
    openrouter_app_referer: str = "https://opencode.ai"
    openrouter_app_title: str = "opencode"

    # Prefissi STORICI riconosciuti come compatibili (OPZIONALI, default NESSUNO):
    # se valorizzati, le colonne del CSV che li usano vengono lette normalmente
    # e i nomi richiesti dai client vengono riscritti al prefisso corrente.
    # Vuoto = si accettano SOLO i nomi col prefisso attuale.
    legacy_prefixes: list[str] = field(default_factory=list)

    estimate_divisor: int = 4
    # Stima token adattiva (densita' per-blocco). shadow=True calcola e logga
    # entrambe ma usa la legacy finche' non si abilita adaptive_enabled.
    estimate_adaptive_enabled: bool = False
    estimate_adaptive_shadow: bool = True
    # AUTO-ADAPTIVE: attiva la stima adattiva da sola quando i campioni shadow
    # accumulati (che ora SOPRAVVIVONO ai restart) mostrano un delta medio
    # <= auto_max_delta_pct su almeno auto_min_n richieste. Serve a chiudere
    # il rollout senza finestre di traffico dedicate ne' interventi manuali;
    # estimate_adaptive_enabled resta il master switch manuale.
    estimate_adaptive_auto_enable: bool = True
    estimate_adaptive_auto_min_n: int = 200
    estimate_adaptive_auto_max_delta_pct: float = 5.0
    # Calibrazione closed-loop del divisore dal VERO prompt_tokens upstream
    # (alpha dell'EMA sull'errore relativo; 0 = disattiva).
    estimate_calib_alpha: float = 0.05
    # TTL (secondi) della cache in-memory delle GET {endpoint}/models: una
    # chiamata per endpoint (prima chiave valida), condivisa tra audit, health
    # e probe. 0 = nessuna cache (una GET per endpoint ad ogni esecuzione).
    provider_models_ttl_sec: int = 300
    # Floor minimo (secondi) del cooldown applicato sui 429 quando il provider
    # indica un Retry-After troppo piccolo/assente: evita loop di 429
    # ravvicinati. 0 = nessun floor (si usa il valore del provider).
    retry_after_min_sec: float = 10.0
    # Floor Retry-After specifici per provider (provider -> secondi), es.
    # {groq: 5, google: 30, openrouter: 15}. Se un provider non e' in mappa
    # vale retry_after_min_sec.
    retry_after_floor_by_provider: dict[str, float] = field(
        default_factory=dict)
    # Watchdog inter-chunk dello streaming (secondi): se l'upstream non manda
    # alcun byte per N secondi a stream avviato -> StreamStallError -> failover
    # (pre-byte) / cooldown (post-byte). 0 = disabilitato.
    stream_stall_sec: float = 20.0
    # F21: stall guard CALIBRATO sul TTFT del bucket di contesto:
    # stall_eff = max(stream_stall_sec, min(TTFT_p50_bucket * mult, max_sec)).
    # Sui light resta ~stream_stall_sec; sugli heavy (prefill lungo) si allarga
    # fino a max_sec. mult 0 = calibrazione spenta (fisso come prima).
    stream_stall_ttft_mult: float = 2.5
    stream_stall_max_sec: float = 60.0
    # Graceful shutdown: attesa massima (secondi) del drain delle richieste in
    # volo prima del flush finale del ledger. 0 = non attendere.
    shutdown_drain_sec: float = 10.0
    # Time-decay dei punteggi di reputazione (_base/_provider/_key_scores):
    # halflife in secondi verso lo zero. Rende la reputazione reattiva ai
    # cambi di qualita' recenti invece di proteggere uno storico gonfio.
    # 0 o negativo = nessun decay (comportamento storico).
    reputation_decay_halflife_sec: float = 129600.0
    # Timeout upstream ADATTIVO per-deployment in base alla latenza media
    # storica: read = clamp(max(floor, avg_ms/1000 * multiplier), floor, max).
    # Provider veloci vengono tagliati presto se si bloccano; i lenti hanno
    # spazio per rispondere. False = timeout globale fisso.
    adaptive_timeout_enabled: bool = True
    adaptive_timeout_floor_sec: float = 30.0
    adaptive_timeout_multiplier: float = 10.0
    adaptive_timeout_max_sec: float = 600.0
    # Pool HTTP persistente PER-ORIGINE (client httpx dedicato per host:port,
    # riuso TCP/TLS fino a 120s, condividendo lo stesso client tra tutte le
    # chiavi di quel provider). DISATTIVATO di default: il forwarder usa un
    # unico client condiviso (comportamento storico). Attivandolo, le chiavi
    # di uno stesso host condividono le connessioni keep-alive (piu' veloce
    # ma con un rischio: un provider puo' legare l'auth alla connessione e
    # bocciare un cambio di chiave su una connessione altrui).
    http_keepalive_pool: bool = False
    # Probe passivo dei deployment dormienti: quando non ci sono chiavi vive,
    # un deployment in cooldown da >= cooldown_probe_after_ratio del suo tempo
    # viene ritentato come "probe": il successo lo riabilita subito, il
    # fallimento raddoppia il cooldown. `cooldown_probe_decay` fa decadere
    # linearmente la penalita' (EMA latenza/successo) col passare del tempo.
    cooldown_probe_enabled: bool = True
    cooldown_probe_after_ratio: float = 0.5
    cooldown_probe_decay: bool = True
    # Decadimento del fail_streak per inattivita' (halflife in secondi):
    # dopo N secondi senza fallimenti lo streak si dimezza, cosi' una chiave
    # riattivata dopo ore riparte con una fedina quasi pulita. 0 = off.
    cooldown_streak_halflife_sec: float = 1800.0
    # Auto-retirement dopo N probe passivi consecutivi falliti (problema
    # permanente: chiave morta). 0 = off.
    probe_retire_after: int = 5
    # Jitter simmetrico RANDOM sui cooldown (0.12 = +/-12%). 0 = off.
    # DEFAULT 0: sostituito dallo spread ADDITIVO DETERMINISTICO
    # `cooldown_jitter_sec_max` (stabile tra restart, anti-herd sui gemelli).
    cooldown_jitter_ratio: float = 0.0
    # Jitter DETERMINISTICO per-unique: spread additivo 0..N secondi calcolato
    # come sha256(unique) — i gemelli che incassano 429 nello stesso secondo
    # non scadono tutti al medesimo millisecondo. 0 = off. Default 2.0s.
    cooldown_jitter_sec_max: float = 2.0
    # SOGLIA "LENTO" SIZE-AWARE (B3 ibrida): lento = oltre
    # max(slow_latency_abs_floor_ms, slow_latency_rel_mult * atteso), dove
    # l'atteso e' la MEDIANA DI FLOTTA del bucket di contesto -> stima dal
    # rate di prefill/generazione -> 90s legacy. Cosi' un 128k che risponde
    # in 100s (normale) NON e' lento; lo e' uno che fa il doppio della norma.
    slow_latency_abs_floor_ms: int = 45000
    slow_latency_rel_mult: float = 2.0
    slow_latency_min_peers: int = 5
    # ANTI-SPRECO della caccia al sostituto: dopo una caccia senza guadagno
    # (il buono non esiste) niente altre gare per la sessione/bucket nel
    # backoff; cap di cacce per finestra come rete.
    hunt_backoff_sec: int = 600
    hunt_max_per_window: int = 5
    hunt_window_sec: int = 3600
    # Classi di errore (F18): durata cooldown dedicata per categoria.
    # 503/529/500 = dep sovraccarico/transitorio -> breve; timeout -> breve
    # dedicato; 429 = quota -> soft per-chiave (durate dal Retry-After).
    # False = comportamento storico (escalation + timeout_cooldown_mult).
    error_class_cooldowns: bool = True
    cooldown_transient_sec: int = 15
    cooldown_timeout_sec: int = 60
    # F25: breaker PROATTIVO per provider|modello. Se lo stesso modello prende
    # 5xx (500/502/503/504/529) da almeno `model_circuit_keys` CHIAVI diverse
    # entro `model_circuit_window_sec`, il problema e' il modello: lo si salta
    # per tutti i suoi deployment per `model_circuit_open_sec` (skip soft, zero
    # penale reputazionale; scaduto il tempo si riprova).
    model_circuit_enabled: bool = True
    model_circuit_keys: int = 3
    model_circuit_window_sec: int = 60
    model_circuit_open_sec: int = 60
    # Autoprobe dei cooldown triggerato da una chiamata (solo gruppi -dim
    # testo). Parte fire-and-forget, senza entrare nella risposta: se ci sono
    # deployment MAI USATI nelle ultime `cooldown_autoprobe_fresh_age_sec`
    # (24h) sonda PRIMA quelli con un probe "normale" (note_result/mark_failed)
    # cosi' i buoni salgono in cima alla classifica; se non ci sono freschi
    # ripiega sul comportamento classico: sonda i deployment dormienti piu'
    # "pronti" e, se rispondono, li risveglia (clear_cooldown). Sul fallimento
    # allunga il cooldown di cooldown_autoprobe_grow_sec cosi' i bersagli
    # ruotano tra le chiamate (nel modo classico non tocca note_result/
    # mark_failed: non avvelena la rotazione adattiva).
    cooldown_autoprobe_enabled: bool = True
    # Conservativo: pochi probe, il piu' possibile 'a prova di quota'
    # (i free-tier contano richieste/giorno, non token).
    cooldown_autoprobe_per_dim: int = 1
    cooldown_autoprobe_min_age_sec: float = 300.0
    cooldown_autoprobe_grow_sec: float = 120.0
    cooldown_autoprobe_min_gap_sec: float = 60.0
    cooldown_autoprobe_max_total: int = 3
    cooldown_autoprobe_timeout_sec: float = 45.0
    cooldown_autoprobe_fresh_age_sec: float = 86400.0
    # F32: la stessa CHIAVE (anche su deployment diversi) non deve essere
    # sondata prima di questo gap: l'autoprobe non deve martellare lo stesso
    # conto, che sia una quota giornaliera o un rate-limit per chiave.
    cooldown_autoprobe_key_gap_sec: float = 3600.0
    # Budget di probe per CHIAVE nelle 24h (provider-aware nel codice:
    # openrouter/llm7/etc. contano le richieste, quindi 1 solo probe/giorno
    # anche con N modelli sulla stessa chiave). NOTTE-SOLO (regola utente
    # 2026-09-15, dopo il ban IP di llm7.io su scalifai): 1/giorno.
    cooldown_autoprobe_key_day_max: int = 1
    # QUANDO sondera': "nightly" = SOLO il giro delle 00:00 locali (niente
    # probe scatenati dalle richieste), "request" = vecchio comportamento
    # fire-and-forget a ogni chiamata.
    cooldown_autoprobe_schedule: str = "nightly"
    # Se una chiave ha servito traffico REALE con successo da meno di
    # questo tempo, e' viva: sondarla e' spreco di quota -> si salta.
    cooldown_autoprobe_key_ok_fresh_sec: float = 43200.0
    # GIRO GIORNALIERO SUI RITIRATI: nessun ritiro e' definitivo. Al primo
    # tick dopo mezzanotte locale l'autoprobe sonda TUTTI i ritirati in
    # sequenza, con un ritmo lento (retired_gap_sec fra un probe e l'altro) e
    # rispettando il gap per-chiave: un probe riuscito li riabilita, un KO li
    # lascia fuori fino al giro dopo. Nessun martellamento dei conti.
    cooldown_autoprobe_retired_enabled: bool = True
    cooldown_autoprobe_retired_gap_sec: float = 20.0    # "Grazia" per i KO TRANSITORI del probe (5xx, timeout/rete, altri 4xx):
    # cooldown MODESTO al posto dello skip (ruotiamo comunque, ma senza
    # bruciare il grow pieno). I KO definitivi e i 429 usano sempre grow.
    cooldown_autoprobe_transient_sec: float = 30.0
    # ESCLUSIONE: i deployment con cooldown (residuo efficace) SUPERIORE a
    # questa soglia NON vengono sondati dall'autoprobe: sono "troppo rotti",
    # li lasciano al tempo o alla ULTIMA SPIAGGIA della scala.
    cooldown_autoprobe_skip_over_sec: float = 7200.0
    # MOLTIPLICATORE del cooldown di un KO del probe: l'incremento base
    # (grow/transient) viene moltiplicato per il numero di probe fatti su quel
    # deployment nelle ultime 24h (>=1). Piu' lo si riprova e piu' lo si fa
    # dormire, senza resettare il residuo.
    cooldown_autoprobe_multiply_24h: bool = True
    # CRISIS MODE autoprobe: se la quota di deployment dim in cooldown supera
    # `cooldown_autoprobe_crisis_ratio`, il pass raddoppia `per_dim` e dimezza
    # `min_gap` per risvegliare il pool piu' in fretta sotto pressione.
    cooldown_autoprobe_crisis_enabled: bool = True
    cooldown_autoprobe_crisis_ratio: float = 0.30
    cooldown_autoprobe_crisis_mult: float = 2.0
    # Probe immediato dei deployment APPENA AGGIUNTI via hot-reload CSV:
    # fire-and-forget prima del traffico reale; OK -> deployment caldo
    # (note_result), KO -> cooldown breve. `_max` limita i probe per reload.
    hotreload_probe_enabled: bool = True
    hotreload_probe_max: int = 20
    hotreload_probe_timeout_sec: float = 15.0
    hotreload_probe_cooldown_sec: float = 300.0
    # CONNECTION DRAINING su hot-reload: i deployment rimossi dal CSV con
    # richieste in volo restano marcati draining (ignorati da pick_deployment)
    # finche' l'inflight non torna a zero o scade questo TTL massimo.
    hotreload_drain_ttl_sec: float = 120.0
    # Inflight request coalescing (solo non-streaming): richieste identiche
    # (stesso payload+profilo) in volo condividono una sola chiamata upstream.
    request_coalescing_enabled: bool = True
    request_coalescing_ttl_sec: float = 60.0
    request_coalescing_max_waiters: int = 10
    # Finestra POST-risposta: un payload identico arrivato entro N secondi dal
    # completamento del leader riceve la stessa risposta (deepcopy) senza
    # ripetere la chiamata upstream (costo/crediti dimezzati nei retry e nei
    # subagenti in rapida sequenza). 0 = spento (solo coalescing in-flight).
    request_coalescing_cache_sec: float = 0.0
    # Sessioni anonime: se il client non invia alcun id di sessione ne'
    # `user`/`metadata.session_id`, il gateway deriva un id deterministico
    # `fq_<sha1(system+primo user+user-agent)>` dal prefisso della
    # conversazione, cosi' anche client come Hermes ottengono sticky/cache.
    # False = lascia la sessione anonima (comportamento storico).
    anon_session_fingerprint: bool = True
    # Il fingerprint anonimo hasha solo i PRIMI N caratteri del system prompt:
    # molti agenti (Hermes) accodano timestamp/contesto variabile che
    # cambierebbe l'hash ad ogni turno. Troncare mantiene la sticky calda.
    # 0 = usa tutto il system prompt.
    anon_session_fp_system_chars: int = 768
    sticky_ttl_sec: int = 3600
    sticky_handoff_same_family: bool = True
    cooldown_sec: int = 600
    hotwords_window: int = 3
    hotwords: list[str] = field(default_factory=lambda: list(DEFAULT_HOTWORDS))
    # Pesi del sistema di reputazione (prima hardcoded in constants.SCORING_WEIGHTS).
    # Punteggio piu' BASSO = meglio. Chiavi ammesse: ATTEMPT_PROVIDER,
    # ATTEMPT_KEY, FAIL_DEPLOYMENT, FAIL_TRANSIENT, FAIL_PROVIDER, FAIL_KEY,
    # SUCCESS_DEPLOYMENT, SUCCESS_PROVIDER, SUCCESS_KEY.
    scoring_weights: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SCORING_WEIGHTS))

    # -------------------------------------------------------- routing tuning
    # Parametri di rotazione adattiva che prima erano costanti hardcoded in
    # router.py. Ogni valore ha un default identico al valore storico; il
    # router li legge DA POLICY con fallback al valore costante, cosi' il
    # comportamento NON cambia se non configurato esplicitamente.
    latency_rotate_threshold_ms: int = 90000   # soglia HARD (demote per tutti)
    soft_slow_latency_ms: int = 60000          # soglia SOFT (solo ctx pesanti)
    soft_slow_ctx_min: int = 30000
    # Bordi DESTRI dei bucket di contesto: <8000->0; 8000..31999->1;
    # 32000..127999->2; >=128000->3.
    ctx_bucket_edges: list[int] = field(default_factory=lambda: [8000, 32000, 128000])
    ttft_rate_min_ctx: int = 8000
    ttft_rate_floor_ms: float = 250.0
    slow_latency_abs_floor_ms: float = 45000.0
    slow_latency_rel_mult: float = 2.0
    slow_latency_min_peers: int = 5
    slow_gen_mult: float = 6.0
    slow_typical_completion_tokens: float = 600.0
    slow_rel_baseline_mult: float = 2.0
    effort_capable_bonus: float = 1.5
    latency_penalty_per_sec: float = 0.5
    # ---------------------------------------------------------- ops tuning
    # Parametri operativi di admin (prima hardcoded): probe di validazione
    # chiavi e playground di prova. Default = valori storici.
    probe_concurrency: int = 5
    probe_timeout_sec: float = 20.0
    playground_timeout_sec: float = 90.0
    playground_max_attempts: int = 128
    # Durate dei cooldown "di categoria" (prima hardcoded in forwarder.py).
    # Default = valori storici; modificabili via gateway.yaml senza restart.
    model_missing_cooldown_sec: int = 86400
    quota_min_cooldown_sec: float = 600.0
    quota_max_cooldown_sec: float = 604800.0        # 7 giorni
    provider_transient_cooldown_sec: float = 60.0
    permission_denied_cooldown_sec: float = 1800.0
    stream_loop_cooldown_sec: float = 300.0
    retry_body_cap_sec: float = 300.0
    min_output_floor: int = 4096
    # ------------------------------------- runtime/memoria & limiti vari
    # Cap e TTL di strutture in memoria + soglie di classificazione/limiti di
    # sicurezza (prima hardcoded in main/keyhealth/ctxcompact/toolrepair/sniff).
    # Default = valori storici; nessun cambiamento di comportamento.
    coalesce_cache_max: int = 64
    video_job_ttl_sec: int = 86400
    keyhealth_streak_dead_threshold: int = 5
    keyhealth_success_ema_floor: float = 0.1
    ctxcompact_min_protected_msgs: int = 8
    toolrepair_max_unwrap_depth: int = 5
    sniff_max_b64_chars: int = 2048
    sniff_max_str_chars: int = 20000
    sniff_max_sse_bytes: int = 1500000
    # ------------------------------------- HTTP upstream (forwarder)
    # Timeout e pool connessioni del client httpx verso gli upstream, prima
    # hardcoded in forwarder.py. Default = valori storici.
    upstream_connect_timeout_sec: float = 10.0
    upstream_read_timeout_sec: float = 180.0
    upstream_write_timeout_sec: float = 30.0
    upstream_pool_timeout_sec: float = 10.0
    upstream_max_keepalive_connections: int = 30
    upstream_max_connections: int = 100
    upstream_keepalive_expiry_sec: float = 120.0
    # None = usa il default storico ({408,409,429} ∪ 5xx). Se impostato,
    # SOSTITUISCE l'insieme dei codici considerati ritentabili.
    retryable_status_codes: list[int] | None = None
    # None = usa il default storico ("api.groq.com",). Se impostato,
    # SOSTITUISCE la lista degli host incompatibili con i modelli "effort".
    effort_incompatible_hosts: list[str] | None = None

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

    # EFFORT/reasoning: quando il client chiede un effort esplicito
    # (`reasoning_effort`), il router bias-a la scelta verso l'intelligence
    # (vedi router._reputation_score) e il forwarder inietta `reasoning_effort`
    # SOLO sui deployment effort_capable. Qui l'override di temperatura,
    # applicato SOLO se abilitato e SOLO se il client non ha inviato
    # `temperature` (il client vince sempre).
    enable_effort_temperature_override: bool = True
    effort_temperature_overrides: dict[str, float] = field(
        default_factory=lambda: {"low": 1.0, "medium": 0.7, "high": 0.2})
    effort_intel_weight: float = 10.0

# DYNAMIC SCORING: feature osservate per-deployment (latency p95, error rate, throughput)
    # per aggiustare il punteggio di reputazione oltre l'EMA di latenza base.
    dynamic_scoring_enabled: bool = True
    dynamic_scoring_latency_p95_weight: float = 1.0   # peso per latency p95 (ms/1000)
    dynamic_scoring_error_rate_weight: float = 2.0    # peso per error rate (0-1 * 100)
    dynamic_scoring_throughput_weight: float = 0.5    # peso per throughput (tok/s / 100)
    dynamic_scoring_history_window: int = 100         # campioni per EMA dinamica

    # Provider bias normalization: "log" (default), "sqrt", "none"
    provider_bias_normalization: str = "log"

    # CIRCUIT BREAKER per API Key: previene il martellamento di chiavi rotte/esauste
    circuit_breaker_enabled: bool = True
    circuit_breaker_threshold: int = 5          # fallimenti consecutivi per aprire
    circuit_breaker_timeout: float = 60.0       # secondi prima di half-open
    circuit_breaker_half_open_requests: int = 3  # successi in half-open per chiudere
    # Ambito: "hybrid" = breaker per-deployment sempre + per-chiave solo per
    # errori di chiave (401/402/403/429); "dep" = solo deployment; "key" = legacy.
    circuit_breaker_scope: str = "hybrid"
    # COLD START reputation: il punteggio di partenza di un deployment senza
    # cronologia e' `-(preferenza × model_preference_base)` (default 10 ->
    # pref=100 parte a -1000, pref=-100 a +1000). La preferenza domina DA
    # FREDDO; solo cooldown/retirement escludono davvero. 0 = nessun seed.
    model_preference_base: float = 10.0

    # THOUGHT_SIGNATURE (Gemini 3): Google pretende il blob `thought_signature`
    # sui functionCall del turno CORRENTE. Se la history arriva da un altro
    # modello (rotazione) la firma reale non esiste: Google documenta due firme
    # dummy — "skip_thought_signature_validator" e
    # "context_engineering_is_the_way_to_go" — che SALTANO la validazione
    # (qualita' di reasoning inferiore, nessun 400). Con dummy_fill attivo
    # Gemini resta sempre eleggibile nel routing, come un provider qualsiasi.
    thought_sig_dummy_fill: bool = True
    thought_sig_dummy_value: str = "skip_thought_signature_validator"

    # TOOL_REPAIR: riparazione argomenti tool-call upstream.
    # Default aggressive su tutti i deployment; Google/Gemini off di default.
    tool_repair_enabled: bool = True
    tool_repair_default_level: str = "aggressive"
    tool_repair_disable_for_google: bool = True
    tool_repair_max_args_size: int = 100000
    tool_repair_annotate_reasoning: bool = False
    # FAKE_CALL: tool-call resi come testo -> escalation diretta -go/-fallback.
    tool_repair_fake_call_enabled: bool = True
    tool_repair_fake_call_patterns: tuple[str, ...] = ()
    tool_repair_fake_call_max_escalations: int = 2
    tool_repair_fake_call_hold_max_bytes: int = 4096
    tool_repair_fake_call_hold_timeout_ms: int = 4000

    # HISTORY_NORMALIZE (#1): compatibilita' STRUTTURALE della copia messages
    # inviata all'upstream (orfani tool/tool_call_id, call pendenti, vuoti,
    # system duplicati). Cache-safe: opera solo sulla coda.
    history_normalize_enabled: bool = True
    history_normalize_tail_only: bool = True
    history_normalize_drop_orphan_tool: bool = True
    history_normalize_drop_dangling_tool_calls: bool = True
    history_normalize_drop_empty_assistant: bool = True
    history_normalize_dedupe_system: bool = True
    # Frontiera lazy del reasoning (histnorm): -1 mai, 0 rimuovi, >0 tronca.
    history_normalize_reasoning_content_max_chars: int = 0
    history_normalize_reasoning_keep_recent: int = 1

    # SAMPLING_DEFAULTS (#2A): default a basso rischio, client vince sempre.
    sampling_enabled: bool = True
    sampling_allow_providers: tuple[str, ...] = ("*",)
    sampling_provider_params: dict = field(
        default_factory=lambda: {"*": {"top_p": 0.95}})
    # LOOP_DETECTOR (#2B): loop testuale/tool-call -> deployment fallita,
    # si prosegue sulla dim successiva del ladder.
    loop_detector_enabled: bool = True
    loop_ngram_size: int = 8
    loop_repeats: int = 3
    loop_toolcall_repeat: int = 2
    loop_min_tokens: int = 16
    # Streaming: loop detector ON-THE-FLY sul buffer circolare degli ultimi
    # N token di contenuto. Con un modello in loop degenere il kill arriva
    # in pochi secondi invece di aspettare lo stall watchdog (che non scatta
    # mai se il modello continua a produrre output). 0 disabilita il check.
    loop_stream_buffer_words: int = 200

    # CONCURRENCY LIMIT dinamico (#C): connessioni concorrenti per
    # deployment. Se inflight >= limite, il deployment e' saturo per le NUOVE
    # richieste (le in volo proseguono). Il limite e' appreso empiricamente:
    # parte da conc_default_limit e sale di 1 ogni conc_learn_success_streak
    # successi consecutivi a saturazione (max conc_max_limit); un errore di
    # concorrenza (429/503) lo dimezza (min 1). Resetta al default al restart.
    conc_default_limit: int = 3
    conc_max_limit: int = 10
    conc_learn_success_streak: int = 20
    # concorrenza PESATA A TOKEN: budget di prefill in volo per dep =
    # max_input * conc_token_ratio. Un heavy non parte se un altro e' gia'
    # in volo sullo stesso dep; N light passano in parallelo. 0 = spento
    # (si torna al conteggio delle richieste).
    conc_token_ratio: float = 0.5

    # CORRECTIVE_RETRY (#3): 1 tentativo correttivo, solo non-streaming,
    # su fallimenti di contenuto/formato (non timeout).
    corrective_retry_enabled: bool = True
    corrective_retry_max_attempts: int = 1

    # TEXT_TOOLCALL (#6): parser dei tool-call resi come testo (davanti a
    # fakecall). require_declared_name: il name deve combaciare con tools[].
    text_toolcall_enabled: bool = True
    text_toolcall_require_declared_name: bool = True
    text_toolcall_allow_formats: tuple[str, ...] = ()
    text_toolcall_max_bytes: int = 200000
    text_toolcall_hold_until_close: bool = True
    text_toolcall_fallback_to_escalation: bool = True
    # TOOLCALL_TRUNCATION: una risposta che si chiude con un tag tool-call
    # APERTO e mai chiuso (`<tool_call>...` senza close) e' troncata o
    # allucinata. Il tag rotto NON deve uscire: il gateway trattiene la coda,
    # salva la chiamata parziale (nome+args -> tool_calls strutturata) cosi'
    # l'agente continua, altrimenti ruota in modo trasparente. Deployment in
    # cooldown breve (default 30s). holdback: attiva il trattenimento coda.
    toolcall_truncation_enabled: bool = True
    toolcall_truncation_cooldown_sec: int = 30
    toolcall_truncation_holdback: bool = True
    # SESSION-DEP GUARD (anti-usurpazione cross-sessione): ricorda l'ULTIMA
    # sessione che ha servito con successo ogni deployment FREE-dims; se un
    # deployment e' stato usato con successo da un'ALTRA sessione meno di
    # `session_dep_guard_sec` fa, viene IGNORATO fra i vivi e reso eleggibile
    # solo in un tier "pre-ultima-spiaggia" (prima del -fallback a pagamento,
    # ordinato per max_input crescente). Direzionale (ultimo successo):
    # due sessioni che partono nello stesso istante possono collidere una
    # volta, poi la vincente tiene il deployment. In-memory, mai persistito.
    #
    # 60 min (era 15): questa finestra e' anche quella che tiene in vita
    # l'owner per il PRESTITO dei warm e il warm_pool ttl (vedi sotto). Serve
    # al caso "cronjob": il primo giro senza warm produce 3+ caldi, finisce,
    # e un secondo giro che parte mezz'ora dopo li trova ancora prestabili
    # (tutto pronto, zero canary) invece di lasciarli tornare liberi.
    session_dep_guard_enabled: bool = True
    session_dep_guard_sec: int = 3600
    # WARM POOL (tier "caldi"): PRIMA del -dim richiesto e della scala, si
    # esauriscono i FREE-dims che QUESTA sessione ha gia' servito con successo
    # (ancora vivi, non in cooldown, che reggono need+max_input). Riusa la
    # finestra di session_dep_guard_sec (rinnovata a ogni attivita'), cosi' e'
    # coerente con _attached_unique. `ttl_sec=0` -> usa session_dep_guard_sec;
    # `max_attempts=0` -> illimitato (bounded dal set `tried`). L'ordine
    # interno e' cache-holder, poi MRU (last_used), poi order, poi max_input.
    warm_pool_enabled: bool = True
    warm_pool_ttl_sec: int = 0
    warm_pool_max_attempts: int = 0
    # WARM-REFILL A CASCATA: finche' la sessione ha MENO di `warm_ready_min`
    # caldi che possono EFFETTIVAMENTE servire la richiesta (need + ctx +
    # output assicurato: dep_deliverable), ogni richiesta reale lancia una
    # gara 2-alla-volta (A + 1 canary NUOVO, solo free-dims, libero da
    # qualsiasi sessione, api_key diversa, stesso tier CSV `order` prioritario)
    # consegnando sempre il piu' veloce e SENZA MAI CANCELLARE i perdenti:
    # finiscono in background come PROBE REALI e ogni risposta completa pulita
    # entra in warm. 0 = feature off. `default_out_tokens` = budget output
    # presunto quando il client non lo chiede.
    warm_refill_enabled: bool = True
    warm_ready_min: int = 3
    # WARM-READY ADATTIVO AL RATE DELLA SESSIONE: la soglia base
    # (`warm_ready_min`) sale con la media rpm della sessione su
    # `warm_ready_rpm_window_sec`. Ogni gradino di `warm_ready_rpm_step` rpm
    # OLTRE `warm_ready_rpm_base` vale +1, fino a `warm_ready_min_max`.
    #   ready_eff = min(warm_ready_min + ceil((rpm-base)/step), min_max)
    # Con i default: rpm<=5 -> 3, >5 -> 4, >10 -> 5, >15 -> 6 (cap).
    # Il confronto resta sui warm "validi per la richiesta" (need+ctx+output),
    # prestiti inclusi: se la sessione ha ancora prestabili utilissimi il
    # canary NON parte. False = soglia fissa (comportamento legacy).
    warm_ready_rpm_adaptive: bool = True
    warm_ready_rpm_window_sec: int = 180
    warm_ready_rpm_base: float = 5.0
    warm_ready_rpm_step: float = 5.0
    warm_ready_min_max: int = 6
    warm_refill_default_out_tokens: int = 4096
    # CODA DEI PROVIDER GIA' IN WARM: le chiavi gia' in warm restano escluse
    # SEMPRE; ma un candidato il cui PROVIDER (colonna `provider`) e' gia'
    # rappresentato nel warm di UNA QUALSIASI sessione non viene scartato:
    # finisce in CODA, dopo tutti i provider non ancora in warm, cosi' si
    # sfruttano tutti i provider riducendo il rischio di ban per-provider.
    canary_warm_last: bool = True
    # Modelli PREFERITI nei bucket -go/-fallback (lista separata da virgole o
    # YAML list): se nel bucket c'e' ALMENO una chiave viva di uno di questi
    # modelli, la scelta cade su quella (regola utente: "-go sempre
    # deepseek-v4.1-flash se disponibile"); altrimenti comportamento normale.
    go_preferred_models: str = ""
    # TETTO DI SPECULATIVO IN VOLO PER SESSIONE: canari refill/legacy e
    # A/loser staccati come probe contano TUTTI; il gate del refill non
    # accende un altro canario se la sessione ne ha gia' `max_inflight` in
    # corsa. Nel caso migliore il warm si trova anche 6-7 caldi transienti
    # (nessun vero spreco: se arrivano tutti buoni "durera' di piu'").
    warm_refill_max_inflight: int = 6
    # PRESTITO DEI WARM ("passaggio di mano"): quando piu' sessioni (es. i
    # subagent di Hermes, ognuno con prompt separato) si accumulano, ognuna
    # "sequestra" i propri warm e i canary delle altre non trovano piu' nulla
    # di buono. Un warm di un'ALTRA sessione il cui DEPLOYMENT (non la
    # sessione!) e' fermo da almeno `borrow_idle_sec` e non ha richieste in
    # volo viene considerato "prestabile": conta nei 3 ready (cosi' il canary
    # non parte) ed entra nel pool IN CODA, dopo i propri (priorita' di
    # consumo: propri >> condivisi in disuso). Al primo successo su un
    # prestato la proprieta' si trasferisce da sola (note_session_success) e
    # il vecchio owner, ricontando, decidera' se gli serve un canary.
    # `selectable=False` = i prestati contano SOLO per i 3 ready.
    warm_borrow_enabled: bool = True
    warm_borrow_idle_sec: float = 240.0
    warm_borrow_selectable: bool = True
    # GARA LENTA: se il primo tentativo sta ANCORA generando dopo
    # `*_slow_race_after_ms` (0 = spenta) parte `stream_slow_race_canaries`
    # (default 1) canario e la gara va fino alla chiusura. Al client va chi
    # consegna PRIMA (nessuna regola nuova), ma l'ELEZIONE per la richiesta
    # successiva va a chi ha IMPIEGATO MENO nel proprio tentativo (tempo
    # proprio, non "chi e' arrivato prima"): il piu' veloce diventa holder,
    # il piu' lento viene marcato "lento per la sessione". Nessuna
    # generazione viene mai buttata: i perdenti restano probe reali.
    stream_slow_race_after_ms: int = 120000
    nonstream_slow_race_after_ms: int = 120000
    stream_slow_race_canaries: int = 1
    # UN DEP CHE IGNORA stream:true (risponde JSON) viene ADATTATO a SSE dal
    # forwarder e CONSEGNATO: non e' un guasto, e' una consegna diversa. Con
    # True (default) resta eleggibile come canario / sveglia / sostituto in
    # gara: si giudica la CONSEGNA finale, non la forma del transport (regola
    # utente: "sia stream che non stream indistintamente"). False = pool
    # sostituti limitato ai soli dep che parlano SSE nativo.
    nonstream_canary_allowed: bool = True
    # SVEglia: il terzo canario del refill cerca un dep in cooldown da 429 da
    # ALMENO questo tempo (default 1h) e, se risponde, lo riporta caldo.
    warm_refill_wake_min_cooldown_age_sec: float = 3600.0
    # Tentativi di risveglio per giro (parametrizzabile): ognuno su una
    # api_key DIVERSA da tutte le sessioni e diversa dagli altri tentativi;
    # ogni KO RADDOPPIA il cooldown del dormiente.
    warm_refill_wake_max_attempts: int = 10
    # 502/503 "mid-stream" di un aggregatore: pausa BREVE dell'HOST (non 24h
    # di quarantena) per non bruciare le chiavi sorelle dello stesso host.
    cooldown_host_midstream_502_sec: float = 120.0
    # ESENZIONE RIPARAZIONE (P0): quanti fallimenti CONSECUTIVI della famiglia
    # reasoning/schema/format possiamo perdonare allo stesso dep (si ripara e
    # si ritenta senza cooldown); esaurito il budget si torna al KO normale.
    # Un successo azzera lo streak.
    repair_exempt_streak_limit: int = 3
    # FINESTRA DI FALLIMENTO DEL MODELLO (cross-chiave): N KO non-esenti entro
    # `model_fail_window_sec` sullo stesso modello (anche su chiavi DIVERSE)
    # -> bench del modello su TUTTE le sue chiavi per `model_fail_cooldown_sec`
    # (le chiavi gemelle non ripescano un modello malato). 0 = disattivata.
    model_fail_window_sec: int = 900
    model_fail_threshold: int = 3
    model_fail_cooldown_sec: int = 600
    # DEGRADED MODE: sotto `degraded_healthy_ratio` di HOST sani (per
    # `degraded_entry_grace_sec`) si sospende l'ESPLORAZIONE (cascata refill,
    # hedge canary, hunt, sveglia) e resta la sola rotazione ladder; si
    # riprende solo dopo `degraded_exit_grace_sec` SOPRA soglia (anti-flap).
    # Sotto `degraded_min_providers` host non si entra mai in degradato.
    degraded_mode_enabled: bool = True
    degraded_healthy_ratio: float = 0.5
    degraded_min_providers: int = 3
    degraded_entry_grace_sec: int = 60
    degraded_exit_grace_sec: int = 120
    # TETTO operatore sui cooldown STIMATI da noi (provenienza 'heuristic'):
    # non tocca MAI un retry dichiarato dal provider (Retry-After, reset
    # quota => 'authoritative') ne' credit/tier. 0 = nessun tetto.
    cooldown_estimate_ceiling_sec: int = 0
    # LEASE DI CONCORRENZA PER CHIAVE (opt-in): chiude la race
    # check-then-act sotto carico parallelo. Se una api_key ha gia'
    # `key_concurrency_max` richieste in volo viene DEPRIORITIZZATA nelle
    # scelte (soft: se non resta altro si usa comunque, mai 503 per il solo
    # lease); una lease scade da sola dopo `key_concurrency_lease_max_age_sec`.
    key_concurrency_enabled: bool = False
    key_concurrency_max: int = 2
    key_concurrency_lease_max_age_sec: int = 120
    # REGISTRO QUIRK LOCALE: lista di {model: glob, flag: <colonna CSV>,
    # severity: blocker|warning|info, note: str}. Conoscenza DICHIARATIVA
    # per-modello (glob case-insensitive, es. "*nemotron-3-ultra*") mappata
    # sui flag esistenti e applicata IN MEMORIA ai deployment, senza
    # riscrivere il CSV e senza catalogo esterno.
    quirks: list = field(default_factory=list)
    # Ammette nei "caldi" (e nello sticky/holder) anche i deployment LENTI
    # (EMA oltre soglia): il successo lento viene comunque registrato cosi' la
    # sessione lo conosce, e la gara sui canary cerca subito un sostituto.
    # Una chiave SATURA (soft-429/fault) resta SEMPRE fuori.
    warm_pool_allow_slow: bool = True
    # CACHE-AWARE: detentore per-sessione + troncamento contesto selettivo
    cache_aware_enabled: bool = True
    # AUDIT prefisso (F4): impronta del prefisso canonico per sessione, per
    # dire PERCHE' la cache upstream si e' spenta (identity vs prefix).
    cache_prefix_audit: bool = True
    cache_prefer_last_success: bool = True
    cache_holder_ttl_sec: int = 3600
    cache_skip_probe_when_holder: bool = True
    cache_ctx_truncation_enabled: bool = True
    cache_ctx_keep_turns: int = 4
    cache_ctx_max_tool_output_chars: int = 2000
    cache_ctx_min_saved_tokens: int = 500
    cache_ctx_stub_text: str = "[tool output omesso: {n} caratteri]"
    cache_ctx_min_ctx_tokens: int = 50000
    cache_ctx_on_deployment_switch: bool = True
    cache_ctx_switch_min_tokens: int = 8000
    # Isteresi anti-churn del troncamento contesto: la soglia assoluta scatta
    # solo entro questa frazione della finestra del deployment scelto
    # (0 = disabilitata, comportamento storico).
    cache_ctx_abs_headroom_ratio: float = 0.8
    # Stub "head+tail": caratteri FISSI di inizio/fine conservati nei tool
    # output vecchi (min 600 di default, configurabili; head=tail=0 -> stub
    # secco legacy). Frontiera dinamica: protegge la coda finche' sta in
    # keep_tail_pct% della finestra del deployment (0 = solo keep_turns).
    # Output con errori (Traceback/...Error/Exception/exit != 0) MAI toccati.
    cache_ctx_head_chars: int = 600
    cache_ctx_tail_chars: int = 600
    cache_ctx_keep_tail_pct: float = 2.0
    cache_ctx_keep_error_outputs: bool = True
    # Troncamento JSON-aware degli ARGOMENTI dei tool_calls vecchi (0 = mai
    # toccare, comportamento storico). Il JSON resta valido: si tagliano solo
    # valori stringa lunghi, con lo stesso head+tail deterministico degli stub.
    cache_ctx_tool_args_max_chars: int = 2000
    # Headroom ABSOLUTO anticipato per deployment reasoning-only (R1/Qwen
    # thinking): la soglia assoluta scatta a una frazione MINORE della
    # finestra cosi' restano token liberi per il reasoning block (0 = off).
    cache_ctx_reasoning_headroom_ratio: float = 0.7
    # H3: riserva di finestra per il blocco reasoning dei modelli thinking,
    # SOMMATA al ctx_est nel gate should_compact (l'output di thinking non e'
    # nell'input: senza riserva si sfonda a meta' risposta). 0 = off.
    cache_ctx_reasoning_reserve_ratio: float = 0.15
    # TRONCAMENTO STRUTTURATO JSON: liste/dict con piu' di N elementi vengono
    # tagliati mantenendo JSON VALIDO (primi json_struct_head + marker totale
    # + ultimi json_struct_tail) invece che a meta' oggetto. 0 = off.
    cache_ctx_json_struct_max_items: int = 40
    cache_ctx_json_struct_head: int = 20
    cache_ctx_json_struct_tail: int = 5
    # RETENTION PER CITAZIONE: non stubbare un output vecchio se la coda
    # protetta lo cita (token ripetuto >= cite_min_freq volte).
    cache_ctx_cite_retention: bool = True
    cache_ctx_cite_min_freq: int = 3
    # DEBUG SNIFF: scatola nera input/output su var/debug-sniff.log con
    # rotazione oraria e retention debug_sniff_retention_hours. Default OFF
    # (file con conversazione completa: solo per debug locale).
    debug_sniff_enabled: bool = False
    debug_sniff_retention_hours: int = 24

    # CACHE-PRESERVING: gli abbonamenti flat (Go/Zen) hanno cache a livello
    # API key; usare la STESSA key ripetutamente entro una sessione massimizza
    # i cache-hit (prezzo cache << prezzo input pieno) e distribuisce il
    # carico mensile tra tutti gli abbonamenti.
    # go_recency_halflife_sec: halflife per i bucket -go e -fallback (dove i
    # limiti SONO MENSILI in dollari-equivalenti); default 300s = le sessioni
    # restano sulla stessa key per minuti (cache calda) e la distribuzione
    # avviene a livello di sessione singola, non di singolo turn.
    # deployment_sticky: nei bucket free (dims/-C primari) la stessa sessione
    # resta INCOLLATA alla stessa chiave finché è viva (non in cooldown e
    # contesto sufficiente); al primo fallimento il cooldown sposta e si
    # ri-àncora. I bucket free hanno rate-limit AL MINUTO, non mensili: la
    # cache è secondaria, e la distribuzione è gestita dallo sticky
    # per-deployment, non dalla recency globale.
    go_recency_halflife_sec: float = 300.0
    deployment_sticky: bool = True
    # STICKY PER CAPABILITY: quando abilitato, le sessioni restano attaccate
    # allo stesso deployment PER CAPABILITÀ RICHIESTA (es. text, vision, stt).
    # Così una sessione che fa solo testo resta sulla stessa key, ma se poi
    # chiede vision può usare un deployment diverso senza rompere lo sticky
    # del testo. DEFAULT OFF per compatibilità.
    deployment_sticky_per_capability: bool = False

    # ESCALATION WINNER (SCORCIATOIA, solo-in-salita): quando una richiesta
    # PARTITA da un bucket (es. -200k) fallisce e SALTA verso un gruppo piu'
    # alto (-1000k/-go/-fallback) trovando un deployment che risponde BENE,
    # lo ricordiamo PER QUEL BUCKET richiesto. Nelle richieste successive il
    # bucket nominale viene SEMPRE tentato per primo (mai anticipato): il
    # winner e' usato solo come SCORCIATOIA del fallback, cioe' quando il
    # bucket richiesto fallisce si salta direttamente al winner invece di
    # rivisitare tutta la scala morta (costava minuti ogni richiesta).
    # Si PURISCE quando il bucket richiesto torna a servire da solo (guarigione)
    # o quando il winner stesso fallisce (mark_failed). Mai persistito: e'
    # memoria runtime, il restart la riapprende alla prima salita buona.
    escalation_pin: bool = True
    escalation_pin_ttl_sec: int = 300        # finestra scorrevole: si rinnova
                                             # a ogni salita buona
    # RICAMPIONAMENTO PRE-PIN: prima di usare la scorciatoia verso il winner
    # (tipicamente -go), si riprova la dim richiesta (1 tentativo) e si sonda
    # fino a `escalation_pin_probe_dims` dim INTERMEDIE (tra richiesta e winner,
    # escluso il winner) scegliendole a caso: se una e' "guarita" nel frattempo,
    # la usiamo invece di saltare subito al winner. Solo candidate VIVI (una
    # dim tutta in cooldown viene saltata, costo zero). 0 = disabilitato
    # (comportamento storico: salto diretto al winner).
    escalation_pin_probe_dims: int = 2
    escalation_pin_probe_retry: bool = True   # 1 retry nella dim richiesta
    escalation_pin_probe_random: bool = True  # scelta casuale delle intermedie

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
    # Numero di risvegli di dim in cooldown (stantii) tentati PRIMA di
    # escalare a -go (oltre a quello del dim sticky/esplicito). 0 = nessuno.
    # Budget A FINESTRA dei cooldown-wakeup della scala (solo dim nati da
    # 429/quota): max N wakeup per deployment per `window_sec`.
    ladder_cooldown_wakeups: int = 20
    ladder_cooldown_wakeup_window_sec: int = 3600
    # COLD SPREAD: a ogni pick "a freddo" nascondi dai candidati il `pct`
    # dei deployment col MAGGIOR numero di tentativi (ok+fail) nelle ultime
    # 24h, cosi' il carico si distribuisce anche su `order`/provider diversi.
    # I dep 'attaccati' alla sessione corrente (successo entro
    # session_dep_guard_sec) non vengono mai nascosti. min_pool =
    # ladder_skip_after (sotto quella soglia non si taglia nulla). 0 = off.
    cold_spread_pct: float = 0.20

    # Risveglio del dim CORRENTE in `initial_pick`: se il pick non trova nulla
    # di vivo, prova il dep cooled da >= stale_cooldown_retry_sec prima di
    # escalare. Opzionale: l'autoprobe risveglia gia' i dormienti, quindi si
    # puo' disattivare senza toccare il resto della scala.
    initial_pick_cooldown_wakeup: bool = True

    # SOGLIA "CRONICO": un deployment con questo numero di fallimenti nelle
    # ultime 24h NON viene riesumato dagli step di ri-tentativo della scala
    # (stantii e ULTIMA SPIAGGIA): è statisticamente rotto, ritentarlo ogni
    # 5 minuti rallenta la catena senza utilità. Resta comunque provato come
    # ultimo paracadute se non c'è più nulla. La finestra 24h si azzera al
    # cambio giorno (fail_day_key) e clear_cooldown su successo lo azzera
    # subito: un deployment "svegliato" che risponde torna pienamente vivo.
    cooldown_retry_max_fail_24h: int = 10

    # PARACADUTE CRONICI (step 4bis della ladder): quando dims/go vivi e
    # stantii sono esauriti, PRIMA di spendere sul -fallback a pagamento si
    # riprovano fino a `ladder_chronic_max` deployment cronici col cooldown
    # SCADUTO (potenzialmente "svegli"), dal meno fallimentare al più
    # fallimentare (a parità: cooldown residuo più breve). 0 = disattivo.
    ladder_chronic_max: int = 3

    # Se un cronico fallisce di nuovo (anche dopo essere stato "svegliato"
    # dal paracadute), resta escluso per almeno questi secondi: non va
    # martellato ogni pochi minuti. 2h = 7200s. clear_cooldown su successo
    # lo azzera subito.
    chronic_fail_cooldown_sec: int = 7200

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
        "min_per_min": 10, "min_per_day": 200,
        # safety_ratio: frazione del cap appreso oltre la quale il deployment
        # e' considerato "virtualmente saturo" e viene saltato a favore del
        # successivo (limitatore PREDITIVO anti-burst). 0 = disabilitato.
        # count_inflight: conta anche le richieste gia' in volo (max con
        # minute_calls, che le include gia': serve al rollover di minuto).
        "safety_ratio": 0.8, "count_inflight": True,
        # rate_hint_threshold: se X-RateLimit-Requests-Remaining del provider
        # scende sotto questa soglia, il budget guard abbassa il cap appreso
        # PRIMA del 429 (anticipo di 2-3 richieste).
        "rate_hint_threshold": 3,
        # suppress_with_headers (F8): una chiave i cui header X-RateLimit-*
        # sono visti di recente NON paga i cap appresi (la verità live degli
        # header batte il tetto imparato a fatica dai 429). Chi non manda
        # header mantiene il comportamento storico.
        "suppress_with_headers": True})
    # --- Rate-hint skip senza colpa (F6) + soft 429 per-CHIAVE (F7) ---
    # Snapshot breve degli header: se Requests-Remaining e' <= remaining_max
    # e l'header e' fresco (ttl), le twin deployment sulla STESSA api_key
    # vengono solo SALTATE a pick (niente cooldown, niente strike, niente
    # penalita' di reputazione). Un 429 blocca soft la chiave per il
    # Retry-After (tetto key_soft_max_sec).
    rate_hint_skip_enabled: bool = True
    rate_hint_ttl_sec: float = 20.0
    rate_hint_remaining_max: int = 5
    rate_hint_proven_sec: float = 900.0
    key_soft_429_enabled: bool = True
    key_soft_max_sec: int = 900
    # cap a 5 ORE: i free-tier si rinnovano su finestre giornaliere/orarie,
    # seppellire una chiave per un intero giorno la toglie dal giro anche
    # quando il limite era solo orario. Il budget_guard (router) dosa PRIMA
    # del muro, quindi il cooldown resta per i casi veramente rotti, non
    # per quota.
    max_cooldown_sec: int = 18000

    # TIMEOUT = DANNO REALE: un modello che "appende" senza rispondere (ne'
    # errore, ne' contenuto) fa perdere tempo vero (fino a stream_first_content_ms
    # per richiesta). Il cooldown del solo fallimento-per-timeout e' quindi
    # `timeout_cooldown_mult` volte il cooldown "classico" (linear/escalation,
    # con gli stessi moltiplicatori su fail_24h), poi clampato a
    # max_cooldown_sec. 1 = nessuna penalizzazione extra.
    timeout_cooldown_mult: int = 10

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
        if "estimate_adaptive_enabled" in raw:
            p.estimate_adaptive_enabled = _coerce_bool(
                raw["estimate_adaptive_enabled"], "estimate_adaptive_enabled")
        if "estimate_adaptive_shadow" in raw:
            p.estimate_adaptive_shadow = _coerce_bool(
                raw["estimate_adaptive_shadow"], "estimate_adaptive_shadow")
        if "estimate_adaptive_auto_enable" in raw:
            p.estimate_adaptive_auto_enable = _coerce_bool(
                raw["estimate_adaptive_auto_enable"],
                "estimate_adaptive_auto_enable")
        _set_int(p, raw, "estimate_adaptive_auto_min_n", minimum=1)
        if "estimate_adaptive_auto_max_delta_pct" in raw:
            v = raw["estimate_adaptive_auto_max_delta_pct"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not (0 <= float(v) <= 100):
                raise ValueError("estimate_adaptive_auto_max_delta_pct "
                                 "deve essere 0..100")
            p.estimate_adaptive_auto_max_delta_pct = float(v)
        if "estimate_calib_alpha" in raw:
            v = raw["estimate_calib_alpha"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not (0.0 <= float(v) <= 1.0):
                raise ValueError("estimate_calib_alpha deve essere 0..1")
            p.estimate_calib_alpha = float(v)
        _set_int(p, raw, "provider_models_ttl_sec", minimum=0)
        _rmin = raw.get("retry_after_min_sec")
        if _rmin is not None:
            try:
                p.retry_after_min_sec = max(0.0, float(_rmin))
            except (TypeError, ValueError):
                raise ValueError(
                    "retry_after_min_sec deve essere un numero >= 0") from None
        _rafp = raw.get("retry_after_floor_by_provider")
        if _rafp is not None:
            if not isinstance(_rafp, dict):
                raise ValueError(
                    "retry_after_floor_by_provider deve essere una mappa "
                    "provider -> secondi") from None
            _tbl: dict[str, float] = {}
            for _k, _v in _rafp.items():
                try:
                    _fv = max(0.0, float(_v))
                except (TypeError, ValueError):
                    raise ValueError(
                        f"retry_after_floor_by_provider[{_k}] non numerico"
                    ) from None
                _tbl[str(_k).strip().lower()] = _fv
            p.retry_after_floor_by_provider = _tbl
        _sds = raw.get("shutdown_drain_sec")
        if _sds is not None:
            try:
                p.shutdown_drain_sec = max(0.0, float(_sds))
            except (TypeError, ValueError):
                raise ValueError(
                    "shutdown_drain_sec deve essere un numero >= 0") from None
        _stall = raw.get("stream_stall_sec")
        if _stall is not None:
            try:
                p.stream_stall_sec = max(0.0, float(_stall))
            except (TypeError, ValueError):
                raise ValueError(
                    "stream_stall_sec deve essere un numero >= 0") from None
        _stm = raw.get("stream_stall_ttft_mult")
        if _stm is not None:
            try:
                p.stream_stall_ttft_mult = max(0.0, float(_stm))
            except (TypeError, ValueError):
                raise ValueError(
                    "stream_stall_ttft_mult deve essere un numero >= 0") from None
        _sts = raw.get("stream_stall_max_sec")
        if _sts is not None:
            try:
                p.stream_stall_max_sec = max(1.0, float(_sts))
            except (TypeError, ValueError):
                raise ValueError(
                    "stream_stall_max_sec deve essere un numero >= 1") from None
        _rdh = raw.get("reputation_decay_halflife_sec")
        if _rdh is not None:
            try:
                p.reputation_decay_halflife_sec = max(0.0, float(_rdh))
            except (TypeError, ValueError):
                raise ValueError(
                    "reputation_decay_halflife_sec deve essere un numero "
                    ">= 0") from None
        if "adaptive_timeout_enabled" in raw:
            p.adaptive_timeout_enabled = _coerce_bool(
                raw["adaptive_timeout_enabled"], "adaptive_timeout_enabled")
        if "http_keepalive_pool" in raw:
            p.http_keepalive_pool = _coerce_bool(
                raw["http_keepalive_pool"], "http_keepalive_pool")
        for _fld in ("adaptive_timeout_floor_sec",
                     "adaptive_timeout_multiplier",
                     "adaptive_timeout_max_sec"):
            _val = raw.get(_fld)
            if _val is not None:
                try:
                    setattr(p, _fld, max(0.0, float(_val)))
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{_fld} deve essere un numero >= 0") from None
        if "cooldown_probe_enabled" in raw:
            p.cooldown_probe_enabled = _coerce_bool(
                raw["cooldown_probe_enabled"], "cooldown_probe_enabled")
        _cpar = raw.get("cooldown_probe_after_ratio")
        if _cpar is not None:
            try:
                p.cooldown_probe_after_ratio = min(1.0, max(0.0, float(_cpar)))
            except (TypeError, ValueError):
                raise ValueError(
                    "cooldown_probe_after_ratio deve essere tra 0 e 1") from None
        if "cooldown_probe_decay" in raw:
            p.cooldown_probe_decay = _coerce_bool(
                raw["cooldown_probe_decay"], "cooldown_probe_decay")
        _csh = raw.get("cooldown_streak_halflife_sec")
        if _csh is not None:
            try:
                p.cooldown_streak_halflife_sec = max(0.0, float(_csh))
            except (TypeError, ValueError):
                raise ValueError(
                    "cooldown_streak_halflife_sec deve essere un numero >= 0") from None
        _set_int(p, raw, "probe_retire_after", minimum=0)
        _cjr = raw.get("cooldown_jitter_ratio")
        if _cjr is not None:
            try:
                p.cooldown_jitter_ratio = min(1.0, max(0.0, float(_cjr)))
            except (TypeError, ValueError):
                raise ValueError(
                    "cooldown_jitter_ratio deve essere tra 0 e 1") from None
        _cjs = raw.get("cooldown_jitter_sec_max")
        if _cjs is not None:
            try:
                p.cooldown_jitter_sec_max = min(
                    60.0, max(0.0, float(_cjs)))
            except (TypeError, ValueError):
                raise ValueError(
                    "cooldown_jitter_sec_max deve essere tra 0 e 60") from None
        if "error_class_cooldowns" in raw:
            p.error_class_cooldowns = _coerce_bool(
                raw.get("error_class_cooldowns"), "error_class_cooldowns")
        _set_int(p, raw, "cooldown_transient_sec", minimum=1)
        _set_int(p, raw, "slow_latency_abs_floor_ms", minimum=0)
        _set_int(p, raw, "slow_latency_min_peers", minimum=1)
        _set_int(p, raw, "hunt_backoff_sec", minimum=0)
        _set_int(p, raw, "hunt_max_per_window", minimum=0)
        _set_int(p, raw, "hunt_window_sec", minimum=1)
        _srel = raw.get("slow_latency_rel_mult")
        if _srel is not None:
            if isinstance(_srel, bool) or not isinstance(_srel, (int, float)) \
                    or not (0.0 <= float(_srel) <= 100.0):
                raise ValueError("slow_latency_rel_mult deve essere 0..100")
            p.slow_latency_rel_mult = float(_srel)
        # ---------------------------------------------------- routing tuning
        _set_int(p, raw, "latency_rotate_threshold_ms", minimum=0)
        _set_int(p, raw, "soft_slow_latency_ms", minimum=0)
        _set_int(p, raw, "soft_slow_ctx_min", minimum=0)
        _set_int(p, raw, "ttft_rate_min_ctx", minimum=0)
        _cbe = raw.get("ctx_bucket_edges")
        if _cbe is not None:
            if not isinstance(_cbe, (list, tuple)) or not _cbe:
                raise ValueError("ctx_bucket_edges deve essere una lista non vuota")
            _edges: list[int] = []
            for _e in _cbe:
                if isinstance(_e, bool) or not isinstance(_e, (int, float)):
                    raise ValueError("ctx_bucket_edges: elementi non numerici")
                _edges.append(int(_e))
            if _edges != sorted(_edges):
                raise ValueError("ctx_bucket_edges deve essere crescente")
            p.ctx_bucket_edges = _edges
        for _name in ("ttft_rate_floor_ms", "slow_latency_abs_floor_ms",
                      "slow_gen_mult", "slow_typical_completion_tokens",
                      "slow_rel_baseline_mult",
                      "effort_capable_bonus", "latency_penalty_per_sec"):
            _v = raw.get(_name)
            if _v is not None:
                if isinstance(_v, bool) or not isinstance(_v, (int, float)):
                    raise ValueError(f"{_name} deve essere un numero")
                setattr(p, _name, float(_v))
        _pbn = raw.get("provider_bias_normalization")
        if _pbn is not None:
            _val = str(_pbn).strip().lower()
            if _val not in ("log", "sqrt", "none"):
                raise ValueError(
                    "provider_bias_normalization deve essere log|sqrt|none")
            p.provider_bias_normalization = _val
        _set_int(p, raw, "dynamic_scoring_history_window", minimum=1)
        # ---------------------------------------------------------- ops tuning
        _set_int(p, raw, "probe_concurrency", minimum=1)
        _set_int(p, raw, "playground_max_attempts", minimum=1)
        for _name in ("probe_timeout_sec", "playground_timeout_sec"):
            _v = raw.get(_name)
            if _v is not None:
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or float(_v) < 0:
                    raise ValueError(f"{_name} deve essere un numero >= 0")
                setattr(p, _name, float(_v))
        # ---------------------------------------------------- cooldown tuning
        _set_int(p, raw, "model_missing_cooldown_sec", minimum=0)
        _set_int(p, raw, "min_output_floor", minimum=1)
        for _name in ("quota_min_cooldown_sec", "quota_max_cooldown_sec",
                      "provider_transient_cooldown_sec",
                      "permission_denied_cooldown_sec",
                      "stream_loop_cooldown_sec", "retry_body_cap_sec"):
            _v = raw.get(_name)
            if _v is not None:
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or float(_v) < 0:
                    raise ValueError(f"{_name} deve essere un numero >= 0")
                setattr(p, _name, float(_v))
        _set_int(p, raw, "cooldown_timeout_sec", minimum=1)
        # ------------------------------- runtime/memoria & limiti vari
        _set_int(p, raw, "coalesce_cache_max", minimum=0)
        _set_int(p, raw, "video_job_ttl_sec", minimum=0)
        _set_int(p, raw, "keyhealth_streak_dead_threshold", minimum=1)
        _set_int(p, raw, "ctxcompact_min_protected_msgs", minimum=0)
        _set_int(p, raw, "toolrepair_max_unwrap_depth", minimum=0)
        _set_int(p, raw, "sniff_max_b64_chars", minimum=1)
        _set_int(p, raw, "sniff_max_str_chars", minimum=1)
        _set_int(p, raw, "sniff_max_sse_bytes", minimum=1)
        _kemf = raw.get("keyhealth_success_ema_floor")
        if _kemf is not None:
            if isinstance(_kemf, bool) or not isinstance(_kemf, (int, float)) \
                    or float(_kemf) < 0:
                raise ValueError(
                    "keyhealth_success_ema_floor deve essere un numero >= 0")
            p.keyhealth_success_ema_floor = float(_kemf)
        # ------------------------------------------ HTTP upstream (forwarder)
        for _name in ("upstream_connect_timeout_sec", "upstream_read_timeout_sec",
                      "upstream_write_timeout_sec", "upstream_pool_timeout_sec",
                      "upstream_keepalive_expiry_sec"):
            _v = raw.get(_name)
            if _v is not None:
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or float(_v) <= 0:
                    raise ValueError(f"{_name} deve essere un numero > 0")
                setattr(p, _name, float(_v))
        _set_int(p, raw, "upstream_max_keepalive_connections", minimum=1)
        _set_int(p, raw, "upstream_max_connections", minimum=1)
        _rsc = raw.get("retryable_status_codes")
        if _rsc is not None:
            if not isinstance(_rsc, list) or not all(
                    isinstance(x, int) and not isinstance(x, bool)
                    and 100 <= x <= 599 for x in _rsc):
                raise ValueError(
                    "retryable_status_codes deve essere una lista di codici "
                    "HTTP interi (100..599)")
            p.retryable_status_codes = [int(x) for x in _rsc]
        _eih = raw.get("effort_incompatible_hosts")
        if _eih is not None:
            if not isinstance(_eih, list) or not all(
                    isinstance(x, str) and x for x in _eih):
                raise ValueError(
                    "effort_incompatible_hosts deve essere una lista di "
                    "stringhe non vuote")
            p.effort_incompatible_hosts = [str(x) for x in _eih]
        if "model_circuit_enabled" in raw:
            p.model_circuit_enabled = _coerce_bool(
                raw.get("model_circuit_enabled"), "model_circuit_enabled")
        _set_int(p, raw, "model_circuit_keys", minimum=2)
        _set_int(p, raw, "model_circuit_window_sec", minimum=1)
        _set_int(p, raw, "model_circuit_open_sec", minimum=1)
        if "cooldown_autoprobe_enabled" in raw:
            p.cooldown_autoprobe_enabled = _coerce_bool(
                raw["cooldown_autoprobe_enabled"], "cooldown_autoprobe_enabled")
        if "cooldown_autoprobe_retired_enabled" in raw:
            p.cooldown_autoprobe_retired_enabled = _coerce_bool(
                raw["cooldown_autoprobe_retired_enabled"],
                "cooldown_autoprobe_retired_enabled")
        if "cooldown_autoprobe_multiply_24h" in raw:
            p.cooldown_autoprobe_multiply_24h = _coerce_bool(
                raw["cooldown_autoprobe_multiply_24h"],
                "cooldown_autoprobe_multiply_24h")
        if raw.get("cooldown_autoprobe_schedule") is not None:
            _sch = str(raw["cooldown_autoprobe_schedule"]).strip().lower()
            if _sch not in ("nightly", "request"):
                raise ValueError(
                    "cooldown_autoprobe_schedule deve essere nightly|request")
            p.cooldown_autoprobe_schedule = _sch
        _set_int(p, raw, "cooldown_autoprobe_per_dim", minimum=0)
        _set_int(p, raw, "cooldown_autoprobe_max_total", minimum=0)
        _set_int(p, raw, "cooldown_autoprobe_key_day_max", minimum=0)
        for _fld in ("cooldown_autoprobe_min_age_sec",
                     "cooldown_autoprobe_grow_sec",
                     "cooldown_autoprobe_min_gap_sec",
                     "cooldown_autoprobe_timeout_sec",
                     "cooldown_autoprobe_fresh_age_sec",
                     "cooldown_autoprobe_key_gap_sec",
                     "cooldown_autoprobe_key_ok_fresh_sec",
                     "cooldown_autoprobe_retired_gap_sec",
                     "cooldown_autoprobe_transient_sec",
                     "cooldown_autoprobe_skip_over_sec",
                     "warm_refill_wake_min_cooldown_age_sec",
                     "cooldown_host_midstream_502_sec"):
            _val = raw.get(_fld)
            if _val is not None:
                try:
                    setattr(p, _fld, max(0.0, float(_val)))
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{_fld} deve essere un numero >= 0") from None
        if "cooldown_autoprobe_crisis_enabled" in raw:
            p.cooldown_autoprobe_crisis_enabled = _coerce_bool(
                raw["cooldown_autoprobe_crisis_enabled"],
                "cooldown_autoprobe_crisis_enabled")
        if raw.get("repair_exempt_streak_limit") is not None:
            try:
                p.repair_exempt_streak_limit = max(
                    0, int(raw["repair_exempt_streak_limit"]))
            except (TypeError, ValueError):
                raise ValueError(
                    "repair_exempt_streak_limit deve essere un intero >= 0"
                ) from None
        for _fld in ("model_fail_window_sec", "model_fail_threshold",
                     "model_fail_cooldown_sec", "degraded_min_providers",
                     "degraded_entry_grace_sec", "degraded_exit_grace_sec",
                     "cooldown_estimate_ceiling_sec"):
            if raw.get(_fld) is not None:
                try:
                    setattr(p, _fld, max(0, int(raw[_fld])))
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{_fld} deve essere un intero >= 0") from None
        if raw.get("degraded_mode_enabled") is not None:
            p.degraded_mode_enabled = bool(raw["degraded_mode_enabled"])
        if raw.get("key_concurrency_enabled") is not None:
            p.key_concurrency_enabled = bool(raw["key_concurrency_enabled"])
        for _fld in ("key_concurrency_max",
                     "key_concurrency_lease_max_age_sec"):
            if raw.get(_fld) is not None:
                try:
                    setattr(p, _fld, max(0, int(raw[_fld])))
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{_fld} deve essere un intero >= 0") from None
        if raw.get("quirks") is not None:
            _q = raw["quirks"]
            if not isinstance(_q, (list, tuple)):
                raise ValueError("quirks deve essere una lista di oggetti")
            _qout = []
            for _it in _q:
                if not isinstance(_it, dict):
                    raise ValueError("ogni quirk deve essere un oggetto")
                _glob = str(_it.get("model") or "").strip().lower()
                _flag = str(_it.get("flag") or "").strip().lower()
                if not _glob or not _flag:
                    raise ValueError(
                        "ogni quirk richiede 'model' (glob) e 'flag'")
                _sev = str(_it.get("severity") or "warning").strip().lower()
                if _sev not in ("blocker", "warning", "info"):
                    raise ValueError(
                        "severity deve essere blocker|warning|info")
                _qout.append({"model": _glob, "flag": _flag,
                              "severity": _sev,
                              "note": str(_it.get("note") or "")})
            p.quirks = _qout
        if raw.get("degraded_healthy_ratio") is not None:
            try:
                _r = float(raw["degraded_healthy_ratio"])
            except (TypeError, ValueError):
                raise ValueError("degraded_healthy_ratio deve essere un "
                                 "numero tra 0 e 1") from None
            if not (0.0 <= _r <= 1.0):
                raise ValueError("degraded_healthy_ratio deve essere tra 0 e 1")
            p.degraded_healthy_ratio = _r
        for _fld in ("cooldown_autoprobe_crisis_ratio",
                     "cooldown_autoprobe_crisis_mult",
                     "hotreload_probe_timeout_sec",
                     "hotreload_probe_cooldown_sec",
                     "hotreload_drain_ttl_sec"):
            _val = raw.get(_fld)
            if _val is not None:
                try:
                    setattr(p, _fld, max(0.0, float(_val)))
                except (TypeError, ValueError):
                    raise ValueError(
                        f"{_fld} deve essere un numero >= 0") from None
        _set_int(p, raw, "hotreload_probe_max", minimum=0)
        if "hotreload_probe_enabled" in raw:
            p.hotreload_probe_enabled = _coerce_bool(
                raw["hotreload_probe_enabled"], "hotreload_probe_enabled")
        _set_int(p, raw, "loop_stream_buffer_words", minimum=0)
        _set_int(p, raw, "conc_default_limit", minimum=1)
        _set_int(p, raw, "conc_max_limit", minimum=1)
        _set_int(p, raw, "conc_learn_success_streak", minimum=1)
        if "conc_token_ratio" in raw:
            v = raw["conc_token_ratio"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) \
                    or not (0.0 <= float(v) <= 5.0):
                raise ValueError("conc_token_ratio deve essere 0..5")
            p.conc_token_ratio = float(v)
        if "request_coalescing_enabled" in raw:
            p.request_coalescing_enabled = _coerce_bool(
                raw["request_coalescing_enabled"], "request_coalescing_enabled")
        _rc_ttl = raw.get("request_coalescing_ttl_sec")
        if _rc_ttl is not None:
            try:
                p.request_coalescing_ttl_sec = max(0.0, float(_rc_ttl))
            except (TypeError, ValueError):
                raise ValueError(
                    "request_coalescing_ttl_sec deve essere un numero >= 0") from None
        _set_int(p, raw, "request_coalescing_max_waiters", minimum=0)
        _rc_cache = raw.get("request_coalescing_cache_sec")
        if _rc_cache is not None:
            try:
                p.request_coalescing_cache_sec = max(0.0, float(_rc_cache))
            except (TypeError, ValueError):
                raise ValueError(
                    "request_coalescing_cache_sec deve essere un "
                    "numero >= 0") from None
        if "rate_hint_skip_enabled" in raw:
            p.rate_hint_skip_enabled = _coerce_bool(
                raw["rate_hint_skip_enabled"], "rate_hint_skip_enabled")
        _rh = raw.get("rate_hint_ttl_sec")
        if _rh is not None:
            try:
                v = float(_rh)
            except (TypeError, ValueError):
                raise ValueError("rate_hint_ttl_sec: numero richiesto") from None
            if not (0.0 <= v <= 300.0):
                raise ValueError("rate_hint_ttl_sec: 0..300")
            p.rate_hint_ttl_sec = v
        _set_int(p, raw, "rate_hint_remaining_max", minimum=0, maximum=100000)
        _rp = raw.get("rate_hint_proven_sec")
        if _rp is not None:
            try:
                v = float(_rp)
            except (TypeError, ValueError):
                raise ValueError("rate_hint_proven_sec: numero richiesto") from None
            if not (0.0 <= v <= 86400.0):
                raise ValueError("rate_hint_proven_sec: 0..86400")
            p.rate_hint_proven_sec = v
        if "key_soft_429_enabled" in raw:
            p.key_soft_429_enabled = _coerce_bool(
                raw["key_soft_429_enabled"], "key_soft_429_enabled")
        _set_int(p, raw, "key_soft_max_sec", minimum=10, maximum=86400)
        if "anon_session_fingerprint" in raw:
            p.anon_session_fingerprint = _coerce_bool(
                raw["anon_session_fingerprint"], "anon_session_fingerprint")
        _set_int(p, raw, "anon_session_fp_system_chars", minimum=0)
        _set_int(p, raw, "sticky_ttl_sec", minimum=1)
        if "sticky_handoff_same_family" in raw:
            p.sticky_handoff_same_family = _coerce_bool(
                raw["sticky_handoff_same_family"], "sticky_handoff_same_family")
        _set_int(p, raw, "cooldown_sec", minimum=0)
        _set_int(p, raw, "stale_cooldown_retry_sec", minimum=0)
        _set_int(p, raw, "max_fallback_tries", minimum=1)
        _set_int(p, raw, "hotwords_window", minimum=1)
        _set_int(p, raw, "step_up_pct", minimum=1, maximum=200)

        if "scoring_weights" in raw:
            sw = raw["scoring_weights"]
            if not isinstance(sw, dict):
                raise ValueError("scoring_weights deve essere un oggetto")
            merged = dict(DEFAULT_SCORING_WEIGHTS)
            for wk, wv in sw.items():
                if wk not in DEFAULT_SCORING_WEIGHTS:
                    raise ValueError(
                        f"scoring_weights.{wk} non riconosciuto "
                        f"(ammessi: {', '.join(sorted(DEFAULT_SCORING_WEIGHTS))})")
                try:
                    merged[wk] = float(wv)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"scoring_weights.{wk} deve essere un numero") from None
            p.scoring_weights = merged

        for key, attr in (("proxy_prefix", "proxy_prefix"),
                          ("go_suffix", "go_suffix"),
                          ("fallback_suffix", "fallback_suffix"),
                          ("service_name", "service_name"),
                          ("openrouter_app_referer", "openrouter_app_referer"),
                          ("openrouter_app_title", "openrouter_app_title")):
            if key in raw:
                if not isinstance(raw[key], str) or not raw[key]:
                    raise ValueError(f"{key} deve essere una stringa non vuota")
                setattr(p, attr, raw[key].strip())

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
        ds = raw.get("deployment_sticky")
        if ds is not None:
            p.deployment_sticky = _coerce_bool(ds, "deployment_sticky")
        dspc = raw.get("deployment_sticky_per_capability")
        if dspc is not None:
            p.deployment_sticky_per_capability = _coerce_bool(dspc, "deployment_sticky_per_capability")
        epp = raw.get("escalation_pin")
        if epp is not None:
            p.escalation_pin = _coerce_bool(epp, "escalation_pin")
        _set_int(p, raw, "escalation_pin_ttl_sec", minimum=1)
        _set_int(p, raw, "escalation_pin_probe_dims", minimum=0)
        _sret = raw.get("escalation_pin_probe_retry")
        if _sret is not None:
            p.escalation_pin_probe_retry = _coerce_bool(
                _sret, "escalation_pin_probe_retry")
        _srnd = raw.get("escalation_pin_probe_random")
        if _srnd is not None:
            p.escalation_pin_probe_random = _coerce_bool(
                _srnd, "escalation_pin_probe_random")
        for num_key, attr in (("recency_halflife_sec", "recency_halflife_sec"),
                              ("latency_ref_ms", "latency_ref_ms"),
                              ("go_recency_halflife_sec", "go_recency_halflife_sec")):
            nv = raw.get(num_key)
            if nv is not None:
                if isinstance(nv, bool) or not isinstance(nv, (int, float)) \
                        or nv <= 0:
                    raise ValueError(f"{num_key} non valido: {nv!r} "
                                     "(numero > 0 richiesto)")
                setattr(p, attr, float(nv))

        _ee = raw.get("enable_effort_temperature_override")
        if _ee is not None:
            p.enable_effort_temperature_override = _coerce_bool(
                _ee, "enable_effort_temperature_override")
        _eto = raw.get("effort_temperature_overrides")
        if _eto is not None:
            if not isinstance(_eto, dict):
                raise ValueError(
                    "effort_temperature_overrides deve essere una mappa "
                    "{low: t, medium: t, high: t}")
            clean: dict[str, float] = {}
            for k, v in _eto.items():
                lk = str(k).strip().lower()
                if lk not in ("low", "medium", "high"):
                    raise ValueError(
                        f"effort_temperature_overrides: chiave {k!r} non valida "
                        "(ammesse: low|medium|high)")
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    raise ValueError(
                        f"effort_temperature_overrides.{lk}: numero >= 0 richiesto")
                clean[lk] = float(v)
            p.effort_temperature_overrides = clean

        # --- DYNAMIC SCORING ---
        _ds = raw.get("dynamic_scoring")
        if _ds is not None:
            if not isinstance(_ds, dict):
                raise ValueError("dynamic_scoring deve essere una mappa")
            if "enabled" in _ds:
                p.dynamic_scoring_enabled = _coerce_bool(
                    _ds["enabled"], "dynamic_scoring.enabled")
            for _k, _attr in (
                    ("latency_p95_weight", "dynamic_scoring_latency_p95_weight"),
                    ("error_rate_weight", "dynamic_scoring_error_rate_weight"),
                    ("throughput_weight", "dynamic_scoring_throughput_weight")):
                _v = _ds.get(_k)
                if _v is not None:
                    if isinstance(_v, bool) or not isinstance(_v, (int, float)) or _v < 0:
                        raise ValueError(f"dynamic_scoring.{_k} deve essere un numero >= 0")
                    setattr(p, _attr, float(_v))

        # --- CIRCUIT BREAKER per API Key ---
        _cb = raw.get("circuit_breaker")
        if _cb is not None:
            if not isinstance(_cb, dict):
                raise ValueError("circuit_breaker deve essere una mappa")
            if "enabled" in _cb:
                p.circuit_breaker_enabled = _coerce_bool(
                    _cb["enabled"], "circuit_breaker.enabled")
            _ct = _cb.get("threshold")
            if _ct is not None:
                if isinstance(_ct, bool) or not isinstance(_ct, (int, float)) or _ct < 1:
                    raise ValueError("circuit_breaker.threshold deve essere intero >= 1")
                p.circuit_breaker_threshold = int(_ct)
            _cto = _cb.get("timeout")
            if _cto is not None:
                if isinstance(_cto, bool) or not isinstance(_cto, (int, float)) or _cto <= 0:
                    raise ValueError("circuit_breaker.timeout deve essere numero > 0")
                p.circuit_breaker_timeout = float(_cto)
            _cho = _cb.get("half_open_requests")
            if _cho is not None:
                if isinstance(_cho, bool) or not isinstance(_cho, (int, float)) or _cho < 1:
                    raise ValueError("circuit_breaker.half_open_requests deve essere intero >= 1")
                p.circuit_breaker_half_open_requests = int(_cho)
            _cs = _cb.get("scope")
            if _cs is not None:
                _csl = str(_cs).lower()
                if _csl not in ("hybrid", "dep", "key"):
                    raise ValueError(
                        "circuit_breaker.scope deve essere hybrid|dep|key")
                p.circuit_breaker_scope = _csl

        _mpb = raw.get("model_preference_base")
        if _mpb is not None:
            if isinstance(_mpb, bool) or not isinstance(_mpb, (int, float)) or _mpb < 0:
                raise ValueError("model_preference_base deve essere numero >= 0")
            p.model_preference_base = float(_mpb)

        # --- THOUGHT_SIGNATURE (Gemini 3 dummy fill) ---
        _tsf = raw.get("thought_sig_dummy_fill")
        if _tsf is not None:
            p.thought_sig_dummy_fill = _coerce_bool(
                _tsf, "thought_sig_dummy_fill")
        _tsv = raw.get("thought_sig_dummy_value")
        if _tsv is not None:
            _v = str(_tsv).strip()
            if _v:
                p.thought_sig_dummy_value = _v

        # --- TOOL_REPAIR ---
        _tr = raw.get("tool_repair")
        if _tr is not None:
            if not isinstance(_tr, dict):
                raise ValueError("tool_repair deve essere una mappa")
            _tr_en = _tr.get("enabled")
            if _tr_en is not None:
                p.tool_repair_enabled = _coerce_bool(_tr_en, "tool_repair.enabled")
            _tr_lvl = _tr.get("default_level")
            if _tr_lvl is not None:
                if str(_tr_lvl).strip().lower() not in ("off", "safe", "aggressive"):
                    raise ValueError("tool_repair.default_level deve essere off|safe|aggressive")
                p.tool_repair_default_level = str(_tr_lvl).strip().lower()
            _tr_g = _tr.get("disable_for_google")
            if _tr_g is not None:
                p.tool_repair_disable_for_google = _coerce_bool(_tr_g, "tool_repair.disable_for_google")
            _tr_sz = _tr.get("max_args_size")
            if _tr_sz is not None:
                if isinstance(_tr_sz, bool) or not isinstance(_tr_sz, (int, float)):
                    raise ValueError("tool_repair.max_args_size deve essere un intero")
                p.tool_repair_max_args_size = int(_tr_sz)
            _tr_ar = _tr.get("annotate_reasoning")
            if _tr_ar is not None:
                p.tool_repair_annotate_reasoning = _coerce_bool(_tr_ar, "tool_repair.annotate_reasoning")
            _fc = _tr.get("fake_call")
            if _fc is not None:
                if not isinstance(_fc, dict):
                    raise ValueError("tool_repair.fake_call deve essere una mappa")
                if "enabled" in _fc:
                    p.tool_repair_fake_call_enabled = _coerce_bool(
                        _fc["enabled"], "tool_repair.fake_call.enabled")
                _pat = _fc.get("patterns")
                if _pat is not None:
                    if not isinstance(_pat, (list, tuple)):
                        raise ValueError("tool_repair.fake_call.patterns deve essere una lista")
                    p.tool_repair_fake_call_patterns = tuple(str(x) for x in _pat)
                for _k, _attr in (("max_escalations", "tool_repair_fake_call_max_escalations"),
                                  ("stream_hold_max_bytes", "tool_repair_fake_call_hold_max_bytes"),
                                  ("stream_hold_timeout_ms", "tool_repair_fake_call_hold_timeout_ms")):
                    _v = _fc.get(_k)
                    if _v is not None:
                        if isinstance(_v, bool) or not isinstance(_v, (int, float)):
                            raise ValueError(f"tool_repair.fake_call.{_k} deve essere un intero")
                        setattr(p, _attr, int(_v))

        # --- HISTORY_NORMALIZE (#1) ---
        _hn = raw.get("history_normalize")
        if _hn is not None:
            if not isinstance(_hn, dict):
                raise ValueError("history_normalize deve essere una mappa")
            for _k, _attr in (
                    ("enabled", "history_normalize_enabled"),
                    ("tail_only", "history_normalize_tail_only"),
                    ("drop_orphan_tool", "history_normalize_drop_orphan_tool"),
                    ("drop_dangling_tool_calls",
                     "history_normalize_drop_dangling_tool_calls"),
                    ("drop_empty_assistant",
                     "history_normalize_drop_empty_assistant"),
                    ("dedupe_system", "history_normalize_dedupe_system")):
                if _k in _hn:
                    setattr(p, _attr, _coerce_bool(
                        _hn[_k], f"history_normalize.{_k}"))
            if "reasoning_content_max_chars" in _hn:
                v = _hn["reasoning_content_max_chars"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (-1 <= int(v) <= 200000):
                    raise ValueError(
                        "history_normalize.reasoning_content_max_chars "
                        "deve essere -1..200000")
                p.history_normalize_reasoning_content_max_chars = int(v)
            if "reasoning_keep_recent" in _hn:
                v = _hn["reasoning_keep_recent"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0 <= int(v) <= 20):
                    raise ValueError(
                        "history_normalize.reasoning_keep_recent "
                        "deve essere 0..20")
                p.history_normalize_reasoning_keep_recent = int(v)

        # --- SAMPLING_DEFAULTS (#2A) + LOOP_DETECTOR (#2B) ---
        _sd = raw.get("sampling_defaults")
        if _sd is not None:
            if not isinstance(_sd, dict):
                raise ValueError("sampling_defaults deve essere una mappa")
            if "enabled" in _sd:
                p.sampling_enabled = _coerce_bool(
                    _sd["enabled"], "sampling_defaults.enabled")
            _ap = _sd.get("allow_providers")
            if _ap is not None:
                if not isinstance(_ap, (list, tuple)):
                    raise ValueError("sampling_defaults.allow_providers "
                                     "deve essere una lista")
                p.sampling_allow_providers = tuple(
                    str(x).lower() for x in _ap)
            _pp = _sd.get("provider_params")
            if _pp is not None:
                if not isinstance(_pp, dict):
                    raise ValueError("sampling_defaults.provider_params "
                                     "deve essere una mappa")
                _allowed = {"top_p", "presence_penalty",
                            "frequency_penalty", "repetition_penalty"}
                _clean_pp: dict = {}
                for _pk, _pv in _pp.items():
                    if not isinstance(_pv, dict):
                        raise ValueError("sampling_defaults.provider_params "
                                         "valori devono essere mappe")
                    _row: dict = {}
                    for _k2, _v2 in _pv.items():
                        if str(_k2) not in _allowed:
                            raise ValueError("sampling_defaults.provider_params:"
                                             f" chiave {_k2!r} non ammessa")
                        if isinstance(_v2, bool) or not isinstance(
                                _v2, (int, float)):
                            raise ValueError(
                                f"sampling_defaults.provider_params.{_k2}"
                                " deve essere numerico")
                        _row[str(_k2)] = float(_v2)
                    _clean_pp[str(_pk).lower()] = _row
                p.sampling_provider_params = _clean_pp
            _lp = _sd.get("loop")
            if _lp is not None:
                if not isinstance(_lp, dict):
                    raise ValueError("sampling_defaults.loop deve essere "
                                     "una mappa")
                for _k, _attr, _lo, _hi in (
                        ("enabled", "loop_detector_enabled", None, None),
                        ("ngram_size", "loop_ngram_size", 2, 64),
                        ("repeats", "loop_repeats", 2, 16),
                        ("toolcall_repeat", "loop_toolcall_repeat", 2, 16),
                        ("min_tokens", "loop_min_tokens", 1, 2048)):
                    _v = _lp.get(_k)
                    if _v is None:
                        continue
                    if _lo is None:
                        setattr(p, _attr, _coerce_bool(
                            _v, f"sampling_defaults.loop.{_k}"))
                        continue
                    if isinstance(_v, bool) or not isinstance(
                            _v, (int, float)) or not (_lo <= int(_v) <= _hi):
                        raise ValueError(
                            f"sampling_defaults.loop.{_k} deve essere "
                            f"{_lo}..{_hi}")
                    setattr(p, _attr, int(_v))
        _ld = raw.get("loop_detector")
        if _ld is not None:
            if not isinstance(_ld, dict):
                raise ValueError("loop_detector deve essere una mappa")
            if "enabled" in _ld:
                p.loop_detector_enabled = _coerce_bool(
                    _ld["enabled"], "loop_detector.enabled")
            for _k, _attr, _lo, _hi in (
                    ("ngram_size", "loop_ngram_size", 2, 64),
                    ("repeats", "loop_repeats", 2, 16),
                    ("toolcall_repeat", "loop_toolcall_repeat", 2, 16),
                    ("min_tokens", "loop_min_tokens", 1, 2048)):
                _v = _ld.get(_k)
                if _v is None:
                    continue
                if isinstance(_v, bool) or not isinstance(
                        _v, (int, float)) or not (_lo <= int(_v) <= _hi):
                    raise ValueError(
                        f"loop_detector.{_k} deve essere {_lo}..{_hi}")
                setattr(p, _attr, int(_v))

        # --- CORRECTIVE_RETRY (#3) ---
        _cr = raw.get("corrective_retry")
        if _cr is not None:
            if not isinstance(_cr, dict):
                raise ValueError("corrective_retry deve essere una mappa")
            if "enabled" in _cr:
                p.corrective_retry_enabled = _coerce_bool(
                    _cr["enabled"], "corrective_retry.enabled")
            if "max_attempts" in _cr:
                _v = _cr["max_attempts"]
                if isinstance(_v, bool) or not isinstance(
                        _v, (int, float)) or not (0 <= int(_v) <= 1):
                    raise ValueError("corrective_retry.max_attempts "
                                     "deve essere 0..1")
                p.corrective_retry_max_attempts = int(_v)

        # --- TEXT_TOOLCALL (#6) ---
        _tt = raw.get("text_toolcall")
        if _tt is not None:
            if not isinstance(_tt, dict):
                raise ValueError("text_toolcall deve essere una mappa")
            if "enabled" in _tt:
                p.text_toolcall_enabled = _coerce_bool(
                    _tt["enabled"], "text_toolcall.enabled")
            if "require_declared_name" in _tt:
                p.text_toolcall_require_declared_name = _coerce_bool(
                    _tt["require_declared_name"],
                    "text_toolcall.require_declared_name")
            _af = _tt.get("allow_formats")
            if _af is not None:
                if not isinstance(_af, (list, tuple)):
                    raise ValueError("text_toolcall.allow_formats deve "
                                     "essere una lista")
                p.text_toolcall_allow_formats = tuple(str(x) for x in _af)
            if "max_bytes" in _tt:
                _v = _tt["max_bytes"]
                if isinstance(_v, bool) or not isinstance(
                        _v, (int, float)) or int(_v) < 1:
                    raise ValueError("text_toolcall.max_bytes deve "
                                     "essere >= 1")
                p.text_toolcall_max_bytes = int(_v)
            if "hold_until_close" in _tt:
                p.text_toolcall_hold_until_close = _coerce_bool(
                    _tt["hold_until_close"], "text_toolcall.hold_until_close")
            if "fallback_to_escalation" in _tt:
                p.text_toolcall_fallback_to_escalation = _coerce_bool(
                    _tt["fallback_to_escalation"],
                    "text_toolcall.fallback_to_escalation")

        # --- TOOLCALL_TRUNCATION ---
        _tct = raw.get("toolcall_truncation")
        if _tct is not None:
            if not isinstance(_tct, dict):
                raise ValueError("toolcall_truncation deve essere una mappa")
            if "enabled" in _tct:
                p.toolcall_truncation_enabled = _coerce_bool(
                    _tct["enabled"], "toolcall_truncation.enabled")
            if "holdback" in _tct:
                p.toolcall_truncation_holdback = _coerce_bool(
                    _tct["holdback"], "toolcall_truncation.holdback")
            _cd = _tct.get("cooldown_sec")
            if _cd is not None:
                if isinstance(_cd, bool) or not isinstance(
                        _cd, (int, float)) or int(_cd) < 1:
                    raise ValueError("toolcall_truncation.cooldown_sec deve "
                                     "essere >= 1")
                p.toolcall_truncation_cooldown_sec = int(_cd)

        sdg = raw.get("session_dep_guard")
        if sdg is not None:
            if not isinstance(sdg, dict):
                raise ValueError("session_dep_guard deve essere una mappa")
            if "enabled" in sdg:
                p.session_dep_guard_enabled = _coerce_bool(
                    sdg["enabled"], "session_dep_guard.enabled")
            if sdg.get("sec") is not None:
                _v = sdg["sec"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(
                        f"session_dep_guard.sec non valido: {_v!r}")
                p.session_dep_guard_sec = int(_v)

        wp = raw.get("warm_pool")
        if wp is not None:
            if not isinstance(wp, dict):
                raise ValueError("warm_pool deve essere una mappa")
            if "enabled" in wp:
                p.warm_pool_enabled = _coerce_bool(
                    wp["enabled"], "warm_pool.enabled")
            if wp.get("ttl_sec") is not None:
                _v = wp["ttl_sec"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(f"warm_pool.ttl_sec non valido: {_v!r}")
                p.warm_pool_ttl_sec = int(_v)
            if wp.get("max_attempts") is not None:
                _v = wp["max_attempts"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(
                        f"warm_pool.max_attempts non valido: {_v!r}")
                p.warm_pool_max_attempts = int(_v)
            if "allow_slow" in wp:
                p.warm_pool_allow_slow = _coerce_bool(
                    wp["allow_slow"], "warm_pool.allow_slow")
            if "refill_enabled" in wp:
                p.warm_refill_enabled = _coerce_bool(
                    wp["refill_enabled"], "warm_pool.refill_enabled")
            if wp.get("ready_min") is not None:
                _v = wp["ready_min"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(
                        f"warm_pool.ready_min non valido: {_v!r}")
                p.warm_ready_min = int(_v)
            if "ready_min_adaptive" in wp:
                p.warm_ready_rpm_adaptive = _coerce_bool(
                    wp["ready_min_adaptive"], "warm_pool.ready_min_adaptive")
            if wp.get("ready_min_rpm_window_sec") is not None:
                _v = wp["ready_min_rpm_window_sec"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v <= 0:
                    raise ValueError(
                        f"warm_pool.ready_min_rpm_window_sec non valido: {_v!r}")
                p.warm_ready_rpm_window_sec = int(_v)
            if wp.get("ready_min_rpm_base") is not None:
                _v = wp["ready_min_rpm_base"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(
                        f"warm_pool.ready_min_rpm_base non valido: {_v!r}")
                p.warm_ready_rpm_base = float(_v)
            if wp.get("ready_min_rpm_step") is not None:
                _v = wp["ready_min_rpm_step"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v <= 0:
                    raise ValueError(
                        f"warm_pool.ready_min_rpm_step non valido: {_v!r}")
                p.warm_ready_rpm_step = float(_v)
            if wp.get("ready_min_max") is not None:
                _v = wp["ready_min_max"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(
                        f"warm_pool.ready_min_max non valido: {_v!r}")
                p.warm_ready_min_max = int(_v)
            if "canary_warm_last" in wp:
                p.canary_warm_last = _coerce_bool(
                    wp["canary_warm_last"], "warm_pool.canary_warm_last")
            if wp.get("refill_default_out_tokens") is not None:
                _v = wp["refill_default_out_tokens"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v <= 0:
                    raise ValueError(
                        f"warm_pool.refill_default_out_tokens non valido: {_v!r}")
                p.warm_refill_default_out_tokens = int(_v)
            if wp.get("max_inflight") is not None:
                _v = wp["max_inflight"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(
                        f"warm_pool.max_inflight non valido: {_v!r}")
                p.warm_refill_max_inflight = int(_v)
            if wp.get("wake_max_attempts") is not None:
                _v = wp["wake_max_attempts"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(
                        f"warm_pool.wake_max_attempts non valido: {_v!r}")
                p.warm_refill_wake_max_attempts = int(_v)
            if "borrow_enabled" in wp:
                p.warm_borrow_enabled = _coerce_bool(
                    wp["borrow_enabled"], "warm_pool.borrow_enabled")
            if wp.get("borrow_idle_sec") is not None:
                _v = wp["borrow_idle_sec"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(
                        f"warm_pool.borrow_idle_sec non valido: {_v!r}")
                p.warm_borrow_idle_sec = float(_v)
            if "borrow_selectable" in wp:
                p.warm_borrow_selectable = _coerce_bool(
                    wp["borrow_selectable"], "warm_pool.borrow_selectable")
            if "nonstream_canary_allowed" in wp:
                p.nonstream_canary_allowed = _coerce_bool(
                    wp["nonstream_canary_allowed"],
                    "warm_pool.nonstream_canary_allowed")
            if wp.get("slow_race_after_ms") is not None:
                _v = wp["slow_race_after_ms"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(
                        f"warm_pool.slow_race_after_ms non valido: {_v!r}")
                p.stream_slow_race_after_ms = int(_v)
            if wp.get("nonstream_slow_race_after_ms") is not None:
                _v = wp["nonstream_slow_race_after_ms"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(
                        "warm_pool.nonstream_slow_race_after_ms non valido: "
                        f"{_v!r}")
                p.nonstream_slow_race_after_ms = int(_v)
            if wp.get("slow_race_canaries") is not None:
                _v = wp["slow_race_canaries"]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 1:
                    raise ValueError(
                        f"warm_pool.slow_race_canaries non valido: {_v!r}")
                p.stream_slow_race_canaries = int(_v)
        if raw.get("warm_borrow_enabled") is not None:
            p.warm_borrow_enabled = _coerce_bool(
                raw["warm_borrow_enabled"], "warm_borrow_enabled")
        if raw.get("warm_borrow_idle_sec") is not None:
            _v = raw["warm_borrow_idle_sec"]
            if isinstance(_v, bool) or not isinstance(_v, (int, float)) or _v < 0:
                raise ValueError(f"warm_borrow_idle_sec non valido: {_v!r}")
            p.warm_borrow_idle_sec = float(_v)
        if raw.get("warm_borrow_selectable") is not None:
            p.warm_borrow_selectable = _coerce_bool(
                raw["warm_borrow_selectable"], "warm_borrow_selectable")
        if raw.get("nonstream_canary_allowed") is not None:
            p.nonstream_canary_allowed = _coerce_bool(
                raw["nonstream_canary_allowed"], "nonstream_canary_allowed")
        for _k, _attr in (("stream_slow_race_after_ms",
                           "stream_slow_race_after_ms"),
                          ("nonstream_slow_race_after_ms",
                           "nonstream_slow_race_after_ms"),
                          ("stream_slow_race_canaries",
                           "stream_slow_race_canaries")):
            if raw.get(_k) is not None:
                _v = raw[_k]
                if isinstance(_v, bool) or not isinstance(_v, (int, float)) \
                        or _v < 0:
                    raise ValueError(f"{_k} non valido: {_v!r}")
                setattr(p, _attr, int(_v))

        ca = raw.get("cache_aware")
        if ca is not None:
            if not isinstance(ca, dict):
                raise ValueError("cache_aware deve essere una mappa")
            if "enabled" in ca:
                p.cache_aware_enabled = _coerce_bool(ca["enabled"], "cache_aware.enabled")
            if "prefix_audit" in ca:
                p.cache_prefix_audit = _coerce_bool(
                    ca["prefix_audit"], "cache_aware.prefix_audit")
            if "prefer_last_success" in ca:
                p.cache_prefer_last_success = _coerce_bool(
                    ca["prefer_last_success"], "cache_aware.prefer_last_success")
            if ca.get("holder_ttl_sec") is not None:
                p.cache_holder_ttl_sec = int(ca["holder_ttl_sec"])
            if "skip_probe_when_holder" in ca:
                p.cache_skip_probe_when_holder = _coerce_bool(
                    ca["skip_probe_when_holder"], "cache_aware.skip_probe_when_holder")
            ct = ca.get("context_truncation")
            if ct is not None:
                if not isinstance(ct, dict):
                    raise ValueError("cache_aware.context_truncation deve essere una mappa")
                if "enabled" in ct:
                    p.cache_ctx_truncation_enabled = _coerce_bool(
                        ct["enabled"], "cache_aware.context_truncation.enabled")
                for _k, _attr in (("keep_turns", "cache_ctx_keep_turns"),
                                  ("max_tool_output_chars", "cache_ctx_max_tool_output_chars"),
                                  ("min_saved_tokens", "cache_ctx_min_saved_tokens"),
                                  ("min_ctx_tokens", "cache_ctx_min_ctx_tokens"),
                                  ("switch_min_tokens", "cache_ctx_switch_min_tokens"),
                                  ("head_chars", "cache_ctx_head_chars"),
                                  ("tail_chars", "cache_ctx_tail_chars"),
                                  ("tool_args_max_chars",
                                   "cache_ctx_tool_args_max_chars"),
                                  ("json_struct_max_items",
                                   "cache_ctx_json_struct_max_items"),
                                  ("json_struct_head",
                                   "cache_ctx_json_struct_head"),
                                  ("json_struct_tail",
                                   "cache_ctx_json_struct_tail"),
                                  ("cite_min_freq",
                                   "cache_ctx_cite_min_freq")):
                    _v = ct.get(_k)
                    if _v is not None:
                        if isinstance(_v, bool) or not isinstance(_v, (int, float)):
                            raise ValueError(f"cache_aware.context_truncation.{_k} deve essere un intero")
                        setattr(p, _attr, int(_v))
                _ktp = ct.get("keep_tail_pct")
                if _ktp is not None:
                    if isinstance(_ktp, bool) or not isinstance(_ktp, (int, float)):
                        raise ValueError("cache_aware.context_truncation."
                                         "keep_tail_pct deve essere un numero")
                    p.cache_ctx_keep_tail_pct = max(0.0, float(_ktp))
                if "keep_error_outputs" in ct:
                    p.cache_ctx_keep_error_outputs = _coerce_bool(
                        ct["keep_error_outputs"],
                        "cache_aware.context_truncation.keep_error_outputs")
                if "on_deployment_switch" in ct:
                    p.cache_ctx_on_deployment_switch = _coerce_bool(
                        ct["on_deployment_switch"],
                        "cache_aware.context_truncation.on_deployment_switch")
                if ct.get("stub_text"):
                    p.cache_ctx_stub_text = str(ct["stub_text"])
                _ahr = ct.get("abs_headroom_ratio")
                if _ahr is not None:
                    if isinstance(_ahr, bool) or not isinstance(_ahr, (int, float)):
                        raise ValueError("cache_aware.context_truncation."
                                         "abs_headroom_ratio deve essere un numero")
                    p.cache_ctx_abs_headroom_ratio = max(0.0, float(_ahr))
                _rhr = ct.get("reasoning_headroom_ratio")
                if _rhr is not None:
                    if isinstance(_rhr, bool) or not isinstance(_rhr, (int, float)):
                        raise ValueError("cache_aware.context_truncation."
                                         "reasoning_headroom_ratio deve essere un numero")
                    p.cache_ctx_reasoning_headroom_ratio = max(0.0, float(_rhr))
                _rrr = ct.get("reasoning_reserve_ratio")
                if _rrr is not None:
                    if isinstance(_rrr, bool) or not isinstance(_rrr, (int, float)):
                        raise ValueError("cache_aware.context_truncation."
                                         "reasoning_reserve_ratio deve essere un numero")
                    p.cache_ctx_reasoning_reserve_ratio = max(0.0,
                                                              float(_rrr))
                if "cite_retention" in ct:
                    p.cache_ctx_cite_retention = _coerce_bool(
                        ct["cite_retention"],
                        "cache_aware.context_truncation.cite_retention")
        # --- DEBUG (sniff input/output) ---
        _dbg = raw.get("debug")
        if _dbg is not None:
            if not isinstance(_dbg, dict):
                raise ValueError("debug deve essere una mappa")
            _sn = _dbg.get("sniff")
            if _sn is not None:
                if not isinstance(_sn, dict):
                    raise ValueError("debug.sniff deve essere una mappa")
                if "enabled" in _sn:
                    p.debug_sniff_enabled = _coerce_bool(
                        _sn["enabled"], "debug.sniff.enabled")
                if _sn.get("retention_hours") is not None:
                    _rh = _sn["retention_hours"]
                    if isinstance(_rh, bool) or \
                            not isinstance(_rh, (int, float)) or int(_rh) < 1:
                        raise ValueError("debug.sniff.retention_hours deve "
                                         "essere >= 1")
                    p.debug_sniff_retention_hours = int(_rh)
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
                        or not (2000 <= int(v) <= 900000):
                    raise ValueError("qc_json.stream_first_content_ms deve "
                                     "essere 2000..900000")
                p.qc_json.stream_first_content_ms = int(v)
            if "stream_first_content_adaptive" in qj:
                p.qc_json.stream_first_content_adaptive = _coerce_bool(
                    qj["stream_first_content_adaptive"],
                    "qc_json.stream_first_content_adaptive")
            if "stream_first_content_mult" in qj:
                v = qj["stream_first_content_mult"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0.5 <= float(v) <= 30.0):
                    raise ValueError("qc_json.stream_first_content_mult deve "
                                     "essere 0.5..30.0")
                p.qc_json.stream_first_content_mult = float(v)
            if "stream_first_content_floor_ms" in qj:
                v = qj["stream_first_content_floor_ms"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0 <= int(v) <= 900000):
                    raise ValueError("qc_json.stream_first_content_floor_ms "
                                     "deve essere 0..900000")
                p.qc_json.stream_first_content_floor_ms = int(v)
            if "stream_hedge_delay_ms" in qj:
                v = qj["stream_hedge_delay_ms"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0 <= int(v) <= 60000):
                    raise ValueError("qc_json.stream_hedge_delay_ms deve "
                                     "essere 0..60000")
                p.qc_json.stream_hedge_delay_ms = int(v)
            if "stream_hedge_ttft_frac" in qj:
                v = qj["stream_hedge_ttft_frac"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0.05 <= float(v) <= 5.0):
                    raise ValueError("qc_json.stream_hedge_ttft_frac deve "
                                     "essere 0.05..5.0")
                p.qc_json.stream_hedge_ttft_frac = float(v)
            if "stream_hedge_min_ms" in qj:
                v = qj["stream_hedge_min_ms"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0 <= int(v) <= 60000):
                    raise ValueError("qc_json.stream_hedge_min_ms deve "
                                     "essere 0..60000")
                p.qc_json.stream_hedge_min_ms = int(v)
            if "stream_hedge_max_ms" in qj:
                v = qj["stream_hedge_max_ms"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0 <= int(v) <= 60000):
                    raise ValueError("qc_json.stream_hedge_max_ms deve "
                                     "essere 0..60000")
                p.qc_json.stream_hedge_max_ms = int(v)
            if "stream_hedge_cross_tier" in qj:
                p.qc_json.stream_hedge_cross_tier = _coerce_bool(
                    qj["stream_hedge_cross_tier"],
                    "qc_json.stream_hedge_cross_tier")
            if "stream_hedge_tiers" in qj:
                v = qj["stream_hedge_tiers"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (1 <= int(v) <= 2):
                    raise ValueError("qc_json.stream_hedge_tiers deve essere 1..2")
                p.qc_json.stream_hedge_tiers = int(v)
            if "stream_hedge_max_races" in qj:
                v = qj["stream_hedge_max_races"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (0 <= int(v) <= 64):
                    raise ValueError("qc_json.stream_hedge_max_races deve "
                                     "essere 0..64 (0=illimitato)")
                p.qc_json.stream_hedge_max_races = int(v)
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
                        or not (5000 <= int(v) <= 3600000):
                    raise ValueError("qc_json.stream_total_deadline_ms deve "
                                     "essere 5000..3600000")
                p.qc_json.stream_total_deadline_ms = int(v)
            if "stream_commit_include_reasoning" in qj:
                p.qc_json.stream_commit_include_reasoning = _coerce_bool(
                    qj["stream_commit_include_reasoning"],
                    "qc_json.stream_commit_include_reasoning")
            if "stream_hold_until_finish" in qj:
                p.qc_json.stream_hold_until_finish = _coerce_bool(
                    qj["stream_hold_until_finish"],
                    "qc_json.stream_hold_until_finish")
            if "stream_hold_idle_ms" in qj:
                v = qj["stream_hold_idle_ms"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (1000 <= int(v) <= 600000):
                    raise ValueError("qc_json.stream_hold_idle_ms deve "
                                     "essere 1000..600000")
                p.qc_json.stream_hold_idle_ms = int(v)
            if "stream_hold_max_buffer_bytes" in qj:
                v = qj["stream_hold_max_buffer_bytes"]
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not (1048576 <= int(v) <= 524288000):
                    raise ValueError("qc_json.stream_hold_max_buffer_bytes "
                                     "deve essere 1048576..524288000")
                p.qc_json.stream_hold_max_buffer_bytes = int(v)
            if "stream_parachute_no_timeout" in qj:
                p.qc_json.stream_parachute_no_timeout = _coerce_bool(
                    qj["stream_parachute_no_timeout"],
                    "qc_json.stream_parachute_no_timeout")
            for _k, _attr in (("struct_out_enabled", "struct_out_enabled"),
                              ("rewrite_content", "rewrite_content"),
                              ("strict_schema", "strict_schema"),
                              ("repair_content", "repair_content"),
                              ("inject_response_format",
                               "inject_response_format"),
                              ("downgrade_response_format",
                               "downgrade_response_format")):
                if _k in qj:
                    setattr(p.qc_json, _attr, _coerce_bool(
                        qj[_k], f"qc_json.{_k}"))
            _iap = qj.get("inject_allow_providers")
            if _iap is not None:
                if not isinstance(_iap, (list, tuple)):
                    raise ValueError("qc_json.inject_allow_providers "
                                     "deve essere una lista")
                p.qc_json.inject_allow_providers = tuple(
                    str(x).lower() for x in _iap)
            _nsp = qj.get("native_schema_providers")
            if _nsp is not None:
                if not isinstance(_nsp, (list, tuple)):
                    raise ValueError("qc_json.native_schema_providers "
                                     "deve essere una lista")
                p.qc_json.native_schema_providers = tuple(
                    str(x).lower() for x in _nsp)
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
        _set_int(p, raw, "timeout_cooldown_mult", minimum=1)

        cm = raw.get("cooldown_mode")
        if cm is not None:
            if str(cm) not in ("linear", "exponential"):
                raise ValueError("cooldown_mode non valido: ammessi linear|exponential")
            p.cooldown_mode = str(cm)
        _gpm = raw.get("go_preferred_models")
        if _gpm is not None:
            if isinstance(_gpm, (list, tuple)):
                _gpm = ",".join(str(x) for x in _gpm)
            p.go_preferred_models = str(_gpm).strip().lower()
        _set_int(p, raw, "cooldown_base_min", minimum=1)
        _set_int(p, raw, "cooldown_linear_mult_min", minimum=0)
        _set_int(p, raw, "ladder_skip_after", minimum=1)
        _set_int(p, raw, "ladder_stale_max", minimum=0)
        _set_int(p, raw, "ladder_cooldown_wakeups", minimum=0)
        _set_int(p, raw, "ladder_cooldown_wakeup_window_sec", minimum=1)
        _csp = raw.get("cold_spread_pct")
        if _csp is not None:
            try:
                _v = float(_csp)
            except (TypeError, ValueError):
                raise ValueError(f"cold_spread_pct non valido: {_csp!r}")
            if not 0.0 <= _v <= 1.0:
                raise ValueError("cold_spread_pct deve essere in [0,1]")
            p.cold_spread_pct = _v
        _ipw = raw.get("initial_pick_cooldown_wakeup")
        if _ipw is not None:
            p.initial_pick_cooldown_wakeup = _coerce_bool(
                _ipw, "initial_pick_cooldown_wakeup")
        _set_int(p, raw, "cooldown_retry_max_fail_24h", minimum=1)
        _set_int(p, raw, "ladder_chronic_max", minimum=0)
        _set_int(p, raw, "chronic_fail_cooldown_sec", minimum=0)

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
            if "safety_ratio" in bg:
                try:
                    v = float(bg["safety_ratio"])
                except (TypeError, ValueError):
                    raise ValueError(
                        "budget_guard.safety_ratio: numero richiesto") from None
                if v < 0:
                    raise ValueError(
                        "budget_guard.safety_ratio: >= 0 richiesto")
                merged["safety_ratio"] = v
            if "count_inflight" in bg:
                merged["count_inflight"] = _coerce_bool(
                    bg["count_inflight"], "budget_guard.count_inflight")
            if "rate_hint_threshold" in bg:
                try:
                    v = int(bg["rate_hint_threshold"])
                except (TypeError, ValueError):
                    raise ValueError(
                        "budget_guard.rate_hint_threshold: intero richiesto") \
                        from None
                if v < 1:
                    raise ValueError(
                        "budget_guard.rate_hint_threshold: >= 1 richiesto")
                merged["rate_hint_threshold"] = v
            if "suppress_with_headers" in bg:
                merged["suppress_with_headers"] = _coerce_bool(
                    bg["suppress_with_headers"],
                    "budget_guard.suppress_with_headers")
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
            if "rotate_on_length_truncated" in qs:
                p.qc_sanity.rotate_on_length_truncated = _coerce_bool(
                    qs["rotate_on_length_truncated"],
                    "qc_sanity.rotate_on_length_truncated")

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


def refill_out_budget(payload: dict, policy) -> int:
    """Budget di output con cui si valuta la DELIVERABILITY nel warm-refill:
    il max_tokens chiesto dal client, o il default di policy quando il client
    non lo chiede (un caldo che non puo' consegnare questi token NON conta
    nei "pronti-caldi")."""
    try:
        v = int(payload.get("max_tokens")
                or payload.get("max_completion_tokens") or 0)
    except (TypeError, ValueError):
        v = 0
    if v <= 0:
        v = int(getattr(policy, "warm_refill_default_out_tokens", 4096) or 4096)
    return v
