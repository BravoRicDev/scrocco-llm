"""Pool HTTP persistente per-origine: stesso host:port riusa lo stesso client
(keep-alive TCP/TLS), origini diverse hanno client distinti; il client
iniettato (test) vince sempre."""
import asyncio

import httpx

from app.forwarder import Forwarder, UPSTREAM_LIMITS


def test_limits_are_generous():
    assert UPSTREAM_LIMITS.max_keepalive_connections == 30
    assert UPSTREAM_LIMITS.max_connections == 100
    assert UPSTREAM_LIMITS.keepalive_expiry == 120.0


def test_client_reused_per_origin():
    f = Forwarder()
    a = f._client_for("https://api.groq.com/openai/v1/chat/completions")
    b = f._client_for("https://api.groq.com/openai/v1/models")
    c = f._client_for("https://openrouter.ai/api/v1/chat/completions")
    assert a is b
    assert a is not c
    assert set(f._clients) == {"https://api.groq.com",
                               "https://openrouter.ai"}
    asyncio.run(f.aclose())
    assert f._clients == {}


def test_injected_client_wins():
    inj = httpx.AsyncClient()
    f = Forwarder(client=inj)
    assert f._client_for("https://any/1") is inj
    assert f._client_for("https://other/2") is inj
    assert f.client is inj
    asyncio.run(f.aclose())
