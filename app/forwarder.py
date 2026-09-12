"""HTTP verso gli upstream: chiamata, streaming, fallback di catena.

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
import hashlib
import json
import logging
import os
import re
import time
from typing import AsyncIterator

import httpx

from . import metrics
from .qc import check_response
from .router import inject_identity
from .thought_sig import (THOUGHT_SIGS, extract_signatures, is_gemini_deployment,
                          has_unsigned_tool_calls)
from .effort import get_effort, get_temperature_config

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


# Logger dedicato: OGNI body upstream che contiene "error" ci finisce (handler
# su file agganciato in main.py -> var/error-audit.log, solo locale). Serve a
# rivedere a posteriori gli errori usciti che non dovevano.
errlog = logging.getLogger("nx.erroraudit")

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0,
                                 pool=10.0)

# "No such model" (cloudflare), model_not_found (openai), "Model X is not
# supported" / {"type":"ModelError"} (opencode-zen), "Model is (currently)
# unavailable", ecc. — il modello non esiste / non e' servito / e' giu' su
# QUESTO provider: deployment-side, sempre ritriabile (mai pass-through del
# 4xx al client). Condizione effettivamente duratura -> cooldown 24h fisso.
_MODEL_MISSING_RE = re.compile(
    r"no such model|model_not_found|unknown model|modello inesistente"
    r"|does not exist|modelerror|model[\w .:/'-]*\bnot supported"
    r"|model[\w .:/'()-]{0,60}?\bunavailable"
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
    r"|insufficient.quota",
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
    r"\baierror\b"
    r"|oneof at '/?[^']*' not met"
    r"|type mismatch of '/messages/\d+/content'"
    r"|'array' not in 'string'|'string' not in 'array'"
    r"|required properties at '/messages/\d+' are"
    r"|(reasoning_content|reasoning)['\" ]* is unsupported"
    r"|for 'role:assistant'[^\]]*reasoning[^\]]*unsupported"
    r"|property 'reasoning[_a-z]*' is unsupported",
    re.IGNORECASE)

# Errore TRANSITORIO del provider/router a monte (non del client, non del
# modello): l'upstream del provider e' giu', non ha endpoint validi ora, ecc.
# Arriva come 4xx col body d'errore ma NON e' un problema della richiesta ->
# si ruota (cooldown CORTO: e' transitorio). Queste frasi compaiono solo nei
# body d'errore di provider/router, mai nel contenuto reale di un modello.
_PROVIDER_TRANSIENT_RE = re.compile(
    r"error from provider|upstream request failed|provider returned error"
    r"|no endpoints found|no allowed providers|temporarily unavailable"
    r"|upstream error|bad gateway|service unavailable|gateway timeout"
    r"|internal server error|too many requests|overloaded",
    re.IGNORECASE)
PROVIDER_TRANSIENT_COOLDOWN_S = 120

# 403 upstream (permission denied / project banned / key disabled...): la key
# non torna presto -> cooldown lungo, poi si ruota sul successivo.
PERMISSION_DENIED_COOLDOWN_S = 3600          # 1h


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
    Ritorna una NUOVA lista messages, per non inquinare il payload originale
    (un fallback su un provider non-Google non deve vedere extra_content)."""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return
    changed = False
    new_msgs = []
    for m in msgs:
        if not (isinstance(m, dict) and m.get("role") == "assistant"
                and m.get("tool_calls")):
            new_msgs.append(m)
            continue
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
      1. `session` (passthrough dal client) se presente -> esattamente quel
         valore;
      2. altrimenti session ID nel formato nativo opencode derivato da
         `api_key + "|" + client_ip + "|" + profilo` (deterministico: stesso
         input -> stesso valore; i componenti vuoti restano vuoti, cosi' il
         valore cambia se cambia anche solo il profilo).

    `attribution`: header di attribuzione app INVIATI DAL CLIENT
    (HTTP-Referer / X-Title / X-OpenRouter-Title). Se presenti, vincono sul
    valore configurato (passthrough fedele: il client si presenta come
    l'harness che e'); altrimenti si usa l'identita' di policy (default
    opencode). Rilevante SOLO per upstream OpenRouter.

    Ritorna SEMPRE l'header: OpenCode Go lo richiede per session affinity e
    prompt caching; gli altri provider lo ignorano senza effetto.
    """
    out: dict[str, str] = {}
    if session:
        out = {"x-opencode-session": session}
        if os.environ.get("SNIFF_HEADERS"):
            log.warning("[sniff] upstream session=passthrough value=%s", session)
    else:
        key = (dep.get("api_key") or "").strip()
        basis = "|".join((key, client_ip, profile))
        value = _native_session_of(basis)
        if os.environ.get("SNIFF_HEADERS"):
            log.warning("[sniff] upstream session=native value=%s "
                        "basis=(%s,%s,%s)", value, bool(key),
                        bool(client_ip), bool(profile))
        out = {"x-opencode-session": value}
    # Attribuzione app OpenRouter: vale per OGNI upstream openrouter.ai
    # (i modelli :free ora, ma il gate 'agentic harness' può estendersi):
    # gli header HTTP-Referer + X-Title servono a identificare un'app
    # riconosciuta (openrouter.ai/apps); il referer del CLIENT vince se
    # presente (passthrough), altrimenti la policy (default opencode,
    # l'harness dietro il gateway).
    out.update(_openrouter_attribution(dep, client_headers=attribution))
    return out


def _client_attribution(request) -> dict[str, str]:
    """Estrae dall'header del client l'attribuzione app che OpenRouter
    pretende per i modelli :free (gate 'agentic harness').

    Ritorna un dict con i SOLI header che il client ha inviato davvero:
    HTTP-Referer, X-Title, X-OpenRouter-Title. Vuoto = il cliente non si
    attribuisce -> si usa l'identita' di policy (default opencode).
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
    return out


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


class UpstreamError(Exception):
    def __init__(self, status: int | None, detail: str,
                 retry_after: float | None = None, *, final: bool = False):
        self.status = status
        self.detail = detail
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


# Google (generativelanguage) e altri mettono il ritardo consigliato NEL BODY
# del 429, non nell'header: `"retryDelay": "58s"` dentro un blocco RetryInfo,
# oppure `... Please retry in 58.93s`. Senza questo il gateway non lo vede e
# applica l'escalation (cooldown di ore) su un semplice rate-limit giornaliero.
_RETRY_BODY_RE = re.compile(
    r'"retryDelay"\s*:\s*"?\s*(\d+(?:\.\d+)?)\s*s'          # "retryDelay": "58s"
    r'|"retryDelay"\s*:\s*\{\s*"seconds"\s*:\s*"?(\d+)'      # {"seconds": 58}
    r'|retry\s+in\s+(\d+(?:\.\d+)?)\s*s(?:econds)?',          # "retry in 58.9s"
    re.IGNORECASE)
_RETRY_BODY_CAP_S = 300.0                                     # un 429 non chiede ore


def _retry_after_from(resp: httpx.Response, body: str | None) -> float | None:
    """Retry-After: prima l'header, poi (fallback) il retryDelay dal body 429.
    Cap a 300s: un rate-limit non deve mai valere un cooldown di ore."""
    hdr = _retry_after_of(resp)
    if hdr is not None:
        return hdr
    if not body:
        return None
    m = _RETRY_BODY_RE.search(body)
    if not m:
        return None
    # I tre gruppi di cattura nell'ordine:
    # 1) "retryDelay": "58s"               -> gruppo 1: (\d+(?:\.\d+)?)
    # 2) {"seconds": 58}                   -> gruppo 2: (\d+)
    # 3) "retry in 58.9s"                  -> gruppo 3: (\d+(?:\.\d+)?)
    # Preferiamo il formato oggetto {"seconds": N} poiche' e' piu' strutturato,
    # poi il formato stringa "retryDelay": "Ns", infine il formato testuale "retry in Ns".
    groups = m.groups()
    # Cerca il gruppo 2 (formato oggetto) per primo, poi gruppo 1 (stringa), poi gruppo 3 (testo)
    for g in (groups[1], groups[0], groups[2]):
        if g is not None:
            try:
                val = float(g)
                return max(1.0, min(val, _RETRY_BODY_CAP_S))
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


class Forwarder:
    def __init__(self, client: httpx.AsyncClient | None = None):
        # client iniettabile per i test (httpx.MockTransport)
        self.client = client or httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------- request
    async def stream_response(self, dep: dict, payload: dict,
                              *, profile: str = "",
                              client_ip: str = "",
                              session: str | None = None,
                              attribution: dict | None = None
                              ) -> AsyncIterator[bytes]:
        """Fa la richiesta con stream=True e yielda i chunk SSE grezzi.

        Solleva UpstreamError per stati ritriabili PRIMA del primo byte inviato
        (così il chiamante può fare fallback senza corrompere la risposta).
        """
        body = dict(payload)
        body["model"] = dep["model"]
        apply_effort_policy(body, dep)
        _google = is_gemini_deployment(dep)
        if _google:
            log.info("[thought_sig] Google provider, injecting for request")
            _inject_thought_signatures(body)
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
        headers = {
            "Authorization": f"Bearer {dep['api_key']}",
            "Content-Type": "application/json",
            **_session_headers(dep, profile=profile, client_ip=client_ip,
                               session=session, attribution=attribution),
        }
        url = f"{dep['api_base']}/chat/completions"
        log.debug("[upstream] %s POST %s (stream=%s, google=%s)", dep.get("unique", "?"), url, payload.get("stream", False), _google)
        try:
            req = self.client.build_request("POST", url, json=body,
                                            headers=headers)
            resp = await self.client.send(req, stream=True)
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
            raise UpstreamError(resp.status_code, raw or "upstream %s (body "
                                "non leggibile)" % resp.status_code,
                                _retry_after_from(resp, raw) if resp.status_code == 429 else None)

        if resp.status_code >= 400:
            # errore non ritriabile: lo restituiamo al client così com'è
            raw = await _safe_aread(resp)
            try:
                await resp.aclose()
            except Exception:
                pass
            raise UpstreamError(-resp.status_code,
                                raw.decode(errors="replace") or
                                "upstream %s (body non leggibile)"
                                % resp.status_code,
                                _retry_after_of(resp))

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
                adapted = _json_to_sse(raw)
                if adapted is None:
                    raise UpstreamError(
                        503, "upstream ignored stream:true, body non "
                             "utilizzabile (content-type=%s) %r"
                             % (ctype, raw[:200]))

                async def _adapted_gen() -> AsyncIterator[bytes]:
                    for b in adapted:
                        yield b
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
            try:
                async for chunk in resp.aiter_bytes():
                    if _google:
                        buf += chunk
                        # processa solo righe complete
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            _capture_sigs_from_sse(line)
                    yield chunk
            finally:
                await resp.aclose()

        return gen()

    async def call(self, dep: dict, payload: dict, *,
                   profile: str = "",
                   client_ip: str = "",
                   session: str | None = None,
                   attribution: dict | None = None) -> dict:
        """Richiesta NON streaming: risposta JSON completa."""
        body = dict(payload)
        body["model"] = dep["model"]
        apply_effort_policy(body, dep)
        _google = is_gemini_deployment(dep)
        if _google:
            log.info("[thought_sig] Google provider, injecting for request")
            _inject_thought_signatures(body)
        headers = {
            "Authorization": f"Bearer {dep['api_key']}",
            "Content-Type": "application/json",
            **_session_headers(dep, profile=profile, client_ip=client_ip,
                               session=session, attribution=attribution),
        }
        url = f"{dep['api_base']}/chat/completions"
        log.debug("[upstream] %s POST %s (stream=%s, google=%s)", dep.get("unique", "?"), url, payload.get("stream", False), _google)
        try:
            resp = await self.client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            # L'upstream ha APPESO (read/connect timeout): danno reale (tempo
            # perso) -> marker distinto, il fallback lo classifica "timeout"
            # e applica il cooldown lungo.
            raise UpstreamError(None, f"upstream timeout: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(None, f"upstream connection error: {exc}") from exc

        if resp.status_code >= 400:
            raise UpstreamError(
                -resp.status_code if resp.status_code not in RETRYABLE_STATUS
                else resp.status_code,
                resp.text[:500],
                _retry_after_from(resp, resp.text) if resp.status_code == 429 else None)
        try:
            data = resp.json()
        except ValueError as exc:
            raise UpstreamError(None, f"upstream non-JSON response: {exc}") from exc
        if _google:
            _capture_sigs_from_obj(data)
            log.debug("[thought_sig-capture] non-streaming capture, sigs_stored=%d", len(THOUGHT_SIGS))
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
        url = f"{dep['api_base']}/chat/completions"
        log.debug("[upstream] %s POST %s (stream=%s, google=%s)", dep.get("unique", "?"), url, payload.get("stream", False), _google)
        try:
            resp = await self.client.post(url, json=body, headers=headers,
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
                _retry_after_from(resp, resp.text) if resp.status_code == 429 else None)
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
            resp = await self.client.post(url, json=body, headers=headers,
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
                _retry_after_from(resp, resp.text) if resp.status_code == 429 else None)
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
            resp = await self.client.post(url, data=data, files=files,
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
                _retry_after_from(resp, resp.text) if resp.status_code == 429 else None)
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
            resp = await self.client.post(url, json=body, headers=headers,
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
            resp = await self.client.get(url, headers=headers)
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
            resp = await self.client.get(url, headers=headers,
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
                                 requested_group: str | None = None
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
        # Gemini 3 tool replay: se la history ha tool_call senza firma
        # (conversazione passata per altri modelli), Gemini risponderebbe 400
        # INVALID_ARGUMENT. Escludilo a monte e salta a un deployment non-Gemini.
        _avoid_gemini = has_unsigned_tool_calls(
            (payload or {}).get("messages") or [])
        _skip_budget = 64                    # sicurezza anti-loop
        while (dep is not None and len(tried) < _max_tries
               and (not _deadline_ms
                    or (time.monotonic() - _t0) * 1000 < _deadline_ms)):
            cur = dep["unique"]             # il deployment DEL TENTATIVO:
            if (_avoid_gemini and _skip_budget > 0 and is_gemini_deployment(dep)
                    and cur not in tried):
                _skip_budget -= 1
                tried.add(cur)              # così fallback_next non lo ripropone
                nxt = router.fallback_next(profile, dep, need, scope, ctx=ctx,
                                           tried=tried,
                                           requested_group=requested_group)
                if nxt is not None:
                    log.warning("[thought_sig] replay senza firma: salto "
                                "Gemini %s -> %s", cur, nxt["unique"])
                    dep = nxt
                    continue
                tried.discard(cur)          # nessuna alternativa: prova comunque
            log.debug("[chain] tentativo %d/%d: %s (group=%s)", len(tried), _max_tries, cur, dep.get("group", "?"))
            _was_dormant = router.is_cooled_down(cur)
            def _fail_cur(seconds=None, reason=None, status=None):
                if _was_dormant:
                    return router.mark_failed_double_residual(cur, reason=reason, status=status)
                return router.mark_failed(cur, seconds=seconds, reason=reason, status=status)
            tried.add(cur)                  # note_end deve riferirsi a QUESTO,
            if attempts_box is not None:
                attempts_box.append(cur)    # osservabilità summary per-richiesta
            # l'identità DEVE riflettere il deployment CHE PROVA ORA: dopo un
            # fallback il system message nominerebbe il modello sbagliato.
            inject_identity(payload, dep)
            router.note_start(cur)          # rotazione adattiva
            t0 = time.monotonic()
            try:
                data = await self.call(dep, payload,
                               profile=profile or "",
                               client_ip=client_ip, session=session,
                               attribution=attribution)
                router.note_result(cur, (time.monotonic() - t0) * 1000)
                if _was_dormant:
                    router.clear_cooldown(cur)
                metrics.observe_latency_ms(cur, (time.monotonic() - t0) * 1000)
                metrics.inc("nx_upstream_calls_total", (cur, "ok"))

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
                        qc_failed.append((cur, reason))
                        metrics.inc("nx_qc_discarded_total",
                                    (cur, reason.split(" ")[0]))
                        last_broken = (data, dep)
                        if len(qc_failed) <= qc.max_attempts:
                            log.warning("[qc] %s JSON non valido (%s): "
                                        "provo il successivo", cur, reason)
                            _fail_cur()
                            dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                            continue        # finally chiude il TENTATIVO
                        # tentativi esauriti: consegna l'ultimo se ha contenuto,
                        # altrimenti errore RETRYABLE (mai un turno vuoto/finto).
                        if _looks_empty(data):
                            raise UpstreamError(
                                503, "catena esaurita, nessun output utile",
                                 final=True)
                        return data, dep, qc_failed
                # successo pulito: se siamo atterrati su un gruppo piu' alto
                # rispetto a quello richiesto, ricorda il winner (scorciatoia
                # per le prossime richieste su QUEL bucket richiesto).
                log.info("[chain] %s successo dopo %d tentativi (durata=%.1fs)", cur, len(tried), time.monotonic() - _t0)
                router.record_escalation_win(requested_group, dep)
                return (data, dep, qc_failed) if collect_qc_failures \
                    else (data, dep)
            except UpstreamError as err:
                if getattr(err, "final", False):
                    raise                    # decisione definitiva: non ruotare
                detail = err.detail or ""
                # QUOTA ESAURITA (abbonamento flat: "GoUsageLimitError" /
                # "usage limit reached" / "Resets in N days"), a prescindere
                # dallo status HTTP (429 o 4xx provider-side): la key NON torna
                # prima del reset. Cooldown = tempo al reset (non escalation) +
                # rilascio dep-sticky, così la sessione riparte su un'altra
                # chiave e la catena non spreca tentativi su altre key soggette
                # allo stesso limite. Parità col percorso streaming
                # (main._stream_with_fallback).
                if is_provider_error_body(detail) \
                        and _QUOTA_EXHAUSTED_RE.search(detail):
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
                              status=abs(err.status) if err.status else None)
                    dep = router.fallback_next(profile, dep, need, scope,
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
                        dep = router.fallback_next(profile, dep, need, scope,
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
                        dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried,
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
                        dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        continue
                    if _THOUGHT_SIG_RE.search(detail):
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "provider_4xx"))
                        last_err = err        # per la consegna a catena esaurita
                        nxt = router.fallback_next(profile, dep, need, scope,
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
                    if _PAYLOAD_SCHEMA_RE.search(detail):
                        # CF Workers AI & co.: rifiuto di SCHEMA della richiesta
                        # (content array vs string, messaggio senza content).
                        # Non e' il modello rotto: ruota SENZA cooldown, un
                        # provider OpenAI-compatibile accetta lo stesso payload.
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "provider_4xx"))
                        last_err = err        # consegna il 400 se catena esaurita
                        nxt = router.fallback_next(profile, dep, need, scope,
                                                   ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
                        if nxt is not None and nxt["unique"] in tried:
                            nxt = None
                        if nxt is None:
                            log.warning("[fallback] %s 400 schema payload "
                                        "(content array / messaggio senza "
                                        "content): nessuna alternativa, "
                                        "consegno il 400 al client", cur)
                            raise
                        log.warning("[fallback] %s 400 schema payload "
                                    "incompatibile col provider: ritento su "
                                    "%s senza cooldown", cur, nxt["unique"])
                        dep = nxt
                        continue
                    if (qc.retry_provider_4xx and (
                            "bad_response_status_code" in detail
                            or "openai_error" in detail)) \
                            or -err.status == 402:
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "provider_4xx"))
                        if media_strike_hook and media_reject_signature(detail):
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
                        dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried,
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
                        dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried,
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
                        dep = router.fallback_next(profile, dep, need, scope,
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
                        dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried,
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
                        dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried,
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
                dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried,
                                                          requested_group=requested_group)
            finally:
                router.note_end(cur)        # SEMPRE il tentativo corrente
        # catena finita dopo fallimenti QC: consegna l'ultimo broken (D3) SE ha
        # contenuto; se e' vuoto -> errore RETRYABLE (mai un turno vuoto/finto).
        if collect_qc_failures and qc_failed and last_broken is not None:
            data0 = last_broken[0]
            if not _looks_empty(data0):
                return data0, last_broken[1], qc_failed
        raise last_err or UpstreamError(503, "nessun deployment disponibile",
                                        final=True)
