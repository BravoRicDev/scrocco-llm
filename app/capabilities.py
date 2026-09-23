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
                elif ptype == "video_url":
                    need.add("video")
                elif ptype == "file":
                    file_obj = part.get("file") or {}
                    mime = (file_obj.get("mime_type") or file_obj.get("mime") or "").lower()
                    if mime.startswith("video/"):
                        need.add("video")
                elif ptype == "inline_data":
                    mime = (part.get("mime_type") or part.get("mime") or "").lower()
                    if mime.startswith("video/"):
                        need.add("video")
                # input_text, text, reasoning, tool_calls, etc. -> ignorati
    return frozenset(need)


def count_image_parts(messages: list[dict] | None) -> int:
    """Conta le parti-immagine nei messaggi per la stima token."""
    if not messages:
        return 0
    count = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("image_url", "input_image"):
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