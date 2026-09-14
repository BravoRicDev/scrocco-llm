"""Pool HTTP persistente.

Default (keepalive_pool=False): UNICO client condiviso per tutto
(comportamento storico, tornati a questo). Con keepalive_pool=True: pool
DEDICATO PER API-KEY (comune per modello): la stessa chiave riusa lo stesso
client anche su URL diversi; chiavi diverse hanno client distinti. Il client
iniettato (test) vince sempre.
"""
import asyncio

import httpx

from app.forwarder import Forwarder, UPSTREAM_LIMITS


def test_limits_are_generous():
    assert UPSTREAM_LIMITS.max_keepalive_connections == 30
    assert UPSTREAM_LIMITS.max_connections == 100
    assert UPSTREAM_LIMITS.keepalive_expiry == 120.0


def test_default_single_shared_client():
    """Default: nessun pool, un unico client condiviso (come prima del
    commit 80fbf72)."""
    f = Forwarder()
    a = f._client_for("https://api.groq.com/openai/v1/chat/completions",
                      "key-A")
    b = f._client_for("https://openrouter.ai/api/v1/chat/completions",
                      "key-B")
    c = f._client_for("https://api.groq.com/openai/v1/models", "key-A")
    assert a is b
    assert a is c
    assert f._clients == {}
    assert f.client is a
    asyncio.run(f.aclose())
    assert f.client is None or f._clients == {}


def test_pool_per_apikey():
    """Pool attivo: una chiave ha il suo client (condiviso tra i modelli/
    URL che serve), chiavi diverse hanno client distinti."""
    f = Forwarder(keepalive_pool=True)
    a = f._client_for("https://api.groq.com/openai/v1/chat/completions",
                      "key-A")
    a2 = f._client_for("https://api.groq.com/openai/v1/models", "key-A")
    b = f._client_for("https://api.groq.com/openai/v1/chat/completions",
                      "key-B")
    c = f._client_for("https://openrouter.ai/api/v1/chat/completions",
                      "key-A")
    assert a is a2              # stessa chiave, URL diversi -> stesso client
    assert a is c               # stessa chiave, provider diverso -> stesso client
    assert a is not b           # chiavi diverse -> client distinti
    assert set(f._clients) == {"key-A", "key-B"}
    asyncio.run(f.aclose())
    assert f._clients == {}


def test_pool_fallback_url_when_no_key():
    """Chiave vuota -> il pool ricade sul per-URL (mai crash)."""
    f = Forwarder(keepalive_pool=True)
    a = f._client_for("https://api.groq.com/v1")
    b = f._client_for("https://api.groq.com/v1")
    assert a is b
    assert set(f._clients) == {"https://api.groq.com/v1"}
    asyncio.run(f.aclose())


def test_injected_client_wins():
    inj = httpx.AsyncClient()
    f = Forwarder(client=inj, keepalive_pool=True)
    assert f._client_for("https://any/1", "key-A") is inj
    assert f._client_for("https://other/2", "key-B") is inj
    assert f.client is inj
    asyncio.run(f.aclose())