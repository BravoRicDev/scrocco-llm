"""STT-bridge: audio nelle chat -> testo per un LLM qualsiasi.

Il problema. Un client puo' mandare audio dentro /v1/chat/completions. I
modelli multimodali non lo gestiscono in modo affidabile: ho misurato in
produzione che su 7 modelli con cap `audio` la forma standard `input_audio`
non dava nessuna trascrizione (2 provider 402 "richiede $0.50 per audio",
`inkling-small` descriveva l'audio invece di trascriverlo, `nemotron` si
rifiutava). Solo 3 modelli dichiarano `stt`, e sono whisper: fatti per
trascrivere, con risposta garantita e testo pulito.

La regola scelta e' quindi STT SEMPRE: l'audio diventa testo PRIMA del
routing, e l'LLM risponde sul testo. Questo elimina la classe di risposte
"ho descritto/immaginato l'audio" e la rende dipendente da 3 deployment
affidabili invece che da 72 inaffidabili.

Le tre regole che questo modulo rispetta:

1. **Ancoraggio alla finestra di compattazione.** La trascrizione (e la sua
   sostituzione testuale) vale per i turni che `ctxcompact.frontier_boundary`
   TENEVE, non per un contatore di turni separato: una sola nozione di "turno
   protetto", che non puo' divergere da quella della compattazione. Un audio
   che esce dalla finestra viene rimosso SENZA traduzione (mandare base64 a
   un LLM solo-testo darebbe 400) e senza marker: non e' un fallimento, e'
   l'ancoraggio che funziona.

2. **Mai 503 al client.** Se l'STT fallisce, la richiesta continua: al posto
   dell'audio va un marker che dice all'LLM cosa non ha potuto ascoltare,
   cosi' lo cita in risposta invece di inventare. Il marker e' una stringa da
   policy (non contenuto utente), quindi non e' iniettabile.

3. **Audio + testo non si perdono.** La trascrizione viene ACCORPATA al testo
   del messaggio che contiene l'audio, non messa in un canale separato:
   `chat_prompt_and_refs` (usato da /images) prende solo l'ultimo user, e una
   trascrizione in un blocco a parte verrebbe ignorata o sostituita.

I chunk seguono `audioconvert.split_pcm` (durata target, confine bilanciato,
aggancio al silenzio) e vengono sparati in parallelo chiedendo al router
deployment freschi con `exclude` progressivo: la freschezza e la penalizzazione
dell'ultimo usato le fa gia' `pick_deployment`, qui non si duplica nulla.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
from typing import Any

from . import audioconvert
from . import audiostore

# Parti che contengono i byte dell'audio, per ogni forma riconosciuta.
# `_is_audio_part` decide QUALI parti sono audio; qui si estrae il dato.
_AUDIO_DATA_KEYS = ("data", "b64_json", "audio_data", "file_data")


def _part_bytes(part: dict) -> tuple[bytes, str]:
    """(byte grezzi, formato dichiarato) di una parte audio.

    Solleva ValueError se la parte non porta dati utilizzabili: il chiamante
    la tratta come audio non trascrivibile."""
    if not isinstance(part, dict):
        raise ValueError("parte audio non e' un dict")
    # 1) forma OpenAI: {"type":"input_audio","input_audio":{"data","format"}}
    ia = part.get("input_audio")
    if isinstance(ia, dict):
        d = ia.get("data")
        if not isinstance(d, str) or not d:
            raise ValueError("input_audio senza dati")
        return _b64(d), str(ia.get("format") or "")
    # 2) {"type":"audio_url","audio_url":{"url"|"data": "data:audio/wav;base64,..."}}
    au = part.get("audio_url")
    if isinstance(au, dict):
        d = au.get("data") or au.get("url")
        if not isinstance(d, str) or not d:
            raise ValueError("audio_url senza dati")
        return _b64(d), str(au.get("format") or "")
    # 3) {"type":"inline_data","mime_type":"audio/wav","data":"..."}
    # 4) {"type":"file","file":{"mime_type":...,"file_data":"..."}}
    fo = part.get("file") if isinstance(part.get("file"), dict) else {}
    for holder in (part, fo):
        for key in _AUDIO_DATA_KEYS:
            v = holder.get(key)
            if isinstance(v, str) and v:
                return _b64(v), str(holder.get("format") or "")
    # 5) {"type":"audio","audio":"data:audio/ogg;base64,..."}
    a = part.get("audio")
    if isinstance(a, str) and a:
        return _b64(a), ""
    if isinstance(a, dict):
        d = a.get("data") or a.get("url")
        if isinstance(d, str) and d:
            return _b64(d), str(a.get("format") or "")
    raise ValueError("nessun dato audio nella parte")


def _b64(value: str) -> bytes:
    """base64 -> byte, tollerante al data-URI e agli spazi di wrapping."""
    s = value.strip()
    if s.startswith("data:"):
        s = s.split(",", 1)[1] if "," in s else ""
    s = "".join(s.split())
    try:
        return base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"base64 non valido: {e}") from e


def audio_parts_in(messages: list) -> list[tuple[int, int, dict]]:
    """(indice_messaggio, indice_parte, parte) di ogni parte audio."""
    from .capabilities import _is_audio_part           # noqa: PLC0415
    out: list[tuple[int, int, dict]] = []
    for i, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for j, part in enumerate(content):
            if isinstance(part, dict) and _is_audio_part(part):
                out.append((i, j, part))
    return out


def _keep_from(messages: list, boundary: int | None) -> int:
    """Indice da cui la history e' considerata 'ancorata'.

    Stessa semantica di `ctxcompact.frontier_boundary`, ma ridotta all'indice:
    il confine condiviso garantisce che trascrizione e compattazione non
    possano divergere. `boundary=None` (nessun turno utente) -> 0."""
    if boundary is None:
        return 0
    return max(0, int(boundary))


async def _transcribe_chunks(chunks: list[bytes], *, transcript_one,
                             max_parallel: int) -> list[str]:
    """Trascrive i chunk in parallelo, riordinati per indice.

    `transcript_one(chunk, idx) -> str` e' fornito dal chiamante (il main.py),
    cosi' la scelta del deployment resta nel router e questo modulo non sa
    nulla di routing, cooldown o policy STT.

    L'ordine e' garantito da asyncio.gather: i risultati restano all'indice del
    chunk, quindi il testo si ricompone file1 file2 file3 anche se i chunk
    finiscono in ordine diverso. Il parallelismo e' limitato a max_parallel
    con un semaforo: su un file da un'ora (20 chunk) non vanno 20 chiamate
    STT insieme, sennò l'upstream le quota tutte insieme."""
    if not chunks:
        return []
    if max_parallel <= 0:
        max_parallel = 1

    sem = asyncio.Semaphore(max_parallel)

    async def _one(idx: int, chunk: bytes):
        async with sem:
            return idx, await transcript_one(chunk, idx)

    results = await asyncio.gather(
        *(_one(i, c) for i, c in enumerate(chunks)),
        return_exceptions=True)
    out: list[str] = [""] * len(chunks)
    for i, r in enumerate(results):
        if isinstance(r, BaseException):
            out[i] = ""              # quel chunk fallito: testo vuoto
            continue
        idx, text = r
        if 0 <= idx < len(out):
            out[idx] = text
    return out


def _join_texts(texts: list[str]) -> str:
    """Unisce le trascrizioni dei chunk, scartando i vuoti."""
    return " ".join(t.strip() for t in texts if t and t.strip())


def _marker(policy_notice: str, why: str) -> str:
    """Il testo che rientra al posto dell'audio quando l'STT non ha potuto.

    Stringa da POLICY, non contenuto utente: nessun audio puo' iniettare
    istruzioni qui dentro. Dice all'LLM cosa non ha potuto ascoltare, cosi' lo
    cita in risposta invece di rispondere come se avesse sentito."""
    return f"[audio non trascritto: {why} {policy_notice}]".strip()


def _text_block(policy_notice: str, why: str) -> dict:
    return {"type": "text", "text": _marker(policy_notice, why)}


def transcript_block(text: str) -> dict:
    """Parte di testo con la trascrizione, delimitata cosi' l'LLM la distingue.

    Il delimitatore serve anche da confine di fiducia: l'audio e' dato
    dell'utente, la trascrizione e' output di un modello whisper che potrebbe
    essere stato ingannato da un audio ostile. Marcarla come citazione rende
    la cosa esplicita invece di lasciarla indistinguibile da un'istruzione."""
    return {"type": "text", "text": f'trascrizione audio: "{text}"'}


async def resolve_audio_in_payload(payload: dict, *, boundary: int | None,
                                   transcript_one, policy) -> dict:
    """Sostituisce l'audio nella history con la trascrizione (o con il marker).

    Ritorna un NUOVO payload; quello del chiamante resta intatto, coerente
    con il copy-on-write del tetto immagini. Se il payload non contiene audio
    o lo STT e' disattivato, lo restituisce invariato (identita').

    Percorso per ogni parte audio, in ordine:
      - indice < confine: FUORI finestra -> rimossa senza traduzione e senza
        marker (non e' un fallimento: e' l'ancoraggio);
      - cache HIT: nessuna chiamata STT;
      - cache MISS: normalizzazione (audioconvert) -> chunk in parallelo ->
        testo; un errore di conversione o di STT produce il MARKER, mai un
        errore al client.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return payload
    if not getattr(policy, "stt_chat_enabled", True):
        return payload                      # kill-switch: comportamento di prima
    spots = audio_parts_in(messages)
    if not spots:
        return payload

    keep_from = _keep_from(messages, boundary)
    notice = str(getattr(policy, "stt_chat_unavailable_notice", "") or "")
    target_sec = float(getattr(policy, "stt_chat_target_sec", 180) or 180)
    search_pct = float(getattr(policy, "stt_chat_search_pct", 0.2) or 0.2)
    max_parallel = int(getattr(policy, "stt_chat_max_parallel", 4) or 4)
    max_bytes = int(getattr(policy, "stt_chat_max_bytes",
                            7 * 1024 * 1024) or 0)

    # 1) raccolta dei dati da trascrivere, e preparazione delle sostituzioni
    replacements: dict[tuple[int, int], list[dict]] = {}
    for (mi, pi, part) in spots:
        if mi < keep_from:
            replacements[(mi, pi)] = []      # fuori finestra: sparisce
            continue
        try:
            raw, fmt = _part_bytes(part)
        except ValueError:
            replacements[(mi, pi)] = [
                _text_block(notice, "formato non leggibile.")]
            continue
        key = audiostore.key_for(raw)
        hit = audiostore.get(key)
        if hit:
            replacements[(mi, pi)] = [transcript_block(hit)]
            continue
        try:
            chunks = audioconvert.to_ogg_chunks(
                raw, fmt_hint=fmt, target_sec=target_sec,
                search_pct=search_pct)
        except audioconvert.AudioConversionError:
            replacements[(mi, pi)] = [
                _text_block(notice, "formato audio non leggibile.")]
            continue
        except Exception:                    # noqa: BLE001
            # mai propagare: un audio non decodificabile non deve portare via
            # la richiesta, solo il marker.
            replacements[(mi, pi)] = [
                _text_block(notice, "conversione non riuscita.")]
            continue
        # Tetto di byte DOPO la normalizzazione, non prima. Applicarlo
        # all'ingresso grezzo ucciderebbe audio valido: 7 minuti di WAV sono
        # 13 MB, ma diventano ~1,5 MB in OGG. Il tetto serve a fermare i chunk
        # che escono, non a rifiutare un file che la compressione riduce.
        if max_bytes:
            oversize = [i for i, c in enumerate(chunks) if len(c) > max_bytes]
            if oversize:
                replacements[(mi, pi)] = [
                    _text_block(
                        notice,
                        f"file troppo grande dopo compressione "
                        f"({sum(len(chunks[i]) for i in oversize)} byte).")]
                continue
        texts = await _transcribe_chunks(
            chunks, transcript_one=transcript_one, max_parallel=max_parallel)
        joined = _join_texts(texts)
        if not joined:
            replacements[(mi, pi)] = [
                _text_block(notice, "nessun modello STT ha risposto.")]
            continue
        audiostore.put(key, joined)
        replacements[(mi, pi)] = [transcript_block(joined)]

    # 2) ricostruzione: la trascrizione e' ACCORPATA al testo del messaggio.
    #    Un blocco di testo separato verrebbe perso da chi legge solo il primo
    #    testo (es. `chat_prompt_and_refs`), quindi qui si unisce.
    out_msgs: list = []
    for mi, msg in enumerate(messages):
        if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
            out_msgs.append(msg)
            continue
        rows = [(pi, part) for (m, pi, part) in spots if m == mi]
        if not rows:
            out_msgs.append(msg)
            continue
        by_pi = {pi: rep for (m, pi), rep in replacements.items() if m == mi}
        new_content: list[dict] = []
        for pi, part in enumerate(msg["content"]):
            if pi not in by_pi:
                new_content.append(part)
                continue
            for rep in by_pi[pi]:
                if not rep:
                    continue                    # fuori finestra: rimossa
                if rep.get("type") == "text":
                    # accorpa al testo esistente nello stesso messaggio
                    _merge_text(new_content, rep["text"])
                else:
                    new_content.append(rep)
        out_msgs.append({**msg, "content": new_content})
    out = dict(payload)
    out["messages"] = out_msgs
    return out


def _merge_text(parts: list[dict], text: str) -> None:
    """Aggiunge `text` alle parti di testo del blocco, creandola se assente.

    Accorpare e' cio' che impedisce di perdere testo e trascrizione: restano
    nello stesso `content`, e qualunque lettore che prenda il testo del
    messaggio li vede entrambi."""
    for p in parts:
        if isinstance(p, dict) and p.get("type") in ("text", "input_text",
                                                     "output_text"):
            cur = p.get("text")
            if isinstance(cur, str) and cur.strip():
                p["text"] = f"{cur}\n\n{text}"
            else:
                p["text"] = text
            return
    parts.append({"type": "text", "text": text})

