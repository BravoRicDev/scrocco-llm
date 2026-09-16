"""QUOTA (429) vs RIFIUTO DI SCHEMA.

Osservato su mioaruba (2026-09-16 06:17): un 429 di quota GIORNALIERA di
Cloudflare Workers AI

    {"errors":[{"message":"AiError: AiError: you have used up your daily free
     allocation of 10,000 neurons, please upgrade ...","code":4006}],
     "success":false,...}

veniva classificato `motivo=payload_schema` per via del pattern troppo largo
`\\baierror\\b` dentro `_PAYLOAD_SCHEMA_RE`: il deployment ruotava SENZA
cooldown e la catena bruciava le ~150 chiavi sorelle (tutte con la stessa
quota esaurita).

Ora: e' una QUOTA -> cooldown fino alla mezzanotte UTC (o al reset dichiarato)
e il body di quota non viene piu' scambiato per un rifiuto di schema.
"""
import asyncio
import os
import tempfile
import time

import httpx
import pytest

from app.config import GatewayConfig
from app.forwarder import (Forwarder, UpstreamError, QUOTA_MIN_COOLDOWN_S,
                           _PAYLOAD_SCHEMA_RE, _QUOTA_EXHAUSTED_RE,
                           classify_error_class, parse_quota_reset_seconds)
from app.policy import Policy
from app.router import Router

CF_QUOTA = ('{"errors":[{"message":"AiError: AiError: you have used up your '
            'daily free allocation of 10,000 neurons, please upgrade to '
            "Cloudflare's Workers Paid plan if you would like to continue "
            'usage. (51afe8c9-9139-446e-9284-928af8ee842d)","code":4006}],'
            '"success":false,"result":{},"messages":[]}')
CF_SCHEMA = ('{"errors":[{"message":"AiError: Bad input: Error: oneOf at '
             "'/' not met, 0 matches: required properties at '/' are "
             "'prompt', Type mismatch of '/messages/0/content', 'array' not "
             'in \'string\'","code":5006}],"success":false,"result":{},'
             '"messages":[]}')
RESET_30M = ('{"error":{"message":"GoUsageLimitError: usage limit reached. '
             'Resets in 30 minutes"}}')

_CSV = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "a,qs-broken,openrouter,https://openrouter.ai/api/v1,paid,128,8000,5,K1,\n"
        "a,qs-good,groq,https://ok.test/v1,paid,128,8000,5,K2,\n")


def _mk():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    pol = Policy.from_dict({"capability_routing": {"model_capabilities": {}}})
    router = Router(cfg, pol)
    os.unlink(path)
    grp = next(g for g, deps in cfg.groups.items()
               if any(d["api_key"] == "K1" for d in deps))
    broken = next(d for d in cfg.groups[grp] if d["api_key"] == "K1")
    good = next(d for d in cfg.groups[grp] if d["api_key"] == "K2")
    return cfg, router, broken, good


# ------------------------------------------------------------- classificatori
def test_body_di_quota_non_e_piu_scambiato_per_schema():
    assert _QUOTA_EXHAUSTED_RE.search(CF_QUOTA)
    assert not _PAYLOAD_SCHEMA_RE.search(CF_QUOTA)      # niente "AiError" nudo
    # il VERO rifiuto di schema di CF resta riconosciuto
    assert _PAYLOAD_SCHEMA_RE.search(CF_SCHEMA)
    assert not _QUOTA_EXHAUSTED_RE.search(CF_SCHEMA)


def test_classify_quota_prima_di_schema():
    assert classify_error_class(-429, CF_QUOTA) == "quota"
    assert classify_error_class(-400, CF_SCHEMA) == "payload_schema"


def test_parse_reset_quota_giornaliera_va_a_mezzanotte_utc():
    secs = parse_quota_reset_seconds(CF_QUOTA)
    assert QUOTA_MIN_COOLDOWN_S <= secs <= 86400.0
    # coerente con i secondi mancanti alla mezzanotte UTC
    to_mid = 86400.0 - (time.time() % 86400.0)
    assert abs(secs - to_mid) < 5.0


def test_parse_reset_esplicito_resta_quello():
    assert parse_quota_reset_seconds(RESET_30M) == 1800.0


# ----------------------------------------------------------------- streaming
async def _drain(resp):
    out = b""
    async for c in resp.body_iterator:
        out += c
    return out


@pytest.mark.parametrize("status", [429, -429])
def test_streaming_quota_cf_va_in_cooldown_lungo(monkeypatch, status):
    import app.main as M
    from fastapi.responses import StreamingResponse

    cfg, router, broken, good = _mk()
    router.fallback_next = lambda *a, **k: good
    seen = []

    class _Fwd:
        async def stream_response(self, d, payload, **kwargs):
            seen.append(d["unique"])
            if d["unique"] == broken["unique"]:
                raise UpstreamError(status, CF_QUOTA)

            async def _g():
                yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield (b'data: {"choices":[{"delta":{},'
                       b'"finish_reason":"stop"}]}\n\n')
                yield b"data: [DONE]\n\n"
            return _g()

    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "config", cfg)
    monkeypatch.setattr(M, "forwarder", _Fwd())

    async def _run():
        payload = {"model": broken["model"],
                   "messages": [{"role": "user", "content": "x"}]}
        return await M._stream_with_fallback(
            "test", broken, payload, need=frozenset({"text"}), scope="chain")

    resp = asyncio.run(_run())
    assert isinstance(resp, StreamingResponse)
    assert b"ok" in asyncio.run(_drain(resp))
    assert broken["unique"] in seen and good["unique"] in seen
    # QUOTA -> cooldown fino alla mezzanotte UTC (non i 15s di un 4xx
    # "senza cooldown"): deve essere ben oltre il minimo.
    assert router.cooldown_residual(broken["unique"]) > QUOTA_MIN_COOLDOWN_S


# --------------------------------------------------------------- non-streaming
def test_nonstream_quota_cf_va_in_cooldown_lungo():
    cfg, router, broken, good = _mk()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openrouter.ai":
            return httpx.Response(429, content=CF_QUOTA.encode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        router.fallback_next = lambda *a, **k: good
        return await fwd.call_with_fallback(
            router, "test", broken,
            {"model": "x", "messages": [{"role": "user", "content": "x"}]})

    data, used = asyncio.run(_run())
    assert data["choices"][0]["message"]["content"] == "ok"
    assert used["unique"] == good["unique"]
    assert router.cooldown_residual(broken["unique"]) > QUOTA_MIN_COOLDOWN_S


# ------------------------------------------------- quota di ACCOUNT (CF)
_CSV_ACCT = (
    "commento,modello,provider,endpoint,data,context,max_input,"
    "priority,scrocco-llm-test,caps\n"
    "a,qs-cf1,cloudflare,https://api.cloudflare.com/client/v4/accounts/"
    "ACC1/ai/v1,paid,128,8000,5,K1,\n"
    "a,qs-cf2,cloudflare,https://api.cloudflare.com/client/v4/accounts/"
    "ACC1/ai/v1,paid,128,8000,5,K2,\n"
    "a,qs-cf3,cloudflare,https://api.cloudflare.com/client/v4/accounts/"
    "ACC2/ai/v1,paid,128,8000,5,K3,\n"
    "a,qs-ok,groq,https://ok.test/v1,paid,128,8000,5,K4,\n")


def _mk_acct():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_CSV_ACCT)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, Policy.from_dict({}))
    os.unlink(path)
    by = {}
    for deps in cfg.groups.values():
        for d in deps:
            by[d["api_key"]] = d
    return cfg, router, by


def test_dep_account_key():
    from app.forwarder import dep_account_key
    assert dep_account_key({"api_base": "https://api.cloudflare.com/client/v4/"
                            "accounts/abcdef123/ai/v1"}) == "abcdef123"
    assert dep_account_key({"endpoint": "https://api.cloudflare.com/client/v4/"
                            "accounts/ABC/ai/v1"}) == "abc"
    assert dep_account_key({"api_base": "https://api.groq.com/openai/v1"}) == ""
    assert dep_account_key({}) == ""


def test_quota_account_manda_in_pausa_tutte_le_chiavi_dell_account():
    from app.forwarder import maybe_account_quota_cooldown
    cfg, router, by = _mk_acct()
    n = maybe_account_quota_cooldown(router, by["K1"], 429, CF_QUOTA)
    assert n == 2                                   # ACC1: K1 + K2
    for k in ("K1", "K2"):
        assert router.cooldown_residual(by[k]["unique"]) > QUOTA_MIN_COOLDOWN_S
    # account diverso e provider diverso: intatti
    assert router.cooldown_residual(by["K3"]["unique"]) == 0.0
    assert router.cooldown_residual(by["K4"]["unique"]) == 0.0
    # richiamata: non accorcia ne' riparte
    r1 = router.cooldown_residual(by["K1"]["unique"])
    assert maybe_account_quota_cooldown(router, by["K1"], 429, CF_QUOTA) == 0
    assert router.cooldown_residual(by["K1"]["unique"]) >= r1 - 1.0


def test_quota_senza_account_non_tocca_nulla():
    from app.forwarder import maybe_account_quota_cooldown
    cfg, router, by = _mk_acct()
    assert maybe_account_quota_cooldown(router, by["K4"], 429, CF_QUOTA) == 0
    assert router.cooldown_residual(by["K4"]["unique"]) == 0.0


def test_nonstream_quota_cf_manda_in_pausa_anche_la_gemella_dell_account():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_CSV_ACCT)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, Policy.from_dict({}))
    os.unlink(path)
    by = {}
    for deps in cfg.groups.values():
        for d in deps:
            by[d["api_key"]] = d
    broken, good = by["K1"], by["K4"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.cloudflare.com":
            return httpx.Response(429, content=CF_QUOTA.encode())
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ok"}}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        router.fallback_next = lambda *a, **k: good
        return await fwd.call_with_fallback(
            router, "test", broken,
            {"model": "x", "messages": [{"role": "user", "content": "x"}]})

    data, used = asyncio.run(_run())
    assert used["unique"] == good["unique"]
    # la chiave colpita E la gemella dello stesso account sono in pausa
    for k in ("K1", "K2"):
        assert router.cooldown_residual(by[k]["unique"]) > QUOTA_MIN_COOLDOWN_S
