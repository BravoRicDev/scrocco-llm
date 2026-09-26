"""Adapter di protocollo: traduzione OpenAI Chat Completions <-> altri SDK.

scrocco-llm (e i suoi client) parlano SEMPRE OpenAI Chat Completions
(`/chat/completions`). Alcuni provider espongono i modelli solo su protocolli
nativi diversi, selezionati dal client OpenCode tramite l'SDK:

  @ai-sdk/openai-compatible -> /chat/completions   (api_style = chat, default)
  @ai-sdk/openai            -> /responses          (api_style = responses)
  @ai-sdk/anthropic         -> /messages           (api_style = messages)
  @ai-sdk/google            -> /models/{model}:*   (api_style = google)

Questo modulo traduce richiesta, risposta (JSON) e stream SSE dal formato
nativo a Chat Completions, cosi' il resto della pipeline (fallback, filtri
tool/stream, note_result, ...) resta invariato.

Copertura: testo, immagini (data URL), tool calls / tool results, usage.
Non copre: audio, video, generazione immagini (che restano endpoint dedicati).
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

CHAT = "chat"
RESPONSES = "responses"
MESSAGES = "messages"
GOOGLE = "google"

VALID_STYLES = (CHAT, RESPONSES, MESSAGES, GOOGLE)


def normalize_style(value: Any) -> str:
    """Normalizza il valore dell'`api_style` (default 'chat')."""
    v = ("" if value is None else str(value)).strip().lower()
    return v if v in VALID_STYLES else CHAT


def style_of(dep: dict) -> str:
    return normalize_style(dep.get("api_style"))


# --------------------------------------------------------------------------- io
def _sse(obj: Any) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"


def _now() -> int:
    return int(time.time())


def _new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000):x}"


# --------------------------------------------------------------- request build
def build_url(dep: dict, *, stream: bool) -> str:
    """URL upstream in base allo stile del deployment."""
    base = (dep.get("api_base") or "").rstrip("/")
    style = style_of(dep)
    if style == RESPONSES:
        return f"{base}/responses"
    if style == MESSAGES:
        return f"{base}/messages"
    if style == GOOGLE:
        model = dep.get("model", "")
        verb = "streamGenerateContent" if stream else "generateContent"
        q = "?alt=sse" if stream else ""
        return f"{base}/models/{model}:{verb}{q}"
    return f"{base}/chat/completions"


def apply_auth(dep: dict, headers: dict[str, str]) -> dict[str, str]:
    """Adatta gli header di autenticazione allo stile del deployment."""
    style = style_of(dep)
    key = dep.get("api_key", "")
    out = dict(headers)
    if style == MESSAGES:
        out.pop("Authorization", None)
        out["x-api-key"] = key
        out.setdefault("anthropic-version", "2023-06-01")
    elif style == GOOGLE:
        out.pop("Authorization", None)
        out["x-goog-api-key"] = key
    # chat / responses: Authorization Bearer va gia' bene.
    return out


def extra_headers(dep: dict) -> dict[str, str]:
    """Header extra richiesti dallo stile (es. beta Responses)."""
    return {}


# ------------------------------------------------------------- data-url helpers
def split_data_url(url: str) -> tuple[str, str] | None:
    """'data:image/png;base64,AAAA' -> ('image/png', 'AAAA'); altrimenti None."""
    if not isinstance(url, str) or not url.startswith("data:"):
        return None
    try:
        head, data = url.split(",", 1)
    except ValueError:
        return None
    if ";base64" not in head:
        return None
    mime = head[len("data:"):].split(";", 1)[0] or "application/octet-stream"
    return mime, data


def _parts_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") in ("text", "input_text", "output_text"):
                    out.append(str(p.get("text") or ""))
                elif isinstance(p.get("text"), str):
                    out.append(p["text"])
        return "".join(out)
    return "" if content is None else str(content)


# ================================================================ RESPONSES
def _reasoning_from_body(body: dict) -> str | None:
    """Effort normalizzato dal body chat (apply_effort_policy lo ha gia'
    messo/tolto): 'low'|'medium'|'high' o None."""
    eff = str(body.get("reasoning_effort") or "").strip().lower()
    return eff if eff in ("low", "medium", "high") else None


# Budget di thinking per livello nei protocolli nativi che lo chiedono in
# token (Anthropic budget_tokens / Gemini thinkingBudget).
_THINK_BUDGET = {"low": 1024, "medium": 4096, "high": 8192}


def chat_to_responses(body: dict, dep: dict) -> dict:
    """OpenAI Chat Completions -> OpenAI Responses API (/responses)."""
    out: dict[str, Any] = {"model": dep.get("model") or body.get("model")}

    instructions: list[str] = []
    items: list[dict] = []
    for m in (body.get("messages") or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            instructions.append(_parts_to_text(m.get("content")))
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id") or "",
                "output": _parts_to_text(m.get("content")),
            })
        elif role == "assistant":
            if m.get("content"):
                items.append({"role": "assistant", "content": [
                    {"type": "output_text", "text": _parts_to_text(
                        m.get("content"))}]})
            for tc in (m.get("tool_calls") or []):
                fn = (tc or {}).get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "",
                })
        else:  # user / developer
            parts = []
            content = m.get("content")
            if isinstance(content, list):
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "image_url":
                        iu = p.get("image_url") or {}
                        url = iu.get("url") if isinstance(iu, dict) else None
                        if url:
                            parts.append({"type": "input_image",
                                          "image_url": url})
                    elif p.get("type") in ("text", "input_text"):
                        parts.append({"type": "input_text",
                                      "text": p.get("text") or ""})
            else:
                parts.append({"type": "input_text",
                              "text": _parts_to_text(content)})
            items.append({"role": role or "user", "content": parts})
    if instructions:
        out["instructions"] = "\n\n".join(t for t in instructions if t)
    out["input"] = items

    tools = []
    for t in (body.get("tools") or []):
        fn = (t or {}).get("function") or {}
        if not fn.get("name"):
            continue
        tools.append({"type": "function", "name": fn["name"],
                      "description": fn.get("description") or "",
                      "parameters": fn.get("parameters") or {"type": "object",
                                                             "properties": {}}})
    if tools:
        out["tools"] = tools
        if body.get("tool_choice") is not None:
            out["tool_choice"] = body.get("tool_choice")
    mx = body.get("max_completion_tokens") or body.get("max_tokens")
    if mx:
        out["max_output_tokens"] = int(mx)
    for k_src, k_dst in (("temperature", "temperature"), ("top_p", "top_p")):
        if body.get(k_src) is not None:
            out[k_dst] = body[k_src]
    eff = _reasoning_from_body(body)
    if eff:
        # 'summary': auto -> l'upstream rimette il riepilogo del thinking
        # (item type=="reasoning") che responses_to_chat riconverte in
        # reasoning_content. Senza effort richiesto, non chiediamo nulla.
        out["reasoning"] = {"effort": eff, "summary": "auto"}
    if body.get("stream"):
        out["stream"] = True
    return out


def responses_to_chat(obj: dict, dep: dict) -> dict:
    """OpenAI Responses API -> OpenAI Chat Completion.

    Gli item `type=="reasoning"` (riepilogo del thinking, presente ANCHE
    nella risposta non-stream quando abbiamo chiesto `reasoning.summary`)
    finiscono in `message.reasoning_content`, come i provider OpenAI-compat.
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    for item in (obj.get("output") or []):
        if not isinstance(item, dict):
            continue
        it = item.get("type")
        if it == "reasoning":
            for s in (item.get("summary") or []):
                if isinstance(s, dict) and s.get("text"):
                    reasoning_parts.append(str(s["text"]))
        elif it == "message":
            for c in (item.get("content") or []):
                if isinstance(c, dict) and c.get("type") in (
                        "output_text", "text"):
                    text_parts.append(c.get("text") or "")
        elif it == "function_call":
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or "",
                "type": "function",
                "function": {"name": item.get("name") or "",
                             "arguments": item.get("arguments") or ""},
            })
    finish = "tool_calls" if tool_calls else "stop"
    if obj.get("status") == "incomplete":
        finish = "length"
    msg: dict[str, Any] = {"role": "assistant",
                           "content": "".join(text_parts) or None}
    if reasoning_parts:
        msg["reasoning_content"] = "\n\n".join(reasoning_parts)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    u = obj.get("usage") or {}
    usage = None
    if u:
        it = int(u.get("input_tokens") or 0)
        ot = int(u.get("output_tokens") or 0)
        usage = {"prompt_tokens": it, "completion_tokens": ot,
                 "total_tokens": it + ot}
    return _chat_obj(obj.get("id"), obj.get("model") or dep.get("model"),
                     obj.get("created_at") or _now(), msg, finish, usage)


# ================================================================ MESSAGES
def _anthropic_tool_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        blocks = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "image_url":
                iu = p.get("image_url") or {}
                url = iu.get("url") if isinstance(iu, dict) else None
                sp = split_data_url(url or "")
                if sp:
                    blocks.append({"type": "image", "source": {
                        "type": "base64", "media_type": sp[0], "data": sp[1]}})
                elif url:
                    blocks.append({"type": "image", "source": {
                        "type": "url", "url": url}})
            elif p.get("type") in ("text", "input_text"):
                blocks.append({"type": "text", "text": p.get("text") or ""})
        return blocks
    return _parts_to_text(content)


def chat_to_messages(body: dict, dep: dict) -> dict:
    """OpenAI Chat Completions -> Anthropic Messages (/messages)."""
    out: dict[str, Any] = {"model": dep.get("model") or body.get("model")}
    system_parts: list[str] = []
    msgs: list[dict] = []
    for m in (body.get("messages") or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            system_parts.append(_parts_to_text(m.get("content")))
            continue
        if role == "tool":
            msgs.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id") or "",
                "content": _parts_to_text(m.get("content"))}]})
            continue
        if role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text",
                               "text": _parts_to_text(m.get("content"))})
            for tc in (m.get("tool_calls") or []):
                fn = (tc or {}).get("function") or {}
                args = fn.get("arguments") or ""
                try:
                    inp = json.loads(args) if isinstance(args, str) else args
                except Exception:
                    inp = {}
                blocks.append({"type": "tool_use", "id": tc.get("id") or "",
                               "name": fn.get("name") or "", "input": inp})
            msgs.append({"role": "assistant", "content": blocks or [
                {"type": "text", "text": ""}]})
            continue
        # user
        msgs.append({"role": "user",
                     "content": _anthropic_tool_content(m.get("content"))})
    if system_parts:
        out["system"] = "\n\n".join(s for s in system_parts if s)
    out["messages"] = _merge_consecutive(msgs)
    tools = []
    for t in (body.get("tools") or []):
        fn = (t or {}).get("function") or {}
        if not fn.get("name"):
            continue
        tools.append({"name": fn["name"],
                      "description": fn.get("description") or "",
                      "input_schema": fn.get("parameters") or {
                          "type": "object", "properties": {}}})
    if tools:
        out["tools"] = tools
        tc = body.get("tool_choice")
        if tc == "auto" or tc is None:
            pass
        elif tc == "required":
            out["tool_choice"] = {"type": "any"}
        elif tc == "none":
            pass
        elif isinstance(tc, dict):
            out["tool_choice"] = {"type": "tool",
                                  "name": (tc.get("function") or {}).get(
                                      "name") or tc.get("name")}
    out["max_tokens"] = int(body.get("max_completion_tokens")
                            or body.get("max_tokens") or 4096)
    eff = _reasoning_from_body(body)
    if eff and out["max_tokens"] > 1024:
        # Anthropic con thinking attivo RIFIUTA temperature/top_p != 1:
        # l'effort (scelta esplicita del client) vince e non li copiamo;
        # il budget deve restare STRETTO sotto max_tokens.
        budget = min(_THINK_BUDGET.get(eff, 4096), out["max_tokens"] - 1)
        out["thinking"] = {"type": "enabled", "budget_tokens": max(1024, budget)}
    if body.get("temperature") is not None and "thinking" not in out:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None and "thinking" not in out:
        out["top_p"] = body["top_p"]
    stop = body.get("stop")
    if isinstance(stop, str):
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list):
        out["stop_sequences"] = stop
    if body.get("stream"):
        out["stream"] = True
    return out


def _merge_consecutive(msgs: list[dict]) -> list[dict]:
    """Anthropic pretende turni alternati: fonde user/user consecutivi."""
    out: list[dict] = []
    for m in msgs:
        if out and out[-1]["role"] == m["role"] == "user":
            prev = out[-1]["content"]
            cur = m["content"]
            pc = prev if isinstance(prev, list) else [
                {"type": "text", "text": str(prev)}]
            cc = cur if isinstance(cur, list) else [
                {"type": "text", "text": str(cur)}]
            out[-1] = {"role": "user", "content": list(pc) + list(cc)}
        else:
            out.append(m)
    return out


def messages_to_chat(obj: dict, dep: dict) -> dict:
    """Anthropic Messages -> OpenAI Chat Completion."""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    for b in (obj.get("content") or []):
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            text_parts.append(b.get("text") or "")
        elif b.get("type") == "thinking":
            # Anthropic: il blocco thinking (o redacted_thinking) diventa
            # reasoning_content OpenAI-compat per il client.
            th = b.get("thinking") or b.get("text")
            if th:
                reasoning_parts.append(str(th))
        elif b.get("type") == "tool_use":
            tool_calls.append({
                "id": b.get("id") or "",
                "type": "function",
                "function": {"name": b.get("name") or "",
                             "arguments": json.dumps(b.get("input") or {},
                                                     ensure_ascii=False)},
            })
    sr = obj.get("stop_reason")
    finish = {"tool_use": "tool_calls", "max_tokens": "length",
              "end_turn": "stop", "stop_sequence": "stop"}.get(sr, "stop")
    msg: dict[str, Any] = {"role": "assistant",
                           "content": "".join(text_parts) or None}
    if reasoning_parts:
        msg["reasoning_content"] = "\n\n".join(reasoning_parts)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    u = obj.get("usage") or {}
    usage = None
    if u:
        it = int(u.get("input_tokens") or 0)
        ot = int(u.get("output_tokens") or 0)
        usage = {"prompt_tokens": it, "completion_tokens": ot,
                 "total_tokens": it + ot}
    return _chat_obj(obj.get("id"), obj.get("model") or dep.get("model"),
                     _now(), msg, finish, usage)


# ================================================================ GOOGLE
def chat_to_gemini(body: dict, dep: dict) -> dict:
    """OpenAI Chat Completions -> Google Generative Language generateContent."""
    out: dict[str, Any] = {}
    sys_parts: list[str] = []
    contents: list[dict] = []
    for m in (body.get("messages") or []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            sys_parts.append(_parts_to_text(m.get("content")))
            continue
        if role == "tool":
            contents.append({"role": "user", "parts": [{
                "functionResponse": {
                    "name": m.get("name") or m.get("tool_call_id") or "",
                    "response": _tool_response_obj(m.get("content"))}}]})
            continue
        if role == "assistant":
            parts: list[dict] = []
            if m.get("content"):
                parts.append({"text": _parts_to_text(m.get("content"))})
            for tc in (m.get("tool_calls") or []):
                fn = (tc or {}).get("function") or {}
                args = fn.get("arguments") or ""
                try:
                    a = json.loads(args) if isinstance(args, str) else args
                except Exception:
                    a = {}
                parts.append({"functionCall": {"name": fn.get("name") or "",
                                               "args": a}})
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
            continue
        # user
        parts = []
        content = m.get("content")
        if isinstance(content, list):
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "image_url":
                    iu = p.get("image_url") or {}
                    url = iu.get("url") if isinstance(iu, dict) else None
                    sp = split_data_url(url or "")
                    if sp:
                        parts.append({"inlineData": {"mimeType": sp[0],
                                                     "data": sp[1]}})
                elif p.get("type") in ("text", "input_text"):
                    parts.append({"text": p.get("text") or ""})
        else:
            parts.append({"text": _parts_to_text(content)})
        contents.append({"role": "user", "parts": parts})
    if sys_parts:
        out["systemInstruction"] = {"parts": [
            {"text": "\n\n".join(s for s in sys_parts if s)}]}
    out["contents"] = contents
    decls = []
    for t in (body.get("tools") or []):
        fn = (t or {}).get("function") or {}
        if not fn.get("name"):
            continue
        decls.append({"name": fn["name"],
                      "description": fn.get("description") or "",
                      "parameters": fn.get("parameters") or {
                          "type": "object", "properties": {}}})
    if decls:
        out["tools"] = [{"functionDeclarations": decls}]
    gen: dict[str, Any] = {}
    mx = body.get("max_completion_tokens") or body.get("max_tokens")
    if mx:
        gen["maxOutputTokens"] = int(mx)
    if body.get("temperature") is not None:
        gen["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        gen["topP"] = body["top_p"]
    stop = body.get("stop")
    if isinstance(stop, str):
        gen["stopSequences"] = [stop]
    elif isinstance(stop, list):
        gen["stopSequences"] = stop
    eff = _reasoning_from_body(body)
    if eff:
        # Gemini: includeThoughts chiede il riepilogo dei thinking (parts con
        # thought:true) che gemini_to_chat converte in reasoning_content.
        # thinkingBudget DEVE stare STRETTO sotto maxOutputTokens, altrimenti
        # Google risponde 400 INVALID_ARGUMENT per l'INTERA chiamata (stesso
        # vincolo che chat_to_anthropic applica piu' sopra). Il clamp di
        # contesto del gateway (forwarder.clamp_max_tokens) porta spesso
        # maxOutputTokens sotto il budget assoluto del livello. Se non c'e'
        # spazio per il thinking si omette il config (risposta senza thinking)
        # invece di far fallire la richiesta.
        budget = _THINK_BUDGET.get(eff, 4096)
        _mx_out = gen.get("maxOutputTokens")
        if _mx_out is not None:
            budget = min(budget, int(_mx_out) - 1)
        if budget > 0:
            gen["thinkingConfig"] = {"includeThoughts": True,
                                     "thinkingBudget": budget}
    if gen:
        out["generationConfig"] = gen
    return out


def _tool_response_obj(content: Any) -> dict:
    if isinstance(content, (dict, list)):
        return {"result": content}
    return {"result": _parts_to_text(content)}


def gemini_to_chat(obj: dict, dep: dict) -> dict:
    """Google generateContent -> OpenAI Chat Completion."""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict] = []
    finish = "stop"
    cands = obj.get("candidates") or []
    if cands:
        c0 = cands[0] or {}
        fr = c0.get("finishReason")
        finish = {"STOP": "stop", "MAX_TOKENS": "length",
                  "SAFETY": "content_filter", "RECITATION": "content_filter",
                  "TOOL_CALLS": "tool_calls"}.get(fr, "stop")
        for p in ((c0.get("content") or {}).get("parts") or []):
            if not isinstance(p, dict):
                continue
            if "text" in p and p.get("text"):
                if p.get("thought"):
                    reasoning_parts.append(p["text"])
                else:
                    text_parts.append(p["text"])
            fc = p.get("functionCall")
            if isinstance(fc, dict):
                tool_calls.append({
                    "id": _new_id("call"),
                    "type": "function",
                    "function": {"name": fc.get("name") or "",
                                 "arguments": json.dumps(
                                     fc.get("args") or {},
                                     ensure_ascii=False)}})
    if tool_calls:
        finish = "tool_calls"
    msg: dict[str, Any] = {"role": "assistant",
                           "content": "".join(text_parts) or None}
    if reasoning_parts:
        msg["reasoning_content"] = "\n\n".join(reasoning_parts)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    um = obj.get("usageMetadata") or {}
    usage = None
    if um:
        it = int(um.get("promptTokenCount") or 0)
        ot = int(um.get("candidatesTokenCount") or 0)
        usage = {"prompt_tokens": it, "completion_tokens": ot,
                 "total_tokens": int(um.get("totalTokenCount") or (it + ot))}
    return _chat_obj(obj.get("responseId"), obj.get("modelVersion")
                     or dep.get("model"), _now(), msg, finish, usage)


# ================================================================ dispatcher
def _chat_obj(cid, model, created, message, finish, usage) -> dict:
    return {
        "id": cid or _new_id("chatcmpl"),
        "object": "chat.completion",
        "created": created,
        "model": model or "",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage,
    }


def translate_request(style: str, body: dict, dep: dict) -> dict:
    style = normalize_style(style)
    if style == RESPONSES:
        return chat_to_responses(body, dep)
    if style == MESSAGES:
        return chat_to_messages(body, dep)
    if style == GOOGLE:
        return chat_to_gemini(body, dep)
    return body


def translate_response(style: str, obj: dict, dep: dict) -> dict:
    style = normalize_style(style)
    if style == RESPONSES:
        return responses_to_chat(obj, dep)
    if style == MESSAGES:
        return messages_to_chat(obj, dep)
    if style == GOOGLE:
        return gemini_to_chat(obj, dep)
    return obj


def chat_obj_to_sse(obj: dict) -> list[bytes]:
    """Adatta una chat.completion JSON a chunk SSE OpenAI (per upstream che
    ignorano stream:true)."""
    base = {"id": obj.get("id") or _new_id("chatcmpl"),
            "object": "chat.completion.chunk",
            "created": obj.get("created") or _now(),
            "model": obj.get("model") or ""}
    choice = (obj.get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    tool_calls = msg.get("tool_calls")
    content = msg.get("content")
    d0: dict[str, Any] = {"role": "assistant"}
    if tool_calls:
        d0["tool_calls"] = tool_calls
    out = [_sse({**base, "choices": [{"index": 0, "delta": d0,
                                      "finish_reason": None}]})]
    rc = msg.get("reasoning_content")
    if isinstance(rc, str) and rc:
        out.append(_sse({**base, "choices": [{"index": 0,
                       "delta": {"reasoning_content": rc},
                       "finish_reason": None}]}))
    if content:
        out.append(_sse({**base, "choices": [{"index": 0,
                       "delta": {"content": content},
                       "finish_reason": None}]}))
    fr = choice.get("finish_reason") or "stop"
    out.append(_sse({**base, "choices": [{"index": 0, "delta": {},
                                          "finish_reason": fr}]}))
    if isinstance(obj.get("usage"), dict):
        out.append(_sse({**base, "choices": [], "usage": obj["usage"]}))
    out.append(b"data: [DONE]\n\n")
    return out


def _sse_objs_from_chunk(chunk) -> list[dict]:
    """Estrae gli oggetti JSON da un chunk SSE (anche multi-riga). Accetta
    bytes/str (righe `data:`) oppure direttamente un dict."""
    if isinstance(chunk, dict):
        return [chunk]
    if isinstance(chunk, str):
        chunk = chunk.encode()
    if not isinstance(chunk, (bytes, bytearray)):
        return []
    objs: list[dict] = []
    for line in bytes(chunk).split(b"\n"):
        s = line.strip()
        if not s.startswith(b"data:"):
            continue
        payload = s[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            o = json.loads(payload)
        except Exception:
            continue
        if isinstance(o, dict):
            objs.append(o)
    return objs


def _merge_tool_call(acc: dict, tc: dict) -> None:
    """Accorpa un delta tool_call nello slot per indice (nome una volta,
    arguments concatenati, come fa OpenAI in streaming)."""
    if not isinstance(tc, dict):
        return
    idx = tc.get("index")
    if not isinstance(idx, int):
        idx = len(acc)
    slot = acc.get(idx)
    if slot is None:
        slot = {"id": tc.get("id") or "",
                "type": tc.get("type") or "function",
                "function": {"name": "", "arguments": ""}}
        acc[idx] = slot
    if tc.get("id"):
        slot["id"] = tc["id"]
    if tc.get("type"):
        slot["type"] = tc["type"]
    fn = tc.get("function") or {}
    if isinstance(fn, dict):
        nm = fn.get("name")
        if isinstance(nm, str) and nm:
            cur = slot["function"]["name"]
            if not cur:
                slot["function"]["name"] = nm
            elif not cur.endswith(nm) and nm != cur:
                slot["function"]["name"] += nm
        args = fn.get("arguments")
        if isinstance(args, str):
            slot["function"]["arguments"] += args


def sse_to_chat_obj(chunks) -> dict:
    """Inverso di `chat_obj_to_sse`: assembla chunk SSE OpenAI Chat in una
    singola `chat.completion` JSON.

    `chunks` e' un iterabile/lista di bytes (o dict) SSE. Solleva ValueError se
    lo stream porta un evento d'errore o non produce alcuna scelta utile: cosi'
    il chiamante (redirect non-stream->stream sotto hold) puo' ruotare invece
    di consegnare un body vuoto.
    """
    out: dict | None = None
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    usage = None
    err = None
    if isinstance(chunks, (bytes, bytearray, str, dict)):
        chunks = [chunks]
    for chunk in chunks or []:
        if not chunk:
            continue
        for obj in _sse_objs_from_chunk(chunk):
            if obj.get("error"):
                err = obj["error"]
                continue
            if out is None:
                out = {"id": obj.get("id") or _new_id("chatcmpl"),
                       "object": "chat.completion",
                       "created": obj.get("created") or _now(),
                       "model": obj.get("model") or ""}
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            for ch in obj.get("choices") or []:
                if not isinstance(ch, dict):
                    continue
                d = ch.get("delta")
                if isinstance(d, dict):
                    c = d.get("content")
                    if isinstance(c, str):
                        content_parts.append(c)
                    rc = d.get("reasoning_content")
                    if rc is None:
                        rc = d.get("reasoning")
                    if isinstance(rc, str):
                        reasoning_parts.append(rc)
                    for tc in d.get("tool_calls") or []:
                        _merge_tool_call(tool_calls, tc)
                fr = ch.get("finish_reason")
                if fr:
                    finish_reason = fr
    if err is not None:
        raise ValueError("stream errore: %s" % str(err)[:200])
    if out is None:
        raise ValueError("stream senza contenuto")
    msg: dict[str, Any] = {"role": "assistant",
                           "content": "".join(content_parts)}
    if reasoning_parts:
        msg["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        msg["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
    out["choices"] = [{"index": 0, "message": msg,
                       "finish_reason": finish_reason or "stop"}]
    out["usage"] = usage if isinstance(usage, dict) else {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return out


# ================================================================ streaming
async def _iter_data_json(source: AsyncIterator[bytes]) -> AsyncIterator[dict]:
    """Estrae gli oggetti JSON da uno stream SSE (righe `data:`), ignorando
    `event:`/commenti e `[DONE]`."""
    buf = b""
    async for chunk in source:
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]" or not payload:
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


class _StreamState:
    def __init__(self, dep: dict):
        self.dep = dep
        self.id = _new_id("chatcmpl")
        self.model = dep.get("model") or ""
        self.role_sent = False
        self.finish: str | None = None
        self.usage: dict | None = None
        self.tool_index = -1
        self.key_to_tool: dict[str, int] = {}

    def start(self) -> bytes:
        self.role_sent = True
        return _sse({"id": self.id, "object": "chat.completion.chunk",
                     "created": _now(), "model": self.model,
                     "choices": [{"index": 0, "delta": {"role": "assistant"},
                                  "finish_reason": None}]})

    def content(self, text: str) -> bytes:
        return _sse({"id": self.id, "object": "chat.completion.chunk",
                     "created": _now(), "model": self.model,
                     "choices": [{"index": 0, "delta": {"content": text},
                                  "finish_reason": None}]})

    def reasoning(self, text: str) -> bytes:
        return _sse({"id": self.id, "object": "chat.completion.chunk",
                     "created": _now(), "model": self.model,
                     "choices": [{"index": 0,
                                  "delta": {"reasoning_content": text},
                                  "finish_reason": None}]})

    def tool_start(self, key: str, call_id: str, name: str) -> bytes:
        self.tool_index += 1
        idx = self.tool_index
        if key:
            self.key_to_tool[key] = idx
        if call_id and call_id != key:
            self.key_to_tool[call_id] = idx
        return _sse({"id": self.id, "object": "chat.completion.chunk",
                     "created": _now(), "model": self.model,
                     "choices": [{"index": 0, "delta": {"tool_calls": [{
                         "index": idx, "id": call_id, "type": "function",
                         "function": {"name": name, "arguments": ""}}]},
                         "finish_reason": None}]})

    def tool_frag_idx(self, idx: int, frag: str) -> bytes:
        return _sse({"id": self.id, "object": "chat.completion.chunk",
                     "created": _now(), "model": self.model,
                     "choices": [{"index": 0, "delta": {"tool_calls": [{
                         "index": idx, "function": {"arguments": frag}}]},
                         "finish_reason": None}]})

    def tool_frag(self, key: str, frag: str) -> bytes:
        idx = self.key_to_tool.get(key)
        if idx is None:
            self.tool_index += 1
            idx = self.tool_index
            if key:
                self.key_to_tool[key] = idx
        return self.tool_frag_idx(idx, frag)

    def final(self) -> bytes:
        fr = self.finish or ("tool_calls" if self.tool_index >= 0 else "stop")
        return _sse({"id": self.id, "object": "chat.completion.chunk",
                     "created": _now(), "model": self.model,
                     "choices": [{"index": 0, "delta": {},
                                  "finish_reason": fr}]})

    def usage_chunk(self) -> bytes | None:
        if not self.usage:
            return None
        return _sse({"id": self.id, "object": "chat.completion.chunk",
                     "created": _now(), "model": self.model,
                     "choices": [], "usage": self.usage})


def _resp_usage(u: dict) -> dict | None:
    if not u:
        return None
    it = int(u.get("input_tokens") or 0)
    ot = int(u.get("output_tokens") or 0)
    return {"prompt_tokens": it, "completion_tokens": ot,
            "total_tokens": it + ot}


async def _stream_responses(source: AsyncIterator[bytes],
                            dep: dict) -> AsyncIterator[bytes]:
    st = _StreamState(dep)
    async for ev in _iter_data_json(source):
        t = ev.get("type")
        if t == "response.created":
            r = ev.get("response") or {}
            st.id = r.get("id") or st.id
            st.model = r.get("model") or st.model
        elif t == "response.output_text.delta":
            if not st.role_sent:
                yield st.start()
            d = ev.get("delta")
            if d:
                yield st.content(d)
        elif t in ("response.reasoning_summary_text.delta",
                   "response.reasoning_text.delta"):
            # Riepilogo del thinking (o corpo reasoning): delta
            # reasoning_content come dai provider OpenAI-compat. Il peek li
            # conta solo per il verdetto di commit, non per i caratteri di
            # risposta.
            if not st.role_sent:
                yield st.start()
            d = ev.get("delta")
            if d:
                yield st.reasoning(d)
        elif t == "response.output_item.added":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                if not st.role_sent:
                    yield st.start()
                yield st.tool_start(
                    item.get("id") or item.get("call_id") or "",
                    item.get("call_id") or item.get("id") or "",
                    item.get("name") or "")
        elif t == "response.function_call_arguments.delta":
            if not st.role_sent:
                yield st.start()
            yield st.tool_frag(ev.get("item_id") or "", ev.get("delta") or "")
        elif t in ("response.completed", "response.incomplete"):
            r = ev.get("response") or {}
            st.usage = _resp_usage(r.get("usage") or {})
            if r.get("status") == "incomplete":
                st.finish = "length"
            if not st.role_sent:
                yield st.start()
    yield st.final()
    uc = st.usage_chunk()
    if uc:
        yield uc
    yield b"data: [DONE]\n\n"


async def _stream_messages(source: AsyncIterator[bytes],
                           dep: dict) -> AsyncIterator[bytes]:
    st = _StreamState(dep)
    async for ev in _iter_data_json(source):
        t = ev.get("type")
        if t == "message_start":
            m = ev.get("message") or {}
            st.id = m.get("id") or st.id
            st.model = m.get("model") or st.model
            u = m.get("usage") or {}
            if u:
                st.usage = {"prompt_tokens": int(u.get("input_tokens") or 0),
                            "completion_tokens": 0, "total_tokens": 0}
        elif t == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "tool_use":
                if not st.role_sent:
                    yield st.start()
                yield st.tool_start(str(ev.get("index")), cb.get("id") or "",
                                    cb.get("name") or "")
        elif t == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta":
                if not st.role_sent:
                    yield st.start()
                if d.get("text"):
                    yield st.content(d["text"])
            elif d.get("type") == "thinking_delta":
                # Anthropic streaming: i block thinking diventano
                # delta.reasoning_content verso il client.
                if not st.role_sent:
                    yield st.start()
                if d.get("thinking"):
                    yield st.reasoning(d["thinking"])
            elif d.get("type") == "input_json_delta":
                if not st.role_sent:
                    yield st.start()
                yield st.tool_frag(str(ev.get("index")),
                                   d.get("partial_json") or "")
        elif t == "message_delta":
            d = ev.get("delta") or {}
            sr = d.get("stop_reason")
            if sr:
                st.finish = {"tool_use": "tool_calls",
                             "max_tokens": "length"}.get(sr, "stop")
            u = ev.get("usage") or {}
            if u and st.usage is not None:
                st.usage["completion_tokens"] = int(u.get("output_tokens") or 0)
                st.usage["total_tokens"] = (st.usage["prompt_tokens"]
                                            + st.usage["completion_tokens"])
    if not st.role_sent:
        yield st.start()
    yield st.final()
    uc = st.usage_chunk()
    if uc:
        yield uc
    yield b"data: [DONE]\n\n"


async def _stream_google(source: AsyncIterator[bytes],
                         dep: dict) -> AsyncIterator[bytes]:
    st = _StreamState(dep)
    async for ev in _iter_data_json(source):
        um = ev.get("usageMetadata")
        if um:
            it = int(um.get("promptTokenCount") or 0)
            ot = int(um.get("candidatesTokenCount") or 0)
            st.usage = {"prompt_tokens": it, "completion_tokens": ot,
                        "total_tokens": int(um.get("totalTokenCount")
                                            or (it + ot))}
        for c in (ev.get("candidates") or []):
            if not isinstance(c, dict):
                continue
            fr = c.get("finishReason")
            if fr:
                st.finish = {"STOP": "stop", "MAX_TOKENS": "length",
                             "SAFETY": "content_filter",
                             "TOOL_CALLS": "tool_calls"}.get(fr, "stop")
            for p in ((c.get("content") or {}).get("parts") or []):
                if not isinstance(p, dict):
                    continue
                if p.get("text"):
                    if not st.role_sent:
                        yield st.start()
                    if p.get("thought"):
                        yield st.reasoning(p["text"])
                    else:
                        yield st.content(p["text"])
                fc = p.get("functionCall")
                if isinstance(fc, dict):
                    if not st.role_sent:
                        yield st.start()
                    yield st.tool_start("", _new_id("call"),
                                        fc.get("name") or "")
                    args = json.dumps(fc.get("args") or {}, ensure_ascii=False)
                    if args:
                        yield st.tool_frag_idx(st.tool_index, args)
    if not st.role_sent:
        yield st.start()
    yield st.final()
    uc = st.usage_chunk()
    if uc:
        yield uc
    yield b"data: [DONE]\n\n"


def stream_translator(style: str, source: AsyncIterator[bytes],
                      dep: dict) -> AsyncIterator[bytes]:
    """Ritorna un async iterator di SSE OpenAI tradotto dallo stile nativo."""
    style = normalize_style(style)
    if style == RESPONSES:
        return _stream_responses(source, dep)
    if style == MESSAGES:
        return _stream_messages(source, dep)
    if style == GOOGLE:
        return _stream_google(source, dep)
    return source
