"""Pulizia delle allucinazioni note dei backend STT (Whisper).

I modelli Whisper (in particolare `large-v3`/`turbo`, quindi sia Groq sia i
server Speaches self-hosted) su silenzio, rumore o audio non parlato inventano
talvolta credit di sottotitolaggio — il piu' noto in italiano e'
"Sottotitoli e revisione a cura di QTSS". Spesso la frase viene anche
*riecheggiata* dal `prompt` (contesto dei chunk precedenti) e torna in output
parziale, es. "e revisione a cura di QTSS".

Questi testi non sono parlato: vengono rimossi dalla risposta prima di
restituirla al client, qualunque sia il backend. Il filtro e' volutamente ad
ALTA precisione (richiede la parola "sottotitoli"/"revisione" o un creditore
noto) per non toccare dettatura reale.
"""
from __future__ import annotations

import re

# creditori di sottotitoli noti (case-insensitive)
_PROPER = r"(?:qtss|whisper|amara(?:\.org)?|opensubtitles(?:\.org)?)"

_HALLUCINATION_RE = re.compile(
    r"(?i)\b(?:"
    # "Sottotitoli [e revisione] a cura di [QTSS|Whisper|...]"
    r"sottotitoli(?:\s+e\s+revisione)?\s+a\s+cura\s+di(?:\s+" + _PROPER + r")?"
    # "e revisione a cura di QTSS" (output parziale / riecheggiato dal prompt)
    r"|(?:e\s+)?revisione\s+a\s+cura\s+di\s+" + _PROPER +
    # "a cura di QTSS" (frammento senza l'intestazione)
    r"|a\s+cura\s+di\s+" + _PROPER +
    r")\.?"
)

# caratteri orfani che possono restare ai bordi dopo la rimozione
_EDGE_JUNK = " \t\r\n.,;:!?…-–—»«\"'`[](){}"


def scrub_text(text: str) -> tuple[str, int]:
    """Rimuove le allucinazioni di credit da `text`.

    Ritorna `(testo_pulito, n_rimosse)`. Se non c'e' nulla da rimuovere il
    testo e' restituito invariato e `n_rimosse == 0`.
    """
    if not text or not isinstance(text, str):
        return text, 0
    new, n = _HALLUCINATION_RE.subn(" ", text)
    if not n:
        return text, 0
    new = re.sub(r"[ \t]{2,}", " ", new)
    new = re.sub(r"\s+([,.;:!?…])", r"\1", new)
    new = new.strip()
    if not new.strip(_EDGE_JUNK):
        new = ""
    return new, n


def scrub_payload(result):
    """Pulisce una risposta STT (dict `{text,segments[]}` o str srt/vtt/testo).

    Ritorna `(payload, n_rimosse)`. Muta il dict in place per text/segments;
    per le stringhe restituisce una nuova stringa senza le righe-allucinazione.
    """
    if isinstance(result, dict):
        total = 0
        if isinstance(result.get("text"), str):
            result["text"], n = scrub_text(result["text"])
            total += n
        segments = result.get("segments")
        if isinstance(segments, list):
            for seg in segments:
                if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                    seg["text"], n = scrub_text(seg["text"])
                    total += n
        return result, total
    if isinstance(result, str):
        out: list[str] = []
        total = 0
        for line in result.splitlines():
            line, n = scrub_text(line)
            total += n
            if line.strip():
                out.append(line)
        return "\n".join(out), total
    return result, 0
