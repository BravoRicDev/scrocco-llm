"""thought_signature sidecar: cattura dalle risposte Google Gemini 3 e
re-iniezione nei replay diretti a Google.

Unit-level per la cache/estratti/iniezione + un end-to-end Forwarder con
httpx.MockTransport (nessuna rete). Il singleton di processo THOUGHT_SIGS
viene sostituito con una cache fresca per test (monkeypatch su forwarder).
"""
import asyncio
import json

import httpx
import pytest

from app import forwarder
from app import thought_sig
from app.forwarder import Forwarder
from app.thought_sig import (
    THOUGHT_SIGS,
    ThoughtSignatureCache,
    extract_signatures,
    is_google_base,
)

GOOGLE_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


@pytest.fixture
def fresh_cache(monkeypatch):
    """Sostituisce il singleton THOUGHT_SIGS del forwarder con una cache vuota
    (isolamento fra test, nessuna dipendenza dall'ordine)."""
    cache = ThoughtSignatureCache()
    monkeypatch.setattr(forwarder, "THOUGHT_SIGS", cache)
    return cache


class _FakeTime:
    """time.time() controllabile senza toccare il modulo stdlib condiviso."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def time(self) -> float:
        return self.t


def _dep(base: str = GOOGLE_BASE) -> dict:
    return {"unique": "g1", "model": "gemini-3.5-flash", "api_base": base,
            "api_key": "k", "group": "g", "priority": 0}


def _replay_payload() -> dict:
    return {"model": "x", "messages": [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "result"}]}


# --------------------------------------------------------------- is_google ----

def test_is_google_base_true_false():
    assert is_google_base(GOOGLE_BASE)
    assert is_google_base("https://generativelanguage.googleapis.com")
    assert not is_google_base("https://api.mistral.ai/v1")
    assert not is_google_base("https://openrouter.ai/api/v1")
    assert not is_google_base("")
    assert not is_google_base(None)


# -------------------------------------------------------- extract_signatures ----

def test_extract_signatures_from_message():
    msg = {"role": "assistant", "tool_calls": [
        {"id": "call_1", "type": "function",
         "extra_content": {"google": {"thought_signature": "SIG1"}},
         "function": {"name": "f", "arguments": "{}"}},
        {"id": "call_2", "type": "function",          # senza firma
         "function": {"name": "g", "arguments": "{}"}},
        {"type": "function",                          # senza id
         "extra_content": {"google": {"thought_signature": "SIG3"}}},
    ]}
    assert extract_signatures(msg) == {"call_1": "SIG1"}


def test_extract_signatures_absent():
    assert extract_signatures({"role": "assistant", "content": "hi"}) == {}
    assert extract_signatures({"tool_calls": []}) == {}
    assert extract_signatures({}) == {}
    assert extract_signatures(None) == {}
    assert extract_signatures("not-a-dict") == {}


def test_extract_signatures_handles_malformed_extra_content():
    msg = {"tool_calls": [
        {"id": "a"},                               # extra_content assente
        {"id": "b", "extra_content": {"google": "non-dict"}},
        {"id": "c", "extra_content": {"google": {"thought_signature": ""}}},
        {"id": "d", "extra_content": {"google": {"thought_signature": "S"}}},
    ]}
    assert extract_signatures(msg) == {"d": "S"}


# ---------------------------------------------------------- ThoughtSignatureCache

def test_cache_store_get():
    c = ThoughtSignatureCache()
    c.store("call_1", "SIG")
    assert c.get("call_1") == "SIG"
    assert c.get("missing") is None
    assert c.get("") is None
    assert len(c) == 1


def test_cache_store_ignores_empty():
    c = ThoughtSignatureCache()
    c.store("", "SIG")
    c.store("call_1", "")
    c.store(None, None)
    assert len(c) == 0


def test_cache_store_many():
    c = ThoughtSignatureCache()
    c.store_many({"a": "1", "b": "2"})
    assert c.get("a") == "1" and c.get("b") == "2"
    c.store_many(None)                       # no crash
    c.store_many({})
    assert len(c) == 2


def test_cache_ttl_expiry(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(thought_sig, "time", fake)
    c = ThoughtSignatureCache(ttl_sec=10.0)
    c.store("call_1", "SIG")
    assert c.get("call_1") == "SIG"
    fake.t += 11.0
    assert c.get("call_1") is None
    assert len(c) == 0                       # get scaduto rimuove la voce


def test_cache_max_size_eviction(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(thought_sig, "time", fake)
    c = ThoughtSignatureCache(max_size=2, ttl_sec=86400.0)
    c.store("a", "1")
    fake.t += 1.0
    c.store("b", "2")
    fake.t += 1.0
    c.store("c", "3")                        # overflow -> butta il piu' vecchio
    assert len(c) == 2
    assert c.get("a") is None
    assert c.get("b") == "2"
    assert c.get("c") == "3"


def test_cache_singleton_is_instance():
    assert isinstance(THOUGHT_SIGS, ThoughtSignatureCache)


# ----------------------------------------------------- _inject_thought_signatures

def test_inject_adds_missing_sig_and_keeps_payload_untouched(fresh_cache):
    fresh_cache.store("call_1", "SIG1")
    tc = {"id": "call_1", "type": "function",
          "function": {"name": "f", "arguments": "{}"}}
    payload = {"model": "x", "messages": [
        {"role": "assistant", "content": "", "tool_calls": [tc]}]}
    body = dict(payload)
    forwarder._inject_thought_signatures(body)
    got = body["messages"][0]["tool_calls"][0]
    assert got["extra_content"]["google"]["thought_signature"] == "SIG1"
    # il payload ORIGINALE non viene inquinato (nessun extra_content)
    assert "extra_content" not in payload["messages"][0]["tool_calls"][0]
    assert payload["messages"][0]["tool_calls"][0] is tc


def test_inject_keeps_existing_signature(fresh_cache):
    fresh_cache.store("call_1", "CACHED")
    tc = {"id": "call_1", "type": "function",
          "extra_content": {"google": {"thought_signature": "CLIENT"}},
          "function": {"name": "f", "arguments": "{}"}}
    body = {"messages": [{"role": "assistant", "tool_calls": [tc]}]}
    forwarder._inject_thought_signatures(body)
    got = body["messages"][0]["tool_calls"][0]
    assert got["extra_content"]["google"]["thought_signature"] == "CLIENT"


def test_inject_no_cache_entry_leaves_message_untouched(fresh_cache):
    tc = {"id": "call_1", "type": "function",
          "function": {"name": "f", "arguments": "{}"}}
    m = {"role": "assistant", "tool_calls": [tc]}
    body = {"messages": [m]}
    forwarder._inject_thought_signatures(body)
    assert "extra_content" not in body["messages"][0]["tool_calls"][0]
    assert body["messages"][0]["tool_calls"][0] is tc


def test_inject_skips_non_assistant_and_tool_call_without_id(fresh_cache):
    fresh_cache.store("call_1", "SIG1")
    user = {"role": "user", "content": "ciao",
            "tool_calls": [{"id": "call_1", "function": {}}]}
    ass_no_id = {"role": "assistant",
                 "tool_calls": [{"type": "function", "function": {}}]}
    body = {"messages": [user, ass_no_id]}
    forwarder._inject_thought_signatures(body)
    assert "extra_content" not in body["messages"][0]["tool_calls"][0]
    assert "extra_content" not in body["messages"][1]["tool_calls"][0]


def test_inject_ignores_non_list_messages(fresh_cache):
    body = {"messages": "nope"}
    forwarder._inject_thought_signatures(body)     # no crash
    assert body == {"messages": "nope"}


# -------------------------------------------------------- _capture_sigs_from_obj

def test_capture_from_obj_message(fresh_cache):
    obj = {"choices": [{"message": {"role": "assistant", "tool_calls": [
        {"id": "call_1", "type": "function",
         "extra_content": {"google": {"thought_signature": "SIG"}}}]}}]}
    forwarder._capture_sigs_from_obj(obj)
    assert fresh_cache.get("call_1") == "SIG"


def test_capture_from_obj_delta(fresh_cache):
    obj = {"choices": [{"delta": {"tool_calls": [
        {"id": "call_2",
         "extra_content": {"google": {"thought_signature": "S2"}}}]}}]}
    forwarder._capture_sigs_from_obj(obj)
    assert fresh_cache.get("call_2") == "S2"


def test_capture_from_obj_ignores_non_dict_and_empty(fresh_cache):
    forwarder._capture_sigs_from_obj(None)
    forwarder._capture_sigs_from_obj("x")
    forwarder._capture_sigs_from_obj({})
    forwarder._capture_sigs_from_obj({"choices": [None, "x", {}]})
    assert len(fresh_cache) == 0


# -------------------------------------------------------- _capture_sigs_from_sse

def test_capture_from_sse_line(fresh_cache):
    line = (b'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1",'
            b'"extra_content":{"google":{"thought_signature":"SIG"}}}]}}]}\n')
    forwarder._capture_sigs_from_sse(line)
    assert fresh_cache.get("call_1") == "SIG"


def test_capture_from_sse_tolerates_garbage(fresh_cache):
    forwarder._capture_sigs_from_sse(b": keep-alive comment\n")
    forwarder._capture_sigs_from_sse(b"data: [DONE]\n")
    forwarder._capture_sigs_from_sse(b"data: not-json\n")
    forwarder._capture_sigs_from_sse(b"event: ping\n")
    forwarder._capture_sigs_from_sse(b"")
    assert len(fresh_cache) == 0


# ------------------------------------------------------------------- end-to-end

def test_end_to_end_capture_then_replay_injects_signature(fresh_cache):
    """1) una call() Google risponde con tool_calls + firma -> in cache;
    2) il replay (tool_call senza firma) diretto a Google viene re-iniettato
    nel body OUTBOUND, senza mutare il payload del chiamante."""
    seen_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.read().decode()))
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "extra_content": {"google": {"thought_signature": "SIG-ABC"}},
                "function": {"name": "f", "arguments": "{}"}}]}}]})

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))

    # 1) prima chiamata: cattura la firma dalla risposta Google
    asyncio.run(fwd.call(_dep(), {"model": "x",
                                  "messages": [{"role": "user",
                                                "content": "hi"}]}))
    assert fresh_cache.get("call_1") == "SIG-ABC"

    # 2) replay: assistant tool_call SENZA firma -> deve essere iniettata
    payload = _replay_payload()
    asyncio.run(fwd.call(_dep(), payload))
    out = seen_bodies[-1]
    got = out["messages"][0]["tool_calls"][0]
    assert got["extra_content"]["google"]["thought_signature"] == "SIG-ABC"
    # payload del chiamante intatto (nessun extra_content)
    assert "extra_content" not in payload["messages"][0]["tool_calls"][0]


def test_end_to_end_non_google_does_not_inject(fresh_cache):
    """Su un api_base non-Google il sidecar NON tocca la richiesta, anche con
    la firma in cache."""
    seen_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_bodies.append(json.loads(request.read().decode()))
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "ok"}}]})

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    fresh_cache.store("call_1", "SIG-ABC")
    asyncio.run(fwd.call(_dep(base="https://api.mistral.ai/v1"),
                         _replay_payload()))
    out = seen_bodies[-1]
    assert "extra_content" not in out["messages"][0]["tool_calls"][0]


def test_end_to_end_stream_captures_signature(fresh_cache):
    """Lo stream Google cattura la firma dai chunk SSE mentre fluiscono."""
    sse = (b'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1",'
           b'"extra_content":{"google":{"thought_signature":"SIG-S"}}}]}}]}\n\n'
           b'data: [DONE]\n\n')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200,
                              headers={"content-type": "text/event-stream"},
                              content=sse)

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [], "stream": True}

    async def _run():
        gen = await fwd.stream_response(_dep(), payload)
        async for _ in gen:
            pass

    asyncio.run(_run())
    assert fresh_cache.get("call_1") == "SIG-S"


# ---------------------------------------- has_unsigned_tool_calls (routing) ---

def _asst(tool_calls):
    return {"role": "assistant", "content": "", "tool_calls": tool_calls}


def _tc(tcid, sig=None):
    tc = {"id": tcid, "type": "function",
          "function": {"name": "f", "arguments": "{}"}}
    if sig:
        tc["extra_content"] = {"google": {"thought_signature": sig}}
    return tc


def test_unsigned_no_tool_calls(fresh_cache, monkeypatch):
    monkeypatch.setattr(thought_sig, "THOUGHT_SIGS", fresh_cache)
    assert not thought_sig.has_unsigned_tool_calls([])
    assert not thought_sig.has_unsigned_tool_calls(
        [{"role": "user", "content": "hi"}])


def test_unsigned_tool_call_without_signature(fresh_cache, monkeypatch):
    monkeypatch.setattr(thought_sig, "THOUGHT_SIGS", fresh_cache)
    msgs = [_asst([_tc("c1")])]
    assert thought_sig.has_unsigned_tool_calls(msgs) is True


def test_unsigned_resolved_from_cache(fresh_cache, monkeypatch):
    monkeypatch.setattr(thought_sig, "THOUGHT_SIGS", fresh_cache)
    msgs = [_asst([_tc("c1")])]
    fresh_cache.store("c1", "SIG")
    assert thought_sig.has_unsigned_tool_calls(msgs) is False


def test_unsigned_inline_signature(fresh_cache, monkeypatch):
    monkeypatch.setattr(thought_sig, "THOUGHT_SIGS", fresh_cache)
    msgs = [_asst([_tc("c2", sig="X")])]
    assert thought_sig.has_unsigned_tool_calls(msgs) is False


def test_unsigned_mixed(fresh_cache, monkeypatch):
    monkeypatch.setattr(thought_sig, "THOUGHT_SIGS", fresh_cache)
    msgs = [_asst([_tc("c2", sig="X"), _tc("c9")])]
    assert thought_sig.has_unsigned_tool_calls(msgs) is True


# --------------------------------------- is_gemini_deployment (OpenRouter) ----

def test_is_gemini_deployment_google_direct():
    d = {"api_base": GOOGLE_BASE, "model": "models/gemini-3.5-flash"}
    assert thought_sig.is_gemini_deployment(d)


def test_is_gemini_deployment_openrouter_gemini():
    d = {"api_base": "https://openrouter.ai/api/v1",
         "model": "google/gemini-3.5-flash"}
    assert thought_sig.is_gemini_deployment(d)


def test_is_gemini_deployment_openrouter_non_gemini():
    d = {"api_base": "https://openrouter.ai/api/v1", "model": "nvidia/nemotron"}
    assert not thought_sig.is_gemini_deployment(d)


def test_is_gemini_deployment_non_google_non_gemini():
    d = {"api_base": "https://api.mistral.ai/v1", "model": "mistral-large"}
    assert not thought_sig.is_gemini_deployment(d)
