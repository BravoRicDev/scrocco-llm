"""403 upstream: errore SEMPRE deployment-side (key/progetto rifiutato dal
provider, non colpa della richiesta). Il client non puo' farci nulla ->
si ruota sul prossimo deployment con cooldown lungo; catena esaurita ->
503 retryable (mai 403 pass-through).

Approccio strutturale (status code) niente regex: il contenuto del body
e' imprevedibile (linguaggio, provider, versioni) e non deve mai
classificare errori.
"""
import asyncio
import os
import tempfile
import time

import httpx
import pytest

from app.config import GatewayConfig
from app.forwarder import (Forwarder, UpstreamError,
                           PERMISSION_DENIED_COOLDOWN_S)
from app.policy import Policy
from app.router import Router


def test_cooldown_is_long():
    """Cooldown >= 1h (key/progetto rifiutato: non torna presto)."""
    assert PERMISSION_DENIED_COOLDOWN_S >= 3600


def test_not_actionable_at_chain_exhaustion():
    """A catena esaurita il 403 NON viene consegnato col suo status reale
    (non azionabile) -> 503 retryable."""
    from app.main import _actionable_upstream_error
    err = UpstreamError(-403, "whatever the provider says")
    assert not _actionable_upstream_error(err)


def test_various_403_bodies_all_not_actionable():
    """QUALSIASI 403 dal provider non e' azionabile dal client."""
    from app.main import _actionable_upstream_error
    for body in [
        '{"error":{"code":403,"status":"PERMISSION_DENIED"}}',
        '{"error":{"message":"Access denied to this model"}}',
        '{"error":{"message":"Project has been denied access"}}',
        'Forbidden',
        '{"message":"key disabled"}',
    ]:
        assert not _actionable_upstream_error(UpstreamError(-403, body)), \
            f"should NOT be actionable: {body}"


def test_plain_401_402_still_actionable():
    """401 e 402 restano actionable (auth, quota)."""
    from app.main import _actionable_upstream_error
    assert _actionable_upstream_error(UpstreamError(-401, "Unauthorized"))
    assert _actionable_upstream_error(UpstreamError(-402, "quota"))


# ----------------------------------------------------------------- integrazione
_CSV = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "a,broken,openrouter,https://openrouter.ai/api/v1,paid,128,8000,5,K1,\n"
        "a,good,groq,https://ok.test/v1,paid,128,8000,5,K2,text\n")


def _mk_router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_CSV)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, pol)
    os.unlink(path)
    return router


def test_403_any_body_rotates_never_reaches_client():
    """403 con QUALSIASI body deve RUOTARE sul deployment successivo, non
    passare al client.  Catena esausta -> 503 (non 403)."""
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")
    good = next(d for d in router.config.groups[grp] if d["api_key"] == "K2")

    # body irrilevante: lo status 403 basta a triggerare la rotazione
    ANY_403_BODY = '{"error":{"message":"anything goes"}}'

    route = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        route["n"] += 1
        if request.url.host == "openrouter.ai":
            return httpx.Response(403, content=ANY_403_BODY.encode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}
    # fallback_next guidato: dal broken si va sul good
    router.fallback_next = lambda *a, **k: good
    data, used = asyncio.run(
        fwd.call_with_fallback(router, "test", broken, payload))
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == good["unique"]
    assert route["n"] == 2                        # 2 chiamate: broken + good
    # key rifiutata: cooldown lungo (>= 1h)
    assert router._cooldown[broken["unique"]] - time.time() >= 0.9 * PERMISSION_DENIED_COOLDOWN_S


def test_403_all_exhausted_returns_503():
    """Tutti i deployment rispondono 403 -> 503 retryable,
    mai 403 pass-through al client."""
    router = _mk_router()
    grp = "scrocco-llm-test-fallback"
    broken = next(d for d in router.config.groups[grp] if d["api_key"] == "K1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b'{"error":{"message":"denied"}}')

    fwd = Forwarder(client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    payload = {"model": "x", "messages": [{"role": "user", "content": "ciao"}]}
    router.fallback_next = lambda *a, **k: None
    with pytest.raises(UpstreamError) as exc_info:
        asyncio.run(
            fwd.call_with_fallback(router, "test", broken, payload))
    # status negativo = -403; a catena esaurita, main.py lo consegna come 503
    assert exc_info.value.status == -403
    # e _actionable lo conferma NON azionabile -> il caller fa 503
    from app.main import _actionable_upstream_error
    assert not _actionable_upstream_error(exc_info.value)
