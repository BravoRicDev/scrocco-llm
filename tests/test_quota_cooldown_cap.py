"""Quota: cooldown che segue il RESET (non tagliato dal ceiling), sveglia che
puo' comunque tentare il risveglio, metrica JSON->SSE.

Regole utente:
  - "la quota giornaliera non deve tornare prima del reset" (prima era tagliata
    da `max_cooldown_sec`, default 5h);
  - "lasciamo comunque fare il tentativo (anche se inutile) al wake-up canary e
    ultima spiaggia": nessuna esclusione NUOVA per i cooldown di quota stimati
    (un KO raddoppia, non e' un problema);
  - metrica dedicata per le consegne adattate da JSON a SSE.
"""
import asyncio
import os
import tempfile
import time

import httpx

from app import metrics
from app.config import GatewayConfig
from app.forwarder import (Forwarder, maybe_account_quota_cooldown,
                           parse_quota_reset_seconds)
from app.policy import Policy
from app.router import Router

CF_QUOTA = ('{"errors":[{"message":"AiError: AiError: you have used up your '
            'daily free allocation of 10,000 neurons, please upgrade to '
            "Cloudflare's Workers Paid plan if you would like to continue "
            'usage. (51afe8c9-9139-446e-9284-928af8ee842d)","code":4006}],'
            '"success":false,"result":{},"messages":[]}')

_CSV = ("commento,modello,provider,endpoint,data,context,max_input,"
        "priority,scrocco-llm-test,caps\n"
        "a,qc-cf,cloudflare,https://api.cloudflare.com/client/v4/accounts/"
        "ACC1/ai/v1,free,128,128000,5,K1,\n"
        "b,qc-ok,groq,https://ok.test/v1,free,128,128000,5,K2,\n")


def _dep(cfg, key):
    return next(d for deps in cfg.groups.values() for d in deps
                if d["api_key"] == key)


def _mk(**pol):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    router = Router(cfg, Policy.from_dict(pol))
    os.unlink(path)
    return cfg, router, _dep(cfg, "K1")


# ------------------------------------------------------ niente ceiling sulla quota
def test_quota_giornaliera_non_viene_tagliata_dal_ceiling():
    cfg, router, dep = _mk()
    secs = parse_quota_reset_seconds(CF_QUOTA)          # ~ fino a mezzanotte UTC
    assert secs > 18000.0                               # oltre max_cooldown_sec
    router.mark_failed(dep["unique"], seconds=secs,
                       reason="quota_exhausted", status=429)
    assert router.cooldown_residual(dep["unique"]) > 18000.0


def test_quota_di_account_non_viene_tagliata_nemmeno_da_cronico():
    cfg, router, dep = _mk()
    router.stats_for(dep["unique"]).fail_count_24h = 99
    n = maybe_account_quota_cooldown(router, dep, 429, CF_QUOTA)
    assert n == 1
    assert router.cooldown_residual(dep["unique"]) > 18000.0


def test_ceiling_operatore_continua_a_valere_per_i_cooldown_stimati():
    cfg, router, dep = _mk(cooldown_estimate_ceiling_sec=120)
    router.mark_failed(dep["unique"], reason="http_429")
    assert router.cooldown_residual(dep["unique"]) <= 122.0


# ---------------------------------------------------- sveglia: prova comunque
def test_sveglia_puo_tentare_un_cooldown_di_quota_di_account():
    cfg, router, dep = _mk()
    u = dep["unique"]
    n = maybe_account_quota_cooldown(router, dep, 429, CF_QUOTA)
    assert n == 1
    assert router.cooldown_probeable(u) is True      # stima nostra -> provabile
    assert router.cooldown_provenance(u) == "heuristic"
    # eta' minima superata (il wake non riprova un cooldown troppo fresco)
    router._cooldown_since[u] = time.time() - 7200.0
    cur = _dep(cfg, "K2")
    got = router.warm_wake_canary("test", cur, frozenset(), 100, 4096,
                                  tried=set(),
                                  requested_group=dep["group"],
                                  min_age_sec=3600.0)
    assert got is not None and got["unique"] == u


def test_sveglia_autoritativa_resta_esclusa():
    """Se il provider DICHIARA il reset (Retry-After/Resets in ...) la sveglia
    non lo tenta: sarebbe solo rumore su una chiave sicuramente satura."""
    cfg, router, dep = _mk()
    u = dep["unique"]
    body = ('{"error":{"message":"usage limit reached. Resets in 9 days"}}')
    router.mark_failed(u, seconds=parse_quota_reset_seconds(body),
                       reason="quota_exhausted", status=429)
    assert router.cooldown_provenance(u) == "authoritative"
    router._cooldown_since[u] = time.time() - 7200.0
    cur = _dep(cfg, "K2")
    assert router.warm_wake_canary("test", cur, frozenset(), 100, 4096,
                                   tried=set(), requested_group=dep["group"],
                                   min_age_sec=3600.0) is None


# ---------------------------------------------------------- metrica JSON->SSE
def test_metrica_json_sse_quando_l_upstream_ignora_lo_stream():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(_CSV)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    os.unlink(path)
    dep = _dep(cfg, "K1")

    def handler(request: httpx.Request) -> httpx.Response:
        # upstream che IGNORA stream:true e risponde JSON intero
        return httpx.Response(200, json={"choices": [
            {"message": {"content": "ciao"}, "finish_reason": "stop"}]})

    async def _run():
        fwd = Forwarder(client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        gen = await fwd.stream_response(
            dep, {"model": "x", "stream": True,
                  "messages": [{"role": "user", "content": "x"}]})
        out = b""
        async for c in gen:
            out += c
        return out

    before = dict(metrics.snapshot(("nx_json_sse_total",))
                  .get("nx_json_sse_total", {}))
    out = asyncio.run(_run())
    assert b"ciao" in out and b"[DONE]" in out
    after = dict(metrics.snapshot(("nx_json_sse_total",))
                 .get("nx_json_sse_total", {}))
    assert sum(after.values()) == sum(before.values()) + 1
