"""Test dei traduttori di protocollo (app/protocols.py).

Verificano la conversione Chat Completions <-> Responses / Messages / Google
sia per le richieste sia per le risposte (JSON e streaming SSE), senza rete.
"""
import asyncio
import json

import pytest

from app import protocols as P


DEP = {"model": "zen-model", "api_base": "https://api.test/v1",
       "api_key": "sk-test"}


def _dep(style):
    return {**DEP, "api_style": style}


async def _aiter(chunks):
    for c in chunks:
        yield c


async def _collect(agen):
    out = b""
    async for b in agen:
        out += b
    return out


def _events(blob: bytes):
    """Estrae gli oggetti JSON dai chunk SSE (ignora [DONE])."""
    out = []
    for line in blob.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == b"[DONE]":
            continue
        out.append(json.loads(payload))
    return out


def _sse(objs):
    return b"".join(b"data: " + json.dumps(o).encode() + b"\n\n"
                    for o in objs)


# ------------------------------------------------------------------ style/url
def test_normalize_and_style():
    assert P.normalize_style(None) == P.CHAT
    assert P.normalize_style("OPENAI-RESPONSES") == P.CHAT
    assert P.normalize_style("responses") == P.RESPONSES
    assert P.normalize_style("messages") == P.MESSAGES
    assert P.normalize_style("google") == P.GOOGLE
    assert P.style_of(_dep("google")) == P.GOOGLE


def test_build_url():
    assert P.build_url(_dep("chat"), stream=False).endswith(
        "/chat/completions")
    assert P.build_url(_dep("responses"), stream=True).endswith("/responses")
    assert P.build_url(_dep("messages"), stream=False).endswith("/messages")
    assert P.build_url(_dep("google"), stream=False).endswith(
        "/models/zen-model:generateContent")
    assert P.build_url(_dep("google"), stream=True).endswith(
        "/models/zen-model:streamGenerateContent?alt=sse")


def test_apply_auth():
    h = P.apply_auth(_dep("messages"), {"Authorization": "Bearer x"})
    assert "Authorization" not in h and h["x-api-key"] == "sk-test"
    assert h["anthropic-version"] == "2023-06-01"
    g = P.apply_auth(_dep("google"), {"Authorization": "Bearer x"})
    assert "Authorization" not in g and g["x-goog-api-key"] == "sk-test"
    c = P.apply_auth(_dep("chat"), {"Authorization": "Bearer x"})
    assert c["Authorization"] == "Bearer x"


# ------------------------------------------------------------------- responses
def test_chat_to_responses():
    body = {
        "model": "zen-model",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": "ok", "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "f", "arguments": "{\"x\":1}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "f", "description": "d",
            "parameters": {"type": "object", "properties": {}}}}],
        "max_tokens": 123,
        "temperature": 0.3,
        "stream": True,
    }
    out = P.chat_to_responses(body, DEP)
    assert out["instructions"] == "sys"
    assert out["max_output_tokens"] == 123
    assert out["stream"] is True and out["temperature"] == 0.3
    types = [i.get("type") or i.get("role") for i in out["input"]]
    assert types == ["user", "assistant", "function_call",
                     "function_call_output"]
    fc = [i for i in out["input"] if i.get("type") == "function_call"][0]
    assert fc["call_id"] == "c1" and fc["name"] == "f"
    assert out["tools"][0]["type"] == "function"
    assert out["tools"][0]["name"] == "f"


def test_responses_to_chat():
    obj = {
        "id": "resp_1", "model": "zen-model", "created_at": 111,
        "output": [
            {"type": "reasoning"},
            {"type": "message", "content": [
                {"type": "output_text", "text": "hello"}]},
            {"type": "function_call", "call_id": "c1", "name": "f",
             "arguments": "{\"a\":1}"},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }
    out = P.responses_to_chat(obj, DEP)
    msg = out["choices"][0]["message"]
    assert msg["content"] == "hello"
    assert msg["tool_calls"][0]["function"]["name"] == "f"
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"] == {"prompt_tokens": 5, "completion_tokens": 3,
                            "total_tokens": 8}


def test_stream_responses_text_and_tools():
    objs = [
        {"type": "response.created",
         "response": {"id": "resp_9", "model": "zen"}},
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        {"type": "response.output_item.added", "item": {
            "type": "function_call", "id": "it_1", "call_id": "c_1",
            "name": "f"}},
        {"type": "response.function_call_arguments.delta", "item_id": "it_1",
         "delta": "{\"a\":1}"},
        {"type": "response.completed", "response": {
            "usage": {"input_tokens": 2, "output_tokens": 4}}},
    ]
    blob = asyncio.run(_collect(P.stream_translator(
        P.RESPONSES, _aiter([_sse(objs)]), _dep("responses"))))
    evs = _events(blob)
    text = "".join(e["choices"][0]["delta"].get("content", "")
                   for e in evs if e.get("choices"))
    assert text == "Hello"
    assert any(e.get("choices") and e["choices"][0]["delta"].get("tool_calls")
               for e in evs)
    assert evs[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert evs[-1]["usage"]["completion_tokens"] == 4
    assert blob.endswith(b"data: [DONE]\n\n")


# -------------------------------------------------------------------- messages
def test_chat_to_messages():
    body = {
        "model": "zen-model",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "f", "arguments": "{\"x\":1}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "res"},
            {"role": "tool", "tool_call_id": "c2", "content": "res2"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "f", "description": "d", "parameters": {"type": "object"}}}],
        "max_tokens": 50,
    }
    out = P.chat_to_messages(body, DEP)
    assert out["system"] == "sys"
    assert out["max_tokens"] == 50
    assert out["tools"][0]["input_schema"] == {"type": "object"}
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant", "user"]      # 2 tool fusi
    tr = out["messages"][2]["content"]
    assert [b["type"] for b in tr] == ["tool_result", "tool_result"]
    tu = out["messages"][1]["content"][0]
    assert tu["type"] == "tool_use" and tu["input"] == {"x": 1}


def test_chat_to_messages_default_max_tokens():
    out = P.chat_to_messages({"messages": [{"role": "user",
                                            "content": "x"}]}, DEP)
    assert out["max_tokens"] == 4096


def test_messages_to_chat():
    obj = {
        "id": "msg_1", "model": "zen-model",
        "content": [{"type": "text", "text": "hi"},
                    {"type": "tool_use", "id": "t1", "name": "f",
                     "input": {"a": 2}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }
    out = P.messages_to_chat(obj, DEP)
    assert out["choices"][0]["message"]["content"] == "hi"
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["choices"][0]["message"]["tool_calls"][0]["function"][
        "arguments"] == '{"a": 2}'


def test_stream_messages():
    objs = [
        {"type": "message_start", "message": {"id": "m1", "model": "zen",
         "usage": {"input_tokens": 3}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "Hi"}},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "t1", "name": "f"}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": "{\"x\":2}"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]
    blob = asyncio.run(_collect(P.stream_translator(
        P.MESSAGES, _aiter([_sse(objs)]), _dep("messages"))))
    evs = _events(blob)
    text = "".join(e["choices"][0]["delta"].get("content", "")
                   for e in evs if e.get("choices"))
    assert text == "Hi"
    assert evs[-2]["choices"][0]["finish_reason"] == "tool_calls"
    assert evs[-1]["usage"]["completion_tokens"] == 5


# ---------------------------------------------------------------------- google
def test_chat_to_gemini():
    body = {
        "model": "zen-model",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "ciao"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "f", "arguments": "{\"x\":1}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "f",
             "content": "res"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "f", "description": "d", "parameters": {"type": "object"}}}],
        "max_tokens": 77, "temperature": 0.2,
    }
    out = P.chat_to_gemini(body, DEP)
    assert out["systemInstruction"]["parts"][0]["text"] == "sys"
    roles = [c["role"] for c in out["contents"]]
    assert roles == ["user", "model", "user"]
    assert out["contents"][1]["parts"][0]["functionCall"]["name"] == "f"
    fr = out["contents"][2]["parts"][0]["functionResponse"]
    assert fr["name"] == "f" and fr["response"] == {"result": "res"}
    assert out["tools"][0]["functionDeclarations"][0]["name"] == "f"
    assert out["generationConfig"]["maxOutputTokens"] == 77


def test_chat_to_gemini_image():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "vedi"},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64,AAAA"}}]}]}
    out = P.chat_to_gemini(body, DEP)
    parts = out["contents"][0]["parts"]
    assert parts[1]["inlineData"] == {"mimeType": "image/png", "data": "AAAA"}


def test_gemini_to_chat():
    obj = {
        "responseId": "r1", "modelVersion": "zen-model",
        "candidates": [{"content": {"parts": [
            {"text": "hey"},
            {"functionCall": {"name": "f", "args": {"a": 1}}}]},
            "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3,
                          "totalTokenCount": 5},
    }
    out = P.gemini_to_chat(obj, DEP)
    assert out["choices"][0]["message"]["content"] == "hey"
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"]["total_tokens"] == 5


def test_stream_google():
    objs = [
        {"candidates": [{"content": {"parts": [{"text": "Hi"}]}}]},
        {"candidates": [{"content": {"parts": [{"text": " there"}]},
                         "finishReason": "STOP"}],
         "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2,
                           "totalTokenCount": 6}},
    ]
    blob = asyncio.run(_collect(P.stream_translator(
        P.GOOGLE, _aiter([_sse(objs)]), _dep("google"))))
    evs = _events(blob)
    text = "".join(e["choices"][0]["delta"].get("content", "")
                   for e in evs if e.get("choices"))
    assert text == "Hi there"
    assert evs[-2]["choices"][0]["finish_reason"] == "stop"
    assert evs[-1]["usage"]["total_tokens"] == 6


# ------------------------------------------------------------------ dispatchers
def test_translate_request_and_response_dispatch():
    body = {"messages": [{"role": "user", "content": "x"}]}
    assert P.translate_request(P.CHAT, body, DEP) is body
    assert "input" in P.translate_request(P.RESPONSES, body, DEP)
    assert "contents" in P.translate_request(P.GOOGLE, body, DEP)
    obj = {"choices": [{"message": {"content": "x"},
                        "finish_reason": "stop"}]}
    assert P.translate_response(P.CHAT, obj, DEP) is obj


def test_chat_obj_to_sse():
    obj = {"id": "c1", "created": 1, "model": "m",
           "choices": [{"index": 0, "message": {"role": "assistant",
                        "content": "hi"}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                     "total_tokens": 2}}
    blob = b"".join(P.chat_obj_to_sse(obj))
    evs = _events(blob)
    assert evs[1]["choices"][0]["delta"]["content"] == "hi"
    assert evs[-1]["usage"]["total_tokens"] == 2
    assert blob.endswith(b"data: [DONE]\n\n")


def test_config_parses_api_style(tmp_path):
    from app.config import GatewayConfig
    csv = tmp_path / "k.csv"
    csv.write_text(
        "commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,api_style\n"
        "a@b.com,m1,groq,https://x/v1,free,64,8000,0,K1,google\n"
        "a@b.com,m2,groq,https://x/v1,free,64,8000,0,K2,\n"
        "a@b.com,m3,groq,https://x/v1,free,64,8000,0,K3,responses\n")
    cfg = GatewayConfig(str(csv), proxy_prefix="scrocco-llm-", seed=1)
    by_model = {d["model"]: d for g in cfg.groups.values() for d in g}
    assert by_model["m1"]["api_style"] == "google"
    assert by_model["m2"]["api_style"] == "chat"
    assert by_model["m3"]["api_style"] == "responses"
