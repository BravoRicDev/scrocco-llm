"""Gestione capacità modelli: rilevamento required, conteggio immagine.

Funzioni PURE e testabili in isolamento; nessuno stato, nessuna I/O.
Il metodo Policy.caps_for() risolve le capacità per nome modello.

[EN] WHAT: pure helpers for model capability detection and image-token
counting; stateless and unit-testable by design.
"""
from __future__ import annotations

import re
from typing import Any
CANONICAL_CAPS = frozenset({"text", "vision", "video", "audio", "image_gen",
                            "tools", "tts", "stt", "video_gen",
                            "image_edit", "image_multi_ref"})
# Quelle che guidano il routing (escluso tools = solo metadato)
ROUTING_CAPS = frozenset({"vision", "video", "audio", "image_gen", "tts",
                          "stt", "video_gen", "image_edit", "image_multi_ref"})
# Token di GENERAZIONE: una riga che li contiene partecipa SOLO ai gruppi
# di generazione corrispondenti (mai alle catene di ingest/analisi)
GEN_CAPS = frozenset({"image_gen", "video_gen"})


class CapabilitiesError(ValueError):
    """Errore validazione capacità (-> HTTP 400 in admin)."""
    pass


def normalize_caps(raw: list[str] | str | None, ctx: str) -> frozenset[str]:
    """Normalizza e valida una lista di capacità.

    Accetta lista di stringhe o stringa comma-separated.
    Solleva CapabilitiesError se nomi non canonici.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, list):
        items = [str(s).strip() for s in raw if str(s).strip()]
    else:
        raise CapabilitiesError(f"{ctx}: deve essere lista o stringa, non {type(raw).__name__}")
    if not items:
        return frozenset()
    unknown = [c for c in items if c not in CANONICAL_CAPS]
    if unknown:
        raise CapabilitiesError(
            f"{ctx}: capacità sconosciute {unknown}; ammesse: {sorted(CANONICAL_CAPS)}"
        )
    return frozenset(items)


def required_caps(payload: dict) -> frozenset[str]:
    """Estrae le capacità richieste da un payload chat/completions.

    Scansiona messages[].content (stringa o lista parti):
      - type in {"image_url", "input_image"}                  -> vision
      - type == "input_audio"                                 -> audio
      - type == "file" con mime video/*                       -> video
      - type == "video_url"                                   -> video
      - type == "inline_data" con mime video/*                -> video
    Parti sconosciute -> ignorate (forward-compat).
    tools nel payload NON instrada (solo metadato, come da decisione).
    """
    need: set[str] = set()
    messages = payload.get("messages") or []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if content is None:
            continue
        if isinstance(content, str):
            continue  # solo testo
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype in ("image_url", "input_image"):
                    need.add("vision")
                elif ptype == "input_audio":
                    need.add("audio")
                elif ptype in ("audio_url", "audio"):
                    need.add("audio")
                elif ptype == "video_url":
                    need.add("video")
                elif ptype in ("file", "inline_data"):
                    # forme Gemini-native: la MIME decide la capacita'. Prima
                    # erano guardate SOLO per video/image: un audio inline
                    # finiva nel mondo testo senza nemmeno chiedere `audio`.
                    mime = _mime_of(part)
                    if mime.startswith("video/"):
                        need.add("video")
                    elif mime.startswith("image/"):
                        need.add("vision")
                    elif mime.startswith("audio/"):
                        need.add("audio")
                # input_text, text, reasoning, tool_calls, etc. -> ignorati
    return frozenset(need)


def wants_image_output(payload: dict) -> bool:
    """True se un body chat/completions chiede un'immagine in OUTPUT.

    Riconosce le forme con cui i client image-capable lo esprimono:
      - `modalities: ["image"]` / `["image","text"]` (Gemini/chat shim)
      - `modalities: ["text","image"]`
      - `output_modalities` (variante OpenAI/Gemini REST)
      - flag espliciti `image_output` / `generate_image` truthy
    Usata per dirotare la richiesta verso la macchina immagini: il modello
    scelto puo' essere image-native (adattamento chat->/images) o chat-only."""
    if not isinstance(payload, dict):
        return False
    for key in ("modalities", "output_modalities", "response_modalities"):
        val = payload.get(key)
        if isinstance(val, str):
            if val.strip().lower() == "image":
                return True
        elif isinstance(val, (list, tuple, set)):
            if any(str(v).strip().lower() == "image" for v in val):
                return True
    for key in ("image_output", "generate_image", "return_image"):
        if payload.get(key):
            return True
    return False


def _mime_of(part: dict) -> str:
    """Mime dichiarato in una parte di content, in qualunque forma.

    Copre `mime_type` in linea (Gemini `inline_data`), dentro `file`
    (`file.mime_type` / `file.mime`) e la `format` dichiarata nelle forme
    OpenAI (`input_audio.format`, es. "wav")."""
    for key in ("mime_type", "mime"):
        v = part.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    fo = part.get("file")
    if isinstance(fo, dict):
        for key in ("mime_type", "mime"):
            v = fo.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
    ia = part.get("input_audio")
    if isinstance(ia, dict):
        v = ia.get("format")
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    v = part.get("format")
    if isinstance(v, str) and v.strip():
        return v.strip().lower()
    return ""


# Tipi di parte che portano MEDIA di qualsiasi specie, in forma OpenAI o
# Gemini-native. Serve a non confondere `input_audio` con `image_url`.
_MEDIA_PART_TYPES = frozenset({
    "image_url", "input_image", "image", "inline_data", "file",
    "input_audio", "audio_url", "audio", "video_url", "input_video",
})

_AUDIO_PART_TYPES = frozenset({
    "input_audio", "audio_url", "audio", "input_video", "video_url",
})


def _is_audio_part(part: dict) -> bool:
    """True se una parte di `content` e' AUDIO, in qualunque forma.

    Fratello di `_is_image_part` e stessa regola: standard OpenAI
    (`input_audio` con `{data, format}`, `audio_url`) e forme Gemini-native
    (`inline_data` con mime audio/*, `file` con file.mime_type audio/*).
    `inline_data`/`file` con mime generico (`application/octet-stream`) non
    vengono classificati come audio: non c'e' modo onesto di saperlo, e
    sbagliare farebbe scambiare un'immagine per un audio."""
    ptype = part.get("type")
    if ptype in ("input_audio", "audio_url", "audio", "input_video",
                 "video_url"):
        return True
    if ptype in ("inline_data", "file"):
        return _mime_of(part).startswith(("audio/", "video/"))
    return False


def _is_image_part(part: dict) -> bool:
    """True se una parte di `content` e' un'immagine, in QUALSIASI forma.

    Riconosce sia lo standard OpenAI (`image_url`, `input_image`) sia le forme
    Gemini-native (`inline_data` con mime image/*, `file` con file.mime_type
    image/*): stessa sorgente per `required_caps` e `count_image_parts`, cosi'
    la stima dei token non puo' discordare dal routing."""
    ptype = part.get("type")
    if ptype in ("image_url", "input_image", "image"):
        return True
    if ptype in ("inline_data", "file"):
        return _mime_of(part).startswith("image/")
    return False


def count_audio_parts(messages: list[dict] | None) -> int:
    """Conta le parti-AUDIO nei messaggi (per la stima token e lo STT-bridge).

    Stessa fonte di verita' di `_is_audio_part`."""
    if not messages:
        return 0
    count = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and _is_audio_part(part):
                    count += 1
    return count


def audio_bytes_estimate(part: dict) -> int:
    """Byte grezzi di una parte audio, per lo stato di contesto.

    Conta il base64 (4/3 dei byte reali) o, se il dato e' assente ma la parte
    dichiara un formato, stima 32 kB: meglio una stima che un audio da 5 minuti
    invisibile al gate di overflow."""
    if not isinstance(part, dict):
        return 0
    for key in ("data", "b64_json", "audio_data"):
        v = part.get(key)
        if isinstance(v, str) and v:
            return (len(v) * 3) // 4
    for holder in ("input_audio", "audio_url", "audio", "file", "image_url"):
        h = part.get(holder)
        if isinstance(h, dict):
            for key in ("data", "b64_json", "url", "file_data"):
                v = h.get(key)
                if isinstance(v, str) and v:
                    return (len(v) * 3) // 4
        elif isinstance(h, str) and h:
            return (len(h) * 3) // 4
    if _mime_of(part):
        return 32768
    return 0


def count_image_parts(messages: list[dict] | None) -> int:
    """Conta le parti-immagine nei messaggi per la stima token.

    Usa `_is_image_part`, quindi conta anche le forme Gemini-native
    (`inline_data`, `file`) oltre allo standard OpenAI."""
    if not messages:
        return 0
    count = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and _is_image_part(part):
                    count += 1
    return count


def refs_max_for(declared: frozenset[str], hard_max: int) -> int:
    """Numero massimo di immagini di riferimento accettate da un modello.

    I modelli con la capacità `image_multi_ref` accettano più reference (fino
    al tetto di sicurezza `hard_max`); tutti gli altri ne accettano UNA sola
    (si tiene la prima). `hard_max` <= 0 viene trattato come 1.
    """
    hard = int(hard_max) if hard_max and hard_max > 0 else 1
    return hard if "image_multi_ref" in (declared or frozenset()) else 1


# Suffissi/complementi che NON identificano la famiglia del modello: varianti
# di chat/instruct/quantizzazione. Rimossi per far convergere gli alias dei
# diversi provider (es. `meta-llama/llama-3-8b-instruct` == `llama3-8b`).
_FAMILY_NOISE = re.compile(
    r"[-_.](?:instruct|it|chat|base|latest|preview|hf|awq|gptq|int4|int8|fp8|"
    r"fp16|bf16|q[0-9_k]+|gguf|quantized)$")
_PROVIDER_PREFIX = re.compile(
    r"^(?:models/|[a-z0-9_.-]+/)+")       # `nvidia/`, `openai/`, `z-ai/`, `models/`
_TAG_SUFFIX = re.compile(r"[:@].*$")       # `:free`, `:latest`, `@200k`


def canonical_family(model: str) -> str:
    """Riduce un nome modello specifico del provider a un ID di FAMIGLIA
    canonico, confrontabile tra provider diversi.

    Esempi:
      `meta-llama/llama-3-8b-instruct` -> `llama-3-8b`
      `llama3-8b`                      -> `llama-3-8b`
      `nvidia/.../gpt-oss-120b:free`   -> `gpt-oss-120b`
      `deepseek-v4.1-flash`            -> `deepseek-v-4-1-flash`

    Pura e senza I/O: usata per lo sticky same-family e la preservazione
    delle soglie di compattamento quando si cambia provider ma NON modello.
    """
    s = (model or "").strip().lower()
    if not s:
        return ""
    s = _TAG_SUFFIX.sub("", s)
    s = _PROVIDER_PREFIX.sub("", s)
    s = s.replace(".", "-").replace("_", "-")
    # `llama3` / `gemini15` -> `llama-3` / `gemini-15`
    s = re.sub(r"([a-z])(\d)", r"\1-\2", s)
    while True:
        new = _FAMILY_NOISE.sub("", s)
        if new == s:
            break
        s = new
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s