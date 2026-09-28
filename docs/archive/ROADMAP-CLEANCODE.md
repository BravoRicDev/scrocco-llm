# ROADMAP Clean Code — Round 4 — traccia MAIN (app/main.py)

Base verificata: HEAD = origin/master = `dc7805c` (0 avanti/0 dietro).
`app/main.py`: 7892 righe, 105 funzioni/metodi top-level (`grep -n "^def \|^async def \|^class "` + fine calcolata come ultima riga non vuota prima del prossimo `def` top-level).
Baseline test: 2469 passed / 8 failed preesistenti (tests/test_stream_zerotoken.py x7, tests/test_upstream_401.py x1) / 1 skipped.

Precedenti da rispettare: `app/sse_utils.py` (M1, helper puri verbatim), `app/compat/ollama.py` (M2, APIRouter + `@app.get`→`@router.get` + `import app.main as M` lazy + `include_router` dopo `app = FastAPI(...)`).

Questa roadmap NON riusa alcuna roadmap precedente (obsolete, basate su commit vecchi). Ogni confine sotto e' stato verificato con script AST-like su righe reali a `dc7805c`.

---

## Struttura non estraibile questo giro (NON cluster, non toccare)

- **Righe 1-337**: import, setup logging colorato, `policy = Policy.load_or_default(...)`, `config = GatewayConfig(...)`, `router = Router(config, policy)`, `authn = AuthManager(...)`, `forwarder = Forwarder(...)`, decine di `set_*` di wiring, dichiarazione variabili globali mutabili (`_watch_task`, `_stats_file`, `_cooldown_file`, `_routing_file`, `_videos_jobs`, ecc.). Sono i singleton che TUTTO il resto del file referenzia per nome nudo (`router`, `policy`, `config`, `authn`, `forwarder`). Spostarli rompe ogni riferimento nudo nel file: nessun adattatore pulito esiste per questo giro. **ESCLUSO.**
- **Righe 949-1097**: `lifespan(...)`, `app = FastAPI(title=policy.service_name, ..., lifespan=lifespan)` (riga 1029), `app.include_router(admin_api)`, `app.include_router(bootstrap_api)`, `@app.exception_handler(AppError)` / `@app.exception_handler(Exception)`. Punto di creazione dell'oggetto `app`: deve restare in `main.py`. **ESCLUSO.**
- **Riga 7885-7892**: `def main()` (entrypoint `pragma: no cover`). Banale, non vale lo spostamento. **ESCLUSO.**

---

## Cluster proposti

### C1 — `app/runtime_persistence.py`
- Righe: 338-948 (611 righe)
- Metodi (24): `set_video_job_ttl_sec`, `set_coalesce_cache_max`, `_apply_misc_policy`, `_coalesce_cache_take`, `_coalesce_cacheable`, `_coalesce_cache_put`, `_coalesce_key`, `_nonstream_hold_redirect`, `_forward_coalesced`, `_load_adaptive_stats`, `_load_thought_sigs`, `_maybe_save_thought_sigs`, `_maybe_save_adaptive_stats`, `_maybe_save_all`, `_load_cooldowns`, `_maybe_save_routing_state`, `_load_routing_state`, `_bootstrap_runtime_from_logs`, `_maybe_save_cooldowns`, `_all_uniques`, `_all_deps`, `_watcher`, `seconds_to_midnight`, `_nightly_scheduler`
- Responsabilita': persistenza stato adattivo/cooldown/routing su disco + coalesce cache + watcher/nightly scheduler in background.
- Rischio: **MEDIO-ALTO**. 7 siti con `global` (`VIDEO_JOB_TTL_SEC`, `_COALESCE_CACHE_MAX`, `_last_stats_save` x3, `_last_routing_save` x2, `_last_cooldown_save`) che mutano variabili modulo definite in `main.py` (righe 314-335). Spostando le funzioni, `global X` punterebbe al namespace del NUOVO modulo, non a quello di `main.py`: le variabili non si aggiornerebbero piu' dove il resto del file le legge. Serve riscrivere ogni mutazione come `M.X = ...` (via `import app.main as M` lazy), non un semplice cut-paste.
- Dipendenze: legge `router`, `policy`, `VAR_DIR`, `_stats_file`, `_cooldown_file`, `_routing_file`, `_videos_jobs` (globals di `main.py`) → `import app.main as M` lazy dentro ogni funzione (pattern M2). `_bootstrap_runtime_from_logs` e' chiamata da `lifespan` (che resta in `main.py`): `main.py` importera' normalmente `from .runtime_persistence import _bootstrap_runtime_from_logs, ...` in testa (nessun ciclo, l'import lazy sta solo nel verso opposto).
- Dipendenze da altri cluster: nessuna a monte. E' un prerequisito concettuale per capire lo stato usato da C-chat (escluso).

### C2 — `app/models_and_health.py`
- Righe: 1098-1355 (258 righe)
- Metodi (12): `liveliness`, `healthz`, `metrics_endpoint`, `_stable_names`, `_names_for_auth`, `_view_for`, `_deps_for_name`, `_dedup_deps`, `_caps_and_deps`, `_model_entry`, `list_models`, `_visible_model_names`
- Responsabilita': endpoint di health/metrics + endpoint e helper `/v1/models`.
- Rischio: **BASSO**. Nessuna closure annidata, nessun `global`. 3 endpoint reali (`liveliness`, `healthz`, `list_models` sono `@app.get`) → pattern M2: `APIRouter`, `@app.get`→`@router.get`, `import app.main as M` lazy per `router`/`policy`/`authn`, `app.include_router(...)` in `main.py` dopo la creazione di `app` (dopo riga 1097).
- Nota dimensione: sotto la soglia 300 ma cluster coeso (tutti endpoint/helper leggeri senza stato); non vale la pena fonderlo con C1 (persistenza, tema diverso) ne' con C3 (prep chat, tema diverso).
- Dipendenze da altri cluster: nessuna.

### C3 — `app/chat_request_prep.py`
- Righe: 1356-1758 (403 righe)
- Metodi (14): `_text_of`, `_anon_session_fingerprint`, `_session_id`, `_client_ip`, `_set_opencode_gate`, `_opencode_session`, `_sniff_headers`, `_emit_summary`, `_cached_tokens_of`, `_usage_of`, `_auto_learn_apply`, `_strike_hook`, `_note_fb_refund`, `_apply_go_refund`
- Responsabilita': helper di preparazione/telemetria per la richiesta chat (fingerprint sessione, opencode-gate, summary log, auto-learn capacita', refund).
- Rischio: **BASSO-MEDIO**. Nessun `global`; solo letture di `router`/`policy` (verificato via grep, es. `router.config.deployment_by_unique`, `router.policy.cap_auto_learn`, `router.note_cap_strike`). Chiamate da `chat_completions` (che resta in `main.py`, cluster escluso sotto): nessun ciclo di import se `chat_request_prep` fa `import app.main as M` lazy per `router`/`policy` e `main.py` importa le funzioni normalmente in testa.
- Dipendenze da altri cluster: nessuna a monte; e' usato dal blocco escluso `chat_completions`.

### C4 — `app/stream_verdicts.py`
- Righe: 2649-2803 (155 righe)
- Metodi (7): `_actionable_upstream_error`, `_soft_cd`, `_discard_stream`, `_exhausted`, `_retry_at_ms`, `_payload_text_empty`, `_parachute_verdict`
- Responsabilita': classificazione errori upstream / verdetti di retry-cooldown usati dal motore di streaming.
- Rischio: **BASSO**. Nessuna closure, funzioni corte e pure o quasi-pure.
- Nota dimensione: cluster piccolo (155 righe) ma non fondibile con i vicini contigui: e' incastrato tra `_apply_go_refund` (fine C3, 1758) e `_hedge_peek` (2806, escluso sotto) — non contiguo con altro materiale estraibile.
- Dipendenze: verificare a mano eventuali riferimenti a `router`/`policy` prima dell'esecuzione (non ancora grep-ati riga per riga in questo giro); presumibilmente lettura sola, stesso pattern di C3.
- Dipendenze da altri cluster: nessuna; e' usato da `_hedge_peek` e `_stream_with_fallback` (entrambi esclusi sotto) — quindi va estratto PRIMA se si vorra' mai toccare quei due in un giro futuro.

### C5 — `app/streaming_support.py`
- Righe: 3418-3970 (553 righe)
- Metodi (10): `_drain_probe_tasks`, `_probe_drain_cap_sec`, `_consume_probe_stream`, `_spawn_probe` (contiene closure annidata `_run` a riga 3491), `_probe_late_open`, `_spawn_wake_sweep`, `_wake_sweep`, `_trim_chat_images`, `_stt_bridge_transcribe` (contiene closures annidate `_tier` riga 3803, `_key` riga 3843), `_stt_bridge` (contiene closure annidata `_one` riga 3955)
- Responsabilita': gestione probe/canary paralleli, wake-sweep, trim immagini chat, bridge STT (transcribe).
- Rischio: **MEDIO**. Le closure annidate (`_run`, `_tier`, `_key`, `_one`) catturano SOLO variabili locali della propria funzione padre, non variabili di `main.py`: spostando la funzione padre INTERA la closure resta valida (nessun Introduce Parameter Object necessario qui, a differenza di `_hedge_peek`/`_stream_with_fallback`). Rischio residuo: dipendenze da `router`/`policy`/`forwarder` da verificare funzione per funzione con `import app.main as M` lazy.
- Dipendenze da altri cluster: nessuna a monte.

### C6 — `app/image_helpers.py`
- Righe: 5804-6522 (719 righe)
- Metodi (14): `_data_uri`, `_public_base_url`, `_b64decode`, `_download_remote_image`, `_localize_images`, `_images_group_for_base`, `_cap_chain_pick_all`, `_cap_chain_pick`, `_images_pick_dep`, `_chat_via_images`, `_profile_of_request`, `_image_chat_intercept` (contiene closure annidata `_serve` a riga 6175), `_try_native_image_edit`, `_images_chat_loop`
- Responsabilita': utility immagini (data-uri, localizzazione remota, scelta dep per capability-chain) + intercettazione/loop chat-verso-immagini.
- Rischio: **MEDIO**. Closure `_serve` resta dentro `_image_chat_intercept` (nessuno split). Uso pesante di `router`/`policy`/`forwarder` per capability-matching: verificare ogni riferimento prima di spostare.
- Dipendenze da altri cluster: nessuna a monte. E' un prerequisito per C7 (endpoint immagini).

### C7 — `app/images_api.py`
- Righe: 6523-6965 (443 righe)
- Metodi (3): `images_generations` (`@app.post`, contiene closure annidata `_attempt_via_chat` a riga 6632), `images_edits` (`@app.post`), `images_files` (`@app.get`)
- Responsabilita': endpoint pubblici `/v1/images/*`.
- Rischio: **MEDIO-ALTO**. Sono route reali: serve pattern M2 (`APIRouter`, decoratori riscritti, `import app.main as M` lazy, `include_router` dopo creazione `app`). `images_generations` e' grande (~300 righe) con closure propria: la closure resta con la funzione, ma l'intera funzione e' un endpoint critico in produzione — testare a fondo dopo lo spostamento.
- Dipendenze: richiede C6 (`app/image_helpers.py`) gia' estratto.
- Dipendenze da altri cluster: dopo C6.

### C8 — `app/audio_api.py`
- Righe: 6966-7541 (576 righe)
- Metodi (6): `_audio_route`, `audio_speech` (`@app.post`), `systemone` (`@app.post`), `_audio_transcribe`, `audio_transcriptions` (`@app.post`), `audio_translations` (`@app.post`)
- Responsabilita': endpoint pubblici `/v1/audio/*` e `/v1/systemone`.
- Rischio: **MEDIO**. Stesso pattern M2. `systemone` (7153-7354, ~200 righe) e `_audio_transcribe` (7355-7528, ~170 righe) sono corpose ma senza closure annidate rilevate.
- Dipendenze da altri cluster: nessuna hard-dependency da C6/C7, ma stesso pattern di rischio; eseguire dopo C7 per riusare l'esperienza del primo endpoint-router.

### C9 — `app/videos_api.py`
- Righe: 7542-7882 (341 righe)
- Metodi (4): `videos_generations` (`@app.post`), `_job_deps`, `videos_status` (`@app.get`), `videos_content` (`@app.get`)
- Responsabilita': endpoint pubblici `/v1/videos/generations*` (job asincroni in-memory).
- Rischio: **MEDIO**. Usa `_videos_jobs` (dict globale di `main.py`, dichiarato riga 334) in lettura/scrittura: verificare se ci sono mutazioni dirette (`_videos_jobs[...] = ...`) che richiederebbero `M._videos_jobs[...]`, non un semplice rebind (i dict mutano in-place quindi `import app.main as M; M._videos_jobs[k]=v` funziona senza bisogno di `global`).
- Dipendenze da altri cluster: nessuna.

---

## Cluster esclusi (con motivazione)

1. **Righe 1-337 (bootstrap/wiring singleton)** — vedi sopra. Nessun adattatore pulito; tutto il file referenzia `router`/`policy`/`config`/`authn`/`forwarder` per nome nudo.
2. **Righe 949-1097 (`lifespan` + creazione `app` + exception handler)** — punto di creazione dell'oggetto `FastAPI`; deve restare in `main.py` per costruzione.
3. **`chat_completions` (righe 1759-2646, 888 righe)** — funzione singola, non un cluster di metodi. 2 closure annidate (`_redirect_once` riga 2454, `_fwd_once` riga 2518) autonome (restano con la funzione se mai spostata), ma il corpo e' il cuore del routing con decine di rami di stato condiviso (`router`, `policy`, `forwarder`, `authn`) e resta l'endpoint piu' critico del gateway. Estrazione prematura senza un refactor dedicato: **ALTO RISCHIO**, non proposta questo giro.
4. **`_hedge_peek` (righe 2806-3415, 610 righe)** — 3 closure annidate che catturano variabili locali della funzione padre in modo profondo (`_peek` riga 2864, `_open_canary`/`_hookB` righe 3067/3075, `_handover_late` riga 3333): il brief richiede di marcare come ALTO RISCHIO o escludere. **Escluso.** Prerequisito per un giro futuro: pattern Introduce Parameter Object per raggruppare lo stato catturato in una classe di contesto, PRIMA di qualunque split.
5. **`_stream_with_fallback` (righe 3973-5803, 1831 righe)** — la funzione singola piu' grande del file (23% delle righe totali). 8 closure annidate (`_ret` 4086, `_next_filtered` 4101, `_fail` 4146, `_trunc_hook` 4174, `sse` 5426, `_summary` 5467, `_ingest` 5491, `_watch_disconnect` 5569) che catturano stato dello streaming (buffer, contatori, hook di troncamento) a piu' livelli di annidamento. **Escluso.** Stesso prerequisito di `_hedge_peek`: Introduce Parameter Object prima di qualsiasi tentativo di split; nessuna estrazione sicura possibile in questo giro.
6. **`main()` (righe 7885-7892)** — entrypoint banale, non vale lo spostamento.

---

## Ordine di esecuzione seriale proposto

1. C2 — `app/models_and_health.py` (nessuna dipendenza, rischio basso, valida il pattern APIRouter su endpoint piccoli)
2. C3 — `app/chat_request_prep.py` (nessuna dipendenza, rischio basso-medio)
3. C4 — `app/stream_verdicts.py` (nessuna dipendenza, rischio basso, piccolo)
4. C5 — `app/streaming_support.py` (nessuna dipendenza, rischio medio, verificare closures una per una)
5. C1 — `app/runtime_persistence.py` (nessuna dipendenza da altri cluster, ma rischio medio-alto: fare DOPO aver rodato il pattern lazy-import sui cluster piu' semplici, per via delle 7 mutazioni `global` da riscrivere con cura)
6. C6 — `app/image_helpers.py` (nessuna dipendenza, rischio medio)
7. C7 — `app/images_api.py` (dipende da C6)
8. C8 — `app/audio_api.py` (nessuna hard-dependency, dopo C7 per riuso esperienza)
9. C9 — `app/videos_api.py` (nessuna dipendenza)

`chat_completions`, `_hedge_peek`, `_stream_with_fallback` restano fuori roadmap: valutarli in un giro dedicato SOLO dopo un refactor Introduce Parameter Object su ciascuno.

ROADMAP R4 COMPLETA
