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

log = logging.getLogger("nx.forwarder")
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
# Formato: 8 hex (es. "5f634ae1") come le sessioni native opencode: stesso
# "aspetto" del provider, indistinguibili tra loro.
def _session_headers(dep: dict, *, profile: str = "",
                     client_ip: str = "",
                     session: str | None = None) -> dict[str, str]:
    """Header x-opencode-session per la richiesta upstream.

    Priorità:
      1. `session` (passthrough dal client) se presente -> esattamente quel
         valore;
      2. altrimenti hash sha256 di `api_key + "|" + client_ip + "|" + profilo`
         troncato a 8 hex (i componenti vuoti restano vuoti, cosi' l'hash
         cambia se cambia anche solo il profilo).

    Ritorna SEMPRE l'header: i provider opencode lo usano per il
    load-balancing/session; gli altri lo ignorano senza effetto.
    """
    if session:
        return {"x-opencode-session": session}
    key = (dep.get("api_key") or "").strip()
    basis = "|".join((key, client_ip, profile))
    digest = hashlib.sha256(basis.encode()).hexdigest()
    return {"x-opencode-session": digest[:8]}


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
                              session: str | None = None) -> AsyncIterator[bytes]:
        """Fa la richiesta con stream=True e yielda i chunk SSE grezzi.

        Solleva UpstreamError per stati ritriabili PRIMA del primo byte inviato
        (così il chiamante può fare fallback senza corrompere la risposta).
        """
        body = dict(payload)
        body["model"] = dep["model"]
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
                                      session=session),
        }
        url = f"{dep['api_base']}/chat/completions"
        try:
            req = self.client.build_request("POST", url, json=body,
                                            headers=headers)
            resp = await self.client.send(req, stream=True)
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
                return _adapted_gen()

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return gen()

    async def call(self, dep: dict, payload: dict, *,
                   profile: str = "",
                   client_ip: str = "",
                   session: str | None = None) -> dict:
        """Richiesta NON streaming: risposta JSON completa."""
        body = dict(payload)
        body["model"] = dep["model"]
        headers = {
            "Authorization": f"Bearer {dep['api_key']}",
            "Content-Type": "application/json",
            **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session),
        }
        url = f"{dep['api_base']}/chat/completions"
        try:
            resp = await self.client.post(url, json=body, headers=headers)
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

    async def call_images(self, dep: dict, payload: dict, *,
                          profile: str = "",
                          client_ip: str = "",
                          session: str | None = None) -> dict:
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
                                      session=session),
        }
        url = f"{dep['api_base']}/images/generations"
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
                          session: str | None = None) -> tuple[bytes, str]:
        """TTS: POST {api_base}/audio/speech -> bytes audio (buffered).

        Body OpenAI {model, input, voice, response_format?, speed?} col model
        riscritto. Ritorna (content, content_type) dalla risposta upstream.
        Errori come call_images: retryable vs -status client-side.
        """
        body = {k: v for k, v in payload.items() if k != "model"}
        body["model"] = dep["model"]
        headers = {"Authorization": f"Bearer {dep['api_key']}",
                   **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session)}
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
                         session: str | None = None) -> dict | str:
        """STT: POST multipart {api_base}/audio/{path} (transcriptions|translations).

        I campi form passano quasi intatti (model riscritto); il file va come
        multipart. Risposta JSON ({text...}) oppure testo per formati srt/vtt/text.
        """
        data = {k: v for k, v in data_fields.items() if k != "model"}
        data["model"] = dep["model"]
        headers = {"Authorization": f"Bearer {dep['api_key']}",
                   **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session)}
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
                           session: str | None = None) -> dict:
        """Submit job video (API asincrona OR-style): POST {base}/videos.
        Ritorna l'envelope {id, status, polling_url,...}. Errori come call."""
        body = {k: v for k, v in payload.items() if k != "model"}
        body["model"] = dep["model"]
        headers = {"Authorization": f"Bearer {dep['api_key']}",
                   **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session)}
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
                         session: str | None = None) -> dict:
        """Stato del job: GET {base}/videos/{job_id} -> JSON di stato."""
        headers = {"Authorization": f"Bearer {dep['api_key']}",
                   **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session)}
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
                             session: str | None = None) -> dict:
        """Poll STATELESS: prova ogni candidato (chiavi diverse) finché uno
        riconosce il job. I job vivono sull'account di chi ha submitto, quindi
        le chiavi di altri account risponderanno 404 -> si prosegue."""
        last: UpstreamError | None = None
        for dep in deps:
            try:
                return await self.poll_video(dep, job_id,
                                             profile=profile,
                                             client_ip=client_ip,
                                             session=session)
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
                                 session: str | None = None) -> tuple[bytes, str]:
        """Download stateless: stessa logica multi-chiave di poll_video_any."""
        last: UpstreamError | None = None
        for dep in deps:
            try:
                return await self.download_video(dep, job_id,
                                                 profile=profile,
                                                 client_ip=client_ip,
                                                 session=session)
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
                             session: str | None = None) -> tuple[bytes, str]:
        """Contenuto MP4 completato: GET {base}/videos/{job_id}/content."""
        headers = {"Authorization": f"Bearer {dep['api_key']}",
                   **_session_headers(dep, profile=profile, client_ip=client_ip,
                                      session=session)}
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
                                 client_ip: str = ""
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
        while (dep is not None and len(tried) < _max_tries
               and (not _deadline_ms
                    or (time.monotonic() - _t0) * 1000 < _deadline_ms)):
            cur = dep["unique"]             # il deployment DEL TENTATIVO:
            _was_dormant = router.is_cooled_down(cur)
            def _fail_cur(seconds=None, reason=None):
                if _was_dormant:
                    return router.mark_failed_double_residual(cur, reason=reason)
                return router.mark_failed(cur, seconds=seconds, reason=reason)
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
                               client_ip=client_ip, session=session)
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
                            dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried)
                            continue        # finally chiude il TENTATIVO
                        # tentativi esauriti: consegna l'ultimo se ha contenuto,
                        # altrimenti errore RETRYABLE (mai un turno vuoto/finto).
                        if _looks_empty(data):
                            raise UpstreamError(
                                503, "catena esaurita, nessun output utile",
                                final=True)
                        return data, dep, qc_failed
                return (data, dep, qc_failed) if collect_qc_failures \
                    else (data, dep)
            except UpstreamError as err:
                if getattr(err, "final", False):
                    raise                    # decisione definitiva: non ruotare
                detail = err.detail or ""
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
                                           seconds=PROVIDER_TRANSIENT_COOLDOWN_S,
                                           reason="provider_transient")
                        dep = router.fallback_next(profile, dep, need, scope,
                                                   ctx=ctx, tried=tried)
                        continue
                    if -err.status == 404:
                        metrics.inc("nx_upstream_calls_total", (cur, "not_found"))
                        log.warning("[fallback] %s 404 upstream (modello "
                                    "inesistente su questo provider): "
                                    "ritento sul successivo", cur)
                        _fail_cur( reason="not_found")
                        dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried)
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
                                           reason="not_found")
                        dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried)
                        continue
                    if _THOUGHT_SIG_RE.search(detail):
                        metrics.inc("nx_upstream_calls_total",
                                    (cur, "provider_4xx"))
                        last_err = err        # per la consegna a catena esaurita
                        nxt = router.fallback_next(profile, dep, need, scope,
                                                   ctx=ctx, tried=tried)
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
                                                   ctx=ctx, tried=tried)
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
                            else "provider_400")
                        dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried)
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
                        _fail_cur( reason="provider_error")
                        dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried)
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
                                           seconds=PROVIDER_TRANSIENT_COOLDOWN_S,
                                           reason="empty_error_body")
                        dep = router.fallback_next(profile, dep, need, scope,
                                                   ctx=ctx, tried=tried)
                        continue
                    raise
                metrics.inc("nx_upstream_calls_total", (cur, "error"))
                last_err = err
                log.warning("[fallback] %s fallito (status=%s): provo il successivo",
                            cur, err.status or "connessione")
                _reason = ("http_%s" % err.status
                           if err.status and err.status > 0 else "network")
                _fail_cur( seconds=err.retry_after,
                                   reason=_reason)
                dep = router.fallback_next(profile, dep, need, scope, ctx=ctx, tried=tried)
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
