"""Marker di tool-call NATIVI (NVIDIA Nemotron / Ling-inclusionAI).

Requisiti utente (2026-09-16):
1. i marker di template non devono MAI comparire nella risposta vera
   (`content`) consegnata al client, in nessun caso;
2. il rilevamento deve valere anche senza `tools` dichiarati;
3. se possibile si SALVA la chiamata (tool repair) altrimenti si RUOTA
   senza penalizzare il deployment;
4. il `reasoning_content` non viene toccato (li' non fanno danno).
"""
import json

from app.fakecall import (
    DEFAULT_PATTERNS, TemplateTokenStripper, looks_like_fake_tool_call,
    message_fake_pattern, sanitize_message, strip_template_tokens)
from app.texttoolparse import parse_text_toolcalls, partial_opener_at_end

# body reale osservato nel reasoning di un modello Nemotron via opencode
_NEMOTRON_BODY = (
    '<|tool_call>call:tool_fTBVuU0D_read'
    '{filePath:<|"|>/home/user/project/scripts/audit_models.py'
    '<|"|>}<tool_call|>')

_NATIVE_MARKERS = (
    "<|tool_call>", "<tool_call|>", "<|tool_calls>", "<tool_calls|>",
    "<|tool_response>", "<tool_response|>", "<|tool",
)


def test_default_patterns_includono_i_marker_nativi():
    for m in _NATIVE_MARKERS:
        assert m in DEFAULT_PATTERNS


def test_looks_like_fake_tool_call_rileva_il_body_nemotron():
    assert looks_like_fake_tool_call(_NEMOTRON_BODY)


def test_message_fake_pattern_senza_tools():
    """Il rilevamento NON richiede piu' `tools` nella richiesta."""
    data = {"choices": [{"message": {"content": _NEMOTRON_BODY}}]}
    assert message_fake_pattern(data, {}, _cfg()) is not None


def test_message_fake_pattern_ignora_se_ci_sono_tool_calls_strutturati():
    data = {"choices": [{"message": {
        "content": _NEMOTRON_BODY,
        "tool_calls": [{"id": "x", "function": {"name": "read",
                                                "arguments": "{}"}}]}}]}
    assert message_fake_pattern(data, {}, _cfg()) is None


def _cfg():
    from app.fakecall import FakeCallConfig
    return FakeCallConfig()


def test_strip_template_tokens_rimuove_i_marker():
    out = strip_template_tokens(_NEMOTRON_BODY)
    assert "<|tool_call>" not in out
    assert "<tool_call|>" not in out
    assert '<|"|>' not in out
    assert '"/home/user' in out          # virgoletta sostituita


def test_stripper_gestisce_token_spezzato_tra_chunk():
    s = TemplateTokenStripper()
    parts = ["risposta <|to", "ol_call>call:x{a:<|\"", "|>1<|\"|>}<tool_call|> fine"]
    got = "".join(s.feed(p) for p in parts) + s.flush()
    assert "<|tool" not in got and "<tool_call|>" not in got
    assert "<|\"|>" not in got
    assert got.startswith("risposta ") and got.endswith(" fine")


def test_stripper_prefisso_minimo_pipe():
    s = TemplateTokenStripper()
    got = "".join(s.feed(p) for p in ["a<|", '"', "|>b"]) + s.flush()
    assert got == 'a"b'


def test_stripper_non_perde_testo_normale():
    s = TemplateTokenStripper()
    got = "".join(s.feed(p) for p in ["the elegant ", "answer"]) + s.flush()
    assert got == "the elegant answer"


def test_sanitize_message_in_place():
    msg = {"role": "assistant", "content": _NEMOTRON_BODY}
    assert sanitize_message(msg) is True
    assert "<|tool_call>" not in msg["content"]
    assert sanitize_message({"content": "testo pulito"}) is False


def test_salvataggio_formato_nemotron():
    tools = [{"type": "function",
              "function": {"name": "read", "parameters": {}}}]
    calls = parse_text_toolcalls(_NEMOTRON_BODY, tools)
    assert calls and calls[0]["function"]["name"] == "read"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args["filePath"].endswith("scripts/audit_models.py")


def test_salvataggio_tool_call_json_con_pipe():
    tools = [{"type": "function",
              "function": {"name": "terminal", "parameters": {}}}]
    body = '<|tool_call>{"name":"terminal","arguments":{"cmd":"ls"}}<tool_call|>'
    calls = parse_text_toolcalls(body, tools)
    assert calls and calls[0]["function"]["name"] == "terminal"


def test_partial_opener_riconosce_il_pipe():
    assert partial_opener_at_end("bla <|tool_ca")


def test_strip_sse_content_end_to_end():
    """Lo stream consegnato non contiene MAI i marker, e la coda e' flushatta
    sul chunk terminale (nessun testo perso)."""
    from app.main import _strip_sse_content

    def chunk(txt, fr=None):
        d = {"delta": {"content": txt}}
        if fr:
            d["finish_reason"] = fr
        return b"data: " + json.dumps({"choices": [d]}).encode() + b"\n\n"

    s = TemplateTokenStripper()
    parts = "elegant <|tool_call>call:read{a:<|\"|>x<|\"|>}<tool_call|> done"
    step = 7
    out = b""
    for i in range(0, len(parts), step):
        out += _strip_sse_content(chunk(parts[i:i + step]), s)
    out += _strip_sse_content(chunk("", fr="stop"), s)

    seen = ""
    for line in out.split(b"\n"):
        line = line.strip()
        if line.startswith(b"data:"):
            body = line[5:].strip()
            if body and body != b"[DONE]":
                obj = json.loads(body)
                seen += (obj["choices"][0]["delta"].get("content") or "")
    assert "<|tool" not in seen and "<tool_call|>" not in seen
    assert '<|"|>' not in seen
    assert seen.startswith("elegant ") and seen.endswith(" done")
