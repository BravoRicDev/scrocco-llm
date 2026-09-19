"""HTTP verso gli upstream: chiamata, streaming, fallback di catena.

[EN] WHAT: the single place that talks to upstream providers (pure httpx, no
litellm). HOW: call/stream_response/call_images/speech/submit_video with a
precise error taxonomy; call_with_fallback walks the chain. Cooldowns are
classified (positive-status retryable vs model-missing vs client-side). Also
clamps max_tokens to the deployment window and builds upstream session /
opencode headers. See docs/ARCHITECTURE.md.

[IT] COSA: punto unico di dialogo coi provider (httpx puro, NIENTE
litellm). HOW: call/stream_response/call_images/speech/submit_video con
tassonomia errori precisa; call_with_fallback cammina la catena. WHY la
tassonomia (cuore del failover corretto):
  - status POSITIVO (429/5xx/timeout): ritriabile, cooldown breve.
  - -404: modello inesistente SU QUESTO provider -> ruota, cooldown lungo.
  - _MODEL_MISSING_RE: cloudflare & co. rispondono 400 "No such model" in
    formato proprietario -> stesso trattamento del 404, mai raw al client.
  - _THOUGHT_SIG_RE: Gemini 3 pretende il blob `thought_signature` sui
    functionCall rigiocati; il gateway e' passthrough OpenAI e non puo'
    sintetizzarlo -> ruota SENZA cooldown (come length), consegna il 400
    solo se la catena non ha alternative.
  - -402 / firma openai_error su 400: deployment-side -> ruota; gli ALTRI
    4xx sono colpa CLIENTE -> pass-through.
  - D3 (non-stream): se TUTTI falliscono il QC si consegna l ultimo
    tentativo annotando i motivi in reasoning_content ("meno peggio"
    batte un 500 secco, per un agente).
  - finish_reason=length e contenuto vuoto: budget finito (i reasoning
    token mangiano max_tokens), NON deployment rotto -> consegna SENZA
    ruotare/ne raffreddare chiavi sane.
  - PROBE (qui in fondo): validazione one-shot con cache persistente --
    alcuni free-tier contano le CHIAMATE: healthy key MAI richiamata
    senza force=true; il probe non tocca note_result/mark_failed.

[EN] WHAT: all upstream HTTP + fallback chain. WHY: precise error
taxonomy drives correct rotation; D3 delivers annotated last response;
length-truncation is not a broken deployment; probe is cached one-shot
because some free tiers count calls, not tokens.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import uuid as _uuid
from collections import deque
from typing import AsyncIterator

import httpx
from urllib.parse import urlsplit

from . import metrics
from . import repairlog
from . import protocols as proto
from .csvlearn import (learn_thinking_replay, learn_strip_reasoning,
                       learn_no_thinking, learn_content_string)
from .policy import refill_out_budget
from .qc import check_response
from .router import inject_identity, ErrorKind, estimate_tokens
from .opencode_gate import (is_opencode_dep as _is_opencode_dep,
                            client_is_opencode as _opencode_client_detect,
                            spoof_enabled as _spoof_enabled,
                            is_native_session as _is_native_session,
                            is_opencode_zen_dep,
                            opencode_cautious_request as _opencode_cautious_request)
from .thought_sig import (THOUGHT_SIGS, extract_signatures, get_dummy_fill,
                          is_gemini_deployment)
from .effort import get_effort, get_temperature_config
from .toolrepair import (ToolRepairConfig, ToolRepairSSEFilter,
                         TruncatedToolcallSSEFilter, create_tool_repair_config,
                         repair_tool_calls)
from .fakecall import (fake_config_from_policy, is_escalation_group,
                       message_fake_pattern, sanitize_message)
from .histnorm import (hist_config_from_policy, normalize_messages,
                       flatten_text_content)
from .sampling import (sampling_config_from_policy,
                       apply_sampling_defaults, response_loop_reason,
                       stream_loop_reason)
from .schemaout import (schemaout_config_from_policy, enforce_response,
                        maybe_inject_response_format,
                        downgrade_response_format)
from .texttoolparse import (TruncationConfig, apply_to_message,
                            has_unclosed_toolcall, salvage_truncated_toolcall,
                            text_config_from_policy)


def _corrective_note(kind: str) -> str:
    """Nota di sistema per il retry correttivo (L1 #3)."""
    if kind == "schema":
        return ("La risposta precedente non rispettava lo schema JSON "
                "richiesto. Rispondi di nuovo SOLO con JSON valido "
                "conforme allo schema, senza testo attorno.")
    if kind == "toolcall":
        return ("La risposta precedente conteneva una chiamata a tool "
                "non valida. Rispondi di nuovo usando il meccanismo "
                "di tool-call previsto, senza scriverla come testo.")
    return ("La risposta precedente non era JSON valido. Rispondi di "
            "nuovo SOLO con un oggetto JSON valido, senza testo "
            "attorno.")

log = logging.getLogger("nx.forwarder")

# Provider che NON accettano `reasoning_effort` nel body (400 garantito):
# il token va rimosso anche se il deployment e' dichiarato effort_capable.
EFFORT_INCOMPATIBLE_HOSTS = ("api.groq.com",)


def apply_effort_policy(body: dict, dep: dict) -> dict:
    """Adatta il body all'effort richiesto per lo specifico deployment.

    - effort=default: nessuna modifica.
    - deployment effort_capable (e provider compatibile): garantisce
      `reasoning_effort` (il valore del client vince sempre; se assente si usa
      il livello richiesto).
    - deployment NON capace o provider incompatibile: rimuove
      `reasoning_effort` (evita un 400 upstream).
    - override temperatura: applicato SOLO se la policy lo abilita e il client
      NON ha inviato `temperature` (il client vince sempre).
    """
    effort = get_effort()
    if dep.get("_no_thinking") or dep.get("no_thinking"):
        # rimedio "downgraded" (o flag appreso nel CSV): il provider rifiuta il
        # thinking su questa history -> nessun campo di reasoning per questo
        # tentativo.
        body.pop("reasoning_effort", None)
        body.pop("thinking", None)
        body.pop("reasoning", None)
        return body
    if effort == "default":
        return body
    host = (dep.get("api_base") or "").lower()
    capable = bool(dep.get("effort_capable"))
    incompatible = any(h in host for h in EFFORT_INCOMPATIBLE_HOSTS)
    if capable and not incompatible:
        body.setdefault("reasoning_effort", effort)
    else:
        body.pop("reasoning_effort", None)
    enabled, overrides = get_temperature_config()
    if enabled and "temperature" not in body and effort in overrides:
        try:
            body["temperature"] = float(overrides[effort])
        except (TypeError, ValueError):
            pass
    log.info("[effort] %s effort=%s capable=%s reasoning=%s temp=%s",
             dep.get("unique", "?"), effort, capable,
             body.get("reasoning_effort"), body.get("temperature"))
    return body


def clamp_max_tokens(body: dict, dep: dict, hook=None) -> None:
    """Riduce `max_tokens` perche' input+output non superi il context window
    del deployment.

    L'upstream conta (input stimato + max_output) contro il context window:
    un client che riserva 32000 token di output su un modello 32k fa fallire
    ogni richiesta con >768 token di input (errore -400/-413). Qui il tetto
    viene portato a `max(1, max_input_tokens - ctx)` quando entrambi noti.

    CLAMP CONTESTUALE (F16): oltre al ctx si sottraggono una RISERVA per il
    reasoning block (solo modelli `effort_capable`) e un margine di sicurezza
    del 5%. Cosi' non si chiede piu' output di quanto entra nella finestra —
    la classe di 503 piu' stupida ("provider morto" quando in realta' era
    "hai chiesto troppo output").

    RISERVA CEDEVOLE (fix 2026-09-15): la riserva NON deve mai
    affamare l'output. Con ctx all'80% della finestra su un thinking model la
    riserva del 30% portava `room` sotto zero e il clamp a 512: il modello
    spendeva tutto in reasoning e la risposta partiva gia' troncata
    (finish_reason=length, 0 caratteri utili). Ora la riserva si riduce al
    massimo fino a lasciare intatto il `max_tokens` chiesto dal client, e
    quando non c'e' davvero spazio il floor e' MIN_OUTPUT_FLOOR (4096), non
    512. `hook(old, new)` (opzionale) viene chiamato solo se il valore e'
    stato ridotto: il watchdog lo usa per non punire il modello per una
    troncatura auto-inflitta dal gateway.
    """
    mi = int(dep.get("max_input_tokens") or 0)
    if mi <= 0:
        return
    if body.get("max_completion_tokens") is not None:
        key = "max_completion_tokens"
    elif body.get("max_tokens") is not None:
        key = "max_tokens"
    else:
        return
    try:
        mt = int(body[key])
    except (TypeError, ValueError):
        return
    ctx = estimate_tokens(body.get("messages") or [], _EST_DIVISOR,
                          _EST_IMAGE_TOKENS, tools=body.get("tools"))
    safety = int(mi * 0.05)
    room0 = mi - ctx - safety
    reserve = 0
    if dep.get("effort_capable") and _REASONING_RESERVE_FRAC > 0:
        # La riserva mangia solo il SURPLUS sopra la richiesta: mai il
        # max_tokens chiesto dal client.
        reserve = min(int(mi * _REASONING_RESERVE_FRAC), max(0, room0 - mt))
    room = max(1, room0 - reserve)
    if mt > room:
        cap = max(1, mi - ctx)
        new_mt = min(cap, max(MIN_OUTPUT_FLOOR, room))
        if new_mt < mt:
            body[key] = new_mt
            try:
                metrics.inc("nx_max_tokens_clamped", ())
            except Exception:
                pass
            log.info("[maxtok] %s clamp %s %d->%d (ctx=%d max_in=%d "
                     "riserva_reasoning=%d sicurezza=%d)",
                     dep.get("unique", "?"), key, mt, new_mt, ctx, mi,
                     reserve, safety)
            if hook is not None:
                try:
                    hook(mt, new_mt)
                except Exception:                   # mai rompere la richiesta
                    pass


# Logger dedicato: OGNI body upstream che contiene "error" ci finisce (handler
# su file agganciato in main.py -> var/error-audit.log, solo locale). Serve a
# rivedere a posteriori gli errori usciti che non dovevano.
errlog = logging.getLogger("nx.erroraudit")

_DEFAULT_RETRYABLE_STATUS = frozenset({408, 409, 429} | set(range(500, 600)))
_DEFAULT_EFFORT_INCOMPATIBLE_HOSTS = ("api.groq.com",)
RETRYABLE_STATUS = set(_DEFAULT_RETRYABLE_STATUS)
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0,
                                 pool=10.0)
# Pool connessioni generoso: con centinaia di deployment su molti provider
# vogliamo riusare TCP/TLS (keep-alive) per host:port il piu' possibile.
UPSTREAM_LIMITS = httpx.Limits(max_keepalive_connections=30,
                               max_connections=100,
                               keepalive_expiry=120.0)
# Generazione HTTP: incrementata quando i parametri di trasporto cambiano a
# caldo. I client httpx cachati vengono ricreati pigramente alla prossima
# richiesta (i limiti del pool valgono solo alla creazione del client).
_HTTP_GEN = 0


def set_upstream_http(*, connect=None, read=None, write=None, pool=None,
                      max_keepalive=None, max_connections=None,
                      keepalive_expiry=None) -> None:
    """Aggiorna timeout/pool del client upstream (None = lascia invariato).

    Default identici alle costanti storiche; cambia solo se la policy lo
    richiede. I client cachati vengono ricreati alla prossima richiesta."""
    global UPSTREAM_TIMEOUT, UPSTREAM_LIMITS, _HTTP_GEN
    cur = UPSTREAM_TIMEOUT
    try:
        c = float(connect) if connect is not None else cur.connect
        r = float(read) if read is not None else cur.read
        w = float(write) if write is not None else cur.write
        pl = float(pool) if pool is not None else cur.pool
    except (TypeError, ValueError):
        return
    new_timeout = httpx.Timeout(connect=c, read=r, write=w, pool=pl)
    lim = UPSTREAM_LIMITS
    try:
        mk = (int(max_keepalive) if max_keepalive is not None
              else lim.max_keepalive_connections)
        mc = (int(max_connections) if max_connections is not None
              else lim.max_connections)
        ke = (float(keepalive_expiry) if keepalive_expiry is not None
              else (lim.keepalive_expiry or 0.0))
    except (TypeError, ValueError):
        return
    new_limits = httpx.Limits(max_keepalive_connections=mk,
                              max_connections=mc, keepalive_expiry=ke)
    if new_timeout != UPSTREAM_TIMEOUT or new_limits != UPSTREAM_LIMITS:
        UPSTREAM_TIMEOUT = new_timeout
        UPSTREAM_LIMITS = new_limits
        _HTTP_GEN += 1


def set_retryable_status(codes=None) -> None:
    """Sostituisce l'insieme dei codici ritentabili (None = default storico)."""
    global RETRYABLE_STATUS
    if codes is None:
        RETRYABLE_STATUS = set(_DEFAULT_RETRYABLE_STATUS)
        return
    try:
        RETRYABLE_STATUS = {int(x) for x in codes}
    except (TypeError, ValueError):
        return


def set_effort_incompatible_hosts(hosts=None) -> None:
    """Sostituisce gli host incompatibili con i modelli 'effort' (None=default)."""
    global EFFORT_INCOMPATIBLE_HOSTS
    if hosts is None:
        EFFORT_INCOMPATIBLE_HOSTS = tuple(_DEFAULT_EFFORT_INCOMPATIBLE_HOSTS)
        return
    try:
        EFFORT_INCOMPATIBLE_HOSTS = tuple(str(h) for h in hosts if str(h))
    except TypeError:
        return


# Timeout upstream ADATTIVO per-deployment: la latenza media storica (EMA,
# fornita dal router) scala il read-timeout. Provider veloci (es. TTFB ~300ms)
# vengono tagliati presto se si bloccano; i lenti hanno lo spazio necessario
# per rispondere senza false rotazioni.
#   read = clamp(max(floor, avg_ms/1000 * multiplier), floor, max)
# Si applica al percorso CHAT (stream e non-stream); media/img/tts/video
# mantengono i loro timeout dedicati. False = timeout globale fisso.
ADAPTIVE_TIMEOUT = True
TIMEOUT_FLOOR_SEC = 15.0
TIMEOUT_MULTIPLIER = 8.0
TIMEOUT_MAX_SEC = 600.0
_LATENCY_LOOKUP = None
# F16: frazione della finestra riservata al reasoning block sui modelli
# `effort_capable` (0 = nessuna riserva). 1 - reasoning_headroom_ratio.
_REASONING_RESERVE_FRAC = 0.30
# Floor minimo di max_tokens dopo il clamp: 512 era di fatto una troncatura
# garantita (su un modello thinking 512 token sono solo reasoning, zero
# risposta). Sotto questo valore meglio None che una risposta monca.
MIN_OUTPUT_FLOOR = 4096

# ------------------------------------------------------- PROBE (warm-refill)
# Il perdente della gara non-streaming NON viene MAI cancellato: finisce la
# chiamata in volo come probe reale; se consegna una risposta piena e pulita
# (contenuto vero, niente finish_reason=length) entra nel warm della sessione.
# Se sbaglia (eccezione/timeout) va in cooldown CON LE SOLITE LOGICHE
# (mark_failed; il 429 lo ha gia' messo il rate_hook dentro self.call); il
# vuoto pulito non e' colpa della chiave: nessuna penale.
_NS_PROBES: set = set()


def _spawn_ns_probe(router, dep: dict, fut, t0: float, ctx, ses,
                    wake: bool = False,
                    race: tuple[str, float] | None = None) -> None:
    u = dep.get("unique", "?")
    with contextlib.suppress(Exception):
        router.note_probe_started(ses, u)

    async def _run():
        ok = False
        delivered = False
        raised = None
        try:
            try:
                d = await asyncio.wait_for(fut, timeout=900.0)
            except BaseException as exc:
                d = None
                raised = exc
            if isinstance(d, dict):
                try:
                    ch0 = (d.get("choices") or [{}])[0]
                except (AttributeError, IndexError, TypeError):
                    ch0 = {}
                if isinstance(ch0, dict):
                    msg = ch0.get("message") or {}
                    txt = msg.get("content")
                    # QUALSIASI consegna conta per il warm (regola utente):
                    # `delivered` = c'e' contenuto; `ok` = contenuto PULITO
                    # (non troncato) -> solo quello puo' eleggere l'holder.
                    delivered = isinstance(txt, str) and bool(txt.strip())
                    ok = (delivered
                          and ch0.get("finish_reason") != "length")
            if ok:
                try:
                    if wake:
                        router.clear_cooldown(u)     # sveglia riuscita
                    router.note_result(
                        u, (time.monotonic() - t0) * 1000, ctx_est=ctx)
                    router.note_warm_owner(ses, u)
                except Exception:
                    pass
                metrics.inc("nx_hedge_total", ("probe_ok",))
                # ELEZIONE per TEMPO DI TENTATIVO: se questo probe ha
                # generato in MENO tempo del vincitore della gara diventa
                # l'holder della sessione (il giro dopo parte da lui), e chi
                # e' stato piu' lento viene marcato "lento per la sessione".
                # Guardia: si promuove solo se l'holder attuale e' ancora il
                # vincitore (per non calpestare un esito piu' recente).
                if race is not None:
                    with contextlib.suppress(Exception):
                        _d = (time.monotonic() - t0) * 1000.0
                        _wu, _wd = race
                        _cur = router._cache_ok().get(ses)
                        _cur_u = _cur[0] if _cur else None
                        if _d < float(_wd) and _cur_u in (None, _wu):
                            router.note_session_success(
                                ses, u, latency_ms=_d, ctx_est=ctx)
                            router._note_session_slow(
                                ses, _wu, latency_ms=float(_wd), ctx_est=ctx)
                            log.info("[slow-race] holder -> %s (tentativo "
                                     "%.0fs vs %s %.0fs)", u, _d / 1000.0,
                                     _wu, float(_wd) / 1000.0)
                        else:
                            router._note_session_slow(
                                ses, u, latency_ms=_d, ctx_est=ctx)
            elif raised is not None:
                # QUOTA DI ACCOUNT (Cloudflare & co.): la quota e' dell'account
                # -> pausa le chiavi sorelle fino al reset, non ruotarle a vuoto.
                _qacct = 0
                with contextlib.suppress(Exception):
                    _qacct = maybe_account_quota_cooldown(
                        router, dep, getattr(raised, "status", None),
                        str(raised))
                if _qacct:
                    metrics.inc("nx_hedge_total", ("probe_quota_acct",))
                # Payload REPLAY-REASONING: non e' colpa della chiave (tutte
                # le chiavi del provider rifiutano lo stesso payload) ->
                # nessuna penale, la richiesta principale lo ripara.
                _rkind = reasoning_err_kind(str(raised)) if not _qacct else None
                if _rkind is not None:
                    metrics.inc("nx_hedge_total", ("probe_payload",))
                    # Il probe ha scoperto la natura del problema: impariamo il
                    # flag per tutti i gemelli cosi' la prossima richiesta parte
                    # gia' corretta (thinking_replay / strip_reasoning /
                    # no_thinking a seconda del tipo).
                    with contextlib.suppress(Exception):
                        if _rkind == "needs":
                            learn_thinking_replay(router, dep.get("model"))
                        elif _rkind == "rejects":
                            learn_strip_reasoning(router, dep.get("model"))
                        elif _rkind == "history":
                            learn_no_thinking(router, dep.get("model"))
                elif not _qacct:
                    try:
                        _sec = getattr(raised, "retry_after", None)
                        if isinstance(_sec, (int, float)) and _sec > 0:
                            router.mark_failed(u, seconds=_sec,
                                               reason="probe_error")
                        else:
                            router.mark_failed(u, reason="probe_error")
                    except Exception:
                        pass
                    metrics.inc("nx_hedge_total", ("probe_fail",))
            elif delivered:
                # Canary che ha CONSEGNATO ma troncato (finish_reason=length):
                # va comunque in warm, nessuna penale (regola utente).
                with contextlib.suppress(Exception):
                    router.note_warm_owner(ses, u)
                metrics.inc("nx_hedge_total", ("probe_partial",))
            else:
                metrics.inc("nx_hedge_total", ("probe_drop",))
            log.info("[probe-ns] %s: %s", u,
                      "in warm" if (ok or delivered) else
                      ("cooldown" if raised is not None else "vuoto, inerme"))
        finally:
            try:
                router.note_probe_done(ses, u)
            except Exception:
                pass
            try:
                router.note_end(u, ctx)
            except Exception:
                pass
    _t = asyncio.ensure_future(_run())
    _NS_PROBES.add(_t)
    _t.add_done_callback(_NS_PROBES.discard)


def set_reasoning_reserve(frac=None) -> None:
    """Configura la riserva di contesto per il reasoning (da policy)."""
    global _REASONING_RESERVE_FRAC
    if frac is None:
        return
    try:
        _REASONING_RESERVE_FRAC = max(0.0, min(0.6, float(frac)))
    except (TypeError, ValueError):
        pass


def set_adaptive_timeout(*, enabled=None, floor_sec=None, multiplier=None,
                         max_sec=None) -> None:
    """Configura il timeout adattivo (da policy). Valori None = invariati."""
    global ADAPTIVE_TIMEOUT, TIMEOUT_FLOOR_SEC, TIMEOUT_MULTIPLIER
    global TIMEOUT_MAX_SEC
    if enabled is not None:
        ADAPTIVE_TIMEOUT = bool(enabled)
    for name, val in (("TIMEOUT_FLOOR_SEC", floor_sec),
                      ("TIMEOUT_MULTIPLIER", multiplier),
                      ("TIMEOUT_MAX_SEC", max_sec)):
        if val is None:
            continue
        try:
            globals()[name] = max(0.0, float(val))
        except (TypeError, ValueError):
            pass


def set_latency_lookup(fn) -> None:
    """Registra la lookup `(unique, ctx_est) -> latenza media (ms)` usata dal
    timeout (il Router passa l'EMA PER BUCKET DI CONTESTO con fallback
    globale)."""
    global _LATENCY_LOOKUP
    _LATENCY_LOOKUP = fn


def apply_cooldown_policy(policy) -> dict:
    """Propaga nella forwarder le durate di cooldown 'di categoria' dalla
    policy. Default = valori storici: se non configurate, nulla cambia.
    Ritorna la mappa dei valori modificati."""
    names = {
        "MODEL_MISSING_COOLDOWN_S": ("model_missing_cooldown_sec", int),
        "QUOTA_MIN_COOLDOWN_S": ("quota_min_cooldown_sec", float),
        "QUOTA_MAX_COOLDOWN_S": ("quota_max_cooldown_sec", float),
        "PROVIDER_TRANSIENT_COOLDOWN_S": (
            "provider_transient_cooldown_sec", float),
        "PERMISSION_DENIED_COOLDOWN_S": (
            "permission_denied_cooldown_sec", float),
        "STREAM_LOOP_COOLDOWN_S": ("stream_loop_cooldown_sec", float),
        "_RETRY_BODY_CAP_S": ("retry_body_cap_sec", float),
        "MIN_OUTPUT_FLOOR": ("min_output_floor", int),
    }
    changed: dict[str, dict] = {}
    for gname, (pname, cast) in names.items():
        try:
            val = getattr(policy, pname, None)
            if val is None:
                continue
            val = cast(val)
        except (TypeError, ValueError):
            continue
        old = globals().get(gname)
        if old != val:
            globals()[gname] = val
            changed[gname] = {"old": old, "new": val}
    return changed


def _timeout_for(dep: dict, ctx_est=None) -> httpx.Timeout | None:
    """Timeout httpx per-deployment, o None per usare il default del client."""
    if not ADAPTIVE_TIMEOUT or _LATENCY_LOOKUP is None:
        return None
    try:
        try:
            ms = _LATENCY_LOOKUP(dep.get("unique", ""), ctx_est)
        except TypeError:                      # lookup legacy a 1 arg
            ms = _LATENCY_LOOKUP(dep.get("unique", ""))
    except Exception:                          # mai bloccare la chiamata
        return None
    if not ms or ms <= 0:
        return None
    read = (float(ms) / 1000.0) * float(TIMEOUT_MULTIPLIER)
    read = min(max(float(TIMEOUT_FLOOR_SEC), read), float(TIMEOUT_MAX_SEC))
    base = UPSTREAM_TIMEOUT
    try:
        if base.read is not None and abs(read - float(base.read)) < 0.5:
            return None                        # identico al default
    except Exception:
        pass
    log.debug("[timeout-adaptive] %s avg=%.0fms -> read=%.0fs",
              dep.get("unique", "?"), float(ms), read)
    return httpx.Timeout(connect=base.connect, read=read, write=base.write,
                         pool=base.pool)


def _timeout_kw(dep: dict, ctx_est=None) -> dict:
    """kwargs httpx con `timeout` per-deployment solo se valorizzato (EMA nel
    bucket di contesto della richiesta, fallback globale)."""
    t = _timeout_for(dep, ctx_est)
    return {} if t is None else {"timeout": t}

# ANTI-STALL (mid-stream): se dopo l'avvio dello stream l'upstream non manda
# NULLA per N secondi (free-tier/reverse-proxy che si "congelano" senza
# chiudere la connessione ne' dare errore), il read-timeout del trasporto
# (180s) e' troppo lento. Un watchdog inter-chunk aborta subito -> failover
# (pre-byte) o cooldown del deployment (post-byte). 0 = disabilitato.
STREAM_STALL_SEC = 8.0


def set_stream_stall_sec(sec) -> None:
    """Imposta il watchdog inter-chunk (secondi). <=0 disabilita."""
    global STREAM_STALL_SEC
    try:
        v = float(sec)
    except (TypeError, ValueError):
        return
    STREAM_STALL_SEC = max(0.0, v)


# F20: parametri di stima condivisi (divisore + allowance per IMMAGINE),
# impostati da main a startup e a ogni reload. Servono dove si stima il
# contesto SENZA il router sottomano (clamp_max_tokens): prima le immagini
# valevano 0 token e il divisore era quello di default.
_EST_DIVISOR = 4
_EST_IMAGE_TOKENS = 0
# F21: stall guard calibrato sul TTFT del bucket di contesto. Un heavy (128k+)
# ha prefill fisiologico di 4-6s: un watchdog fisso a 8-20s lo uccideva. Il
# lookup arriva dal router (bucket EMA 'ttft', F1/F9).
_TTFT_LOOKUP = None
_STALL_TTFT_MULT = 2.5
_STALL_MAX_SEC = 60.0


def set_estimate_defaults(divisor=None, image_token_estimate=None) -> None:
    """Divisore e allowance-immagine usati quando il router non e' in scope."""
    global _EST_DIVISOR, _EST_IMAGE_TOKENS
    if divisor is not None:
        try:
            _EST_DIVISOR = max(1, int(divisor))
        except (TypeError, ValueError):
            pass
    if image_token_estimate is not None:
        try:
            _EST_IMAGE_TOKENS = max(0, int(image_token_estimate))
        except (TypeError, ValueError):
            pass


def set_ttft_lookup(fn) -> None:
    """Lookup (unique, ctx) -> ms del TTFT per bucket (calibra lo stall)."""
    global _TTFT_LOOKUP
    _TTFT_LOOKUP = fn


# P4: annota i deployment che IGNORANO stream:true (rispondono JSON e il
# forwarder lo adatta a SSE). Un canary cosi' non puo' vincere la gara: viene
# escluso dal pool dei sostituti (vedi Router._is_known_nonstream).
_NONSTREAM_HOOK = None


def set_nonstream_hook(fn) -> None:
    global _NONSTREAM_HOOK
    _NONSTREAM_HOOK = fn


def _note_nonstream(dep: dict) -> None:
    if _NONSTREAM_HOOK is None:
        return
    try:
        _NONSTREAM_HOOK(dep.get("unique"))
    except Exception:                          # mai rompere lo stream
        pass


def set_stall_bucket(*, multiplier=None, max_sec=None) -> None:
    """Moltiplicatore e tetto dello stall guard calibrato sul TTFT (F21)."""
    global _STALL_TTFT_MULT, _STALL_MAX_SEC
    if multiplier is not None:
        try:
            _STALL_TTFT_MULT = max(0.0, float(multiplier))
        except (TypeError, ValueError):
            pass
    if max_sec is not None:
        try:
            _STALL_MAX_SEC = max(1.0, float(max_sec))
        except (TypeError, ValueError):
            pass


def _stall_sec_for(unique, ctx_est=None) -> float:
    """F21: stall effettivo = max(base, min(TTFT_p50_bucket * mult, max_sec)).

    Un heavy (128k+) ha prefill fisiologico di 4-6s: il watchdog fisso lo
    uccideva; qui si allarga solo quanto serve, con tetto. Se il bucket non ha
    campioni (o la calibrazione e' spenta) si torna al valore base."""
    base = STREAM_STALL_SEC
    if base <= 0 or _TTFT_LOOKUP is None or _STALL_TTFT_MULT <= 0:
        return base
    try:
        t = _TTFT_LOOKUP(unique, ctx_est)
    except Exception:
        return base
    try:
        t = float(t)
    except (TypeError, ValueError):
        return base
    if t <= 0:
        return base
    return max(base, min(t * _STALL_TTFT_MULT / 1000.0, _STALL_MAX_SEC))


# config schemaout (degradazione gentile response_format): impostata da main
# all'avvio e ad ogni reload della policy. None = default sicuro.
_SCHEMAOUT_CFG = None


def set_schemaout_config(cfg) -> None:
    global _SCHEMAOUT_CFG
    _SCHEMAOUT_CFG = cfg


class StreamStallError(asyncio.TimeoutError):
    """Nessun chunk SSE per `seconds`: l'upstream e' appeso a meta' stream.

    Sottoclasse di asyncio.TimeoutError cosi' i consumatori esistenti lo
    trattano come un TIMEOUT di trasporto (rotazione pre-byte / cooldown
    lungo post-byte) senza casi speciali."""

    def __init__(self, seconds: float, model: str = ""):
        self.seconds = float(seconds)
        self.model = model
        msg = "upstream stream stall: nessun chunk per %.1fs" % self.seconds
        if model:
            msg += " (%s)" % model
        super().__init__(msg)


async def _stall_guard(aiter, timeout: float, model: str = ""):
    """Avvolge un async-iterator e solleva StreamStallError se l'attesa tra
    due chunk supera `timeout` secondi."""
    _it = aiter.__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(_it.__anext__(), timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise StreamStallError(timeout, model) from None
        yield chunk

# "No such model" (cloudflare), model_not_found (openai), "Model X is not
# supported" / {"type":"ModelError"} (opencode-zen), "Model is (currently)
# unavailable", ecc. — il modello non esiste / non e' servito / e' giu' su
# QUESTO provider: deployment-side, sempre ritriabile (mai pass-through del
# 4xx al client). Condizione effettivamente duratura -> cooldown 24h fisso.
# NOTA: le alternative \bunavailable / "not (currently|temporarily)?available"
# NON sono incluse qui: "Model is unavailable" / "The requested model is not
# available" sono ERRORI TRANSIENT del provider (es. OpenCode Go risponde cosi'
# quando l'endpoint/upstream e' giu' o il modello e' temporaneamente
# indisponibile) -> vengono trattate da _PROVIDER_TRANSIENT_RE, mai da
# PERMANENT_DEAD/retire. Restano PERMANENTI solo le condizioni che davvero non
# tornano: modello inesistente, ritirato/EOL, deprecated.
_MODEL_MISSING_RE = re.compile(
    r"no such model|model_not_found|unknown model|modello inesistente"
    r"|does not exist|modelerror|model[\w .:/'-]*\bnot supported"
    # EOL / ritiro: OpenRouter risponde 410 {"title":"Gone","detail":"The model
    # '...' has reached [end of life]..."}; altri "no longer available",
    # "has been deprecated/retired/sunset". Il modello non torna -> stesso
    # trattamento del "no such model" (cooldown 24h, mai raw al client).
    r"|has reached \[?end.of.life|end.of.life"
    r"|no longer (available|supported)"
    r'|"title"\s*:\s*"gone"'
    r"|has been (deprecated|retired|sunset|removed|discontinued)",
    re.IGNORECASE)
# un modello inesistente/non servito/giu' non torna in minuti: tienilo fermo
# 24h invece dell'escalation standard (che riparte da cooldown_sec).
MODEL_MISSING_COOLDOWN_S = 86400

# --- QUOTA ESAURITA (abbonamento flat a scadenza mensile/settimanale) ------
# OpenCode Go e altri piani subscription rispondono con un envelope provider
# di tipo "GoUsageLimitError" + message che include "Resets in N days" quando
# il tetto mensile è esaurito. La chiave non tornerà disponibile prima del
# reset: ruotarla e rimetterla in coda e' inutile (ogni tentativo spreca una
# chiamata reale e allunga la catena). Cooldown = tempo al reset, clampato
# [10 min, 7 giorni] per non bucare cooldown infiniti (provider bug).
# Formato osservato:
#   {"type":"error","error":{"type":"GoUsageLimitError","message":
#     "Monthly usage limit reached. Resets in 9 days. ..."},
#     "metadata":{"limitName":"monthly"}}
_QUOTA_EXHAUSTED_RE = re.compile(
    r"GoUsageLimitError"
    r"|\"limitName\"\s*:\s*\"(monthly|weekly)\""
    r"|usage limit reached"
    r"|insufficient.quota"
    # Cloudflare Workers AI: quota GIORNALIERA esaurita. Il body e'
    # {"errors":[{"message":"AiError: you have used up your daily free
    # allocation of 10,000 neurons, please upgrade ...","code":4006}]}:
    # e' una QUOTA, non un rifiuto di schema del payload (il vecchio
    # \baierror\b in _PAYLOAD_SCHEMA_RE la classificava come schema e la
    # faceva ruotare SENZA cooldown, bruciando tutte le chiavi sorelle).
    r"|used up your (?:daily|monthly) free allocation"
    r"|free allocation of [\d.,]+ ?(?:k|m)? ?neurons"
    # OpenRouter / NVIDIA: tetto GIORNALIERO di richieste sui modelli free dell'
    # ACCOUNT. Body: {"error":{"message":"Rate limit exceeded:
    # free-models-per-day. Add 10 credits to unlock 1000 free model requests
    # per day","code":429}}. Senza questa firma era un 429 generico -> cooldown
    # 90s e rotazione a vuoto su tutte le chiavi sorelle (osservato su
    # esempio: nemotron-3.5-lightning-free__16 -> __17 e cosi' via).
    r"|free.models?[ -]?per[ -]?day"
    r"|free model requests per day",
    re.IGNORECASE)
# BILANCIO CREDITI ESAURITO ("insufficient balance"): condizione
# dell'account, NON transitoria. PRIORITA' su `_QUOTA_EXHAUSTED_RE` perche'
# i body la accompagnano con "type":"insufficient_quota" (che altrimenti la
# farebbe classificare come quota -> cooldown breve, con retry a ripetizione).
_INSUFFICIENT_BALANCE_RE = re.compile(
    r"insufficient[\s_-]*(?:account[\s_-]*)?balance", re.IGNORECASE)
# Quota a finestra GIORNALIERA (reset a mezzanotte): senza un hint esplicito
# "Resets in ..." il cooldown ragionevole e' fino alla mezzanotte UTC, non 10
# minuti (altrimenti si riprova la stessa quota esaurita ogni 10 min).
_DAILY_QUOTA_RE = re.compile(
    r"daily free allocation"
    r"|used up your daily"
    r"|daily (?:quota|limit) (?:reached|exceeded|exhausted)"
    r"|reached (?:your|the) daily"
    # qualunque "N richieste/token PER DAY" e' una finestra giornaliera: il
    # reset e' a mezzanotte, non tra 10 minuti.
    r"|per[ -]?day",
    re.IGNORECASE)
# parses: "Resets in 9 days", "Resets in 4 hours", "Resets in 30 minutes"
_QUOTA_RESET_RE = re.compile(
    r"resets?\s+in\s+(\d+)\s+(day|hour|minute)s?",
    re.IGNORECASE)

QUOTA_MIN_COOLDOWN_S = 600.0        # 10 minuti (minimo)
QUOTA_MAX_COOLDOWN_S = 7 * 86400.0  # 7 giorni (massimo: non bucare il reset)

def parse_quota_reset_seconds(detail: str | None) -> float:
    """Dal message del provider, calcola i secondi al reset.
    Ritorna il cooldown clampato, o 0.0 se non riconosce niente."""
    if not detail:
        return 0.0
    m = _QUOTA_RESET_RE.search(detail)
    if not m:
        if _DAILY_QUOTA_RE.search(detail):
            # quota giornaliera senza reset dichiarato: il provider azzera a
            # mezzanotte (UTC per Cloudflare) -> aspettiamo quella.
            _to_mid = 86400.0 - (time.time() % 86400.0)
            return max(QUOTA_MIN_COOLDOWN_S,
                       min(QUOTA_MAX_COOLDOWN_S, _to_mid))
        return QUOTA_MIN_COOLDOWN_S   # riconosciuto esausto ma senza reset:
                                       # minimo sicuro (riprova tra 10min)
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "day":
        secs = n * 86400.0
    elif unit == "hour":
        secs = n * 3600.0
    else:  # minute
        secs = n * 60.0
    return max(QUOTA_MIN_COOLDOWN_S, min(QUOTA_MAX_COOLDOWN_S, secs))

# Gemini 3: rimanda i functionCall di un turno precedente ESIGE il blob
# opaco `thought_signature` che l'API nativa aveva emesso. Questo gateway e'
# passthrough OpenAI puro: non traduce ne' persiste quella firma e il
# formato chat OpenAI non ha un campo per trasportarla -> un client che
# rigioca tool call NON puo' soddisfare quello specifico deployment. Un
# altro deployment (non-Gemini o provider tollerante) gestisce lo stesso
# payload -> ruota SENZA cooldown (il modello non e' rotto in generale),
# come per finish_reason=length. Mai pass-through del 400 al client finche'
# esiste un'alternativa non ancora provata.
_THOUGHT_SIG_RE = re.compile(r"thought[_ ]signature", re.IGNORECASE)

# Cloudflare Workers AI (e altri gateway con schema JSON stretto) rifiutano il
# payload OpenAI quando `messages[].content` e' un ARRAY di blocchi multimodali
# invece di una stringa, o quando un messaggio non ha `content` (es. assistant
# con soli tool_calls). Il body e' la busta CF:
#   {"success":false,"result":{},"errors":[{"code":5006,"message":
#     "AiError: Bad input: Error: oneOf at '/' not met, 0 matches: ...
#      Type mismatch of '/messages/0/content', 'array' not in 'string', ...
#      required properties at '/messages/17' are 'role,content'"}]}
# E' un rifiuto di FORMA della richiesta da parte di QUESTO provider, non del
# modello: un provider OpenAI-compatibile gestisce lo stesso payload -> si
# ruota SENZA cooldown (il deployment non e' rotto in generale), come per
# _THOUGHT_SIG_RE / finish_reason=length. Il 400 va al client SOLO se non
# esiste alcuna alternativa non ancora provata.
#
# Stesso trattamento anche per: un turno `assistant` nella history porta
# `reasoning_content` (da un modello reasoning: qwen3.x, Gemini, R1-style) e il
# provider successivo lo rifiuta -> 400 "property 'reasoning_content' is
# unsupported" / "'role:assistant' ... reasoning ... unsupported". Un altro
# provider lo accetta (o lo ignora): ruota senza cooldown.
_PAYLOAD_SCHEMA_RE = re.compile(
    r"oneof at '/?[^']*' not met"
    r"|type mismatch of '/messages/\d+/content'"
    r"|'array' not in 'string'|'string' not in 'array'"
    r"|required properties at '/messages/\d+' are"
    r"|(reasoning_content|reasoning)['\" ]* is unsupported"
    r"|for 'role:assistant'[^\]]*reasoning[^\]]*unsupported"
    r"|property 'reasoning[_a-z]*' is unsupported"
    # CATENA TOOL ROTTA (orfano inverso): un assistant con tool_calls senza il
    # corrispondente messaggio `tool`. I provider severi rispondono 400
# bloccante; un altro deployment tollerante accetta lo stesso payload ->
# ruota senza cooldown, mai raw al client. La history viene comunque
# bonificata a monte da histnorm.
#
# NB: il sotto-caso "content array vs string" ha un percorso DEDICATO
# (_CONTENT_ARRAY_RE, sopra): si impara `content_string` e si ritenta lo
# STESSO deployment col payload appiattito, invece di ruotare.
    r"|assistant message with ['\"]?tool_calls['\"]? must be followed by"
    r"|must be a response to a preceding message with ['\"]?tool_calls"
    r"|messages? with role ['\"]?tool['\"]? must be a response"
    r"|unknown tool_call_id|invalid tool_call id"
    r"|tool_call_id['\"]?\s*(?:of\s+)?[^ ,;]{0,64}\s*(?:not found|does not exist|is invalid)"
    r"|tool_(?:use|call)_id[^,;]{0,64}(?:not found|no corresponding|without)"
    r"|unexpected .{0,16}tool_(?:result|use)_id"
    r"|must have a corresponding .{0,24}tool_(?:result|use)"
    r"|does not have a corresponding tool (?:result|message)"
    r"|missing (?:corresponding )?tool (?:result|response|output)"
    r"|(?:unsupported|not supported|invalid|unknown)\s+"
    r"(?:value\s+)?['\"]?response_format"
    r"|response_format['\"]?[^,;.]{0,40}(?:unsupported|not supported|"
    r"invalid|unknown)"
    r"|(?:unsupported|not supported)\s+['\"]?json_schema",
    re.IGNORECASE)

# Sotto-firma SPECIFICA del rifiuto "content array vs string": il payload e'
# RIPARABILE appiattendo `messages[].content` da array di solo testo a stringa
# (media-safe) e aggiungendo `content:""` agli assistant con tool_calls senza
# content. A differenza delle altre firme di _PAYLOAD_SCHEMA_RE NON si ruota
# subito: si IMPARA il flag `content_string` e si ritenta LO STESSO deployment
# col payload bonificato (il deployment non e' rotto: e' la forma che non gli
# piace). Se la bonifica non basta (array con media) si ricade sulla rotazione.
_CONTENT_ARRAY_RE = re.compile(
    r"'array' not in 'string'"
    r"|type mismatch of '/messages/\d+/content'"
    r"|required properties at '/messages/\d+' are",
    re.IGNORECASE)

# COMBINAZIONE built-in tools + function calling rifiutata dal provider
# (Google/Gemini 3, anche via proxy OpenAI-compat: es. requesty mappa il tool
# `web_search` sul built-in google_search). Il provider pretende
# toolConfig.includeServerSideToolInvocations=true, che i proxy non espongono
# nel body OpenAI-compat -> 400 bloccante. Il payload NON e' malformato: un
# altro deployment accetta la stessa richiesta. Regola utente: RUOTA senza
# penalita' (nessun cooldown) e, a catena esaurita, NON consegnare il 400
# grezzo al client (che non puo' farci nulla) -> 503 retryable.
_TOOL_COMBO_RE = re.compile(
    r"include_server_side_tool_invocations"
    r"|enable\s+tool_config"
    r"|tool_config[^,;.]{0,40}to use built-?in tools",
    re.IGNORECASE)


def tool_combo_signature(detail: str | None) -> bool:
    """True se il body d'errore e' il rifiuto della COMBINAZIONE
    built-in tools + function calling (Google/Gemini 3 & proxy)."""
    return bool(detail) and _TOOL_COMBO_RE.search(detail) is not None

# REPLAY DEL REASONING (thinking mode). opencode zen "Console Go" (deepseek
# v4.1 thinking & co.) pretende che un assistant CON tool_calls riporti il suo
# `reasoning_content`: il client agentico lo droppa dalla history, quindi il
# provider risponde 400 bloccante "The `reasoning_content` in the thinking mode
# must be passed back to the API." Il payload e' RIPARABILE: si re-inietta un
# segnaposto sui turni assistant con tool_calls e si RITENTA LO STESSO
# deployment (ruotare non aiuta: tutte le chiavi dello stesso provider
# rifiutano lo stesso payload — in produzione bruciava 26 chiavi per niente).
_REASONING_REPLAY_RE = re.compile(
    r"reasoning[_\w]*[^\n]{0,120}?must be passed back", re.IGNORECASE)

# FAMIGLIA "REASONING" (thinking): un'unica regex con gruppi nominati per
# distinguere il RIMEDIO giusto. I provider sbagliano il messaggio e a volte
# lo mascherano ("does not support vision input" su richieste di testo), quindi
# si guarda la firma del testo + il payload reale:
#   needs_field      -> il provider PRETENDE il campo `reasoning_content` che
#                       il client agentico ha droppato -> si INIETTA (reasoning
#                       vero se disponibile, altrimenti un segnaposto).
#   rejects_field    -> il provider RIFIUTA i campi reasoning nella history
#                       ("reasoning_content is unsupported") -> si TOGLIE il
#                       campo (content/tool_calls restano) e si ritenta.
#   history_mismatch -> il provider (Anthropic/Gemini nativi) vuole blocchi
#                       thinking coerenti con la history, che noi costruiamo da
#                       soli content+tool_calls -> si DISABILITA il thinking
#                       per QUESTA richiesta, history intatta.
_REASONING_ERR_RE = re.compile(
    r"(?P<needs_field>reasoning[_\w]*[^\n]{0,120}?must be passed back"
    r"|reasoning[_\w]*[^\n,;.]{0,60}?(?:is |are )?required"
    r"|missing[^\n,;.]{0,30}reasoning_content)"
    r"|(?P<rejects_field>(?:reasoning[_a-z]*|reasoning)['\" ]*"
    r"(?:is|are|was|were)?[ ]*(?:unsupported|not supported|not allowed|"
    r"invalid|unknown)"
    r"|(?:unsupported|not supported|unknown|unexpected|invalid)\s{1,3}"
    r"['\"]?reasoning[_a-z]*['\"]?"
    r"|property ['\"]?reasoning[_a-z]*['\"]? is (?:unsupported|unknown)"
    # Validazione STRICT del body (Pydantic, es. router.requesty.ai): 422
    # "extra_forbidden" -> il campo reasoning nella history NON e' ammesso
    # ("Extra inputs are not permitted", loc .../assistant/reasoning_content).
    # Il rimedio e' lo STESSO del rifiuto esplicito: si toglie il campo
    # (content/tool_calls restano) e si ritenta lo stesso deployment; il flag
    # `strip_reasoning` viene appreso per tutte le righe di quel modello.
    r"|extra_forbidden[\s\S]{0,500}?reasoning[_a-z]*"
    r"|reasoning[_a-z]*[\s\S]{0,500}?(?:extra[ _]?forbidden|not permitted)"
    r"|(?:extra|additional|unexpected|unknown)[ _]"
    r"(?:inputs?|fields?|properties|parameters?)[\s\S]{0,300}?reasoning[_a-z]*"
    r"|not permitted[\s\S]{0,500}?reasoning[_a-z]*)"
    r"|(?P<history_mismatch>thinking[^\n]{0,60}(?:block|signature|content)"
    r"|thought[_ ]signature)",
    re.IGNORECASE)

# Segnaposto neutro: il provider vuole il CAMPO presente, non il contenuto
# (verificato live: anche una stringa fissa viene accettata).
_RC_REPLAY_PLACEHOLDER = "[reasoning non disponibile: turno replayato dal gateway]"


def repair_reasoning_replay(body: dict) -> int:
    """Re-inietta il segnaposto di `reasoning_content` sugli assistant con
    tool_calls che non lo portano. Ritorna quanti messaggi sono stati
    riparati. Non tocca i turni senza tool_calls (accettati senza replay)."""
    n = 0
    msgs = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(msgs, list):
        return 0
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        if not m.get("tool_calls"):
            continue
        rc = m.get("reasoning_content")
        if isinstance(rc, str) and rc.strip():
            continue
        m["reasoning_content"] = _RC_REPLAY_PLACEHOLDER
        n += 1
    return n


def restore_reasoning(body: dict, orig: list | None) -> int:
    """Re-inietta SOLO il `reasoning_content` che histnorm aveva tagliato.

    La history in uscita resta quella NORMALIZZATA (orfani/chiusure/assistant
    vuoti gia' sistemati): si rimette solo il campo, abbinando i messaggi per
    firma (role assistant + id dei tool_calls + content) perche' la lista
    normalizzata puo' essere piu' corta dell'originale. Ritorna quanti campi
    sono stati ripristinati (0 = niente da fare)."""
    if not isinstance(body, dict) or not isinstance(orig, list) or not orig:
        return 0
    store: dict[tuple, list[str]] = {}
    for m in orig:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        rc = m.get("reasoning_content")
        if not (isinstance(rc, str) and rc.strip()):
            continue
        ids = tuple(t.get("id") for t in (m.get("tool_calls") or [])
                    if isinstance(t, dict))
        store.setdefault((ids, str(m.get("content") or "")), []).append(rc)
    if not store:
        return 0
    n = 0
    for m in body.get("messages") or []:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        rc = m.get("reasoning_content")
        if isinstance(rc, str) and rc.strip():
            continue                      # gia' presente: non toccare
        ids = tuple(t.get("id") for t in (m.get("tool_calls") or [])
                    if isinstance(t, dict))
        lst = store.get((ids, str(m.get("content") or "")))
        if lst:
            m["reasoning_content"] = lst.pop(0)
            n += 1
    return n


def apply_thinking_replay(body: dict, dep: dict,
                          orig: list | None = None) -> int:
    """Riparazione PROATTIVA per i deployment con `thinking_replay` attivo:
    il provider esige il campo `reasoning_content` sui turni assistant con
    tool_calls, quindi lo rimettiamo PRIMA dell'invio (niente 400 al primo
    colpo).

    Ordine: prima il reasoning VERO (se abbiamo la history originale del
    client, es. quando histnorm l'aveva tagliato per risparmiare token),
    poi il segnaposto sui turni che restano scoperti (probe/canary/sveglia,
    che non hanno una history originale). Ritorna i campi sistemati.

    Se il deployment ha imparato `strip_reasoning` (il provider RIFIUTA i
    campi reasoning) si tolgono invece di rimetterli."""
    if not isinstance(dep, dict):
        return 0
    if dep.get("strip_reasoning"):
        return strip_reasoning_fields(body)
    if not dep.get("thinking_replay"):
        return 0
    n = restore_reasoning(body, orig) if orig else 0
    return n + repair_reasoning_replay(body)


def apply_content_string(body: dict, dep: dict) -> int:
    """Riparazione PROATTIVA per i deployment col flag `content_string`
    (schema JSON stretto, es. Cloudflare Workers AI): appiattisce
    `messages[].content` da array di solo testo a STRINGA e aggiunge
    `content:""` agli assistant con tool_calls che non ce l'hanno, PRIMA
    dell'invio (niente 400 al primo colpo). Media-safe: se un array contiene
    blocchi non-testo il messaggio resta intatto. Ritorna i messaggi sistemati.
    """
    if not isinstance(dep, dict) or not dep.get("content_string"):
        return 0
    msgs = body.get("messages") if isinstance(body, dict) else None
    new_msgs, n = flatten_text_content(msgs)
    if n:
        body["messages"] = new_msgs
    return n


# Campi NON-OpenAI che alcuni client aggiungono al body (es. le opzioni degli
# agenti opencode come `fallback_models`): i provider severi (Google via
# /v1beta/openai) li rifiutano con 400 "Unknown name ...". Vengono RIMOSSI dal
# body prima dell'invio a monte. Denylist configurabile via policy.
# `store` (OpenAI opzionale, es. store:false): Google via /v1beta/openai lo
# rifiuta con 400 "Unknown name \"store\"" -> va rimosso come i campi client.
_STRIP_CLIENT_FIELDS: tuple[str, ...] = ("fallback_models", "store")


def set_strip_client_fields(fields=None) -> None:
    """Aggiorna la denylist dei campi client-only da rimuovere (da policy)."""
    global _STRIP_CLIENT_FIELDS
    if fields is None:
        return
    try:
        _STRIP_CLIENT_FIELDS = tuple(
            str(f).strip() for f in fields if str(f).strip())
    except TypeError:
        return


def strip_client_fields(body: dict, fields=None) -> int:
    """Rimuove dal body i campi client-only non standard (top-level).
    Ritorna il numero di campi rimossi; no-op su input non-dict."""
    if not isinstance(body, dict):
        return 0
    names = _STRIP_CLIENT_FIELDS if fields is None else tuple(fields)
    n = 0
    for f in names:
        if f in body:
            body.pop(f, None)
            n += 1
            metrics.inc("nx_client_fields_stripped_total", (f,))
    return n


def strip_reasoning_fields(body: dict) -> int:
    """Rimuove i campi reasoning che il provider RIFIUTA (es. Cloudflare
    "reasoning_content is unsupported"): il contenuto del modello resta
    (content/tool_calls), si elimina solo il campo incriminato. Ritorna
    quanti campi sono stati tolti (0 = niente da fare)."""
    n = 0
    msgs = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(msgs, list):
        return 0
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        for k in ("reasoning_content", "reasoning"):
            if k in m:
                m.pop(k, None)
                n += 1
    return n


def downgrade_thinking(body: dict) -> int:
    """Disabilita il THINKING per QUESTA richiesta (history intatta): toglie
    `thinking`/`reasoning_effort`/`reasoning` a livello di payload. Rimedio
    per i provider che pretendono blocchi thinking coerenti con la history
    (Anthropic/Gemini) e rispondono 400 su una history costruita dal gateway
    da soli `content`+`tool_calls`. Ritorna quanti campi ha tolto.

    NB: `apply_effort_policy` puo' RE-INJECTARE `reasoning_effort`; per questo
    il chiamante marca il dep con `_no_thinking` (copia locale) che la blocca."""
    if not isinstance(body, dict):
        return 0
    n = 0
    for k in ("thinking", "reasoning_effort", "reasoning"):
        if k in body:
            body.pop(k, None)
            n += 1
    return n


def reasoning_err_kind(detail: str) -> str | None:
    """Classifica l'errore nella famiglia reasoning:
    'needs' | 'rejects' | 'history' | None (errore di altra natura)."""
    m = _REASONING_ERR_RE.search(detail or "")
    if not m:
        return None
    if m.group("needs_field"):
        return "needs"
    if m.group("rejects_field"):
        return "rejects"
    return "history"


def repair_reasoning_error(body: dict, detail: str, dep: dict,
                           steps: set, orig: list | None = None,
                           force_kind: str | None = None) -> str | None:
    """Applica UN rimedio (mai lo stesso due volte) per un errore della
    famiglia reasoning. Ritorna 'repaired' | 'stripped' | 'downgraded' se ha
    modificato il body (il chiamante ritenta LO STESSO deployment), altrimenti
    None (nessun rimedio: si prosegue con la classificazione normale).

    `steps` e' lo stato del singolo deployment (set dei rimedi gia' provati).
    `force_kind` serve quando il testo dell'errore e' fuorviante (es. il falso
    "does not support vision input" di llm7/Cloudflare)."""
    kind = force_kind or reasoning_err_kind(detail)
    if kind == "needs":
        if "repaired" in steps:
            return None
        steps.add("repaired")
        n = restore_reasoning(body, orig) if orig else 0
        n += repair_reasoning_replay(body)
        return "repaired" if n else None
    if kind == "rejects":
        if "stripped" in steps:
            return None
        steps.add("stripped")
        return "stripped" if strip_reasoning_fields(body) else None
    if kind == "history":
        if "downgraded" in steps:
            return None
        steps.add("downgraded")
        return "downgraded" if downgrade_thinking(body) else None
    return None


def is_unclear_error(status: int | None, detail: str | None) -> bool:
    """True se l'errore upstream NON rientra in una firma NOTA (quota, auth,
    ban/ToS, modello mancante, schema, modalita'/media, replay reasoning,
    transitorio del provider): solo per questi casi 'oscuri', con una history
    a cui abbiamo tagliato il reasoning, vale UN ritentativo con la history
    originale (se l'errore e' chiaro, invece, il tentativo non serve)."""
    d = detail or ""
    st = abs(int(status)) if status else 0
    if st in (401, 402, 403, 404, 413, 429):
        return False
    if st >= 500:
        return False
    if not d:
        return True
    return not (_MODEL_MISSING_RE.search(d) or _PAYLOAD_SCHEMA_RE.search(d)
                or _THOUGHT_SIG_RE.search(d)
                or _QUOTA_EXHAUSTED_RE.search(d)
                or _PROVIDER_TRANSIENT_RE.search(d)
                or _REASONING_REPLAY_RE.search(d)
                or media_reject_signature(d) or is_provider_fault_body(d)
                or ban_signature_hit(d))

def is_insufficient_balance(detail: str | None) -> bool:
    """True se il body dichiara bilancio CREDITI esaurito ("insufficient
    balance"): condizione dell'account, non transitoria -> il deployment va
    RITIRATO (sblocco manuale), non messo in cooldown. Vale per ogni provider."""
    if not detail:
        return False
    return bool(_INSUFFICIENT_BALANCE_RE.search(detail))


def classify_error_class(status, detail: str | None = None) -> str:
    """Classe d'errore "onesta" per l'ATTEMPT TRAIL mostrato al client: un
    perche' leggibile per ogni hop, senza esporre testo del provider. Riusa
    le firme gia' esistenti, cosi' client e log parlano la stessa lingua."""
    d = detail or ""
    st = abs(int(status)) if status else 0
    low = d.lower()
    if ban_signature_hit(d):
        return "ban_tos"
    if _REASONING_ERR_RE.search(d) or _REASONING_REPLAY_RE.search(d):
        return "reasoning"
    if _THOUGHT_SIG_RE.search(d):
        return "thought_signature"
    if _MODEL_MISSING_RE.search(d):
        return "model_missing"
    # QUOTA prima dello schema: un body di quota puo' contenere parole che
    # somigliano a un rifiuto di schema (CF "AiError: ... free allocation") e
    # la quota va in cooldown, non ruotata a vuoto.
    if _QUOTA_EXHAUSTED_RE.search(d):
        return "quota"
    if tool_combo_signature(d):
        return "tool_combo"
    if _PAYLOAD_SCHEMA_RE.search(d) or _UNKNOWN_FIELD_RE.search(d):
        return "payload_schema"
    if media_reject_signature(d):
        return "media"
    if st == 402:
        return "out_of_credits"
    if st == 401:
        return "auth"
    if st == 403:
        return "forbidden"
    if st == 404:
        return "model_missing"
    if st == 413:
        return "context_too_large"
    if st == 429:
        return "rate_limited"
    if host_transient_signature_hit(d):
        return "host_transient"
    if _PROVIDER_TRANSIENT_RE.search(d):
        return "provider_transient"
    if st >= 500:
        return "upstream_error"
    if st >= 400:
        return "provider_bad_request"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "connect" in low or "network" in low or "transport" in low:
        return "network"
    return "other"


# Errore TRANSITORIO del provider/router a monte (non del client, non del
# modello): l'upstream del provider e' giu', non ha endpoint validi ora, ecc.
# Arriva come 4xx col body d'errore ma NON e' un problema della richiesta ->
# si ruota (cooldown CORTO: e' transitorio). Queste frasi compaiono solo nei
# body d'errore di provider/router, mai nel contenuto reale di un modello.
_PROVIDER_TRANSIENT_RE = re.compile(
    r"error from provider|upstream request failed|provider returned error"
    r"|no endpoints found|no allowed providers|temporarily unavailable"
    r"|upstream error|bad gateway|service unavailable|gateway timeout"
    r"|internal server error|too many requests|overloaded"
    # auth del NOSTRO deployment verso l'upstream (token service giu' o chiave
    # rifiutata): errore provider-side -> ruota, mai 400 raw al client.
    r"|upstream[_ ]+(?:provider[_ ]+)?auth\w*[_ ]*fail"
    r"|upstream_authentication_failed|provider authentication failed"
    # Modello momentaneamente NON servito dall'aggregatore (es. bynara: "The
    # requested model is not available."): problema deployment-side -> ruota
    # (cooldown corto, escalating sui fallimenti ripetuti), MAI 400 al client.
    r"|requested model is not available|model is not available"
    r"|model not available",
    re.IGNORECASE)
PROVIDER_TRANSIENT_COOLDOWN_S = 60

# 403 upstream (permission denied / project banned / key disabled...): la key
# non torna presto -> cooldown lungo, poi si ruota sul successivo.
PERMISSION_DENIED_COOLDOWN_S = 1800          # 30min
# BAN/ToS del PROVIDER verso il NOSTRO IP: non e' colpa della singola chiave,
# e' l'endpoint intero (llm7.io "ip_banned" con 253 hit a raffica su un host,
# openrouter "policy_review_required"). Se il body contiene una di queste
# firme, quarantena dell'HOST per 24h (il router la applica a TUTTI i gate di
# eleggibilita'): nessun deployment su quell'endpoint verra' piu' riprovato,
# ruotato o risvegliato, finche' non scade da sola.
_BAN_TOS_SIGNATURES = (
    "ip_banned",
    "policy_review_required",
    "terms of service",
    "access from this ip address is restricted",
    "access is temporarily unavailable for this client",
)


def ban_signature_hit(detail: str | None) -> bool:
    low = (detail or "").lower()
    return any(sig in low for sig in _BAN_TOS_SIGNATURES)


def maybe_quarantine_ban(router, dep: dict | None, status,
                         detail) -> bool:
    """True se l'errore e' una BAN/ToS dell'endpoint: mette in quarantena
    l'host (24h). Chiamata su 403/429 con il body dell'upstream."""
    try:
        st = abs(int(status or 0))
    except (TypeError, ValueError):
        return False
    if st not in (403, 429):
        return False
    if not ban_signature_hit(detail):
        return False
    url = str((dep or {}).get("api_base") or (dep or {}).get("endpoint") or "")
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:                              # noqa: BLE001
        return False
    if not host:
        return False
    router.quarantine_endpoint(host, 86400.0)
    log.warning("[quarantina] %s: %s dal provider %s -> host %s fuori "
                "gioco 24h", (dep or {}).get("unique"), st, host, host)
    return True


# 502/503 di un AGGREGATORE che riporta "il provider ha errore a meta'
# stream": e' l'HOST a essere momentaneamente malato, non la singola chiave.
# Senza host-cooldown la rotazione brucia una chiave sorella dopo l'altra
# sullo stesso host (i nemotron tokenrouter di un host). Cooldown breve e
# configurabile: elastico per un problema transitorio, non una condanna.
_HOST_TRANSIENT_SIGNATURES = (
    "sent an error mid-stream",
    "provider sent an error mid-stream",
)


def host_transient_signature_hit(detail: str | None) -> bool:
    low = (detail or "").lower()
    return any(sig in low for sig in _HOST_TRANSIENT_SIGNATURES)


def maybe_host_transient_cooldown(router, dep: dict | None, status,
                                  detail) -> bool:
    """True se l'errore e' un 502/503 mid-stream di un aggregatore: cooldown
    BREVE dell'host (default 120s) per non girare a vuoto sulle chiavi
    sorelle. Rientra da solo appena passa il malumore dell'host."""
    try:
        st = abs(int(status or 0))
    except (TypeError, ValueError):
        return False
    if st not in (502, 503):
        return False
    if not host_transient_signature_hit(detail):
        return False
    url = str((dep or {}).get("api_base") or (dep or {}).get("endpoint") or "")
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:                              # noqa: BLE001
        return False
    if not host:
        return False
    try:
        sec = float(getattr(router.policy, "cooldown_host_midstream_502_sec",
                            120.0) or 120.0)
    except Exception:                              # noqa: BLE001
        sec = 120.0
    sec = max(1.0, sec)
    router.quarantine_endpoint(host, sec, reason="mid-stream 5xx host")
    log.warning("[host-cd] %s: 502 mid-stream da %s -> host %s in pausa "
                "%.0fs", (dep or {}).get("unique"), host, host, sec)
    return True


# ------------------------------------------------- QUOTA DI ACCOUNT (CF)
_ACCOUNT_PATH_RE = re.compile(r"/accounts/([A-Za-z0-9_-]+)/", re.IGNORECASE)


def dep_account_key(dep: dict | None) -> str:
    """Chiave di ACCOUNT del deployment: l'id account nel path dell'endpoint
    (es. Cloudflare `/client/v4/accounts/<id>/ai/...`). Le chiavi di uno
    stesso account CONDIVIDONO la quota."""
    url = str((dep or {}).get("api_base") or (dep or {}).get("endpoint") or "")
    m = _ACCOUNT_PATH_RE.search(url)
    return m.group(1).lower() if m else ""


def maybe_account_quota_cooldown(router, dep: dict | None, status,
                                 detail) -> int:
    """Quota GIORNALIERA di un ACCOUNT (Cloudflare Workers AI: "you have used
    up your daily free allocation of ... neurons"): la quota e' dell'ACCOUNT,
    non della singola chiave -> cooldown fino al reset su TUTTE le chiavi
    dell'account. Senza questo la cascata riprova a vuoto ogni ~90s le decine
    di chiavi sorelle (osservato: 154 chiavi CF bruciate a vuoto).
    Ritorna il numero di deployment messi in cooldown."""
    if not _QUOTA_EXHAUSTED_RE.search(str(detail or "")):
        return 0
    acct = dep_account_key(dep)
    if not acct:
        return 0
    secs = parse_quota_reset_seconds(detail) or QUOTA_MIN_COOLDOWN_S
    # PROVENIENZA: 'authoritative' solo se il provider ha DICHIARATO il reset
    # ("Resets in ..."). La stima nostra (mezzanotte UTC / minimo) resta
    # 'heuristic' -> la SVEglia puo' comunque tentare il risveglio (regola
    # utente: lasciamo fare il tentativo anche se inutile; un KO raddoppia e
    # non e' un problema, prima o poi ci ricapitiamo sopra).
    _prov = "authoritative" if _QUOTA_RESET_RE.search(str(detail or "")) \
        else "heuristic"
    try:
        st = abs(int(status or 0)) or None
    except (TypeError, ValueError):
        st = None
    n = 0
    seen: set[str] = set()
    try:
        groups = router.config.groups
    except Exception:                              # noqa: BLE001
        groups = {}
    for lst in groups.values():
        for d in lst:
            u = d.get("unique")
            if not u or u in seen:
                continue
            if dep_account_key(d) != acct:
                continue
            seen.add(u)
            with contextlib.suppress(Exception):
                # Gia' in pausa "sostanziosa" (il residuo EFFETTIVO e' cappato
                # da max_cooldown_sec, quindi confrontare i valori esatti e'
                # inaffidabile): non riscrivere, per non accorciare un
                # cooldown piu' lungo.
                if router.is_cooled_down(u) and \
                        router.cooldown_residual(u) >= QUOTA_MIN_COOLDOWN_S:
                    continue
                router.mark_failed(u, seconds=secs,
                                   reason="quota_exhausted_account",
                                   status=st, provenance=_prov)
                n += 1
    if n:
        log.warning("[quota-acct] %s: quota giornaliera dell'account %s "
                    "esaurita -> %d deployment dell'account in pausa %.0fs",
                    (dep or {}).get("unique"), acct, n, secs)
    return n


# ---------------------------------------------------------- MAX_INPUT 413
# Il CSV puo' MENTIRE sul massimo input (dichiara 32k, il provider taglia a
# 16k). Molti provider mettono il limite VERO nel body del 400/413:
#   "maximum context length is 16384 tokens, but you requested 21500"
# Lo estraiamo e ridimensioniamo il deployment (router.note_discovered_max_input).
_MAXCTX_PATTERNS = (
    re.compile(r"maximum\s+(?:context|input|prompt)\s+length\s+is\s+"
               r"(\d{3,9})", re.I),
    re.compile(r"(?:context|input|prompt)\s+length\s+(?:is\s+|of\s+|limit\s+"
               r"(?:is\s+)?)?(\d{3,9})", re.I),
    re.compile(r"limit\s+of\s+(\d{3,9})\s*tokens", re.I),
    re.compile(r"(?:reduce|shorten)\s+(?:the\s+)?(?:length|messages|prompt)"
               r"[^0-9]{0,40}(\d{3,9})\s*tokens", re.I),
    re.compile(r"max(?:imum)?\s+(?:of\s+)?(\d{3,9})\s*tokens", re.I),
    re.compile(r"too\s+long[^0-9]{0,40}(\d{3,9})\s*tokens", re.I),
)


def extract_provider_max_input(detail: str | None) -> int | None:
    """Estrae il VERO limite di input (token) dal body di un errore
    context-length del provider. None se non c'e' un numero credibile."""
    d = detail or ""
    for rx in _MAXCTX_PATTERNS:
        m = rx.search(d)
        if not m:
            continue
        try:
            n = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if 256 <= n <= 32_000_000:
            return n
    return None


def _looks_context_limit(status, detail: str | None) -> bool:
    try:
        st = abs(int(status or 0))
    except (TypeError, ValueError):
        st = 0
    if st == 413:
        return True
    low = (detail or "").lower()
    return ("context length" in low or "context_length_exceeded" in low
            or "maximum context" in low or "too many tokens" in low
            or "prompt is too long" in low or "input is too long" in low)


def note_context_limit(router, dep: dict | None, status, detail,
                       ctx: int | None = None) -> int | None:
    """Regola: se il provider rivela il vero limite (o risponde 413 senza
    numero), il deployment viene RIDIMENSIONATO al vero max_input, cosi' il
    router non gli rimanda piu' payload troppo grossi. Ritorna il limite o
    None."""
    if not dep or not _looks_context_limit(status, detail):
        return None
    lim = extract_provider_max_input(detail)
    if not lim and ctx:
        try:
            lim = max(1024, int(int(ctx) * 0.9))
        except (TypeError, ValueError):
            lim = None
    if not lim:
        return None
    try:
        router.note_discovered_max_input(dep.get("unique"), lim)
        return lim
    except Exception:                              # noqa: BLE001
        log.debug("[max-input] note_discovered fallito per %s",
                  dep.get("unique"), exc_info=True)
        return None

# Loop degenere rilevato in STREAMING (kill precoce): il modello produce
# output ripetitivo all'infinito -> cooldown medio, il routing ruota subito.
STREAM_LOOP_COOLDOWN_S = 300                  # 5min


def _sse(obj) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()


async def _safe_aread(resp, timeout: float = 6.0) -> bytes:
    """Legge il body di una resp streaming SENZA propagare errori httpx: se la
    connessione cade mentre leggiamo il body d'errore, torniamo b"" (lo status
    HTTP lo conosciamo gia'). Cap di tempo: un body d'errore non deve mai
    ereditare il read-timeout da 180s dello stream."""
    try:
        return await asyncio.wait_for(resp.aread(), timeout=timeout)
    except (httpx.HTTPError, asyncio.TimeoutError, Exception):
        return b""


def _json_to_sse(raw: bytes):
    """Adatta una risposta chat.completion JSON (upstream che IGNORA
    stream:true) a chunk SSE: role -> content -> finish -> [DONE].
    Ritorna list[bytes] oppure None se il body non e' una risposta utile
    (envelope d'errore / vuoto) -> il chiamante ruota."""
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("type") == "error" or (obj.get("error") and
                                      not obj.get("choices")):
        return None
    try:
        msg = (obj.get("choices") or [{}])[0].get("message") or {}
    except Exception:
        return None
    tool_calls = msg.get("tool_calls")
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content
                          if isinstance(p, dict))
    if not tool_calls and not (isinstance(content, str) and content.strip()):
        return None                       # vuoto -> ruota
    base = {"id": obj.get("id") or "chatcmpl-nx-adapted",
            "object": "chat.completion.chunk",
            "created": obj.get("created") or int(time.time()),
            "model": obj.get("model") or ""}
    d0 = {"role": "assistant"}
    if tool_calls:
        d0["tool_calls"] = tool_calls
    out = [_sse(dict(base, choices=[{"index": 0, "delta": d0,
                                     "finish_reason": None}]))]
    if isinstance(content, str) and content:
        out.append(_sse(dict(base, choices=[{"index": 0,
                   "delta": {"content": content}, "finish_reason": None}])))
    fr = (obj.get("choices") or [{}])[0].get("finish_reason") or "stop"
    out.append(_sse(dict(base, choices=[{"index": 0, "delta": {},
                                         "finish_reason": fr}])))
    if isinstance(obj.get("usage"), dict):
        out.append(_sse(dict(base, choices=[], usage=obj["usage"])))
    out.append(b"data: [DONE]\n\n")
    return out


def _inject_thought_signatures(body: dict) -> None:
    """Re-inietta le firme note nei tool_calls di replay (solo se mancanti).

    Se la dummy-fill per-request e' attiva (`get_dummy_fill`), per i tool_call
    del TURNO CORRENTE (dopo l'ultimo messaggio `user`) privi di firma reale
    viene iniettata la firma DUMMY ufficiale: Google 3 valida solo il turno
    corrente e le dummy saltano la validazione (nessun 400, reasoning degradato).
    Ritorna una NUOVA lista messages, per non inquinare il payload originale
    (un fallback su un provider non-Google non deve vedere extra_content)."""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return
    # inizio del turno corrente: indice dell'ultimo messaggio `user`
    # (-1 = nessun user: considera tutto come turno corrente)
    turn_start = -1
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            turn_start = i
    dummy_fill, dummy_value = get_dummy_fill()
    changed = False
    new_msgs = []
    for mi, m in enumerate(msgs):
        if not (isinstance(m, dict) and m.get("role") == "assistant"
                and m.get("tool_calls")):
            new_msgs.append(m)
            continue
        in_current_turn = mi > turn_start
        new_tcs = []
        msg_changed = False
        for tc in m["tool_calls"]:
            if not isinstance(tc, dict) or not tc.get("id"):
                new_tcs.append(tc)
                continue
            g = ((tc.get("extra_content") or {}).get("google") or {})
            if isinstance(g, dict) and g.get("thought_signature"):
                log.debug("[thought_sig] skipped tc_id=%s (already has extra_content)", tc["id"])
                new_tcs.append(tc)          # il client l'ha gia': non tocco
                continue
            sig = THOUGHT_SIGS.get(tc["id"])
            is_dummy = False
            if not sig and dummy_fill and in_current_turn and dummy_value:
                sig = dummy_value           # firma dummy ufficiale Google
                is_dummy = True
            if not sig:
                new_tcs.append(tc)
                continue
            tc2 = dict(tc)
            ec = dict(tc2.get("extra_content") or {})
            g2 = dict(ec.get("google") or {})
            g2["thought_signature"] = sig
            ec["google"] = g2
            tc2["extra_content"] = ec
            new_tcs.append(tc2)
            msg_changed = True
            if is_dummy:
                log.info("[thought_sig] dummy-fill for tc_id=%s (turno corrente)",
                         tc["id"])
            else:
                log.info("[thought_sig] injected for tc_id=%s", tc["id"])
        if msg_changed:
            m2 = dict(m)
            m2["tool_calls"] = new_tcs
            new_msgs.append(m2)
            changed = True
        else:
            new_msgs.append(m)
    if not changed:
        log.debug("[thought_sig] no signatures to inject")
    if changed:
        body["messages"] = new_msgs


def _capture_sigs_from_obj(obj) -> None:
    """Cattura firme da un oggetto chat.completion/chunk OpenAI-style."""
    if not isinstance(obj, dict):
        return
    for ch in (obj.get("choices") or []):
        if not isinstance(ch, dict):
            continue
        msg = ch.get("delta") or ch.get("message") or {}
        sigs = extract_signatures(msg)
        if sigs:
            THOUGHT_SIGS.store_many(sigs)
            log.debug("[thought_sig-capture] extracted sig for tc_id=%s from upstream", msg.get("tool_calls", [{}])[0].get("id", "unknown") if msg.get("tool_calls") else "unknown")


def _capture_sigs_from_sse(data_bytes: bytes) -> None:
    """Cattura firme da un blocco SSE (una o piu' righe 'data: {...}')."""
    for line in data_bytes.split(b"\n"):
        s = line.strip()
        if not s.startswith(b"data:"):
            continue
        body = s[5:].strip()
        if not body or body == b"[DONE]":
            continue
        try:
            _capture_sigs_from_obj(json.loads(body))
            log.debug("[thought_sig-capture] processed SSE chunk")
        except Exception:
            continue


def _looks_empty(data: dict) -> bool:
    """True se la risposta chat NON porta contenuto utile (ne' testo ne'
    tool_calls). Usato per non consegnare MAI un turno vuoto agli agenti."""
    try:
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        if msg.get("tool_calls") or msg.get("images"):
            return False
        c = msg.get("content")
        if isinstance(c, str):
            return not c.strip()
        if isinstance(c, list):
            return not any(isinstance(p, dict) and str(p.get("text") or "").strip()
                           for p in c)
        return c is None
    except Exception:
        return False

# Fallback diretto SOLO per il body che INIZIA con l'envelope d'errore
# provider {"type":"error",...} (opencode-zen / Anthropic: AuthError,
# ModelError, CreditsError, RegionError, ...). Una risposta valida parte
# sempre con {"id":... o {"choices":... (o SSE `data: {...}`), MAI cosi'.
# Qualsiasi ALTRO body che contiene "error" — es. codice di gestione errori
# scritto da un modello, che arriva comunque in un 200 con `choices` — non
# viene MAI toccato: finisce solo in error-audit.log per la revisione.
_PROVIDER_ERR_ENVELOPE_RE = re.compile(
    r'\s*(?:data:\s*)?\{\s*"type"\s*:\s*"error"', re.IGNORECASE)


def is_provider_error_body(detail: str) -> bool:
    """True SOLO se il body INIZIA con l'envelope {"type":"error",...}."""
    return bool(detail) and _PROVIDER_ERR_ENVELOPE_RE.match(detail) is not None


# Envelope OpenAI-style {"error":{...}}: alcuni provider (es. bynara) lo
# usano per un fault LATO PROVIDER nel leggere/parsare la richiesta
# ("Could not read the request body.", con request_id). Non e' un rifiuto del
# CONTENUTO da parte del modello: un altro deployment accetta lo stesso
# payload -> si RUOTA (cooldown corto), mai pass-through del 400 al client.
_OPENAI_ERR_ENVELOPE_RE = re.compile(
    r'\s*(?:data:\s*)?\{\s*"error"\s*:\s*\{', re.IGNORECASE)
_PROVIDER_BODY_FAULT_RE = re.compile(
    r"could not read the request body"
    r"|unable to (read|parse) (the )?request( body)?"
    r"|invalid request body|malformed request"
    r"|cannot parse (the )?request|error parsing (the )?request"
    r"|failed to (read|parse) (the )?request"
    r"|error reading (the )?request",
    re.IGNORECASE)


_MESSAGE_FIELD_RE = re.compile(
    r'"message"\s*:\s*"((?:[^"\\]|\\.){0,400})"', re.IGNORECASE)


def _provider_error_message(detail: str) -> str:
    """Estrae SOLO il campo `message` dell'envelope d'errore del provider.

    Non si guarda il resto del body: alcuni provider ci rimettono dentro la
    richiesta (che puo' contenere codice con frasi d'errore, scritte da NOI o
    generate dal modello) e non va MAI classificata come fault del provider.
    """
    s = (detail or "").lstrip()
    if s[:5].lower() == "data:":
        s = s[5:].lstrip()
    if s[:1] == "{":
        try:
            obj = json.loads(s)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            err = obj.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                return err["message"]
            if isinstance(err, str):
                return err
            if obj.get("type") == "error" and isinstance(obj.get("message"), str):
                return obj["message"]
            if isinstance(obj.get("message"), str):
                return obj["message"]
    # fallback (body eventualmente troncato): SOLO i valori di "message"
    return "\n".join(m.group(1) for m in _MESSAGE_FIELD_RE.finditer(detail or ""))


def is_provider_fault_body(detail: str) -> bool:
    """True se `detail` e' un envelope OpenAI `{"error":{...}}` il cui MESSAGE
    (NON il resto del body) segnala un fault del PROVIDER nel leggere/parsare
    la richiesta (deployment-side)."""
    if not detail or _OPENAI_ERR_ENVELOPE_RE.match(detail) is None:
        return False
    msg = _provider_error_message(detail)
    return bool(msg) and _PROVIDER_BODY_FAULT_RE.search(msg) is not None


def is_embedded_provider_error(text: str) -> bool:
    """True SOLO se `text` e' INTERAMENTE un envelope d'errore provider
    incollato nel content (bug di alcuni provider che lo mettono in
    delta.content come testo).

    Serve a NON scambiare per errore il CODICE/testo scritto dal modello:
    richiede un oggetto JSON completo, senza prosa attorno, con le chiavi
    tipiche di un envelope provider (`error`/`type:error` + message/
    request_id/code). Una funzione, un frammento di codice o una frase non
    matchano.
    """
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not (s.startswith("{") and s.endswith("}")):
        return False
    try:
        obj = json.loads(s)
    except Exception:
        return False
    if not isinstance(obj, dict):
        return False
    if obj.get("type") == "error":
        return (isinstance(obj.get("error"), (dict, str))
                or isinstance(obj.get("message"), str))
    err = obj.get("error")
    if isinstance(err, dict):
        return bool(err.get("type") or err.get("code")) and bool(
            err.get("message") or err.get("request_id")
            or obj.get("request_id"))
    return False


# Header x-opencode-session (OpenCode Go / opencode-zen): la sessione viene
# calcolata AL VOLO come hash deterministico di api_key + client_ip + profilo,
# cosi' ogni (chiave, client, profilo) genera una sessione stabile e
# distinguibile, senza mai esporre la chiave in chiaro. Due profili che
# condividono la stessa chiave dallo stesso IP hanno comunque sessioni
# diverse. Se il client ha gia' inviato l'header (passthrough), quel valore
# PREVALE su tutto: la richiesta del client passa dritta com'e' arrivata.
#
# Formato: identico alle sessioni native opencode (`ses_` + 12 hex + 14 base62,
# es. "ses_fb5856a92ffekeJf366z10YP19", verificato sui log reali): OpenCode Go
# usa l'header per session affinity/prompt caching e riconosce il valore solo
# se "assomiglia" a un session ID valido.
_B62_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_B62_LEN = 14

# Session id NATIVO opencode: "ses_" + 12 hex lowercase + 14 base62 (26 char
# dopo il prefisso). opencode.ai/zen e /zen/go ACCETTANO solo questo formato:
# il fingerprint interno "fq_..." (cosi' come "abc" o "ses_x") viene rifiutato
# con 403 FreeTierError. Quando il client non manda una sessione nativa, la si
# rigenera dai segnali interni per restare verosimile e coerente.
# (regex + `_is_native_session` vivono in opencode_gate, fonte unica condivisa
#  col router.)


def _native_session_of(basis: str) -> str:
    """Session ID nel formato nativo opencode, deterministico dal seed.

    Formato: "ses_" + 12 hex lowercase + 14 base62 (26 char totali), come le
    sessioni generate dal client opencode (es. ses_fb5856a92ffekeJf366z10YP19).
    Stesso seed -> stesso valore; mai la chiave in chiaro.
    """
    digest = hashlib.sha256(basis.encode()).digest()      # 32 byte
    hex12 = digest[:6].hex()                              # 6 byte -> 12 hex
    n = int.from_bytes(digest[6:20], "big")               # 14 byte -> base62
    out = []
    for _ in range(_B62_LEN):
        n, r = divmod(n, 62)
        out.append(_B62_ALPHABET[r])
    return f"ses_{hex12}{''.join(reversed(out))}"


def _session_headers(dep: dict, *, profile: str = "",
                     client_ip: str = "",
                     session: str | None = None,
                     attribution: dict | None = None) -> dict[str, str]:
    """Header x-opencode-session per la richiesta upstream.

    Priorità:
      1. `session` (passthrough dal client) se presente E nel formato nativo
         opencode (`ses_` + 12 hex + 14 base62) -> esattamente quel valore;
      2. altrimenti session ID nel formato nativo opencode derivato da
         `api_key + "|" + client_ip + "|" + profilo` (+ la sessione interna se
         presente, per coerenza). Deterministico: stesso input -> stesso valore.

    `attribution`: header di identità INVIATI DAL CLIENT
    (HTTP-Referer / X-Title / X-OpenRouter-Title per OpenRouter; user-agent /
    x-opencode-* / x-session-* per opencode.ai). Se presenti, vincono sul
    valore configurato (passthrough fedele: il client si presenta come
    l'harness che e'); altrimenti si usa l'identita' di policy.

    Oltre a x-opencode-session ritorna (solo per upstream opencode.ai)
    gli header di identità opencode completi: opencode.ai/zen rifiuta con
    403 FreeTierError le richieste che non provengono da un client opencode
    reale (gate "can only be used from within OpenCode").

    Ritorna SEMPRE l'header: OpenCode Go lo richiede per session affinity e
    prompt caching; gli altri provider lo ignorano senza effetto.
    """
    out: dict[str, str] = {}
    if session and _is_native_session(session):
        # Passthrough fedele: il client opencode manda gia' una sessione nel
        # formato nativo (x-session-affinity / x-opencode-session).
        out = {"x-opencode-session": session}
        if os.environ.get("SNIFF_HEADERS"):
            log.warning("[sniff] upstream session=passthrough value=%s", session)
    else:
        # Nessuna sessione dal client, oppure sessione "interna" NON nativa
        # (es. fingerprint anonimo `fq_...` calcolato dal gateway): opencode.ai
        # rifiuta con 403 i valori fuori formato, quindi si ricalcola un id
        # nativo VEROSIMILE. Se esiste una sessione interna la si aggancia al
        # basis (coerenza: stessa conversazione -> stesso session id upstream,
        # deterministico e distinto per chiave/IP/profilo/sessione).
        key = (dep.get("api_key") or "").strip()
        parts = [key, client_ip, profile]
        if session:
            parts.append(session)
        basis = "|".join(parts)
        value = _native_session_of(basis)
        if os.environ.get("SNIFF_HEADERS"):
            log.warning("[sniff] upstream session=native value=%s "
                        "basis=(%s,%s,%s,derived=%s)", value, bool(key),
                        bool(client_ip), bool(profile), bool(session))
        out = {"x-opencode-session": value}
    # Attribuzione app OpenRouter: vale per OGNI upstream openrouter.ai
    # (i modelli :free ora, ma il gate 'agentic harness' può estendersi):
    # gli header HTTP-Referer + X-Title servono a identificare un'app
    # riconosciuta (openrouter.ai/apps); il referer del CLIENT vince se
    # presente (passthrough), altrimenti la policy (default opencode,
    # l'harness dietro il gateway).
    out.update(_openrouter_attribution(dep, client_headers=attribution))
    # Identità opencode verso opencode.ai (zen / zen/go): il gate
    # "can only be used from within OpenCode" richiede gli header nativi
    # del client opencode. Per client opencode reale è passthrough fedele
    # (sintesi dei soli campi mancanti); per gli altri è un POC disattivato
    # di default (env OPENCODE_SPOOF_HEADERS).
    out.update(_opencode_upstream_headers(dep, client_headers=attribution))
    return out


def _client_attribution(request) -> dict[str, str]:
    """Estrae dall'header del client gli header di identità upstream.

    Due famiglie, stesso dict:
    - Attribuzione app OpenRouter (HTTP-Referer, X-Title, X-OpenRouter-Title):
      i modelli :free sono limitati agli "agentic harness" riconosciuti.
    - Identità client opencode (user-agent, x-opencode-*, x-session-*):
      opencode.ai/zen e /zen/go verificano che la richiesta arrivi da un
      client opencode reale (gate "can only be used from within OpenCode");
      servono per replicare in upstream gli stessi header del client.

    Ritorna un dict con i SOLI header che il client ha inviato davvero.
    Vuoto = il cliente non si attribuisce -> si usano i default di policy.
    """
    out: dict[str, str] = {}
    try:
        headers = request.headers or {}
    except Exception:
        return out

    for name in ("HTTP-Referer", "X-Title", "X-OpenRouter-Title"):
        v = headers.get(name) or ""
        if isinstance(v, str) and v.strip():
            out[name] = v.strip()
    for name in ("user-agent", "x-opencode-client", "x-opencode-request",
                 "x-opencode-project", "x-opencode-session",
                 "x-session-affinity", "x-session-id"):
        v = headers.get(name) or ""
        if isinstance(v, str) and v.strip():
            out[name] = v.strip()
    return out


def _is_opencode_upstream(dep: dict) -> bool:
    """Vero per i deployment che parlano con opencode.ai (zen / zen/go)."""
    return _is_opencode_dep(dep)


def _client_is_opencode(client_headers: dict) -> bool:
    """Vero se il client si presenta come opencode reale.

    Il client opencode 1.18.x verso un gateway custom manda SOLO
    `user-agent: opencode/<ver> ...` + `x-session-affinity`; gli header
    `x-opencode-*` espliciti sono un segnale altrettanto valido.
    """
    return _opencode_client_detect(client_headers)


def _opencode_upstream_headers(dep: dict, *,
                               client_headers: dict | None = None
                               ) -> dict[str, str]:
    """Header opencode per upstream opencode.ai (zen / zen/go).

    opencode.ai/zen verifica che la richiesta arrivi da un client opencode
    reale: senza questi header risponde 403 FreeTierError "can only be used
    from within OpenCode". Il client opencode REALE (TUI/CLI 1.18.x) verso
    il gateway manda SOLO `user-agent: opencode/...` + `x-session-affinity`:
    gli header `x-opencode-*` mancanti vanno sintetizzati qui (request uuid,
    client "cli", project "default"), esattamente come fa il client quando
    parla diretto con opencode.ai.

    Regole:
      - SEMPRE per deployment opencode.ai quando il CLIENT è opencode:
        il gateway replica l'identità del client (passthrough fedele) e
        completa i campi mancanti.
      - Per client NON-opencode: POC "trucco" OFF di default; si sintetizza
        l'identità opencode SOLO se l'env OPENCODE_SPOOF_HEADERS è truthy
        (esperimento: il client si presenta come opencode all'upstream).
    """
    if not _is_opencode_upstream(dep):
        return {}
    client = client_headers or {}
    is_oc = _client_is_opencode(client)
    spoof = _spoof_enabled()
    if not is_oc and not spoof:
        return {}
    metrics.inc("nx_opencode_headers_total",
                ("client" if is_oc else "spoof",))
    ua = (client.get("user-agent") or "").strip()
    if not ua or "opencode/" not in ua.lower():
        ua = "opencode/1.18.31"
    out = {
        "User-Agent": ua,
        "x-opencode-client": (client.get("x-opencode-client")
                              or "cli").strip(),
        "x-opencode-request": (client.get("x-opencode-request")
                               or _uuid.uuid4().hex).strip(),
        "x-opencode-project": (client.get("x-opencode-project")
                               or "default").strip(),
    }
    if os.environ.get("SNIFF_HEADERS"):
        log.warning("[sniff] opencode upstream headers=%s "
                    "(client_opencode=%s spoof=%s)",
                    {k: v[:44] for k, v in out.items()}, is_oc, spoof)
    return out


def _emit_rate_hint(unique: str | None, rl: dict, hook) -> None:
    """Consegna gli hint quota al router (budget guard proattivo). Best-effort:
    un hook rotto non deve mai far fallire la richiesta upstream."""
    if hook is None or not rl or not unique:
        return
    try:
        hook(unique, rl)
    except Exception:                        # noqa: BLE001
        pass


def _openrouter_attribution(dep: dict,
                            client_headers: dict | None = None) -> dict[str, str]:
    """Header di attribuzione app per upstream OpenRouter (harness gate).

    OpenRouter (2026) applica un gate 'agentic harness' a diversi modelli
    (es. i `:free`, limitati alle app riconosciute su openrouter.ai/apps) e
    può estenderlo a nuovi modelli senza preavviso: la richiesta che non
    identifica un'app riconosciuta viene rifiutata con 403 "only available
    on agentic harnesses". L'identificazione avviene tramite gli header di
    app-attribution `HTTP-Referer` + `X-Title` (vedi /docs/app-attribution).

    La regola vale per OGNI deployment con api_base OpenRouter (a prescindere
    dal modello: pagato o :free, esistente o futuro): gli header di
    attribuzione non costano nulla sui modelli che non li richiedono e
    mettono il gateway al sicuro se il requisito si estende.

    Precedenza:
      1. header inviati DAL CLIENT (`client_headers`, passthrough fedele);
      2. env OPENROUTER_APP_REFERER / OPENROUTER_APP_TITLE;
      3. policy openrouter_app_referer / openrouter_app_title;
      4. default https://opencode.ai / opencode (l'harness dietro il gateway).

    ATTENZIONE solo per api_base OpenRouter: gli altri provider non vogliono
    (o rifiutano) header estranei come HTTP-Referer.
    """
    base = (dep.get("api_base") or "").lower()
    if "openrouter.ai" not in base:
        return {}
    client = client_headers or {}
    # 1) passthrough fedele: se il client si attribuisce, vince tutto.
    ref = client.get("HTTP-Referer") or ""
    title = (client.get("X-Title") or client.get("X-OpenRouter-Title") or "")
    if not ref:
        # 2) env, 3) policy, 4) default
        ref = os.environ.get("OPENROUTER_APP_REFERER") or ""
        title = title or os.environ.get("OPENROUTER_APP_TITLE") or ""
        if not ref or not title:
            try:
                from . import main as _gw
                pol = getattr(_gw, "policy", None)
                if pol is not None:
                    ref = ref or (getattr(pol, "openrouter_app_referer",
                                          "") or "")
                    title = title or (getattr(pol, "openrouter_app_title",
                                              "") or "")
            except Exception:                   # mai bloccare il routing
                pass
        ref = ref or "https://opencode.ai"
        title = title or "opencode"
    out: dict[str, str] = {}
    if ref:
        out["HTTP-Referer"] = ref
    if title:
        out["X-Title"] = title
        out["X-OpenRouter-Title"] = title
    if os.environ.get("SNIFF_HEADERS"):
        log.warning("[sniff] openrouter attribution: referer=%s title=%s "
                    "(client=%s)", ref, title, bool(client))
    return out


def classify_error(status: int | None, reason: str | None,
                   detail: str | None, headers=None) -> str:
    """Classificazione centralizzata: determina la strategia di recupero.

    - PERMANENT_DEAD: chiave non valida/revocata, modello rimosso -> retire.
    - QUOTA_RESET:    quota esaurita con reset noto -> cooldown esatto al reset.
    - TRANSIENT:      rete/timeout/5xx -> cooldown breve, ritenta.
    - GENERIC_4XX:    altro 4xx -> escalation.
    """
    st = abs(int(status)) if status else 0
    d = detail or ""
    if _MODEL_MISSING_RE.search(d):
        return ErrorKind.PERMANENT_DEAD
    if st in (401, 403):
        return ErrorKind.PERMANENT_DEAD
    if st == 402:
        return ErrorKind.PERMANENT_DEAD
    if st == 429 or _QUOTA_EXHAUSTED_RE.search(d):
        return (ErrorKind.QUOTA_RESET if parse_quota_reset_seconds(d)
                else ErrorKind.GENERIC_4XX)
    if st >= 500 or st == 0 or (reason or "").lower() in (
            "timeout", "network_error", "provider_transient"):
        return ErrorKind.TRANSIENT
    return ErrorKind.GENERIC_4XX


class UpstreamError(Exception):
    def __init__(self, status: int | None, detail: str,
                 retry_after: float | None = None, *, final: bool = False,
                 rate_limits: dict | None = None):
        self.status = status
        self.detail = detail
        # hint quota dagli header X-RateLimit-* del provider (remaining/limit/
        # reset): utili per il budget guard proattivo PRIMA del 429.
        self.rate_limits = rate_limits or {}
        # final=True: NON ritentare/ruotare — e' gia' la decisione definitiva
        # (es. catena esaurita, output vuoto). call_with_fallback la ri-alza
        # subito invece di trattarla come un errore upstream ritriabile.
        self.final = final
        # secondi richiesti dal provider (header Retry-After su 429):
        # il chiamante lo passa a router.mark_failed(seconds=...) per un
        # cooldown proporzionato invece dell'escalation standard.
        self.retry_after = retry_after
        # AUDIT: ogni body che contiene "error" va nel file locale (chokepoint
        # unico). Chi lo consegna al client aggiunge poi una riga PASS-THROUGH.
        if detail and "error" in detail.lower():
            errlog.warning("status=%s :: %s", status, str(detail)[:500])
        super().__init__(detail)


class StreamLoopDetected(UpstreamError):
    """Loop degenere rilevato DURANTE lo streaming: stream interrotto subito,
    il chiamante tratta il fallimento come una normale rotazione
    (reason="loop_detected"). Nessun status HTTP: lo stream e' gia' stato
    interrotto, non c'e' una risposta upstream da passare al client."""

    def __init__(self, detail: str):
        super().__init__(None, detail)


_STREAM_CONTENT_KEYS = ("content", "reasoning_content", "text", "thinking")


def _extract_stream_words(obj, out: deque, max_words: int) -> None:
    """Estrae le parole di CONTENUTO da un oggetto SSE (delta content /
    reasoning / tool_call args) nel buffer circolare. Best-effort, mai errori."""
    if not isinstance(obj, dict):
        return
    try:
        choices = obj.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or choices[0].get("message") or {}
            if isinstance(delta, dict):
                for k in _STREAM_CONTENT_KEYS:
                    v = delta.get(k)
                    if isinstance(v, str) and v:
                        out.extend(v.split())
                _tcs = delta.get("tool_calls")
                if isinstance(_tcs, list):
                    for _tc in _tcs:
                        if not isinstance(_tc, dict):
                            continue
                        _fn = _tc.get("function")
                        if isinstance(_fn, dict):
                            _a = _fn.get("arguments")
                            if isinstance(_a, str) and _a:
                                out.extend(_a.split())
            return
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    # provider non-OpenAI: stringa grezza o campo contenuto in testa
    for k in _STREAM_CONTENT_KEYS:
        v = obj.get(k)
        if isinstance(v, str) and v:
            out.extend(v.split())
            return


def _feed_stream_words(line: bytes, out: deque, max_words: int) -> None:
    if max_words <= 0:
        return
    ln = line.strip()
    if not ln.startswith(b"data:"):
        return
    data = ln[5:].strip()
    if not data or data == b"[DONE]":
        return
    try:
        obj = json.loads(data)
    except Exception:
        return
    _extract_stream_words(obj, out, max_words)


def _stream_loop_guard(source: AsyncIterator[bytes], lc, max_words: int,
                       unique: str, model: str) -> AsyncIterator[bytes]:
    """Wrapper dello stream SSE con loop detector ON-THE-FLY.

    Buffer circolare delle ultime `max_words` parole di contenuto estratte dai
    chunk. A ogni chunk riesegue `stream_loop_reason` sul buffer: se scatta,
    solleva StreamLoopDetected -> il generator upstream viene chiuso dal
    teardown del for (resp.aclose()) e il chiamante ruota (pre-byte) o chiude
    lo stream verso il client (mid-stream)."""
    buf: deque[str] = deque(maxlen=max(1, max_words))
    line_buf = b""

    async def _gen() -> AsyncIterator[bytes]:
        nonlocal line_buf
        async for chunk in source:
            line_buf += chunk
            while b"\n" in line_buf:
                line, line_buf = line_buf.split(b"\n", 1)
                _feed_stream_words(line, buf, max_words)
            _feed_stream_words(line_buf, buf, max_words)
            if max_words > 0:
                _lr = stream_loop_reason(list(buf), lc)
                if _lr:
                    log.warning("[stream-loop] %s (%s): loop rilevato (%s) "
                                "dopo %d parole -> kill stream", unique, model,
                                _lr, len(buf))
                    raise StreamLoopDetected(
                        f"stream loop rilevato ({_lr}) su {model}")
            yield chunk

    return _gen()


def _retry_after_of(resp: httpx.Response) -> float | None:
    """Header Retry-After (delta-seconds). HTTP-date non supportato: raro e
    ambiguo -> meglio l'escalation standard."""
    v = resp.headers.get("retry-after")
    if not v:
        return None
    try:
        return max(1.0, float(v.strip()))
    except ValueError:
        return None


def _rate_limits_from(resp: httpx.Response) -> dict:
    """Header informativi quota: X-RateLimit-Requests-{Remaining,Limit,Reset}
    e X-RateLimit-Tokens-{Remaining,Limit,Reset} (case-insensitive, usati da
    Groq/OpenRouter/DeepInfra/Fireworks/...). Normalizzati in
    `requests_remaining`, `requests_limit`, `requests_reset`,
    `tokens_remaining`, `tokens_limit`, `tokens_reset`. `reset` puo' essere
    un epoch (grande) o un delta-seconds (piccolo): la semantica la decide
    il chiamante."""
    out: dict = {}
    h = {k.lower(): v.strip() for k, v in resp.headers.items()}
    for base, tag in (("x-ratelimit-requests-", "requests_"),
                      ("x-ratelimit-tokens-", "tokens_")):
        for field in ("remaining", "limit", "reset"):
            v = h.get(base + field)
            if not v:
                continue
            try:
                out[tag + field] = float(v)
            except (TypeError, ValueError):
                continue
    return out


# Google (generativelanguage) e altri mettono il ritardo consigliato NEL BODY
# del 429, non nell'header: `"retryDelay": "58s"` dentro un blocco RetryInfo,
# oppure `... Please retry in 58.93s`. Senza questo il gateway non lo vede e
# applica l'escalation (cooldown di ore) su un semplice rate-limit giornaliero.
_RETRY_BODY_RE = re.compile(
    r'"retryDelay"\s*:\s*"?\s*(\d+(?:\.\d+)?)\s*s'          # "retryDelay": "58s"
    r'|"retryDelay"\s*:\s*\{\s*"seconds"\s*:\s*"?(\d+)'      # {"seconds": 58}
    r'|"retry_?after"\s*:\s*"?\s*(\d+(?:\.\d+)?)\s*s?"?'     # "retry_after": 20 / "23s"
    r'|retry\s+in\s+(\d+(?:\.\d+)?)\s*s(?:econds)?'          # "retry in 58.9s"
    r'|retry\s+after\s+(\d+(?:\.\d+)?)\s*s(?:econds)?'       # "retry after 23s"
    r'|try\s+again\s+in\s+(\d+(?:\.\d+)?)\s*s(?:econds)?',    # "try again in 30s"
    re.IGNORECASE)
_RETRY_BODY_CAP_S = 300.0                                     # un 429 non chiede ore
# Floor minimo di cooldown per i 429: molti provider free restituiscono
# Retry-After di 1-2s (o assente) ma bloccano piu' a lungo; senza floor si
# entra in un loop di 429 ravvicinati. Override da policy
# (`retry_after_min_sec`); <=0 disabilita il floor.
RETRY_AFTER_MIN_SEC = 10.0
# Floor specifici per provider (provider -> secondi). Se assente vale
# RETRY_AFTER_MIN_SEC. Un free-tier lento (quota giornaliera) merita un
# floor piu' alto di uno veloce (es. groq).
_RETRY_FLOOR_BY_PROVIDER: dict[str, float] = {}


def set_retry_after_floor(sec) -> None:
    """Imposta il floor di cooldown dei 429 (0 o negativo = disabilitato)."""
    global RETRY_AFTER_MIN_SEC
    try:
        v = float(sec)
    except (TypeError, ValueError):
        return
    RETRY_AFTER_MIN_SEC = max(0.0, v)


def set_retry_after_floors(default, by_provider=None) -> None:
    """Floor di default + tabella per-provider (provider -> secondi >= 0)."""
    set_retry_after_floor(default)
    global _RETRY_FLOOR_BY_PROVIDER
    table: dict[str, float] = {}
    if isinstance(by_provider, dict):
        for k, v in by_provider.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv >= 0:
                table[str(k).strip().lower()] = fv
    _RETRY_FLOOR_BY_PROVIDER = table


def _apply_retry_floor(v: float, provider: str | None = None) -> float:
    floor = RETRY_AFTER_MIN_SEC
    if provider:
        floor = _RETRY_FLOOR_BY_PROVIDER.get(str(provider).strip().lower(),
                                             floor)
    return max(floor, v) if floor > 0 else v


def _retry_after_from(resp: httpx.Response, body: str | None,
                      provider: str | None = None) -> float | None:
    """Retry-After: prima il reset esatto X-RateLimit-*-Reset (epoch/delta,
    piu' preciso del probe passivo), poi l'header Retry-After, infine (fallback)
    il retryDelay dal body 429. Cap a 300s: un rate-limit non deve mai valere
    un cooldown di ore. In coda applichiamo il floor minimo anti-loop
    (per-provider se noto)."""
    now = time.time()
    for rk in ("requests_reset", "tokens_reset"):
        rv = _rate_limits_from(resp).get(rk)
        if not rv or rv <= 0:
            continue
        seconds = rv - now if rv > 1_000_000_000 else rv   # epoch vs delta
        if seconds > 0:
            return _apply_retry_floor(
                max(1.0, min(seconds, _RETRY_BODY_CAP_S)), provider)
    hdr = _retry_after_of(resp)
    if hdr is not None:
        return _apply_retry_floor(hdr, provider)
    if not body:
        return None
    m = _RETRY_BODY_RE.search(body)
    if not m:
        return None
    # Gruppi di cattura (in ordine di pattern):
    # 1) "retryDelay": "58s"       2) {"seconds": 58}
    # 3) "retry_after": 20 | "23s" 4) retry in Ns
    # 5) retry after Ns            6) try again in Ns
    # Preferiamo il formato oggetto {"seconds": N} (piu' strutturato), poi
    # tutti gli altri nell'ordine in cui compaiono.
    groups = m.groups()
    order = ([groups[1]] if len(groups) > 1 else []) \
        + [g for i, g in enumerate(groups) if i != 1]
    for g in order:
        if g is not None:
            try:
                val = float(g)
                return _apply_retry_floor(
                    max(1.0, min(val, _RETRY_BODY_CAP_S)), provider)
            except (TypeError, ValueError):
                continue
    return None


# marker tipici di rifiuto MODALITÀ nei 400 provider-side (auto-learn caps)
_MEDIA_REJECT_MARKERS = ("image", "vision", "multimodal", "multi-modal",
                         "audio", "video", "modalit", "modality",
                         "not supported", "unsupported")


def media_reject_signature(detail: str) -> bool:
    """True se il dettaglio errore somiglia a un rifiuto di modalità input."""
    low = (detail or "").lower()
    return any(m in low for m in _MEDIA_REJECT_MARKERS)


# Marker SPECIFICI di modalita' (senza il generico "unsupported"/"not
# supported", che compare anche negli errori di reasoning/schema): serve a
# distinguere un vero rifiuto vision/image da un messaggio fuorviante.
_MEDIA_MODALITY_MARKERS = ("vision", "image", "audio", "video",
                           "multimodal", "multi-modal", "modalit", "modality")


def media_modality_signature(detail: str) -> bool:
    """True se il dettaglio nomina ESPLICITAMENTE una modalita' di input."""
    low = (detail or "").lower()
    return any(m in low for m in _MEDIA_MODALITY_MARKERS)


# capability di INPUT media: se la richiesta non ne ha bisogno, un rifiuto
# "does not support vision input" non e' un rifiuto di modalita' ma un
# modello/proxy rotto per QUESTA richiesta (caso llm7/Cloudflare che risponde
# "vision" a richieste di puro testo) -> va in cooldown come un KO normale.
MEDIA_INPUT_CAPS = frozenset({"vision", "image", "audio", "video"})


def media_input_needed(need) -> bool:
    """True se la richiesta richiede capability di input media."""
    return bool(set(need or ()) & MEDIA_INPUT_CAPS)


# --------------------------------------------------------------- image_gen
# Modelli immagine esposti come CHAT (es. Gemini image via shim OpenAI-compat
# `/v1beta/openai`): l'endpoint NATIVO /images/generations puo' mancare oppure
# rifiutare lo schema OpenAI `{"prompt": ...}`. Google risponde:
#   400 'Invalid JSON payload received. Unknown name "prompt": Cannot find field.'
# In quel caso la STESSA richiesta va ritentata su /chat/completions con
# `messages` + `modalities:["image"]` (vedi image_chat_payload).
_IMAGES_PAYLOAD_UNSUPPORTED_RE = re.compile(
    r"unknown name|unknown field|cannot find field|invalid json payload"
    r"|invalid argument|unrecognized|unexpected (?:field|property|parameter)"
    r"|additional propert|unknown parameter|does not support"
    r"|no such endpoint|method not allowed|unsupported media type",
    re.IGNORECASE)

# Firma STRETTA "campo/argomento sconosciuto al provider" (tipico dei campi
# client-only come `fallback_models`): il 400 e' colpa della RICHIESTA, non del
# deployment -> ruota SENZA cooldown (vedi il ramo schema-payload).
_UNKNOWN_FIELD_RE = re.compile(
    r"unknown name|unknown field|cannot find field|invalid json payload"
    r"|unrecognized|unexpected (?:field|property|parameter)"
    r"|additional propert|unknown parameter",
    re.IGNORECASE)

# Rifiuti di CONTENUTO/policy: la richiesta e' rifiutata per policy, NON perche'
# lo schema sia inadatto. Ritentarla via chat fallirebbe identicamente e
# addosserebbe al deployment un errore del client -> vanno ESCLUSI.
_IMAGE_CONTENT_REJECT_RE = re.compile(
    r"\bsafety\b|content policy|policy violation|prohibited|recitation"
    r"|responsible ai|\bblocked\b|\bflagged\b", re.IGNORECASE)


def image_chat_fallback_signature(status: int | None,
                                  detail: str | None) -> bool:
    """True se un errore dell'endpoint NATIVO /images/generations suggerisce che
    il modello vada servito via chat/completions: endpoint assente (404/405),
    media-type non accettato (415) o schema immagine non riconosciuto
    (400/403/422 con firma "campo/argomento non valido"). I rifiuti di
    contenuto/policy sono esclusi (non dipendono dallo schema)."""
    d = (detail or "").strip()
    if _IMAGE_CONTENT_REJECT_RE.search(d):
        return False
    st = abs(int(status)) if status else 0
    if st in (404, 405, 415):
        return True
    if st not in (400, 403, 422):
        return False
    return bool(_IMAGES_PAYLOAD_UNSUPPORTED_RE.search(d))


def image_chat_payload(payload: dict, model: str) -> dict:
    """Converte un body OpenAI /images/generations in un body chat/completions
    per i modelli immagine serviti come chat (`messages` + `modalities`).

    Le opzioni specifiche dell'endpoint immagini (n, size, quality, style,
    response_format, user, stream) non hanno equivalente chat e vengono
    scartate; gli altri campi vengono preservati (es. `seed`)."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        prompt = "" if prompt is None else str(prompt)
    out: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image"],
    }
    n = payload.get("n")
    if isinstance(n, int) and n > 1:
        out["n"] = n
    out.update({k: v for k, v in payload.items()
                if k not in ("model", "prompt", "n", "size", "quality",
                             "style", "response_format", "user", "stream")})
    return out


def _image_item(obj) -> dict | None:
    """Normalizza UNA immagine nelle forme note -> {url}|{b64_json}."""
    if isinstance(obj, str):
        return {"url": obj} if obj else None
    if not isinstance(obj, dict):
        return None
    src = obj.get("image_url")
    if isinstance(src, dict) and src.get("url"):
        return {"url": src["url"]}
    if obj.get("url"):
        return {"url": obj["url"]}
    if obj.get("b64_json"):
        return {"b64_json": obj["b64_json"]}
    data = obj.get("data")
    if isinstance(data, str) and data:
        return {"b64_json": data}
    return None


def extract_chat_images(data: dict) -> list[dict]:
    """Estrae le immagini (schema OpenAI images) da una risposta
    chat/completions di un modello immagine. Forme coperte:
    `choices[].message.images[]` (Gemini shim), content multimodale e URL
    `data:image...` testuale."""
    items: list[dict] = []
    choices = data.get("choices") if isinstance(data, dict) else None
    for ch in choices or []:
        msg = (ch or {}).get("message") or {}
        for im in msg.get("images") or []:
            it = _image_item(im)
            if it:
                items.append(it)
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                it = _image_item(part)
                if it:
                    items.append(it)
        elif isinstance(content, str) and content.startswith("data:image"):
            items.append({"url": content})
    return items


def dep_host(dep: dict) -> str:
    """Hostname dell'endpoint di un deployment (chiave di skip/quarantena)."""
    url = str((dep or {}).get("api_base") or (dep or {}).get("endpoint") or "")
    if not url:
        return ""
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:                                  # noqa: BLE001
        return ""


# Errori PROVIDER-LEVEL: la colpa e' dell'host, non della singola chiave.
# Per il resto della richiesta conviene saltare TUTTO l'host (skipPlatforms)
# invece di bruciare un hop per ogni chiave che ci vive sopra.
PROVIDER_LEVEL_CLASSES = frozenset({
    "upstream_error", "provider_transient", "host_transient", "timeout",
    "network",
})


def is_provider_level(cls: str | None) -> bool:
    return (cls or "") in PROVIDER_LEVEL_CLASSES


class Forwarder:
    def __init__(self, client: httpx.AsyncClient | None = None,
                 keepalive_pool: bool = False):
        # client iniettabile per i test (httpx.MockTransport); se assente
        # usiamo un unico client condiviso (comportamento storico) oppure,
        # se `keepalive_pool` e' True, un pool di client persistenti
        # DEDICATI PER API-KEY (comune per modello): ogni chiave ha la sua
        # connessione/cookie-jar, cosi' un provider non vede MAI un cambio di
        # chiave su una connessione altrui. Niente pool per-origine.
        self.client = client
        self._injected = client
        self._keepalive_pool = bool(keepalive_pool)
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._http_gen = _HTTP_GEN

    def _reset_http_clients(self) -> None:
        """Chiude e scarta i client cachati (timeout/pool cambiati a caldo)."""
        self._http_gen = _HTTP_GEN
        old = list(self._clients.values())
        if self.client is not None:
            old.append(self.client)
        self._clients.clear()
        self.client = None
        if not old:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for cli in old:
            loop.create_task(cli.aclose())

    def _client_for(self, url: str, key: str = "") -> httpx.AsyncClient:
        """Client persistente per API-KEY, creato lazy e riusato.

        Con `keepalive_pool` attivo, una chiave ha il suo client dedicato
        (condiviso tra TUTTI i modelli che quella chiave serve): il riuso
        TCP/TLS e' dentro la stessa chiave, mai tra chiavi diverse dello
        stesso provider. Con il pool disattivato (default) si usa un unico
        client condiviso per tutto (comportamento pre-80fbf72).
        """
        if self._injected is not None:
            return self._injected
        if self._http_gen != _HTTP_GEN:
            self._reset_http_clients()
        if not self._keepalive_pool:
            if self.client is None:
                self.client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
            return self.client
        k = key or url        # fallback: per-URL se la chiave e' vuota
        cli = self._clients.get(k)
        if cli is None:
            cli = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT,
                                    limits=UPSTREAM_LIMITS)
            self._clients[k] = cli
            log.info("[http-pool] client dedicato per api-key "
                     "(keepalive=%d, max=%d, expiry=%.0fs)",
                     UPSTREAM_LIMITS.max_keepalive_connections,
                     UPSTREAM_LIMITS.max_connections,
                     UPSTREAM_LIMITS.keepalive_expiry or 0.0)
        return cli

    async def aclose(self) -> None:
        seen: set[int] = set()
        clients = list(self._clients.values())
        if self.client is not None:
            clients.append(self.client)
        if self._injected is not None:
            clients.append(self._injected)
        for cli in clients:
            if cli is None or id(cli) in seen:
                continue
            seen.add(id(cli))
            try:
                await cli.aclose()
            except Exception:
                pass
        self._clients.clear()

    # ------------------------------------------------------------- request
    async def stream_response(self, dep: dict, payload: dict,
                              *, profile: str = "",
                              ctx_est=None,
                              client_ip: str = "",
                              session: str | None = None,
                              attribution: dict | None = None,
                              tool_repair_config: ToolRepairConfig | None = None,
                              truncation_config: TruncationConfig | None = None,
truncation_hook=None,
                               rate_hook=None,
                               maxtok_hook=None,
                               loop_config=None,
                               loop_stream_words=0,
                               ) -> AsyncIterator[bytes]:
        """Fa la richiesta con stream=True e yielda i chunk SSE grezzi.

        Solleva UpstreamError per stati ritriabili PRIMA del primo byte inviato
        (così il chiamante può fare fallback senza corrompere la risposta).
        """
        body = dict(payload)
        body["model"] = dep["model"]
        if strip_client_fields(body):
            log.info("[sanitize] %s: rimossi campi client non standard",
                     dep.get("unique", "?"))
        _cs = apply_content_string(body, dep)
        if _cs:
            metrics.inc("nx_content_string_total", ("applied",))
            log.info("[content-string] %s: %d messaggi appiattiti "
                     "PRIMA dell'invio (proattivo, stream)",
                     dep.get("unique", "?"), _cs)
        _tp = apply_thinking_replay(body, dep)
        if _tp:
            log.info("[thinking-replay] %s: %d turni assistant riparati "
                     "PRIMA dell'invio (proattivo, stream)",
                     dep.get("unique", "?"), _tp)
        apply_effort_policy(body, dep)
        _dg = downgrade_response_format(body, dep, _SCHEMAOUT_CFG)
        if _dg:
            log.info("[schemaout] %s: response_format %s rimosso -> "
                     "istruzione schema nel prompt (%s)",
                     dep.get("unique", "?"), _dg["kind"], _dg["where"])
        _google = is_gemini_deployment(dep)
        if _google:
            log.info("[thought_sig] Google provider, injecting for request")
            _inject_thought_signatures(body)
        clamp_max_tokens(body, dep, hook=maxtok_hook)
        _style = proto.style_of(dep)
        if _style != proto.CHAT:
            _up = proto.translate_request(_style, body, dep)
        else:
            _up = body
        # senza include_usage i provider non mandano mai il chunk usage ->
        # il summary per-richiesta resta usage:null. Iniettato
        # SOLO sui provider che lo supportano sicuramente (gli altri restano
        # intatti: uno stream_options rifiutato costerebbe un 400).
        if payload.get("stream") and any(
                h in dep.get("api_base", "") for h in
                ("api.groq.com", "openrouter.ai")):
            so = dict(body.get("stream_options") or {})
            so.setdefault("include_usage", True)
            body["stream_options"] = so
            if _style == proto.CHAT:
                _up["stream_options"] = so
        headers = proto.apply_auth(dep, {
            "Authorization": f"Bearer {dep['api_key']}",
            "Content-Type": "application/json",
            **_session_headers(dep, profile=profile, client_ip=client_ip,
                               session=session, attribution=attribution),
        })
        url = proto.build_url(dep, stream=True)
        log.debug("[upstream] %s POST %s (stream=%s, google=%s, style=%s)", dep.get("unique", "?"), url, payload.get("stream", False), _google, _style)
        try:
            _cli = self._client_for(url, dep.get("api_key", ""))
            req = _cli.build_request("POST", url, json=_up, headers=headers,
                                     **_timeout_kw(dep, ctx_est))
            resp = await _cli.send(req, stream=True)
        except httpx.TimeoutException as exc:
            # Headers mai arrivati entro il read-timeout: l'upstream ha
            # APPESO -> danno reale, marker 'timeout' (cooldown lungo).
            raise UpstreamError(None, f"upstream timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(None, f"upstream connection error: {exc}") from exc

        if resp.status_code in RETRYABLE_STATUS:
            raw = (await _safe_aread(resp)).decode(errors="replace")[:500]
            try:
                await resp.aclose()
            except Exception:
                pass
            rl = _rate_limits_from(resp)
            _emit_rate_hint(dep.get("unique"), rl, rate_hook)
            raise UpstreamError(resp.status_code, raw or "upstream %s (body "
                                "non leggibile)" % resp.status_code,
                                _retry_after_from(resp, raw,
                                                  dep.get("provider", ""))
                                if resp.status_code == 429 else None,
                                rate_limits=rl)

        if resp.status_code >= 400:
            # errore non ritriabile: lo restituiamo al client così com'è
            raw = await _safe_aread(resp)
            try:
                await resp.aclose()
            except Exception:
                pass
            rl = _rate_limits_from(resp)
            _emit_rate_hint(dep.get("unique"), rl, rate_hook)
            raise UpstreamError(-resp.status_code,
                                raw.decode(errors="replace") or
                                "upstream %s (body non leggibile)"
                                % resp.status_code,
                                _retry_after_of(resp),
                                rate_limits=rl)

        _emit_rate_hint(dep.get("unique"), _rate_limits_from(resp), rate_hook)

        # upstream che IGNORA stream:true e risponde JSON intero: invece di
        # scartare un deployment FUNZIONANTE, ADATTIAMO la risposta a SSE
        # (role -> content -> finish -> [DONE]). Solo se il body e' una chat
        # completion con contenuto reale; altrimenti si ruota (503).
        if payload.get("stream"):
            ctype = (resp.headers.get("content-type") or "").lower()
            if ctype and "text/event-stream" not in ctype:
                raw = await _safe_aread(resp)
                try:
                    await resp.aclose()
                except Exception:
                    pass
                _note_nonstream(dep)
                adapted = _json_to_sse(raw)
                if _style != proto.CHAT:
                    try:
                        _o = json.loads(raw)
                        adapted = (None if (isinstance(_o, dict) and _o.get(
                            "error") and not _o.get("candidates")
                            and not _o.get("output"))
                            else proto.chat_obj_to_sse(
                                proto.translate_response(_style, _o, dep)))
                    except Exception:
                        adapted = None
                if adapted is None:
                    raise UpstreamError(
                        503, "upstream ignored stream:true, body non "
                             "utilizzabile (content-type=%s) %r"
                             % (ctype, raw[:200]))

                async def _adapted_gen() -> AsyncIterator[bytes]:
                    for b in adapted:
                        yield b
                metrics.inc("nx_json_sse_total",
                            (str(dep.get("provider") or "?"),))
                log.info("[stream] %s ha ignorato stream:true -> risposta "
                         "JSON adattata a SSE", dep["unique"])
                if _google:
                    try:
                        _capture_sigs_from_obj(json.loads(raw))
                    except Exception:
                        pass
                return _adapted_gen()

        async def gen() -> AsyncIterator[bytes]:
            buf = b""
            source = resp.aiter_bytes()
            _stall = _stall_sec_for(dep.get("unique"), ctx_est)
            if _stall > 0:
                source = _stall_guard(source, _stall, dep.get("model", ""))
            try:
                async for chunk in source:
                    if _google:
                        buf += chunk
                        # processa solo righe complete
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            _capture_sigs_from_sse(line)
                    yield chunk
            finally:
                await resp.aclose()

        raw_gen = gen()

        # ---- TRADUZIONE PROTOCOLLO nativo -> SSE OpenAI ----
        # Cosi' i filtri a valle (loop guard, tool repair, truncation) e il
        # client vedono sempre Chat Completions.
        if _style != proto.CHAT:
            raw_gen = proto.stream_translator(_style, raw_gen, dep)

        # ---- STREAMING LOOP DETECTOR (kill precoce) ----
        # Buffer circolare delle ultime parole di contenuto, check n-gram a
        # ogni chunk: un loop degenere viene killato in 2-5s invece di
        # streammare all'infinito (lo stall watchdog non scatta se il modello
        # produce output costante). Il generator upstream viene chiuso dal
        # teardown del for -> resp.aclose().
        if loop_config is not None \
                and getattr(loop_config, "enabled", False) \
                and loop_stream_words > 0:
            raw_gen = _stream_loop_guard(
                raw_gen, loop_config, int(loop_stream_words),
                dep.get("unique", "?"), dep.get("model", ""))

        # ---- TOOL REPAIR streaming ----
        _tr_cfg = tool_repair_config or ToolRepairConfig()
        _tr_filter = ToolRepairSSEFilter(_tr_cfg, dep)
        if _tr_filter.level != "off" and payload.get("tools"):
            async def _repaired_gen() -> AsyncIterator[bytes]:
                try:
                    async for chunk in raw_gen:
                        for repaired in _tr_filter.feed(chunk):
                            yield repaired
                except (GeneratorExit, asyncio.CancelledError):
                    raise
                except BaseException:
                    # F23: stream morto a metà (stall/timeout/upstream rotto)
                    # con un tool-call in buffer: NON lasciare al client un
                    # JSON di argomenti troncato. Si emette la chiusura
                    # sintattica (JSON valido + finish_reason) e poi si
                    # ri-solleva, così il chiamante applica il cooldown.
                    for fixed in _tr_filter.abort_finalize():
                        yield fixed
                    raise
                for repaired in _tr_filter.finalize():
                    yield repaired
            base_gen: AsyncIterator[bytes] = _repaired_gen()
        else:
            base_gen = raw_gen

        # ---- TRUNCATED TOOL-CALL guard streaming ----
        # Trattiene la coda da un tag aperto; a fine stream salva la tool-call
        # o scarta il tag rotto (mai verso il client) + hook di declassamento.
        _tct_cfg = truncation_config or TruncationConfig()
        if _tct_cfg.enabled and _tct_cfg.holdback and payload.get("tools"):
            _tct_filter = TruncatedToolcallSSEFilter(
                dep.get("model", ""), payload.get("tools"), _tct_cfg,
                on_truncation=truncation_hook)

            async def _tct_gen() -> AsyncIterator[bytes]:
                try:
                    async for chunk in base_gen:
                        for guarded in _tct_filter.feed(chunk):
                            yield guarded
                except (GeneratorExit, asyncio.CancelledError):
                    raise
                except BaseException:
                    # F23: anche il tag testuale resta aperto a metà stream:
                    # salva/scarta la tool-call e chiudi pulito prima di
                    # propagare l'errore.
                    for guarded in _tct_filter.finalize():
                        yield guarded
                    raise
                for guarded in _tct_filter.finalize():
                    yield guarded
            return _tct_gen()
        return base_gen

    async def call(self, dep: dict, payload: dict, *,
                   profile: str = "",
                   ctx_est=None,
                   client_ip: str = "",
                    session: str | None = None,
                    attribution: dict | None = None,
                    rate_hook=None,
                    maxtok_hook=None) -> dict:
        """Richiesta NON streaming: risposta JSON completa."""
        body = dict(payload)
        body["model"] = dep["model"]
        if strip_client_fields(body):
            log.info("[sanitize] %s: rimossi campi client non standard",
                     dep.get("unique", "?"))
        # stream_options vale solo in streaming: alcuni provider (opencode
        # zen / Console Go) rifiutano con 400 "stream_options should be set
        # along with stream = true" se arriva su una richiesta non-stream.
        if not body.get("stream"):
            body.pop("stream_options", None)
        _cs = apply_content_string(body, dep)
        if _cs:
            metrics.inc("nx_content_string_total", ("applied",))
            log.info("[content-string] %s: %d messaggi appiattiti "
                     "PRIMA dell'invio (proattivo)", dep.get("unique", "?"),
                     _cs)
        _tp = apply_thinking_replay(body, dep)
        if _tp:
            log.info("[thinking-replay] %s: %d turni assistant riparati "
                     "PRIMA dell'invio (proattivo)", dep.get("unique", "?"),
                     _tp)
        apply_effort_policy(body, dep)
        _dg = downgrade_response_format(body, dep, _SCHEMAOUT_CFG)
        if _dg:
            log.info("[schemaout] %s: response_format %s rimosso -> "
                     "istruzione schema nel prompt (%s)",
                     dep.get("unique", "?"), _dg["kind"], _dg["where"])
        _google = is_gemini_deployment(dep)
        if _google:
            log.info("[thought_sig] Google provider, injecting for request")
            _inject_thought_signatures(body)
        clamp_max_tokens(body, dep, hook=maxtok_hook)
        _style = proto.style_of(dep)
        _up = (body if _style == proto.CHAT
               else proto.translate_request(_style, body, dep))
        headers = proto.apply_auth(dep, {
            "Authorization": f"Bearer {dep['api_key']}",
            "Content-Type": "application/json",
            **_session_headers(dep, profile=profile, client_ip=client_ip,
                               session=session, attribution=attribution),
        })
        url = proto.build_url(dep, stream=False)
        log.debug("[upstream] %s POST %s (stream=%s, google=%s, style=%s)", dep.get("unique", "?"), url, payload.get("stream", False), _google, _style)
        try:
            resp = await self._client_for(url, dep.get("api_key", "")).post(url, json=_up,
                                                    headers=headers,
                                                    **_timeout_kw(dep, ctx_est))
        except httpx.TimeoutException as exc:
            # L'upstream ha APPESO (read/connect timeout): danno reale (tempo
            # perso) -> marker distinto, il fallback lo classifica "timeout"
            # e applica il cooldown lungo.
            raise UpstreamError(None, f"upstream timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(None, f"upstream connection error: {exc}") from exc

        if resp.status_code >= 400:
            rl = _rate_limits_from(resp)
            _emit_rate_hint(dep.get("unique"), rl, rate_hook)
            raise UpstreamError(
                -resp.status_code if resp.status_code not in RETRYABLE_STATUS
                else resp.status_code,
                resp.text[:500],
                _retry_after_from(resp, resp.text, dep.get("provider", ""))
                if resp.status_code == 429 else None,
                rate_limits=rl)
        _emit_rate_hint(dep.get("unique"), _rate_limits_from(resp), rate_hook)
        try:
            data = resp.json()
        except ValueError as exc:
            raise UpstreamError(None, f"upstream non-JSON response: {exc}") from exc
        if _google:
            _capture_sigs_from_obj(data)
            log.debug("[thought_sig-capture] non-streaming capture, sigs_stored=%d", len(THOUGHT_SIGS))
        if _style != proto.CHAT:
            data = proto.translate_response(_style, data, dep)
        return data

    async def call_images(self, dep: dict, payload: dict, *,
                          profile: str = "",
                          client_ip: str = "",
                          session: str | None = None,
                          attribution: dict | None = None) -> dict:
        """Generazione immagini: POST {api_base}/images/generations (non streaming).

        Il body viene passato quasi intatto (solo il modello è riscritto col
        nome upstream del deployment). Errori 4xx/5xx -> UpstreamError come
        per il chat: 404/provider-4xx sono ritriabili lungo la catena.
        """
        body = {k: v for k, v in payload.items() if k != "model"}
        body["model"] = dep["model"]
        headers = {
            "Authorization": f"Bearer {dep['api_key']}",
            "Content-Type": "application/json",
            **_session_headers(dep, profile=profile, client_ip=client_ip,
                               session=session, attribution=attribution),
        }
        url = f"{dep['api_base']}/images/generations"
        log.debug("[upstream] %s POST %s (images, stream=%s)",
                  dep.get("unique", "?"), url, payload.get("stream", False))
        try:
            resp = await self._client_for(url, dep.get("api_key", "")).post(url, json=body, headers=headers,
                                          timeout=httpx.Timeout(connect=10.0,
                                                                read=300.0,
                                                                write=60.0,
                                                                pool=10.0))
        except httpx.HTTPError as exc:
            raise UpstreamError(None, f"upstream connection error: {exc}") from exc
        if resp.status_code >= 400:
            raise UpstreamError(
                -resp.status_code if resp.status_code not in RETRYABLE_STATUS
                else resp.status_code,
                resp.text[:500],
                _retry_after_from(resp, resp.text, dep.get("provider", ""))
                if resp.status_code == 429 else None)
        try:
            return resp.json()
        except ValueError as exc:
            raise UpstreamError(None, f"upstream non-JSON response: {exc}") from exc

    async def call_speech(self, dep: dict, payload: dict, *,
                          profile: str = "",
                          client_ip: str = "",
                          session: str | None = None,
                          attribution: dict | None = None) -> tuple[bytes, str]:
        """TTS: POST {api_base}/audio/speech -> bytes audio (buffered).

        Body OpenAI {model, input, voice, response_format?, speed?} col model
        riscritto. Ritorna (content, content_type) dalla risposta upstream.
        Errori come call_images: retryable vs -status client-side.
        """
        body = {k: v for k, v in payload.items() if k != "model"}
        body["model"] = dep["model"]
        headers = {"Authorization": f"Bearer {dep['api_key']}",
                   **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session, attribution=attribution)}
        url = f"{dep['api_base']}/audio/speech"
        try:
            resp = await self._client_for(url, dep.get("api_key", "")).post(url, json=body, headers=headers,
                                          timeout=httpx.Timeout(connect=10.0,
                                                                read=300.0,
                                                                write=60.0,
                                                                pool=10.0))
        except httpx.HTTPError as exc:
            raise UpstreamError(None, f"upstream connection error: {exc}") from exc
        if resp.status_code >= 400:
            raise UpstreamError(
                -resp.status_code if resp.status_code not in RETRYABLE_STATUS
                else resp.status_code,
                resp.text[:500],
                _retry_after_from(resp, resp.text, dep.get("provider", ""))
                if resp.status_code == 429 else None)
        content = resp.content
        if not content:
            raise UpstreamError(None, "upstream audio/speech risposta vuota")
        return content, (resp.headers.get("content-type") or "audio/mpeg")

    async def transcribe(self, dep: dict, data_fields: dict,
                         file_bytes: bytes, filename: str, content_type: str,
                         path: str = "transcriptions",
                         *, profile: str = "",
                         client_ip: str = "",
                         session: str | None = None,
                         attribution: dict | None = None) -> dict | str:
        """STT: POST multipart {api_base}/audio/{path} (transcriptions|translations).

        I campi form passano quasi intatti (model riscritto); il file va come
        multipart. Risposta JSON ({text...}) oppure testo per formati srt/vtt/text.
        """
        data = {k: v for k, v in data_fields.items() if k != "model"}
        data["model"] = dep["model"]
        headers = {"Authorization": f"Bearer {dep['api_key']}",
                   **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session, attribution=attribution)}
        url = f"{dep['api_base']}/audio/{path}"
        files = {"file": (filename or "audio.wav", file_bytes,
                          content_type or "audio/wav")}
        try:
            resp = await self._client_for(url, dep.get("api_key", "")).post(url, data=data, files=files,
                                          headers=headers,
                                          timeout=httpx.Timeout(connect=10.0,
                                                                read=300.0,
                                                                write=300.0,
                                                                pool=10.0))
        except httpx.HTTPError as exc:
            raise UpstreamError(None, f"upstream connection error: {exc}") from exc
        if resp.status_code >= 400:
            raise UpstreamError(
                -resp.status_code if resp.status_code not in RETRYABLE_STATUS
                else resp.status_code,
                resp.text[:500],
                _retry_after_from(resp, resp.text, dep.get("provider", ""))
                if resp.status_code == 429 else None)
        ctype = (resp.headers.get("content-type") or "").lower()
        if "json" in ctype:
            try:
                return resp.json()
            except ValueError as exc:
                raise UpstreamError(None, f"upstream non-JSON response: {exc}") from exc
        return resp.text

    # ------------------------------------------------------------ video async
    async def submit_video(self, dep: dict, payload: dict, *,
                           profile: str = "",
                           client_ip: str = "",
                           session: str | None = None,
                           attribution: dict | None = None) -> dict:
        """Submit job video (API asincrona OR-style): POST {base}/videos.
        Ritorna l'envelope {id, status, polling_url,...}. Errori come call."""
        body = {k: v for k, v in payload.items() if k != "model"}
        body["model"] = dep["model"]
        headers = {"Authorization": f"Bearer {dep['api_key']}",
                   **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session, attribution=attribution)}
        url = f"{dep['api_base']}/videos"
        try:
            resp = await self._client_for(url, dep.get("api_key", "")).post(url, json=body, headers=headers,
                                          timeout=httpx.Timeout(connect=10.0,
                                                                read=120.0,
                                                                write=120.0,
                                                                pool=10.0))
        except httpx.HTTPError as exc:
            raise UpstreamError(None, f"upstream connection error: {exc}") from exc
        if resp.status_code >= 400:
            raise UpstreamError(
                -resp.status_code if resp.status_code not in RETRYABLE_STATUS
                else resp.status_code,
                resp.text[:500])
        try:
            return resp.json()
        except ValueError as exc:
            raise UpstreamError(None, f"upstream non-JSON response: {exc}") from exc

    async def poll_video(self, dep: dict, job_id: str, *,
                         profile: str = "",
                         client_ip: str = "",
                         session: str | None = None,
                         attribution: dict | None = None) -> dict:
        """Stato del job: GET {base}/videos/{job_id} -> JSON di stato."""
        headers = {"Authorization": f"Bearer {dep['api_key']}",
                   **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session, attribution=attribution)}
        url = f"{dep['api_base']}/videos/{job_id}"
        try:
            resp = await self._client_for(url, dep.get("api_key", "")).get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(None, f"upstream connection error: {exc}") from exc
        if resp.status_code >= 400:
            raise UpstreamError(-resp.status_code, resp.text[:300])
        try:
            return resp.json()
        except ValueError as exc:
            raise UpstreamError(None, f"upstream non-JSON response: {exc}") from exc

    async def poll_video_any(self, deps: list[dict], job_id: str, *,
                             profile: str = "",
                             client_ip: str = "",
                             session: str | None = None,
                             attribution: dict | None = None) -> dict:
        """Poll STATELESS: prova ogni candidato (chiavi diverse) finché uno
        riconosce il job. I job vivono sull'account di chi ha submitto, quindi
        le chiavi di altri account risponderanno 404 -> si prosegue."""
        last: UpstreamError | None = None
        for dep in deps:
            try:
                return await self.poll_video(dep, job_id,
                                             profile=profile,
                                             client_ip=client_ip,
                                             session=session,
                                             attribution=attribution)
            except UpstreamError as err:
                if err.status is not None and err.status == -404:
                    last = err
                    continue
                raise
        raise last or UpstreamError(-404, f"job '{job_id}' non trovato su "
                                          "nessun account video_gen")

    async def download_video_any(self, deps: list[dict], job_id: str, *,
                                 profile: str = "",
                                 client_ip: str = "",
                                 session: str | None = None,
                                 attribution: dict | None = None) -> tuple[bytes, str]:
        """Download stateless: stessa logica multi-chiave di poll_video_any."""
        last: UpstreamError | None = None
        for dep in deps:
            try:
                return await self.download_video(dep, job_id,
                                                 profile=profile,
                                                 client_ip=client_ip,
                                                 session=session,
                                                 attribution=attribution)
            except UpstreamError as err:
                if err.status is not None and err.status == -404:
                    last = err
                    continue
                raise
        raise last or UpstreamError(-404, f"contenuto '{job_id}' non trovato "
                                          "su nessun account video_gen")

    async def download_video(self, dep: dict, job_id: str, *,
                             profile: str = "",
                             client_ip: str = "",
                             session: str | None = None,
                             attribution: dict | None = None) -> tuple[bytes, str]:
        """Contenuto MP4 completato: GET {base}/videos/{job_id}/content."""
        headers = {"Authorization": f"Bearer {dep['api_key']}",
                   **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session, attribution=attribution)}
        url = f"{dep['api_base']}/videos/{job_id}/content"
        try:
            resp = await self._client_for(url, dep.get("api_key", "")).get(url, headers=headers,
                                         follow_redirects=True,
                                         timeout=httpx.Timeout(600.0,
                                                               connect=15.0))
        except httpx.HTTPError as exc:
            raise UpstreamError(None, f"upstream connection error: {exc}") from exc
        if resp.status_code >= 400:
            raise UpstreamError(-resp.status_code, resp.text[:300])
        return resp.content, (resp.headers.get("content-type") or "video/mp4")

    # ------------------------------------------------------- fallback loop
    async def call_with_fallback(self, router, profile: str | None,
                                 first_dep: dict, payload: dict, *,
                                 collect_qc_failures: bool = False,
                                 media_strike_hook=None,
                                 need: frozenset | None = None,
                                 scope: str = "chain",
                                 ctx: int | None = None,
                                 attempts_box: list | None = None,
                                 session: str | None = None,
                                 ses: str | None = None,
                                 client_ip: str = "",
                                 attribution: dict | None = None,
                                 requested_group: str | None = None,
                                 orig_messages: list | None = None
                                 ) -> tuple[dict, dict] | tuple[dict, dict, list]:
        """Prova i deployment lungo la catena finché uno risponde.

        Ritorna (risposta_json, deployment_usato); con collect_qc_failures=True
        ritorna (risposta, deployment, qc_failed) dove qc_failed è la lista
        (unique, motivo) dei deployment scartati dal QC del contenuto.

        QC: se il contenuto è JSON rotto e ci sono ancora tentativi
        disponibili (< max_attempts), marca il deployment e
        prosegue la catena; esauriti, consegna l'ULTIMO tentativo ("meno peggio",
        D3) insieme alla lista fallimenti per l'annotazione nel reasoning.

        `media_strike_hook(model, detail)` viene invocata su 400 provider-side
        con firma rifiuto-modalità: main la usa per l'auto-learn delle capacità.

        FALLBACK DI SCOPO (need/scope): con need non-vuoto la catena salta i
        deployment che non dichiarano le capacità richieste — un fallback su
        richiesta vision/video/audio NON cade mai su un modello text-only.
        scope="group" (richieste esplicite) ruota SOLO nello stesso gruppo.
        """
        qc = router.policy.qc_json          # snapshot per questa chiamata
        san = router.policy.qc_sanity       # sanity generica (vuoto/trivial)
        tr_cfg = create_tool_repair_config({
            "tool_repair": {
                "enabled": router.policy.tool_repair_enabled,
                "default_level": router.policy.tool_repair_default_level,
                "disable_for_google": router.policy.tool_repair_disable_for_google,
                "max_args_size": router.policy.tool_repair_max_args_size,
                "annotate_reasoning": router.policy.tool_repair_annotate_reasoning,
            },
        })
        _fc = fake_config_from_policy(router.policy)
        fake_escalations = 0
        _hn = hist_config_from_policy(router.policy)
        _sm = sampling_config_from_policy(router.policy)
        _so = schemaout_config_from_policy(router.policy)
        _tt = text_config_from_policy(router.policy)
        _corrected: set[str] = set()
        _rsn_steps: dict[str, set] = {}      # rimedi reasoning per dep
        _cstr_steps: dict[str, set] = {}     # rimedi content-string per dep
        trail: list = []                     # ATTEMPT TRAIL (P0) per il 503
        skip_hosts: set[str] = set()         # P1-5: host saltati (provider KO)
        _rsn_restored = False                # history originale gia' riprovata
        dep = first_dep
        requested_group = requested_group or (first_dep or {}).get("group")
        last_err: UpstreamError | None = None
        tried: set[str] = set()
        qc_failed: list[tuple[str, str]] = []
        last_broken: tuple[dict, dict] | None = None
        _max_tries = int(getattr(router.policy, "max_fallback_tries",
                                 os.environ.get("GATEWAY_MAX_FALLBACK_TRIES", "128"))
                         or 128)
        _deadline_ms = int(getattr(router.policy.qc_json,
                                   "stream_total_deadline_ms", 180000) or 0)
        _t0 = time.monotonic()
        _kind_default: str | None = None
        # ---- WARM-REFILL a cascata (non-streaming): finche' la sessione ha
        # meno di warm_ready_min caldi "deliverable" (need + ctx + output
        # assicurato), ogni tentativo corsia UN canary nuovo in parallelo
        # (2 alla volta, free-only, api_key diversa, libero da ogni sessione):
        # consegna il piu' veloce, l'altro finisce come probe reale -> warm.
        _pol = router.policy
        _refill_on = (bool(getattr(_pol, "warm_refill_enabled", True))
                      and bool(getattr(_pol, "warm_pool_enabled", True))
                      and bool(ses) and bool(profile))
        _ready_min = router.warm_ready_effective(ses, _pol)
        _maxif = max(0, int(getattr(_pol, "warm_refill_max_inflight",
                                     6) or 0))
        _raced: set[str] = set()
        _raced_keys: set[str] = set()
        _refill_rounds = 0
        _zen_hunt = False                # caccia canary zen-only (nativo)
        try:
            _outb = refill_out_budget(payload, _pol)
        except Exception:
            _outb = 4096

        def _pick(*a, **k):
            # Fallback + warm sticky handoff: se il prossimo deployment e' della
            # stessa famiglia dello sticky corrente, sposta lo sticky su di lui
            # (la sessione riparte warm invece che fredda).
            k.setdefault("out_tokens", _outb)
            _n = router.fallback_next(*a, **k)
            if _n is None:
                return None
            if dep_host(_n) not in skip_hosts:
                router.sticky_handoff(ses, _n)
                return _n
            # P1-5 skipPlatforms: l'host ha gia' fallito a livello provider in
            # QUESTA richiesta -> provo un altro host; se non ne restano, torno
            # al candidato saltato (mai lasciare la richiesta senza risposta).
            _saved = _n
            for _ in range(8):
                tried.add(_saved["unique"])
                cand = router.fallback_next(*a, **k)
                if cand is None:
                    router.sticky_handoff(ses, _saved)
                    return _saved
                if dep_host(cand) not in skip_hosts:
                    router.sticky_handoff(ses, cand)
                    return cand
                _saved = cand
            router.sticky_handoff(ses, _saved)
            return _saved
        # ---- L1 #1: normalizzazione STRUTTURALE della history (coda) ----
        if _hn.enabled:
            _new_msgs, _hn_rep = normalize_messages(
                (payload or {}).get("messages"), _hn,
                tail_floor=router.ctx_boundary_floor(ses))
            if _hn_rep.get("changed"):
                payload["messages"] = _new_msgs
                metrics.inc("nx_histnorm_total", ("changed",))
                log.info("[histnorm] coda normalizzata: orphan=%d "
                         "dangling=%d empty=%d dupsys=%d",
                         _hn_rep.get("shown_orphan_tool", 0),
                         _hn_rep.get("dangling_tool_calls", 0),
                         _hn_rep.get("empty_assistant", 0),
                         _hn_rep.get("dup_system", 0))
        # Gemini 3 tool replay: una history con tool_call prive di firma rende
        # Gemini inutilizzabile. L'esclusione avviene A MONTE nel router
        # (set_avoid_gemini in chat_completions -> _gemini_blocked in
        # pick_deployment/_walk_chain): qui non serve alcun salto/tentativo.
        while (dep is not None and len(tried) < _max_tries
               and (not _deadline_ms
                    or (time.monotonic() - _t0) * 1000 < _deadline_ms)):
            cur = dep["unique"]             # il deployment DEL TENTATIVO:
            log.debug("[chain] tentativo %d/%d: %s (group=%s)", len(tried), _max_tries, cur, dep.get("group", "?"))
            _was_dormant = router.is_cooled_down(cur)
            # Solo se il gruppo RICHIESTO esplicitamente e' un bucket di
            # escalation (-go/-fallback): niente refill/canary/gara lenta
            # (i bucket a pagamento non usano il caldo). Se ci si arriva via
            # FALLBACK dal dim, la speculativa resta attiva.
            _esc_grp = is_escalation_group(
                str(requested_group or ""),
                router.config.go_suffix, router.config.fallback_suffix)
            def _fail_cur(seconds=None, reason=None, status=None, kind=None,
                          provenance=None):
                _k = kind if kind is not None else _kind_default
                if _was_dormant and _k != ErrorKind.PERMANENT_DEAD:
                    _r = router.mark_failed_double_residual(
                        cur, reason=reason, status=status)
                else:
                    _r = router.mark_failed(
                        cur, seconds=seconds, reason=reason, status=status,
                        kind=_k, provenance=provenance)
                # P1-4: KO ripetuti dello stesso MODELLO -> bench cross-chiave.
                with contextlib.suppress(Exception):
                    router.note_model_failure(dep)
                return _r
            tried.add(cur)                  # note_end deve riferirsi a QUESTO,
            if attempts_box is not None:
                attempts_box.append(cur)    # osservabilità summary per-richiesta
            # l'identità DEVE riflettere il deployment CHE PROVA ORA: dopo un
            # fallback il system message nominerebbe il modello sbagliato.
            inject_identity(payload, dep)
            _tp = apply_thinking_replay(payload, dep, orig_messages)
            if _tp:
                log.info("[thinking-replay] %s: %d campi reasoning rimessi "
                         "PRIMA dell'invio (proattivo)", cur, _tp)
            router.note_start(cur, ctx)     # rotazione adattiva (peso token)
            t0 = time.monotonic()
            _lease = router.key_lease_acquire(dep)   # P2-8 (opt-in)
            try:
                # ---- L1 #2A: default di sampling (client vince) ----
                if _sm.enabled:
                    _applied = apply_sampling_defaults(payload, dep, _sm)
                    if _applied:
                        log.debug("[sampling] %s: default %s",
                                  cur, _applied)
                    if (_so.enabled and maybe_inject_response_format(
                            payload, dep, _so)):
                        metrics.inc("nx_resp_format_injected_total",
                                    (cur,))
                _fB = None
                _B = None
                _wake_b = False
                _tB = t0
                try:
                    _fly = router.probes_in_flight(ses)
                except Exception:
                    _fly = 0
                # DEGRADED (P1-6): in blackout upstream niente speculativo
                # (cascata/canary/sveglia): brucia rate-limit senza costrutto.
                try:
                    _degraded = router.degraded_active()
                except Exception:
                    _degraded = False
                def _open_canary(label: str, zen_only: bool = False):
                    """Apre UN canario (o sveglia un cooldown 429 maturo) e lo
                    mette in volo accanto ad A. Ritorna (dep, fut, t0, wake)
                    oppure None. Usata dal gate refill e dalla GARA LENTA."""
                    _age = 3600.0
                    try:
                        _age = float(getattr(
                            _pol, "warm_refill_wake_min_cooldown_age_sec",
                            3600.0) or 3600.0)
                    except Exception:
                        _age = 3600.0
                    _b = None
                    _wake = False
                    try:
                        _b = router.warm_fill_canary(
                            profile, dep, need, ctx, _outb,
                            tried=tried | _raced,
                            requested_group=requested_group,
                            exclude_keys=_raced_keys, exclude_uniq=_raced,
                            only_zen=zen_only)
                    except Exception:
                        _b = None
                    if _b is None:
                        try:
                            _b = router.warm_wake_canary(
                                profile, dep, need, ctx, _outb,
                                tried=tried | _raced,
                                requested_group=requested_group,
                                exclude_keys=_raced_keys, exclude_uniq=_raced,
                                only_zen=zen_only,
                                min_age_sec=_age)
                        except Exception:
                            _b = None
                        if _b is not None:
                            _wake = True
                            log.info("[refill] ns: sveglia %s (429 maturo)",
                                     _b["unique"])
                    if _b is None:
                        return None
                    log.info("[%s] ns: canario %s (order=%s, chiavi "
                             "warm+in-volo escluse=%d)", label, _b["unique"],
                             _b.get("order"), len(_raced_keys))
                    _raced.add(_b["unique"])
                    _raced_keys.add(str(_b.get("api_key") or ""))
                    _pb = dict(payload)
                    inject_identity(_pb, _b)
                    router.note_start(_b["unique"], ctx)
                    _tb = time.monotonic()
                    _fb = asyncio.ensure_future(self.call(
                        _b, _pb, profile=profile or "", ctx_est=ctx,
                        client_ip=client_ip, session=session,
                        attribution=attribution,
                        rate_hook=lambda u2, rl:
                        router.note_rate_limit(u2, rl)))
                    with contextlib.suppress(Exception):
                        router.note_probe_started(ses, _b["unique"])
                    return _b, _fb, _tb, _wake

                if (_refill_on and _ready_min and not _degraded
                        and not _esc_grp
                        and not _opencode_cautious_request()
                        and _refill_rounds < _maxif
                        and _fly < _maxif
                        and (not _deadline_ms
                             or (time.monotonic() - _t0) * 1000
                             < _deadline_ms)):
                    try:
                        _pool = router.warm_valid_for(
                            ses, profile,
                            requested_group or dep.get("group"),
                            need, ctx, _outb, tried=tried | _raced,
                            include_borrowed=True)
                        _nv = len(_pool)
                    except Exception:
                        _pool, _nv = [], _ready_min
                    # Nativo opencode SENZA zen nel warm: caccia un canary
                    # zen-only anche se il conteggio MISTO basta (basta 1 zen).
                    _zen_hunt = (router._zen_first_active()
                                 and not any(is_opencode_zen_dep(d)
                                             for d in _pool)
                                 and router.hunt_allowed(ses, ctx))
                    if (_nv < _ready_min) or _zen_hunt:
                        if _zen_hunt:
                            router.note_hunt(ses, ctx, gained=False)
                            log.info("[refill] ns %s: 0 zen nel warm per client "
                                     "nativo -> caccia canary zen-only", cur)
                        _refill_rounds += 1
                        _rpm = router.session_rpm(ses)
                        log.info("[refill] ns %s: warm validi %d/%d, in volo "
                                 "%d/%d (ctx=%s, out=%s, rpm=%.1f) -> "
                                 "2 alla volta",
                                 cur, _nv, _ready_min, _fly, _maxif, ctx,
                                 _outb, _rpm)
                        _raced.add(cur)
                        _raced_keys.add(str(dep.get("api_key") or ""))
                        # chiavi gia' rappresentate nel warm: non si rimette
                        # alla prova la STESSA api_key di un caldo
                        try:
                            _raced_keys |= router.warm_api_keys(
                                ses, profile,
                                requested_group or dep.get("group"))
                        except Exception:
                            pass
                        _op = _open_canary("refill", zen_only=_zen_hunt)
                        if _op is None:
                            log.info("[refill] ns %s: nessun canario free "
                                     "consegnabile (chiavi escluse=%d)",
                                     cur, len(_raced_keys))
                        else:
                            _B, _fB, _tB, _wake_b = _op
                futA = None
                # GARA LENTA (non-stream): soglia misurata dall'inizio del
                # TENTATIVO di A. Vale SEMPRE, anche quando un canario di
                # refill e' gia' in volo (il timer lento e' indipendente dal
                # tetto per-sessione e non applica penali al lento).
                _ns_slow = 0
                if not _degraded and not _esc_grp:
                    try:
                        _ns_slow = int(getattr(
                            _pol, "nonstream_slow_race_after_ms", 0) or 0)
                    except Exception:
                        _ns_slow = 0
                _slow_dl = ((t0 + _ns_slow / 1000.0) if _ns_slow > 0
                            else None)
                if _fB is None:
                    # Se il primo tentativo sta ancora generando oltre la
                    # soglia si apre UN canario e si tiene per buono il PRIMO
                    # che consegna; A resta in volo (e se ha generato in MENO
                    # tempo diventa holder).
                    data = None
                    _A = dep
                    _tA = t0
                    if _slow_dl is not None:
                        futA = asyncio.ensure_future(self.call(
                            _A, payload, profile=profile or "",
                            ctx_est=ctx, client_ip=client_ip,
                            session=session, attribution=attribution,
                            rate_hook=lambda u4, rl:
                            router.note_rate_limit(u4, rl)))
                        _d_s, _ = await asyncio.wait(
                            {futA}, timeout=max(
                                0.0, _slow_dl - time.monotonic()))
                        if futA in _d_s:
                            data = futA.result()
                            futA = None
                        else:
                            log.info("[slow-race] ns %s in generazione da "
                                     "%.0fs (> %.0fs) -> canario in gara",
                                     cur, time.monotonic() - _tA,
                                     _ns_slow / 1000.0)
                            # R3: A e' lento per la sessione, a prescindere
                            # dall'esito della gara.
                            with contextlib.suppress(Exception):
                                router.mark_session_slow(ses, cur)
                            # R2: gate — solo se la sessione ha pochi warm.
                            _op = None
                            try:
                                _allow = bool(router.slow_race_allowed(
                                    ses, profile, dep.get("group"), need,
                                    ctx, _outb, tried))
                            except Exception:       # noqa: BLE001
                                _allow = True
                            if _allow:
                                _raced.add(cur)
                                _raced_keys.add(
                                    str(dep.get("api_key") or ""))
                                _op = _open_canary("slow-race")
                            else:
                                metrics.inc("nx_slow_race_total",
                                            ("warm_full",))
                                log.info("[slow-race] ns %s: warm gia' pieno "
                                         "(>=%s), niente canario", cur,
                                         getattr(_pol, "slow_race_max_warm",
                                                 6))
                            if _op is None:
                                data = await futA
                                futA = None
                            else:
                                _B, _fB, _tB, _wake_b = _op
                    if _fB is None and data is None:
                        data = await self.call(dep, payload,
                                       profile=profile or "",
                                       ctx_est=ctx,
                                       client_ip=client_ip, session=session,
                                       attribution=attribution,
                                       rate_hook=lambda u, rl: router.note_rate_limit(
                                           u, rl))
                if _fB is not None:
                    # GARA (A + canario refill, eventualmente + canario
                    # LENTO): vince chi risponde PER PRIMO con successo; gli
                    # altri restano in volo come probe (mai cancellati) e se
                    # consegnano entrano in warm.
                    _A = dep
                    _tA = t0
                    if futA is None:
                        futA = asyncio.ensure_future(self.call(
                            _A, payload, profile=profile or "",
                            ctx_est=ctx, client_ip=client_ip,
                            session=session, attribution=attribution,
                            rate_hook=lambda u3, rl:
                            router.note_rate_limit(u3, rl)))
                    _parts: list[dict] = [
                        {"fut": futA, "dep": _A, "t": _tA,
                         "wake": False, "a": True}]
                    if _fB is not None:
                        _parts.append({"fut": _fB, "dep": _B, "t": _tB,
                                       "wake": _wake_b, "a": False})
                    _pending = {p["fut"] for p in _parts}
                    _slow_opened = _slow_dl is None
                    _errs: list[BaseException] = []
                    data = None
                    _win = None
                    while _pending:
                        _to = None
                        if not _slow_opened:
                            _to = max(0.0, _slow_dl - time.monotonic())
                        _cmp, _rest = await asyncio.wait(
                            _pending, timeout=_to,
                            return_when=asyncio.FIRST_COMPLETED)
                        _pending = set(_rest)
                        # NB: bisogna esaminare TUTTI i future completati in
                        # questo giro, non solo uno: scartare gli altri
                        # lascerebbe la loro eccezione non recuperata (e il
                        # probe del loser non partirebbe -> nessuna penale).
                        for _f in _cmp:
                            try:
                                _r = _f.result()
                            except BaseException as exc:
                                _errs.append(exc)
                                continue
                            data = _r
                            _win = _f
                            break
                        if data is not None:
                            break
                        if not _slow_opened and time.monotonic() >= _slow_dl:
                            _slow_opened = True
                            log.info("[slow-race] ns %s in generazione da "
                                     "%.0fs (> %.0fs) -> canario in gara",
                                     cur, time.monotonic() - _tA,
                                     _ns_slow / 1000.0)
                            # R3: A e' lento per la sessione, a prescindere
                            # dall'esito della gara.
                            with contextlib.suppress(Exception):
                                router.mark_session_slow(ses, cur)
                            # R2: gate — solo se la sessione ha pochi warm.
                            _op = None
                            try:
                                _allow = bool(router.slow_race_allowed(
                                    ses, profile, dep.get("group"), need,
                                    ctx, _outb, tried))
                            except Exception:       # noqa: BLE001
                                _allow = True
                            if _allow:
                                _raced.add(cur)
                                _raced_keys.add(
                                    str(dep.get("api_key") or ""))
                                _op = _open_canary("slow-race")
                            else:
                                metrics.inc("nx_slow_race_total",
                                            ("warm_full",))
                                log.info("[slow-race] ns %s: warm gia' pieno "
                                         "(>=%s), niente canario", cur,
                                         getattr(_pol, "slow_race_max_warm",
                                                 6))
                            if _op is not None:
                                _C, _fC, _tC, _wake_c = _op
                                _parts.append({"fut": _fC, "dep": _C,
                                               "t": _tC, "wake": _wake_c,
                                               "a": False})
                                _pending.add(_fC)
                                log.info("[hedge] slow-race: %s in gara con "
                                         "A (fuori dal tetto)", _C["unique"])
                    if data is None:
                        # Nessuno ha consegnato: A finisce nell'handler errori
                        # esistente (penali solite); gli altri ricevono la
                        # loro da probe (future gia' completati con
                        # l'eccezione).
                        for p in _parts:
                            if p["a"]:
                                continue
                            _spawn_ns_probe(router, p["dep"], p["fut"],
                                            p["t"], ctx, ses, wake=p["wake"])
                        raise (_errs[0] if _errs else UpstreamError(
                            -503, "gara non-stream: nessun consegnato"))
                    _wd = next(p for p in _parts if p["fut"] is _win)
                    _race = (_wd["dep"]["unique"], max(
                        0.0, (time.monotonic() - _wd["t"]) * 1000.0))
                    for p in _parts:
                        if p["fut"] is _win:
                            continue
                        _spawn_ns_probe(router, p["dep"], p["fut"], p["t"],
                                        ctx, ses, wake=p["wake"],
                                        race=_race)
                    if not _wd["a"]:
                        log.info("[refill] consegna %s (piu' veloce di %s, "
                                 "che finisce come probe senza penale)",
                                 _wd["dep"]["unique"], cur)
                        with contextlib.suppress(Exception):
                            router.note_probe_done(ses, _wd["dep"]["unique"])
                        dep = _wd["dep"]
                        cur = _wd["dep"]["unique"]
                        t0 = _wd["t"]
                        _was_dormant = False
                        if _wd["wake"]:
                            with contextlib.suppress(Exception):
                                router.clear_cooldown(cur)   # sveglia ok
                            log.info("[refill] ns: sveglia riuscita, %s "
                                     "torna caldo", cur)
                        if attempts_box is not None:
                            attempts_box.append(cur)
                if _was_dormant:
                    router.clear_cooldown(cur)
                    metrics.observe_latency_ms(cur, (time.monotonic() - t0) * 1000)
                    metrics.inc("nx_upstream_calls_total", (cur, "ok"))
    
                    # ---- TOOL REPAIR (prima del QC) ----
                tr_result = repair_tool_calls(data, payload, dep, tr_cfg)
                if tr_result["repaired"]:
                    metrics.inc("nx_tool_repair_total", (cur, "ok"))

                # ---- L2 #6: recupero tool-call resi come testo ----
                _text_parsed = False
                if _tt.enabled and payload.get("tools"):
                    try:
                        _tc_info = apply_to_message(
                            ((data.get("choices") or [{}])[0].get(
                                "message") or {}),
                            payload.get("tools"), _tt)
                    except Exception:
                        _tc_info = None
                    if _tc_info:
                        _text_parsed = True
                        metrics.inc("nx_text_toolcall_total",
                                    (cur, "parsed"))
                        repairlog.note("salvage_text", source="nostream",
                                       outcome="ok", dep=cur,
                                       model=dep.get("model", ""),
                                       detail="tool-call resi come testo",
                                       count=len(_tc_info))
                # ---- TOOLCALL TRUNCATION: tag aperto mai chiuso ----
                if (_tt.enabled and payload.get("tools") and not _text_parsed
                        and getattr(router.policy,
                                    "toolcall_truncation_enabled", True)):
                    _msg0 = ((data.get("choices") or [{}])[0].get(
                        "message") or {})
                    _ctxt = _msg0.get("content")
                    if isinstance(_ctxt, list):
                        _ctxt = "".join(
                            p.get("text", "") for p in _ctxt
                            if isinstance(p, dict)
                            and isinstance(p.get("text"), str))
                    if isinstance(_ctxt, str) and has_unclosed_toolcall(_ctxt):
                        _salv = None
                        try:
                            _salv = salvage_truncated_toolcall(
                                _ctxt, payload.get("tools"), _tt)
                        except Exception:
                            _salv = None
                        _cd = int(getattr(
                            router.policy,
                            "toolcall_truncation_cooldown_sec", 30) or 30)
                        if _salv:
                            _msg0["tool_calls"] = _salv
                            _msg0["content"] = ""
                            _text_parsed = True
                            metrics.inc("nx_truncated_toolcall_total",
                                        (cur, "salvaged"))
                            repairlog.note("salvage_truncated",
                                           source="nostream", outcome="ok",
                                           dep=cur,
                                           model=dep.get("model", ""),
                                           detail="tag tool-call rotto",
                                           count=len(_salv))
                        else:
                            metrics.inc("nx_truncated_toolcall_total",
                                        (cur, "rotate"))
                            repairlog.note("salvage_truncated",
                                           source="nostream", outcome="fail",
                                           dep=cur,
                                           model=dep.get("model", ""),
                                           detail="tag tool-call non salvabile")
                            log.warning("[truncation] %s: tag tool-call rotto "
                                        "non salvabile -> ruoto (cooldown "
                                        "%ds)", cur, _cd)
                            _fail_cur(seconds=_cd, reason="truncated_toolcall")
                            last_broken = (data, dep)
                            dep = _pick(
                                profile, dep, need, scope, ctx=ctx,
                                tried=tried, requested_group=requested_group)
                            continue
                # ---- L2 #5: output strutturato (A/B/D) ----
                _so_rep = enforce_response(data, payload, _so) \
                    if (_so.enabled and not _text_parsed) \
                    else {"status": "skip"}
                if _so_rep.get("status") in ("cleaned", "repaired"):
                    metrics.inc("nx_struct_out_total",
                                (cur, _so_rep.get("status")))
                    log.info("[struct-out] %s: %s", cur,
                             _so_rep.get("status"))
                    repairlog.note(
                        "struct_cleaned"
                        if _so_rep.get("status") == "cleaned"
                        else "struct_repaired",
                        source="nostream", outcome="ok", dep=cur,
                        model=dep.get("model", ""),
                        detail=",".join(_so_rep.get("moves") or [])
                        or _so_rep.get("status"))
                # ---- L1 #2B: loop detector ----
                _loop_reason = None
                if _sm.loop.enabled and not _text_parsed:
                    _loop_reason = response_loop_reason(data, _sm)

                # ---- QC del contenuto (solo percorso non-streaming) ----
                if collect_qc_failures and (qc.enabled or san.enabled):
                    reason = check_response(data, payload, qc) \
                        if qc.enabled else None
                    if not reason:
                        from .qc import check_sanity
                        reason = check_sanity(data, payload, san)
                        if reason:
                            # contenuto vuoto ma il modello HA ragionato o ha
                            # esaurito max_tokens (finish_reason=length): NON e'
                            # rotto, ruotare la catena non cambia nulla (tutto
                            # il gruppo si comporterebbe uguale) e raffredda
                            # chiavi sane. NON consegnare vuoto (blocca gli
                            # agenti): risposta "notice" subito, un solo tentativo.
                            _ch0 = (data.get("choices") or [{}])[0]
                            fr = _ch0.get("finish_reason")
                            _rc = (_ch0.get("message") or {}).get(
                                "reasoning_content") or (
                                _ch0.get("message") or {}).get("reasoning")
                            no_rotate = ((fr == "length"
                                          or (isinstance(_rc, str) and _rc.strip()))
                                         and not getattr(
                                             san, "rotate_on_length_empty", False))
                            if no_rotate:
                                # il modello ha esaurito il budget / ha solo
                                # ragionato: ruotare non aiuta -> errore
                                # RETRYABLE al client (mai un turno finto).
                                raise UpstreamError(
                                    503, "empty output (fr=%s) da %s"
                                         % (fr, cur), final=True)
                    if reason:
                        if (getattr(router.policy,
                                    "corrective_retry_enabled", True)
                                and cur not in _corrected
                                and not reason.lower().startswith(
                                    "timeout")):
                            _corrected.add(cur)
                            payload.setdefault("messages", []).append(
                                {"role": "system",
                                 "content": _corrective_note("json")})
                            metrics.inc("nx_corrective_retry_total",
                                        (cur, "json"))
                            log.warning("[retry] %s contenuto non "
                                        "valido (%s): retry correttivo",
                                        cur, reason)
                            repairlog.note("struct_corrective",
                                           source="nostream", outcome="ok",
                                           dep=cur,
                                           model=dep.get("model", ""),
                                           detail="json")
                            continue
                        qc_failed.append((cur, reason))
                        metrics.inc("nx_qc_discarded_total",
                                    (cur, reason.split(" ")[0]))
                        last_broken = (data, dep)
                        if len(qc_failed) <= qc.max_attempts:
                            log.warning("[qc] %s JSON non valido (%s): "
                                        "provo il successivo", cur, reason)
                            _fail_cur()
                            dep = _pick(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                            continue        # finally chiude il TENTATIVO
                        # tentativi esauriti: consegna l'ultimo se ha contenuto,
                        # altrimenti errore RETRYABLE (mai un turno vuoto/finto).
                        if _looks_empty(data):
                            raise UpstreamError(
                                503, "catena esaurita, nessun output utile",
                                 final=True)
                        router.note_result(cur, (time.monotonic() - t0) * 1000,
                                           quality=0.5, ctx_est=ctx)
                        return data, dep, qc_failed
                # ---- L2 #5 non conforme: retry correttivo/rotazione ----
                if _so_rep.get("status") == "invalid" \
                        and not _text_parsed:
                    _r5 = _so_rep.get("reason") or "schema"
                    if (getattr(router.policy,
                                "corrective_retry_enabled", True)
                            and cur not in _corrected):
                        _corrected.add(cur)
                        payload.setdefault("messages", []).append(
                            {"role": "system",
                             "content": _corrective_note("schema")})
                        metrics.inc("nx_corrective_retry_total",
                                    (cur, "schema"))
                        log.warning("[retry] %s contenuto non "
                                    "conforme (%s): retry correttivo",
                                    cur, _r5)
                        repairlog.note("struct_corrective",
                                       source="nostream", outcome="ok",
                                       dep=cur, model=dep.get("model", ""),
                                       detail="schema")
                        continue
                    metrics.inc("nx_struct_out_total", (cur, "invalid"))
                    log.warning("[struct-out] %s non conforme (%s): "
                                "ruoto", cur, _r5)
                    repairlog.note("struct_invalid", source="nostream",
                                   outcome="fail", dep=cur,
                                   model=dep.get("model", ""), detail=_r5)
                    _fail_cur()
                    last_broken = (data, dep)
                    qc_failed.append((cur, "schema"))
                    dep = _pick(profile, dep, need, scope,
                                               ctx=ctx, tried=tried,
                                               requested_group=
                                               requested_group)
                    continue
                # ---- L1 #2B loop rilevato: dim successiva ----
                if _loop_reason:
                    metrics.inc("nx_loop_detected_total",
                                (cur, _loop_reason))
                    log.warning("[loop] %s: %s -> dim successiva",
                                cur, _loop_reason)
                    _fail_cur(reason="loop")
                    last_broken = (data, dep)
                    nxt = _pick(profile, dep, need, scope,
                                               ctx=ctx, tried=tried,
                                               requested_group=
                                               requested_group)
                    if nxt is not None and len(tried) < _max_tries:
                        dep = nxt
                        continue
                    raise UpstreamError(
                        503, "loop rilevato, catena esaurita",
                        final=True)
                # ---- FAKE TOOL-CALL: tool-call reso come testo ----
                if _fc.enabled and not _text_parsed:
                    _pat = message_fake_pattern(data, payload, _fc)
                    if _pat:
                        metrics.inc("nx_fake_toolcall_total", (cur, "detected"))
                        _esc = is_escalation_group(
                            dep.get("group"), router.config.go_suffix,
                            router.config.fallback_suffix)
                        if _esc:
                            # sul bucket di escalation non c'e' dove ruotare:
                            # si logga e si lascia al sanitizzatore (strip).
                            log.warning("[fake-tool-call] %s: tool-call reso come "
                                        "testo (pattern=%s) su bucket di "
                                        "escalation -> strip", cur, _pat)
                        else:
                            log.warning("[fake-tool-call] %s: tool-call reso come "
                                        "testo (pattern=%s), escalation", cur, _pat)
                            # ROTAZIONE SENZA PENALITA': nessun _fail_cur
                            # (il modello ha solo reso la chiamata come testo).
                            last_broken = (data, dep)
                            fake_escalations += 1
                            if fake_escalations <= _fc.max_escalations:
                                nxt = router.force_escalation(
                                    dep, need, ctx, tried=tried,
                                    out_tokens=_outb) \
                                    if profile else None
                                if nxt is not None:
                                    dep = nxt
                                    continue
                            raise UpstreamError(
                                503, "fake tool-call: catena di escalation "
                                     "esaurita", final=True)
                # successo pulito: se siamo atterrati su un gruppo piu' alto
                # rispetto a quello richiesto, ricorda il winner (scorciatoia
                # per le prossime richieste su QUEL bucket richiesto).
                # STRIP dei marker di template (Nemotron/Ling): non devono mai
                # comparire nel content consegnato (nemmeno su -go/-fallback).
                try:
                    _msg = ((data or {}).get("choices") or [{}])[0].get(
                        "message") if isinstance(data, dict) else None
                    if isinstance(_msg, dict) and sanitize_message(_msg):
                        metrics.inc("nx_template_tokens_stripped_total", (cur,))
                except Exception:
                    pass
                log.info("[chain] %s successo dopo %d tentativi (durata=%.1fs)", cur, len(tried), time.monotonic() - _t0)
                _q = 0.6 if _text_parsed else (
                    0.7 if tr_result.get("repaired") else 1.0)
                if collect_qc_failures and qc_failed:
                    _q = min(_q, 0.5)
                router.note_result(cur, (time.monotonic() - t0) * 1000,
                                   quality=_q, ctx_est=ctx)
                router.record_escalation_win(requested_group, dep)
                router.note_session_success(ses, dep["unique"],
                                            (time.monotonic() - t0) * 1000,
                                            ctx_est=ctx)
                return (data, dep, qc_failed) if collect_qc_failures \
                    else (data, dep)
            except UpstreamError as err:
                if getattr(err, "final", False):
                    raise                    # decisione definitiva: non ruotare
                detail = err.detail or ""
                # QUOTA DI ACCOUNT: la quota e' dell'account (non della
                # chiave) -> pausa fino al reset TUTTE le chiavi sorelle.
                with contextlib.suppress(Exception):
                    maybe_account_quota_cooldown(router, dep, err.status,
                                                 detail)
                # ATTEMPT TRAIL (P0): un record per hop fallito, con la classe
                # d'errore onesta (finisce nel body del 503 finale).
                try:
                    trail.append({
                        "ord": len(trail) + 1, "dep": cur,
                        "group": dep.get("group"), "model": dep.get("model"),
                        "cls": classify_error_class(err.status, detail),
                        "status": abs(int(err.status)) if err.status else None,
                        "ms": int((time.monotonic() - t0) * 1000)})
                except Exception:            # noqa: BLE001
                    pass
                # P1-5 skipPlatforms: errore PROVIDER-level (5xx/timeout/
                # transport) -> salta TUTTO l'host per questa richiesta invece
                # di bruciare un hop per ogni chiave che ci vive sopra.
                try:
                    if is_provider_level(classify_error_class(err.status, detail)):
                        _h = dep_host(dep)
                        if _h and _h not in skip_hosts:
                            skip_hosts.add(_h)
                            log.info("[skip-host] %s: errore provider-level "
                                     "-> host %s saltato per questa richiesta",
                                     cur, _h)
                except Exception:            # noqa: BLE001
                    pass
                # BAN/ToS dell'endpoint? quarantena l'host 24h PRIMA di
                # ruotare (altrimenti bruciamo una chiave dietro l'altra).
                maybe_quarantine_ban(router, dep, err.status, detail)
                maybe_host_transient_cooldown(router, dep, err.status, detail)
                # 413/400 context-length: ridimensiona al vero max_input
                note_context_limit(router, dep, err.status, detail, ctx)
                # FAMIGLIA REASONING (needs/rejects/history): un rimedio per
                # dep, poi si ritenta LO STESSO deployment. Vale anche per il
                # falso "does not support vision input" (llm7/Cloudflare) su
                # richieste SENZA media: il proxy maschera lo stesso problema
                # del reasoning mancante -> si forza il rimedio "needs".
                _media_raw = bool(media_reject_signature(detail))
                _rsn_media = media_modality_signature(detail) \
                    and not media_input_needed(need) \
                    and reasoning_err_kind(detail) is None
                _steps = _rsn_steps.setdefault(cur, set())
                _replim = int(getattr(router.policy,
                                      "repair_exempt_streak_limit", 3) or 0)
                _rexb = router.repair_exempt_blocked(cur, _replim)
                _rr = None if _rexb else repair_reasoning_error(
                    payload, detail, dep, _steps, orig_messages,
                    force_kind=("needs" if _rsn_media else None))
                if _rexb:
                    log.warning("[reasoning-exempt] %s: budget esenzione "
                                "esaurito (%d) -> KO normale", cur, _replim)
                    # Booking NORMALE: cooldown esplicito (le classi payload/
                    # schema da sole non lo prevedono) per non ritentare il
                    # dep all'infinito su ogni richiesta.
                    _fail_cur(seconds=None,
                              reason="repair_exempt_exhausted",
                              status=abs(int(err.status)) if err.status else None)
                if _rr == "downgraded":
                    dep = dict(dep)
                    dep["_no_thinking"] = True      # copia locale, non il CSV
                if _rr:
                    with contextlib.suppress(Exception):
                        router.note_repair_exempt(cur)
                    metrics.inc("nx_reasoning_replay_total", (_rr,))
                    log.warning("[reasoning-%s] %s: rimedio applicato -> "
                                "ritento lo stesso deployment", _rr, cur)
                    # IMPARA il flag corrispondente: d'ora in poi il CSV lo
                    # porta per questo modello (tutti i gemelli) e la richiesta
                    # parte corretta senza errori continui.
                    with contextlib.suppress(Exception):
                        if _rr == "repaired":
                            learn_thinking_replay(router, dep.get("model"))
                        elif _rr == "stripped":
                            learn_strip_reasoning(router, dep.get("model"))
                        elif _rr == "downgraded":
                            learn_no_thinking(router, dep.get("model"))
                    continue
                # CONTENT ARRAY -> STRING (provider schema stretto, es.
                # Cloudflare Workers AI): 400 "'array' not in 'string'" /
                # "required properties ... 'role,content'". Il payload e'
                # RIPARABILE: impariamo `content_string` (gemelli del modello)
                # e ritentiamo LO STESSO deployment col payload appiattito
                # (media-safe). Se la bonifica non basta (array con media) o il
                # flag c'e' gia', si ricade sulla rotazione di _PAYLOAD_SCHEMA_RE.
                if _CONTENT_ARRAY_RE.search(detail):
                    _csteps = _cstr_steps.setdefault(cur, set())
                    # Solo se c'e' DAVVERO qualcosa da appiattire: se il
                    # payload e' gia' di sole stringhe (o di soli media) il
                    # retry non aiuterebbe -> si ricade sulla rotazione.
                    _flat, _fn = flatten_text_content(
                        (payload or {}).get("messages"))
                    if (_fn and "flatten" not in _csteps
                            and not dep.get("content_string")):
                        _csteps.add("flatten")
                        metrics.inc("nx_content_string_total", ("learned",))
                        log.warning("[content-string] %s: 400 schema "
                                    "content-array -> imparo content_string e "
                                    "ritento lo stesso deployment (%d messaggi)",
                                    cur, _fn)
                        with contextlib.suppress(Exception):
                            learn_content_string(router, dep.get("model"))
                        dep = dict(dep)
                        dep["content_string"] = True     # copia locale (retry)
                        continue
                # ERRORE "OSCURO" su richiesta reasoning: il taglio del
                # reasoning (histnorm) e' un'ottimizzazione di token; senza una
                # firma chiara si ritenta UNA volta lo STESSO deployment con la
                # history ORIGINALE (reasoning intatto).
                if (orig_messages is not None and not _rsn_restored
                        and is_unclear_error(err.status, detail)):
                    _nres = restore_reasoning(payload, orig_messages)
                    if _nres:
                        _rsn_restored = True
                        metrics.inc("nx_reasoning_replay_total", ("restored",))
                        log.warning("[reasoning-restore] %s: errore non chiaro "
                                    "(%s) -> reasoning ripristinato (%d campi), "
                                    "ritento lo stesso deployment", cur,
                                    (detail or "")[:90], _nres)
                        continue
                _kind_default = classify_error(err.status, None, detail)
                # QUOTA ESAURITA (abbonamento flat: "GoUsageLimitError" /
                # "usage limit reached" / "Resets in N days"), a prescindere
                # dallo status HTTP (429 o 4xx provider-side): la key NON torna
                # prima del reset. Cooldown = tempo al reset (non escalation) +
                # rilascio dep-sticky, così la sessione riparte su un'altra
                # chiave e la catena non spreca tentativi su altre key soggette
                # allo stesso limite. Parità col percorso streaming
                # (main._stream_with_fallback).
                if is_insufficient_balance(detail):
                    # BILANCIO ESAURITO ("insufficient balance"): condizione
                    # dell'account -> ritira il DEPLOYMENT (sblocco manuale),
                    # NON cooldown. Priorita' sulla quota: i body la
                    # accompagnano con "type":"insufficient_quota".
                    metrics.inc("nx_upstream_calls_total",
                                (cur, "insufficient_balance"))
                    last_err = err
                    if ses:
                        _st = router.dep_sticky_get(ses)
                        if _st and _st == cur:
                            router.dep_sticky_release(ses)
                    router.mark_failed(cur, reason="insufficient_balance",
                                       status=402,
                                       kind=ErrorKind.PERMANENT_DEAD)
                    log.warning("[fallback] %s 402 'insufficient balance': "
                                "DEPLOYMENT RITIRATO (sblocco manuale)", cur)
                    dep = _pick(profile, dep, need, scope, ctx=ctx,
                                tried=tried,
                                requested_group=requested_group)
                    continue
                if _QUOTA_EXHAUSTED_RE.search(detail):
                    _qcd = (parse_quota_reset_seconds(detail)
                            or QUOTA_MIN_COOLDOWN_S)
                    metrics.inc("nx_upstream_calls_total",
                                (cur, "quota_exhausted"))
                    last_err = err
                    if ses:
                        _st = router.dep_sticky_get(ses)
                        if _st and _st == cur:
                            router.dep_sticky_release(ses)
                    log.warning("[fallback] %s quota esaurita (%.90s): "
                                "cooldown %.0fs al reset, ruoto",
                                cur, detail, _qcd)
                    _fail_cur(seconds=_qcd, reason="quota_exhausted",
                              status=abs(err.status) if err.status else None,
                              provenance=("authoritative"
                                          if _QUOTA_RESET_RE.search(detail or "")
                                          else "heuristic"))
                    dep = _pick(profile, dep, need, scope,
                                               ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                    continue
                # 4xx pass-through SOLO se non deployment-side. Due casi
                # RITRIABILI: (a) firma provider-side (openai_error /
                # bad_response_status_code = errore del LORO upstream);
                # (b) 404 = modello/path inesistente su QUESTO provider
                # (deployment rotto, colpa sua non della richiesta).
                if err.status is not None and err.status < 0:
                    # errore TRANSITORIO del provider/router a monte (upstream
                    # giu', nessun endpoint valido ora, 5xx del provider): NON
                    # e' un problema della richiesta -> ruota con cooldown
                    # CORTO (e' transitorio, non bruciare la chiave per ore).
                    if _PROVIDER_TRANSIENT_RE.search(detail):
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "provider_transient"))
                        last_err = err
                        log.warning("[fallback] %s errore transitorio provider "
                                    "(%.80s): ritento sul successivo (cd corto)",
                                    cur, detail)
                        _fail_cur(
                                            seconds=router.escalate_cooldown(
                                                PROVIDER_TRANSIENT_COOLDOWN_S,
                                                router.stats_for(cur).fail_count_24h),
                                            reason="provider_transient",
                                            status=abs(err.status) if err.status else None)
                        dep = _pick(profile, dep, need, scope,
                                                   ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        continue
                    if -err.status == 404:
                        metrics.inc("nx_upstream_calls_total", (cur, "not_found"))
                        log.warning("[fallback] %s 404 upstream (modello "
                                    "inesistente su questo provider): "
                                    "ritento sul successivo", cur)
                        _fail_cur(reason="not_found",
                                  status=-err.status if err.status else None)
                        dep = _pick(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        continue
                    # alcuni provider (es. cloudflare) rispondono 400 con
                    # "No such model"/code propri invece di 404 e senza firme
                    # litellm ("openai_error"): un deployment col modello
                    # sbagliato non deve MAI passare l'errore al client — è un
                    # problema del deployment, ruotiamo.
                    if _MODEL_MISSING_RE.search(detail):
                        metrics.inc("nx_upstream_calls_total", (cur, "not_found"))
                        log.warning("[fallback] %s modello inesistente/giu' sul "
                                    "provider (%.80s): fermo 24h, ritento sul "
                                    "successivo", cur, detail)
                        _fail_cur( seconds=MODEL_MISSING_COOLDOWN_S,
                                           reason="not_found",
                                           status=-err.status if err.status else None)
                        dep = _pick(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        continue
                    if _THOUGHT_SIG_RE.search(detail):
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "provider_4xx"))
                        last_err = err        # per la consegna a catena esaurita
                        nxt = _pick(profile, dep, need, scope,
                                                   ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        if nxt is not None and nxt["unique"] in tried:
                            nxt = None        # gruppo/catena tutto Gemini 3
                        if nxt is None:
                            log.warning("[fallback] %s 400 thought_signature: "
                                        "nessun deployment alternativo, "
                                        "consegno il 400 al client", cur)
                            raise
                        log.warning("[fallback] %s 400 thought_signature "
                                    "(Gemini 3 tool replay): ritento su %s "
                                    "senza cooldown", cur, nxt["unique"])
                        dep = nxt
                        continue
                    if _PAYLOAD_SCHEMA_RE.search(detail) or \
                            tool_combo_signature(detail) or \
                            _UNKNOWN_FIELD_RE.search(detail):
                        # CF Workers AI & co.: rifiuto di SCHEMA della richiesta
                        # (content array vs string, messaggio senza content).
                        # Google/Gemini 3 (anche via proxy): rifiuto della
                        # COMBINAZIONE built-in tools + function calling
                        # (tool_config flag non passabile via OpenAI-compat).
                        # Non e' il modello rotto: ruota SENZA cooldown, un
                        # provider OpenAI-compatibile accetta lo stesso payload.
                        _tc = tool_combo_signature(detail)
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "tool_combo" if _tc else "provider_4xx"))
                        last_err = err        # consegna il 400 se catena esaurita
                        nxt = _pick(profile, dep, need, scope,
                                                   ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        if nxt is not None and nxt["unique"] in tried:
                            nxt = None
                        if nxt is None:
                            log.warning("[fallback] %s 400 %s: nessuna "
                                        "alternativa, consegno l'errore", cur,
                                        "tool mix built-in+function"
                                        if _tc else "schema payload")
                            raise
                        log.warning("[fallback] %s 400 %s incompatibile col "
                                    "provider: ritento su %s senza cooldown",
                                    cur,
                                    "tool mix built-in+function" if _tc
                                    else "schema payload", nxt["unique"])
                        dep = nxt
                        continue
                    if (qc.retry_provider_4xx and (
                            "bad_response_status_code" in detail
                            or "openai_error" in detail)) \
                            or -err.status == 402:
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "provider_4xx"))
                        if media_strike_hook and media_reject_signature(detail) \
                                and media_input_needed(need):
                            try:
                                media_strike_hook(dep["model"], detail)
                            except Exception as exc:   # mai bloccare il fallback
                                log.debug("[strike] hook error: %s", exc)
                        log.warning("[fallback] %s 400 provider-side "
                                    "(firma openai_error): ritento sul "
                                    "successivo", cur)
                        router.mark_failed(
                            cur,
                            reason="no_credits" if -err.status == 402
                            else "provider_400",
                            status=abs(err.status) if err.status else None)
                        dep = _pick(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        continue
                    # ENVELOPE D'ERRORE PROVIDER: qualsiasi body {"type":"error",
                    # "error":{"type":"XxxError","message":...}} (AuthError
                    # "Invalid API key", ModelError, CreditsError, RegionError,
                    # ...). Non e' MAI contenuto reale del modello -> e' un
                    # problema del DEPLOYMENT: ruota SEMPRE, mai al client.
                    # (Se un giorno un modello restituisse davvero quella forma
                    # come contenuto, la rotazione fa rispondere un altro
                    # modello in modo diverso: nessun danno.)
                    if is_provider_error_body(detail):
                        metrics.inc("nx_upstream_calls_total", (cur, "provider_error"))
                        last_err = err     # consegna l'errore vero se la catena si esaurisce
                        log.warning("[fallback] %s %s body errore provider "
                                    "(%.100s): ritento sul successivo",
                                    cur, -err.status, detail)
                        _fail_cur(reason="provider_error",
                                  status=-err.status if err.status else None)
                        dep = _pick(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        continue
                    # 4xx con body ASSENTE/illeggibile: nessun messaggio
                    # azionabile per il client -> infrastruttura, non colpa
                    # della richiesta. Ruota (cooldown corto); se la catena si
                    # esaurisce -> 503 retryable, mai un 4xx nudo senza spiega.
                    if (not detail.strip()
                            or "body non leggibile" in detail.lower()
                            or len(detail.strip()) < 12):
                        metrics.inc("nx_upstream_calls_total", (cur, "empty_4xx"))
                        last_err = err
                        log.warning("[fallback] %s %s body d'errore vuoto: "
                                    "ritento sul successivo (cd corto)",
                                    cur, -err.status)
                        _fail_cur(
                                            seconds=router.escalate_cooldown(
                                                PROVIDER_TRANSIENT_COOLDOWN_S,
                                                router.stats_for(cur).fail_count_24h),
                                            reason="empty_error_body",
                                            status=-err.status if err.status else None)
                        dep = _pick(profile, dep, need, scope,
                                                   ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        continue
                    # 403 di qualsiasi tipo (permission denied, project banned,
                    # access denied, key disabled...): e' SEMPRE un errore di
                    # deployment/chiave, NON della richiesta -> ruota con
                    # cooldown lungo (la key non torna presto), mai al client
                    # (a catena esaurita -> 503 retryable).
                    if -err.status == 403:
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "upstream_403"))
                        last_err = err
                        log.warning("[fallback] %s 403 upstream: "
                                    "key/progetto rifiutato, cd %.0fs: "
                                    "ritento sul successivo",
                                    cur, PERMISSION_DENIED_COOLDOWN_S)
                        _fail_cur(seconds=PERMISSION_DENIED_COOLDOWN_S,
                                   reason="upstream_403",
                                   status=abs(err.status) if err.status else None)
                        dep = _pick(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        continue
                    # 401 upstream: la NOSTRA chiave e' rifiutata dal provider
                    # (assente/invalidata/revocata). SEMPRE deployment-side (il
                    # client si e' gia' autenticato da noi) -> ruota come il
                    # 403, mai pass-through.
                    if -err.status == 401:
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "upstream_401"))
                        last_err = err
                        log.warning("[fallback] %s 401 upstream: "
                                    "chiave rifiutata/assente, cd %.0fs: "
                                    "ritento sul successivo",
                                    cur, PERMISSION_DENIED_COOLDOWN_S)
                        _fail_cur(seconds=PERMISSION_DENIED_COOLDOWN_S,
                                   reason="upstream_401",
                                   status=abs(err.status) if err.status else None)
                        dep = _pick(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        continue
                    if is_provider_fault_body(detail):
                        # Il provider non e' riuscito a leggere/parsare la
                        # richiesta (envelope OpenAI {"error":{...}}): non e'
                        # un rifiuto del contenuto -> un altro deployment
                        # accetta lo stesso payload. Ruota (cd corto), mai
                        # pass-through del 400 al client.
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "provider_fault"))
                        last_err = err
                        log.warning("[fallback] %s %s body d'errore provider "
                                    "(lettura richiesta): ritento sul "
                                    "successivo", cur, -err.status)
                        _fail_cur(
                            seconds=router.escalate_cooldown(
                                PROVIDER_TRANSIENT_COOLDOWN_S,
                                router.stats_for(cur).fail_count_24h),
                            reason="provider_fault",
                            status=-err.status if err.status else None)
                        dep = _pick(profile, dep, need, scope,
                                                   ctx=ctx, tried=tried,
                                                   requested_group=requested_group)
                        continue
                    # Rifiuto di MODALITA' (vision/image/audio/…): il modello
                    # non e' rotto, un altro deployment multimodale accetta lo
                    # stesso payload -> ruota (strike per l'auto-learn), mai
                    # pass-through del 400 al client.
                    if media_reject_signature(detail):
                        last_err = err
                        if media_input_needed(need):
                            metrics.inc("nx_upstream_calls_total",
                                        (cur, "media_reject"))
                            if media_strike_hook:
                                try:
                                    media_strike_hook(dep["model"], detail)
                                except Exception as exc:
                                    log.debug("[strike] hook error: %s", exc)
                            nxt = _pick(profile, dep, need, scope,
                                                       ctx=ctx, tried=tried,
                                                       requested_group=requested_group)
                            if nxt is None:
                                log.warning("[fallback] %s %s rifiuto modalita': "
                                            "nessuna alternativa -> 503",
                                            cur, -err.status)
                                raise
                            log.warning("[fallback] %s %s rifiuto modalita' -> %s",
                                        cur, -err.status, nxt["unique"])
                            dep = nxt
                            continue
                        # FALSO rifiuto di modalita': la richiesta NON ha media
                        # (caso llm7/Cloudflare che risponde "does not support
                        # vision input" a puro testo). Il modello e' rotto per
                        # QUESTA richiesta -> cooldown normale + rotazione, cosi'
                        # non viene ritentato a ogni richiesta.
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "model_feature"))
                        log.warning("[fallback] %s %s 'vision' ma la richiesta "
                                    "non ha media -> cooldown normale",
                                    cur, -err.status)
                        _fail_cur(seconds=err.retry_after,
                                  reason="model_feature",
                                  status=abs(err.status) if err.status else None)
                        dep = _pick(profile, dep, need, scope, ctx=ctx,
                                    tried=tried,
                                    requested_group=requested_group)
                        continue
                    raise
                metrics.inc("nx_upstream_calls_total", (cur, "error"))
                last_err = err
                log.warning("[fallback] %s fallito (status=%s): provo il successivo",
                            cur, err.status or "connessione")
                if err.status is None and "upstream timeout" in detail.lower():
                    # TIMEOUT (upstream che appende): danno reale -> cooldown
                    # lungo (timeout_cooldown_mult x classico).
                    _reason = "timeout"
                elif err.status and err.status > 0:
                    _reason = "http_%s" % err.status
                else:
                    _reason = "network"
                _fail_cur( seconds=err.retry_after,
                                    reason=_reason,
                                    status=abs(err.status) if err.status else None)
                dep = _pick(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
            finally:
                router.key_lease_release(_lease)   # P2-8
                router.note_end(cur, ctx)   # SEMPRE il tentativo corrente
        # catena finita dopo fallimenti QC: consegna l'ultimo broken (D3) SE ha
        # contenuto; se e' vuoto -> errore RETRYABLE (mai un turno vuoto/finto).
        if collect_qc_failures and qc_failed and last_broken is not None:
            data0 = last_broken[0]
            if not _looks_empty(data0):
                return data0, last_broken[1], qc_failed
        if last_err is not None and trail:
            with contextlib.suppress(Exception):
                last_err.trail = trail
        raise last_err or UpstreamError(503, "nessun deployment disponibile",
                                        final=True)
