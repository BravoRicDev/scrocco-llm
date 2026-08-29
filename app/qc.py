"""Qualita del contenuto: QC JSON, sanity, watchdog, annotazione D3.

[IT] COSA: decide se una risposta upstream e UTILIZZABILE. WHY tre livelli
separati:
  - check_response: SOLO quando la richiesta voleva JSON (trigger esplicito
    o euristico): valida parse + tool_calls.arguments. Un client che non
    chiede JSON non viene giudicato su euristiche.
  - check_sanity: anti-vuoto generico (min_chars) con eccezioni legittime
    (tool_calls/images senza testo).
  - watchdog STREAMING: passivo (conta chunk, cerca [DONE]) perche in
    stream non si puo bufferizzare senza aggiungere latenza.
  - annotate_reasoning: nota D3 in reasoning_content -- il client che
    ignora quel campo non e toccato; un agente la LEGGE.

[EN] WHAT: response usability checks. WHY: JSON-QC only when requested;
sanity exempts legit empty-content cases; streaming watchdog is passive by
design; D3 notes ride reasoning_content so plain clients are untouched.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

_FENCE_RE = re.compile(r"\s*```(?:json)?\s*[ \t]*\r?\n?(.*?)\r?\n?\s*```\s*",
                       re.DOTALL | re.IGNORECASE)


def wants_json(payload: dict) -> bool:
    """True se il payload chiede esplicitamente un JSON (response_format)."""
    rf = payload.get("response_format")
    if isinstance(rf, dict):
        return str(rf.get("type", "")).lower() in ("json_object", "json_schema")
    if isinstance(rf, str):
        return rf.strip().lower() in ("json_object", "json_schema")
    return False


def looks_like_json(text: Any) -> bool:
    """Euristica: contenuto che inizia letteralmente con '{' o '['."""
    return isinstance(text, str) and text.lstrip()[:1] in ("{", "[")


def strip_fences(text: str) -> str:
    """Estrae il blocco da ```json ... ``` se l'INTERO testo è fenced;
    altrimenti lascia il testo invariato (il parse fallirà in modo onesto)."""
    m = _FENCE_RE.fullmatch(text)
    return m.group(1) if m else text


def check_response(data: dict, payload: dict, qc) -> str | None:
    """Valida la risposta upstream; ritorna il MOTIVO di fallimento o None.

    `qc` è l'oggetto policy.qc_json (attrs enabled/strip_fences/max_attempts).
    """
    if not qc.enabled:
        return None
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return "struttura risposta assente"

    content = msg.get("content")
    has_tools = bool(payload.get("tools"))

    # --- tool_calls: valida SOLO se la richiesta li prevedeva ------------
    if has_tools:
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments")
            if args is None or not isinstance(args, str):
                return "tool_calls.function.arguments non è una stringa"
            if args.strip() == "":
                continue                    # zero-arg: VALIDO, non un errore
            try:
                json.loads(args)
            except ValueError as exc:
                return f"tool_calls.function.arguments JSON non valido ({str(exc)[:60]})"

    # --- contenuto: trigger esplicito O euristico ------------------------
    trigger = wants_json(payload) or looks_like_json(content)
    if not trigger:
        return None
    if content is None:
        # legittimo con tool_calls presenti; sospetto se richiesto JSON esplicito
        if has_tools and msg.get("tool_calls"):
            return None
        return "contenuto mancante"
    if not isinstance(content, str) or not content.strip():
        return "contenuto vuoto"
    text = strip_fences(content) if qc.strip_fences else content
    try:
        json.loads(text)
    except ValueError as exc:
        return f"JSON non valido ({str(exc)[:60]})"
    return None


def check_sanity(data: dict, payload: dict, san) -> str | None:
    """QC generico (non-streaming): scarta SOLO contenuti vuoti/triviali.

    `san` è policy.qc_sanity (enabled, min_chars). Non tocca i casi già
    coperti da check_response (JSON) e rispetta le eccezioni legittime:
    tool_calls senza contenuto e messaggi con immagini allegate.
    """
    if not getattr(san, "enabled", False):
        return None
    try:
        msg = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return "struttura risposta assente"
    content = msg.get("content")
    if content is None:
        if msg.get("tool_calls") or msg.get("images"):
            return None
        return "contenuto mancante"
    # Fallimento silenzioso: il provider riporta usage.completion_tokens == 0
    # (output nullo) pur avendo ricevuto input reale -> risposta vuota "di
    # fatto" anche quando content e' "" o spazi. Quando l'usage c'e' e
    # completion_tokens == 0, QUESTO controllo governa (prende la precedenza
    # sul check generico min_chars qui sotto): su input reale e' un
    # fallimento, su richiesta vuota e' legittimo (e il check min_chars
    # segnalerebbe a torto "contenuto vuoto"). Scartiamo SOLO se il contenuto
    # e' davvero vuoto/assente, per evitare falsi positivi su provider che
    # mentono sul conteggio (es. Cloudflare: prompt_tokens:0 /
    # completion_tokens sballati mentre il testo c'e' davvero).
    u = data.get("usage") if isinstance(data, dict) else None
    if isinstance(u, dict) and u.get("completion_tokens") == 0:
        pt = u.get("prompt_tokens")
        # input davvero vuoto? controlla il payload, non fidarti solo di pt
        req_empty = _request_is_empty(payload)
        if pt in (0, None) and req_empty:
            return None  # richiesta vuota: 0 token e' atteso, niente scarto
        if not msg.get("tool_calls") and not msg.get("images"):
            # se c'e' testo reale nonostante ct==0, il provider mente sul
            # conteggio: NON scartare (evita falsi positivi tipo Cloudflare)
            if not (isinstance(content, str) and content.strip()):
                return "output 0 token (completion_tokens=0)"
        # tool_calls/images presenti -> esenzione legittima, niente scarto
        return None

    min_chars = max(0, int(getattr(san, "min_chars", 1)))
    if isinstance(content, str) and len(content.strip()) < min_chars:
        if not msg.get("tool_calls") and not msg.get("images"):
            return f"contenuto vuoto (<{min_chars} char)"
    return None


def _request_is_empty(payload: dict) -> bool:
    """True se la richiesta non ha di fatto input testuale (prompt vuoto)."""
    try:
        msgs = payload.get("messages") or []
        for m in msgs:
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return False
            if isinstance(c, list):
                for part in c:
                    if isinstance(part, dict):
                        t = part.get("text") or part.get("input_text")
                        if isinstance(t, str) and t.strip():
                            return False
                    elif isinstance(part, str) and part.strip():
                        return False
        # anche un solo "input"/"prompt" non vuoto conta
        for k in ("input", "prompt"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return False
        return True
    except Exception:
        return False


def annotate_reasoning(data: dict, failed: list[tuple[str, str]]) -> dict:
    """Prepende la nota (D3) al reasoning della risposta consegnata.

    Copia SOLO il messaggio (mai mutare strutture condivise dai test);
    crea `reasoning_content` se assente; preserva il reasoning del provider.
    """
    lines = [f"Il modello `{u}` ha passato un json non valido ({reason}); "
             f"proviamo con uno migliore." for u, reason in failed]
    if not lines:
        return data
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError):
        return data
    msg = dict(choice.get("message") or {})
    nota = "\n".join(lines)
    prev = msg.get("reasoning_content")
    msg["reasoning_content"] = nota if not prev else f"{nota}\n{prev}"
    choice = dict(choice)
    choice["message"] = msg
    data = copy.copy(data)
    data["choices"] = list(data.get("choices", []))
    data["choices"][0] = choice
    return data