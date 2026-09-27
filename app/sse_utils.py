"""SSE streaming helpers extracted from app/main.py (M1).

Contiene le funzioni di utilità per lo streaming SSE che erano
inizialmente in app/main.py. Spostate qui per ridurre la dimensione
di main.py e migliorare la manutenibilità.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from .forwarder import is_embedded_provider_error
from .suppressed import report_suppressed

log = logging.getLogger("nx.sse_utils")


# --------------------------------------------------- streaming helpers (TASK D2)
def _sse_data_objs(chunk: bytes):
    """Yield gli oggetti JSON dei righi 'data: {...}' in un chunk SSE
    (salta [DONE] e i rigi non-JSON)."""
    for line in chunk.split(b"\n"):
        s = line.strip()
        if not s.startswith(b"data:"):
            continue
        body = s[5:].strip()
        if body == b"[DONE]" or not body:
            continue
        try:
            yield json.loads(body)
        except Exception:
            continue


def _merge_qc_tool_calls(chunk: bytes):
    """Assembla le tool-call di uno stream SSE bufferizzato UNENDO i frammenti
    per `index` (come fa OpenAI in streaming).

    Necessario per il QC: gli argomenti di una tool-call arrivano in molti
    delta; validarli frammento per frammento da' falsi positivi ("Unterminated
    string" sul primo pezzo `{"`). Qui si uniscono in un array di tool-call
    complete, pronte per `check_response`.
    """
    from .protocols import _merge_tool_call

    acc: dict[int, dict] = {}
    for obj in _sse_data_objs(chunk):
        for ch in (obj.get("choices") or []) if isinstance(obj, dict) else []:
            d = ch.get("delta") if isinstance(ch, dict) else None
            tc = d.get("tool_calls") if isinstance(d, dict) else None
            for one in tc or []:
                _merge_tool_call(acc, one)
    return [acc[k] for k in sorted(acc)]


def _strip_sse_content(chunk: bytes, stripper) -> bytes:
    """STRIP dei marker di template (Nemotron/Ling) da `delta.content` in SSE.

    GARANZIA: i marker di tool-call testuali non devono MAI raggiungere il
    client. Il `stripper` e' stateful (gestisce token spezzati tra chunk).
    Il `reasoning_content` NON viene toccato.
    """
    if not isinstance(chunk, bytes) or b'"content"' not in chunk:
        return chunk
    out_lines = []
    changed = False
    for line in chunk.split(b"\n"):
        st = line.strip()
        if not st.startswith(b"data:"):
            out_lines.append(line)
            continue
        body = st[5:].strip()
        if not body or body == b"[DONE]":
            out_lines.append(line)
            continue
        try:
            obj = json.loads(body)
        except Exception:
            out_lines.append(line)
            continue
        hit = False
        terminal = False
        for ch in (obj.get("choices") or []) if isinstance(obj, dict) else []:
            if not isinstance(ch, dict):
                continue
            if ch.get("finish_reason"):
                terminal = True
            d = ch.get("delta")
            if isinstance(d, dict) and isinstance(d.get("content"), str):
                new = stripper.feed(d["content"])
                if new != d["content"]:
                    d["content"] = new
                    hit = True
        if terminal and getattr(stripper, "tail", None):
            flushed = stripper.flush()
            if flushed:
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") if isinstance(ch, dict) else None
                    if isinstance(d, dict) and isinstance(d.get("content"), str):
                        d["content"] = d["content"] + flushed
                        hit = True
                        break
                else:
                    try:
                        obj["choices"][0].setdefault("delta", {})["content"] = flushed
                        hit = True
                    except Exception:
                        report_suppressed("sse_utils._strip_sse_content")
        if hit:
            out_lines.append(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8"))
            changed = True
        else:
            out_lines.append(line)
    return b"\n".join(out_lines) if changed else chunk


def _collapse_sse_field(chunks, field: str, text):
    """Riscrive il campo `field` (content/reasoning_content) dei delta di uno
    stream SSE (choices[0]) con `text`.

    Usato dopo la pulizia/riparazione in HOLD: la risposta e' gia' completa in
    `chunks`, quindi si sostituisce il valore originale con quello sanificato,
    preservando finish_reason, usage e [DONE]. Il testo viene scritto una sola
    volta (la prima occorrenza del campo); le successive diventano vuote.
    Ritorna una NUOVA lista di chunk.
    """
    if not isinstance(text, str) or not chunks:
        return chunks
    key = ('"%s"' % field).encode()
    out = []
    placed = False
    for chunk in chunks:
        if not isinstance(chunk, bytes) or key not in chunk:
            out.append(chunk)
            continue
        changed = False
        new_lines = []
        for line in chunk.split(b"\n"):
            st = line.strip()
            if not st.startswith(b"data:"):
                new_lines.append(line)
                continue
            body = st[5:].strip()
            if not body or body == b"[DONE]":
                new_lines.append(line)
                continue
            try:
                obj = json.loads(body)
            except Exception:
                new_lines.append(line)
                continue
            hit = False
            chs = obj.get("choices") if isinstance(obj, dict) else None
            ch0 = chs[0] if isinstance(chs, list) and chs else None
            d = ch0.get("delta") if isinstance(ch0, dict) else None
            if isinstance(d, dict) and isinstance(d.get(field), str):
                d[field] = text if not placed else ""
                placed = True
                hit = True
            if hit:
                new_lines.append(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8"))
                changed = True
            else:
                new_lines.append(line)
        out.append(b"\n".join(new_lines) if changed else chunk)
    if not placed:
        try:
            synth = {"choices": [{"index": 0, "delta": {field: text}, "finish_reason": None}]}
            out.insert(0, b"data: " + json.dumps(synth, ensure_ascii=False).encode("utf-8") + b"\n\n")
        except Exception:  # noqa: BLE001
            report_suppressed("sse_utils._collapse_sse_field")
    return out


def _collapse_sse_content(chunks, text):
    """Riscrive l'intero content di uno stream SSE (choices[0]) con `text`.

    Usato dopo la pulizia/riparazione dell'output STRUTTURATO in HOLD: la
    risposta e' gia' completa in `chunks`, quindi si sostituisce il contenuto
    originale (che espone JSON sporco) con quello sanificato, preservando
    finish_reason, usage e [DONE]. Ritorna una NUOVA lista di chunk.
    """
    return _collapse_sse_field(chunks, "content", text)


def _rewrite_sse_tool_calls(chunks, tool_calls):
    """Riscrive i tool_calls di uno stream SSE bufferizzato (HOLD).

    Rimuove i delta `tool_calls` originali e ne inserisce uno sintetico con
    l'array riparato (formato chat.completion, con `index`), nel punto del
    primo. Preserva finish_reason, usage e [DONE]. Ritorna una NUOVA lista.
    """
    if not isinstance(tool_calls, list) or not tool_calls or not chunks:
        return chunks
    tcs = [dict(tc, index=i) for i, tc in enumerate(tool_calls) if isinstance(tc, dict)]
    if not tcs:
        return chunks
    out = []
    inserted = False
    for chunk in chunks:
        if not isinstance(chunk, bytes) or b'"tool_calls"' not in chunk:
            out.append(chunk)
            continue
        changed = False
        new_lines = []
        for line in chunk.split(b"\n"):
            st = line.strip()
            if not st.startswith(b"data:"):
                new_lines.append(line)
                continue
            body = st[5:].strip()
            if not body or body == b"[DONE]":
                new_lines.append(line)
                continue
            try:
                obj = json.loads(body)
            except Exception:
                new_lines.append(line)
                continue
            chs = obj.get("choices") if isinstance(obj, dict) else None
            ch0 = chs[0] if isinstance(chs, list) and chs else None
            d = ch0.get("delta") if isinstance(ch0, dict) else None
            if isinstance(d, dict) and "tool_calls" in d:
                if not inserted:
                    d["tool_calls"] = tcs
                    inserted = True
                    new_lines.append(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8"))
                else:
                    d.pop("tool_calls", None)
                    if d:
                        new_lines.append(b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8"))
                changed = True
                continue
            new_lines.append(line)
        out.append(b"\n".join(new_lines) if changed else chunk)
    if not inserted:
        try:
            synth = {"choices": [{"index": 0, "delta": {"tool_calls": tcs}, "finish_reason": None}]}
            out.insert(0, b"data: " + json.dumps(synth, ensure_ascii=False).encode("utf-8") + b"\n\n")
        except Exception:  # noqa: BLE001
            report_suppressed("sse_utils._rewrite_sse_tool_calls")
    return out


def _delta_has_content(obj) -> bool:
    """True se un oggetto chunk OpenAI-style porta contenuto reale
    (answer, reasoning o tool_call)."""
    if not isinstance(obj, dict):
        return False
    for ch in obj.get("choices") or []:
        d = ch.get("delta") or ch.get("message") or {}
        if not isinstance(d, dict):
            continue
        c = d.get("content")
        if isinstance(c, str) and c.strip():
            return True
        if isinstance(c, list) and c:
            return True
        rc = d.get("reasoning_content") or d.get("reasoning")
        if isinstance(rc, str) and rc.strip():
            return True
        if d.get("tool_calls"):
            return True
    return False


def _answer_chars(obj) -> int:
    """Solo il testo di risposta (NON reasoning) per il verdetto finale C."""
    n = 0
    for ch in (obj.get("choices") or []) if isinstance(obj, dict) else []:
        d = ch.get("delta") or ch.get("message") or {}
        c = d.get("content") if isinstance(d, dict) else None
        if isinstance(c, str):
            n += len(c.strip())
        elif isinstance(c, list):
            for p in c:
                t = p.get("text") if isinstance(p, dict) else None
                if isinstance(t, str):
                    n += len(t.strip())
    return n


def _obj_is_error(obj) -> bool:
    return isinstance(obj, dict) and bool(obj.get("error"))


def _delta_has_answer(obj) -> bool:
    """Contenuto di RISPOSTA (testo answer o tool_calls), NON reasoning.
    E' questo che impegna lo stream verso il client."""
    if not isinstance(obj, dict):
        return False
    for ch in obj.get("choices") or []:
        d = ch.get("delta") or ch.get("message") or {}
        if not isinstance(d, dict):
            continue
        c = d.get("content")
        if isinstance(c, str) and c.strip():
            return True
        if isinstance(c, list) and c:
            return True
        if d.get("tool_calls"):
            return True
    return False


def _chunk_finish_reason(obj):
    for ch in (obj.get("choices") or []) if isinstance(obj, dict) else []:
        fr = ch.get("finish_reason") if isinstance(ch, dict) else None
        if fr:
            return fr
    return None


def _tool_calls_sse(tool_calls, model) -> list[bytes]:
    """SSE OpenAI sintetico per consegnare tool_calls dal testo (#6)."""
    import time as _time
    import uuid as _uuid

    cid = "chatcmpl-" + _uuid.uuid4().hex[:24]
    created = int(_time.time())

    def _chunk(delta, finish=None) -> bytes:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()

    out = [
        _chunk(
            {
                "role": "assistant",
                "tool_calls": [
                    {"index": i, "id": tc.get("id"), "type": "function", "function": tc.get("function")}
                    for i, tc in enumerate(tool_calls)
                ],
            }
        )
    ]
    out.append(_chunk({}, "tool_calls"))
    out.append(b"data: [DONE]\n\n")
    return out


def _buffered_answer_text(buffered) -> str:
    """Testo di risposta (delta.content) accumulato nei chunk da _peek_stream."""
    parts: list[str] = []
    for chunk in buffered or []:
        for obj in _sse_data_objs(chunk):
            for ch in obj.get("choices") or []:
                d = (ch.get("delta") or ch.get("message") or {}) if isinstance(ch, dict) else {}
                c = d.get("content") if isinstance(d, dict) else None
                if isinstance(c, str):
                    parts.append(c)
    return "".join(parts)


async def _peek_stream(
    gen,
    first_content_ms: int,
    include_reasoning: bool,
    min_chars: int = 40,
    hold_until_finish: bool = False,
    hold_idle_ms: int = 120000,
    hold_max_bytes: int = 50 * 1024 * 1024,
    first_byte: "asyncio.Event | None" = None,
):
    """Consuma `gen` finche' arriva CONTENUTO DI RISPOSTA sufficiente, oppure
    error / EOF / deadline.

    Impegna lo stream (verdict 'content') solo quando:
      - i caratteri di risposta accumulati raggiungono `min_chars`, OPPURE
      - arriva un finish_reason / [DONE] con almeno 1 char di risposta
        (risposta breve ma COMPLETA, es. "OK"), OPPURE
      - tool_calls (risposta valida senza testo), OPPURE
      - reasoning (solo se include_reasoning).
    Un finish_reason / [DONE] con 0 char -> 'empty_eof'. Un solo token seguito
    dalla morte dello stream NON impegna: -> 'timeout' -> rotazione.

    Con `hold_until_finish` (hold mode) NON si committa a `min_chars`: si
    consuma fino a una chiusura PULITA (finish_reason stop/tool_calls o
    [DONE]) cosi' da non consegnare MAI una risposta a meta'. Chiusure non
    pulite -> verdict 'truncated'. finish_reason=='length' NON e' mai
    'content', nemmeno con 0 caratteri (reasoning che ha esaurito il budget):
    -> 'length_truncated' (il chiamante consegna solo se e' il cap del
    client). Chiusura pulita con 0 caratteri -> 'empty_eof' con
    meta['empty_clean']=True: si ruota SENZA punire il deployment.
    Idle per-chunk = `hold_idle_ms`; cap buffer = `hold_max_bytes` (oltre il
    cap si consegna il buffer accumulato come 'content').
    `first_byte`: se fornito (gara hedge in hold), viene settato al primo
    chunk ricevuto: il chiamante capisce se A sta streammando o e' muto.

    Ritorna (verdict, buffered, pending, meta) con verdict in
    {'content','error','empty_eof','timeout','truncated','length_truncated'};
    `pending` = task `__anext__` in volo (SOLO se 'timeout'): NON cancellato
    qui, chi ruota chiama _discard_stream(). meta = {'finish_reason': str|None}.
    """
    buffered: list[bytes] = []
    meta = {"finish_reason": None, "saw_reasoning": False}
    answer_chars = 0
    saw_tool_calls = False
    saw_reasoning = False
    saw_done = False
    buffered_bytes = 0
    hold = bool(hold_until_finish)
    # FIX: cap difensivo anti-memoria. In condizioni normali si esce dopo
    # min_chars (40 char): il cap scatta SOLO su upstream patologici che
    # floodano stream reasoning-only senza contenuto di risposta.
    # In hold mode il cap e' quello configurato (50MB): oltre il cap si
    # consegna comunque il buffer (risposta enorme, ma NON troncata).
    MAX_PEEK_BUFFER_BYTES = 10 * 1024 * 1024  # 10MB prima di ruotare
    if hold:
        # in hold mode il cap e' quello configurato (default 50MB).
        MAX_PEEK_BUFFER_BYTES = max(1024, int(hold_max_bytes))

    def _eof():
        # fine stream senza una risposta impegnalbile.
        if hold:
            fr = meta.get("finish_reason")
            if fr == "length":
                # TRONCATURE mai 'content' (fix incidente 2026-09-15):
                # finish_reason=length significa che l'output e' stato TAGLIATO
                # (budget o nostro clamp), anche con 0 caratteri (reasoning che
                # si e' mangiato tutto il budget): si ruota, non si consegna il
                # moncone. Il chiamante riconsegna solo se e' il cap del client.
                meta["saw_reasoning"] = saw_reasoning
                meta["no_rotate"] = False
                return "length_truncated", buffered, None, meta
            if answer_chars or saw_tool_calls or (include_reasoning and saw_reasoning):
                if fr or saw_done:
                    # chiusura PULITA (finish_reason o [DONE]) -> risposta completa.
                    return "content", buffered, None, meta
                # contenuto ma nessun terminatore pulito -> troncata.
                return "truncated", buffered, None, meta
            # zero caratteri utili: se il modello ha CHIUSO pulito (stop/[DONE])
            # non e' rotto, ha solo risposto vuoto -> si ruota SENZA penale
            # (empty_clean); se non ha chiuso e' upstream rotto -> ruota + penale.
            meta["saw_reasoning"] = saw_reasoning
            if fr or saw_done:
                meta["empty_clean"] = True
            meta["no_rotate"] = False
            return "empty_eof", buffered, None, meta
        if answer_chars or saw_tool_calls or (include_reasoning and saw_reasoning):
            return "content", buffered, None, meta
        meta["saw_reasoning"] = saw_reasoning
        # no_rotate = il modello ha COMPLETATO (c'e' un finish_reason) senza
        # rispondere: non e' rotto, ruotare nel gruppo non aiuta -> notice.
        # reasoning TRONCATO senza finish_reason = troncamento upstream -> un
        # altro deployment puo' farcela -> ruota (+ mark_failed).
        meta["no_rotate"] = bool(meta.get("finish_reason"))
        return "empty_eof", buffered, None, meta

    deadline = time.monotonic() + max(0.0, first_content_ms) / 1000.0
    while True:
        if hold:
            # idle timeout per-chunk: si resetta ad ogni chunk ricevuto.
            remaining = max(0.001, hold_idle_ms / 1000.0)
            if not buffered:
                # PRIMA del primo byte vale anche il deadline primo-contenuto:
                # un upstream MUTO (morto) non puo' trattenere la richiesta per
                # 120s; la rotazione (e il paracadute sull'ultimo scaglione)
                # restano possibili. Dopo il primo byte, solo idle per-chunk.
                remaining = min(remaining, max(0.0, deadline - time.monotonic()))
                if remaining <= 0:
                    return "timeout", buffered, None, meta
        else:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", buffered, None, meta
        task = asyncio.ensure_future(gen.__anext__())
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if not done:
            return "timeout", buffered, task, meta
        try:
            chunk = task.result()
        except StopAsyncIteration:
            return _eof()
        except (asyncio.TimeoutError, httpx.TimeoutException):
            # TIMEOUT del TRASPORTO (l'upstream "appende" senza rispondere ne'
            # fallire con codice): danno REALE, tempo perso -> verdict
            # 'timeout' (cooldown lungo via reason=timeout), NON 'empty_eof'.
            return "timeout", buffered, None, meta
        except Exception:
            return "empty_eof", buffered, None, meta
        buffered.append(chunk)
        buffered_bytes += len(chunk)
        if first_byte is not None:
            first_byte.set()
        # FIX: upstream patologico (flood reasoning-only / contenuto enorme):
        # esci prima della deadline per non accumulare memoria illimitata.
        if buffered_bytes > MAX_PEEK_BUFFER_BYTES:
            log.warning(
                "[peek] buffer %d byte senza contenuto sufficiente: rotazione (answer_chars=%d)",
                buffered_bytes,
                answer_chars,
            )
            if hold:
                # risposta enorme: la consegniamo (non e' troncata).
                return "content", buffered, None, meta
            if answer_chars or saw_tool_calls:
                return "content", buffered, None, meta
            return "timeout", buffered, None, meta
        saw_fr_here = False
        for obj in _sse_data_objs(chunk):
            if _obj_is_error(obj):
                return "error", buffered, None, meta
            # alcuni provider infilano il PROPRIO envelope d'errore
            # ({"type":"error",...} / {"error":...}) DENTRO delta.content come
            # se fosse testo: non e' una risposta reale -> ruota.
            for ch in obj.get("choices") or []:
                dd = (ch.get("delta") or ch.get("message") or {}) if isinstance(ch, dict) else {}
                cc = dd.get("content") if isinstance(dd, dict) else None
                if isinstance(cc, str) and is_embedded_provider_error(cc):
                    return "error", buffered, None, meta
            answer_chars += _answer_chars(obj)
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or ch.get("message") or {} if isinstance(ch, dict) else {}
                if isinstance(d, dict):
                    if d.get("tool_calls"):
                        saw_tool_calls = True
                    rc = d.get("reasoning_content") or d.get("reasoning")
                    if isinstance(rc, str) and rc.strip():
                        saw_reasoning = True
            fr = _chunk_finish_reason(obj)
            if fr:
                meta["finish_reason"] = fr
                saw_fr_here = True
            if hold:
                continue
            if saw_tool_calls or answer_chars >= max(1, min_chars):
                return "content", buffered, None, meta
            if fr:
                return ("content", buffered, None, meta) if answer_chars > 0 else _eof()
            if include_reasoning and saw_reasoning:
                return "content", buffered, None, meta
        if b"[DONE]" in chunk:
            saw_done = True
            if answer_chars > 0 or saw_tool_calls or (include_reasoning and saw_reasoning):
                return "content", buffered, None, meta
            return _eof()
        if hold and saw_fr_here:
            if meta.get("finish_reason") == "length":
                # troncatura (con o senza answer): mai content, vedi _eof()
                return _eof()
            if answer_chars > 0 or saw_tool_calls or (include_reasoning and saw_reasoning):
                return "content", buffered, None, meta
            return _eof()
