"""Tool-call troncati: un tag tool-call APERTO e mai chiuso non deve mai
raggiungere il client. Il gateway trattiene la coda, salva la chiamata
parziale (nome+args -> tool_calls strutturata) cosi' l'agente continua,
altrimenti ruota in modo trasparente e declassa il deployment (cooldown breve).
"""
import asyncio
import json
import os
import tempfile
import time

import httpx
import pytest

from app.config import GatewayConfig
from app.forwarder import Forwarder
from app.policy import Policy
from app.router import Router
from app.texttoolparse import (TruncationConfig, has_unclosed_toolcall,
                               partial_opener_at_end,
                               salvage_truncated_toolcall,
                               truncation_config_from_policy)
from app.toolrepair import TruncatedToolcallSSEFilter

BASH = {"type": "function", "function": {"name": "bash"}}


def _ev(c: str) -> bytes:
    return ("data: " + json.dumps(
        {"choices": [{"index": 0, "delta": {"content": c},
                      "finish_reason": None}]}) + "\n\n").encode()


# ----------------------------------------------------------- detection
@pytest.mark.parametrize("text,expected", [
    ('ciao <tool_call>{"name":"bash"', True),
    ("<tool_call>{}</tool_call>", False),
    ('<function_call>{"x":1}', True),
    ("nessun tag qui", False),
    ("<function=foo></function>", False),
    ("<function=foo>", True),
    ("<tool_calls></tool_calls> e poi <tool_call>x", True),
])
def test_has_unclosed_toolcall(text, expected):
    assert has_unclosed_toolcall(text) is expected


def test_partial_opener_at_end():
    assert partial_opener_at_end("blah <too") is True
    assert partial_opener_at_end("blah <tool") is True
    assert partial_opener_at_end("blah testo") is False
    assert partial_opener_at_end("blah <") is False


# ----------------------------------------------------------- salvage
def test_salvage_truncated_json():
    calls = salvage_truncated_toolcall(
        'x <tool_call>{"name":"bash","arguments":{"command":"ls"', [BASH])
    assert calls
    assert calls[0]["function"]["name"] == "bash"
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls"}


def test_salvage_fuzzy_name():
    calls = salvage_truncated_toolcall(
        '<tool_call>{"name":"bas","arguments":{}}', [BASH])
    assert calls and calls[0]["function"]["name"] == "bash"


def test_salvage_undeclared_returns_none():
    assert salvage_truncated_toolcall(
        '<tool_call>{"name":"zzz","arguments":{}}', [BASH]) is None


def test_salvage_closed_returns_none():
    assert salvage_truncated_toolcall(
        '<tool_call>{"name":"bash","arguments":{}}</tool_call>', [BASH]) is None


def test_salvage_xml_returns_none():
    assert salvage_truncated_toolcall('<function=foo>', [BASH]) is None


# ----------------------------------------------------------- policy
def test_policy_parses_toolcall_truncation():
    p = Policy.from_dict({"toolcall_truncation": {
        "enabled": False, "cooldown_sec": 5, "holdback": False}})
    assert p.toolcall_truncation_enabled is False
    assert p.toolcall_truncation_cooldown_sec == 5
    assert p.toolcall_truncation_holdback is False
    cfg = truncation_config_from_policy(p)
    assert cfg.enabled is False and cfg.cooldown_sec == 5


def test_policy_defaults():
    p = Policy()
    assert p.toolcall_truncation_enabled is True
    assert p.toolcall_truncation_cooldown_sec == 30
    assert p.toolcall_truncation_holdback is True


def test_policy_invalid_cooldown():
    with pytest.raises(ValueError):
        Policy.from_dict({"toolcall_truncation": {"cooldown_sec": 0}})


# ----------------------------------------------------------- streaming filter
def test_filter_passthrough_normal():
    f = TruncatedToolcallSSEFilter("m", [BASH], TruncationConfig())
    out = f.feed(_ev("hello world")) + f.finalize()
    assert b"hello world" in b"".join(out)
    assert not f.truncated


def test_filter_salvages_open_tag():
    f = TruncatedToolcallSSEFilter("m", [BASH], TruncationConfig())
    out = f.feed(_ev('run <tool_call>{"name":"bash","arguments":{"command":"ls"'))
    out += f.finalize()
    joined = b"".join(out)
    assert b"<tool_call>" not in joined          # tag rotto mai emesso
    assert b'"tool_calls"' in joined
    assert b'"finish_reason": "tool_calls"' in joined
    assert b"run" in joined                       # prefisso conservato
    assert f.salvaged and f.truncated


def test_filter_drops_unsalvageable():
    f = TruncatedToolcallSSEFilter("m", [BASH], TruncationConfig())
    out = f.feed(_ev('prefix <tool_call>{"zzz":')) + f.finalize()
    joined = b"".join(out)
    assert b"<tool_call>" not in joined
    assert b"[DONE]" in joined
    assert f.truncated and not f.salvaged


def test_filter_releases_closed_tag():
    f = TruncatedToolcallSSEFilter("m", [BASH], TruncationConfig())
    out = f.feed(_ev('hi <tool_call>{"name":"bash"}</tool_call>')) + f.finalize()
    assert b"</tool_call>" in b"".join(out)       # rilasciato invariato
    assert not f.truncated


def test_filter_callback_on_truncation():
    seen = []
    f = TruncatedToolcallSSEFilter(
        "m", [BASH], TruncationConfig(),
        on_truncation=lambda salvaged: seen.append(salvaged))
    f.feed(_ev('<tool_call>{"name":"bash","arguments":{'))
    f.finalize()
    assert seen == [True]


def test_filter_disabled_passthrough():
    f = TruncatedToolcallSSEFilter(
        "m", [BASH], TruncationConfig(enabled=False))
    raw = _ev('x <tool_call>{"name":"bash"')
    assert f.feed(raw) == [raw]


# ----------------------------------------------------------- non-streaming
_CSV = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "a,broken,groq,https://broken.test/v1,free,128,8000,0,K1,text\n"
        "a,good,groq,https://good.test/v1,free,128,8000,0,K2,text\n")


def _mk_router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_CSV)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, pol)
    os.unlink(path)
    return router


def _deps(router):
    grp = "scrocco-llm-test-128k"
    broken = next(d for d in router.config.groups[grp]
                  if d["api_key"] == "K1")
    good = next(d for d in router.config.groups[grp]
                if d["api_key"] == "K2")
    return broken, good


def test_nonstream_salvages_truncated_toolcall():
    router = _mk_router()
    broken, _good = _deps(router)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": 'run <tool_call>{"name":"bash","arguments":{"command":"ls"'}}]})

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}],
               "tools": [BASH]}
    data, used = asyncio.run(
        fwd.call_with_fallback(router, "test", broken, payload))
    msg = data["choices"][0]["message"]
    assert used["unique"] == broken["unique"]
    assert msg.get("tool_calls")
    assert msg["content"] == ""
    args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
    assert args == {"command": "ls"}


def test_nonstream_rotates_unsalvageable():
    router = _mk_router()
    broken, good = _deps(router)

    def handler(request):
        if request.url.host == "broken.test":
            return httpx.Response(200, json={"choices": [{"message": {
                "content": '<tool_call>{"zzz":'}}]})
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}],
               "tools": [BASH]}
    router.fallback_next = lambda *a, **k: good
    data, used = asyncio.run(
        fwd.call_with_fallback(router, "test", broken, payload))
    assert used["unique"] == good["unique"]
    assert data["choices"][0]["message"]["content"] == "ok"
    remaining = router._cooldown[broken["unique"]] - time.time()
    assert 20 <= remaining <= 31
