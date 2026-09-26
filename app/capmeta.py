"""Proiezioni delle capability del gateway nei formati che i client leggono.

Non esiste uno standard: l'oggetto `Model` di OpenAI ha solo
`id/created/object/owned_by/shutdown_date` (cfr. `openai/types/model.py`), senza
`capabilities` ne' `modalities`. Ogni famiglia di client ne ha quindi inventato
uno, e un gateway multi-provider che vuole essere scoperto bene li pubblica
tutti. Qui le produciamo da UNA sola fonte di verita': le capability del gateway
(`capabilities.py`).

Formati coperti, con la fonte da cui derivano:
  * Ollama    `capabilities: [str]` — enum `completion, tools, insert, vision,
    embedding, thinking, image, audio` (types/model/capability.go).
  * OpenRouter `architecture.input_modalities` / `output_modalities` + `supported_parameters`.
    NB: il suo enum di output contiene GIA' `decisions` (oltre a `speech`,
    `transcription`, `image`, `video`, `embeddings`, `rerank`).
  * Anthropic forma strutturata `capabilities: {cap: {"supported": bool}}`.
  * LiteLLM   `supports_*` + `supported_endpoints` + `supported_output_modalities`.
  * llama.cpp `modalities`.

`capabilities` e' un SUPERSET: prima i valori dell'enum Ollama (cosi' un client
Ollama trova i nomi che conosce), poi i token nostri (`decision`, `video_gen`,
...). Il tipo Go di Ollama e' `[]Capability` = `[]string` senza validazione, quindi
i valori extra sono inerti per i client che filtrano per appartenenza.

Attenzione sul significato: `capabilities` elenca le capability INSTRADABILI per
quel nome, non "tutto cio' che il modello sa fare". I bucket sono mutuamente
esclusivi per design (una riga senza `text` non entra nei gruppi testo), quindi
`...-vision` non dichiara `text`. Sul NOME BASE l'unione e' completa: e' la
risposta alla domanda "cosa sa fare questo gateway".
"""
from __future__ import annotations

# capability -> valore dell'enum Ollama (None = non mappabile)
OLLAMA: dict[str, str | None] = {
    "text": "completion",
    "vision": "vision",
    "audio": "audio",
    "video": None,             # l'enum Ollama non prevede 'video'
    "image_gen": "image",
    "image_edit": "image",
    "image_multi_ref": "image",
    "video_gen": None,
    "tts": "audio",
    "stt": "audio",
    "decision": None,          # nostro: non esiste in nessun enum altrui
    "tools": "tools",
}

# capability -> modality di input accettate (enum OpenRouter InputModality)
IN_MODALITIES: dict[str, tuple[str, ...]] = {
    "text": ("text",),
    "vision": ("text", "image"),
    "audio": ("text", "audio"),
    "video": ("text", "video"),
    "image_gen": ("text",),
    "image_edit": ("text", "image"),
    "image_multi_ref": ("text", "image"),
    "video_gen": ("text", "image"),
    "tts": ("text",),
    "stt": ("text", "audio"),
    "decision": ("text",),
    "tools": (),
}

# capability -> modality di output prodotte (enum OpenRouter OutputModality)
OUT_MODALITIES: dict[str, tuple[str, ...]] = {
    "text": ("text",),
    "vision": ("text",),
    "audio": ("text",),
    "video": ("text",),
    "image_gen": ("image",),
    "image_edit": ("image",),
    "image_multi_ref": ("image",),
    "video_gen": ("video",),
    "tts": ("audio", "speech"),
    "stt": ("text", "transcription"),
    "decision": ("decisions",),
    "tools": (),
}

# capability -> endpoint del gateway che la serve
ENDPOINTS: dict[str, tuple[str, ...]] = {
    "text": ("/v1/chat/completions",),
    "vision": ("/v1/chat/completions",),
    "audio": ("/v1/chat/completions", "/v1/audio/transcriptions"),
    "video": ("/v1/chat/completions",),
    "image_gen": ("/v1/images/generations",),
    "image_edit": ("/v1/images/edits",),
    "image_multi_ref": ("/v1/images/edits",),
    "video_gen": ("/v1/videos/generations",),
    "tts": ("/v1/audio/speech",),
    "stt": ("/v1/audio/transcriptions", "/v1/audio/translations"),
    "decision": ("/v1/systemone",),
    "tools": ("/v1/chat/completions",),
}

# ordine canonico dei parametri (stile OpenRouter `supported_parameters`)
_PARAMS_ORDER: tuple[str, ...] = (
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop",
    "seed", "stream", "response_format", "structured_outputs",
    "reasoning_effort", "tools", "tool_choice",
)
_CHAT_ONLY_PARAMS: frozenset[str] = frozenset({"tools", "tool_choice"})
_CHAT = "/v1/chat/completions"


def _uniq(seq) -> list[str]:
    out: list[str] = []
    for x in seq:
        if x not in out:
            out.append(x)
    return out


def ollama_capabilities(caps) -> list[str]:
    """Enum Ollama (prima) + token nostri (poi). Superset, nessuna perdita."""
    caps = set(caps or ())
    mapped = _uniq(OLLAMA[c] for c in sorted(caps) if OLLAMA.get(c))
    return _uniq(mapped + sorted(caps))


def modalities(caps) -> dict:
    """Forma OpenRouter: input/output_modalities + la stringa `modality`."""
    caps = set(caps or ())
    ins = _uniq(m for c in sorted(caps) for m in IN_MODALITIES.get(c, ()))
    outs = _uniq(m for c in sorted(caps) for m in OUT_MODALITIES.get(c, ()))
    return {"input_modalities": ins, "output_modalities": outs,
            "modality": f"{'+'.join(ins)}->{'+'.join(outs)}"}


def supported_endpoints(caps) -> list[str]:
    """Endpoint del gateway che servono ciascuna capability: COME si usa."""
    caps = set(caps or ())
    return _uniq(e for c in sorted(caps) for e in ENDPOINTS.get(c, ()))


def serves_chat(caps) -> bool:
    return _CHAT in supported_endpoints(caps)


def supported_parameters(caps) -> list[str]:
    """Parametri realmente onorati dal gateway per questo nome."""
    caps = set(caps or ())
    chat = serves_chat(caps)
    return [p for p in _PARAMS_ORDER
            if chat or p not in _CHAT_ONLY_PARAMS]


def structured_style(caps) -> dict:
    """Forma strutturata vendor (Anthropic): {cap: {"supported": bool}}."""
    return {c: {"supported": True} for c in sorted(set(caps or ()))}


def litellm_style(caps) -> dict:
    """Dizionario capability stile LiteLLM (`/model/info`)."""
    caps = set(caps or ())
    mods = modalities(caps)
    return {
        "capabilities": ollama_capabilities(caps),
        "supports_vision": "vision" in caps,
        "supports_function_calling": serves_chat(caps),
        "supports_response_schema": serves_chat(caps),
        "supports_structured_output": serves_chat(caps),
        "supports_audio_input": bool(caps & {"audio", "stt"}),
        "supports_audio_output": "tts" in caps,
        "supports_image_generation": "image_gen" in caps,
        "supports_video_input": "video" in caps,
        "supports_video_generation": "video_gen" in caps,
        "supports_decisions": "decision" in caps,
        "supported_endpoints": supported_endpoints(caps),
        "supported_modalities": mods["input_modalities"],
        "supported_output_modalities": mods["output_modalities"],
    }


def llama_cpp_modalities(caps) -> dict:
    """Forma llama.cpp (`/props`): dict di modalita' con default espliciti."""
    caps = set(caps or ())
    mods = modalities(caps)
    return {"vision": "image" in mods["input_modalities"],
            "audio": "audio" in mods["input_modalities"],
            "image": "image" in mods["output_modalities"],
            "video": "video" in mods["output_modalities"]}


def context_lengths(deps) -> tuple[int, int]:
    """(max, min) dei `max_input_tokens` dei deployment del nome.

    `context_length` = MAX (il routing scala le dim, quindi la finestra piu'
    ampia e' raggiungibile); il MIN e' il pavimento garantito su ogni member.
    """
    vals = [int(d.get("max_input_tokens") or 0) for d in deps or ()]
    vals = [v for v in vals if v > 0]
    if not vals:
        return 0, 0
    return max(vals), min(vals)


def usage_hint(caps) -> dict:
    """Esempio di invocazione per capability non-chat (cap -> endpoint)."""
    caps = set(caps or ())
    hint: dict = {}
    if "decision" in caps:
        hint["decision"] = {
            "endpoint": "/v1/systemone",
            "method": "POST",
            "body": {"model": "<modello>", "state": "testo da valutare",
                     "questions": {"esito": {"type": "choice",
                                             "instructions": "la domanda",
                                             "criteria": {"si": "positivo",
                                                          "no": "negativo"}},
                                   "urgenza": {"type": "noul",
                                               "instructions": "e' urgente?"}}},
            "response": "{model, answers: {<chiave>: {...}}, usage}",
        }
    for cap in ("image_gen", "image_edit", "image_multi_ref", "video_gen",
                "tts", "stt", "audio", "text", "vision", "video", "tools"):
        if cap in caps and cap != "decision":
            eps = [e for e in ENDPOINTS.get(cap, ()) if e != _CHAT]
            if eps:
                hint[cap] = {"endpoint": eps[0], "method": "POST"}
    return hint
