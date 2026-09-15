"""Fix di review (utente):
- round-robin sui tier `order` di canary e sveglia (dentro lo stesso -dim);
- auto-discovery del vero max_input dal body 400/413 + persistenza;
- coalescing: metrica nx_coalesce_total e niente errori in cache;
- shutdown: drain dei task speculativi prima di chiudere il client httpx.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"

CSV_MT = f"""commento,modello,provider,endpoint,data,context,max_input,priority,{BASE},caps,intelligence_score,model_preference,order
t@x.com,m/rf-small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5,0,0
t@x.com,m/rf-t20a,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-20A,,5,0,20
t@x.com,m/rf-t20b,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-20B,,5,0,20
t@x.com,m/rf-t50a,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-50A,,5,0,50
t@x.com,m/rf-t50b,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-50B,,5,0,50
t@x.com,m/rf-t100a,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-100A,,5,0,100
t@x.com,m/rf-t100b,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-100B,,5,0,100
"""


@pytest.fixture()
def router_mt():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_MT)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, Policy.from_dict({}))
    os.unlink(path)


def _dep(r, gname, key):
    return next(d for d in r.config.groups[gname] if d.get("api_key") == key)


# ------------------------------------------------- round-robin sui tier
def test_canary_round_robin_tier_e_riciclo(router_mt):
    r = router_mt
    small = _dep(r, f"{BASE}-32k", "K-S")
    got, excl = [], set()
    for _ in range(6):
        d = r.warm_fill_canary("test", small, frozenset(), 100, 4096,
                               tried=set(), requested_group=None,
                               exclude_keys=set(), exclude_uniq=set(excl))
        assert d is not None
        got.append(int(d["order"]))
        excl.add(d["unique"])
    # 1 probe per tier in ordine, poi RICICLO dal piu' basso (chiavi diverse)
    assert got == [20, 50, 100, 20, 50, 100]
    # tier esauriti del tutto -> il -dim successivo non esiste -> None
    assert r.warm_fill_canary("test", small, frozenset(), 100, 4096,
                              tried=set(), requested_group=None,
                              exclude_keys=set(), exclude_uniq=set(excl)) is None


def test_sveglia_round_robin_tier(router_mt):
    r = router_mt
    small = _dep(r, f"{BASE}-32k", "K-S")
    now = time.time()
    for d in r.config.groups[f"{BASE}-1000k"]:
        r._cooldown[d["unique"]] = now + 600
        r._cooldown_since[d["unique"]] = now - 7200
        r.stats_for(d["unique"]).last_reason = "http_429"
    got, excl = [], set()
    for _ in range(4):
        w = r.warm_wake_canary("test", small, frozenset(), 100, 4096,
                               tried=set(), requested_group=None,
                               exclude_keys=set(), exclude_uniq=set(excl))
        assert w is not None
        got.append(int(w["order"]))
        excl.add(w["unique"])
    assert got == [20, 50, 100, 20]


def test_cold_pick_sampled_tiers(router_mt):
    r = router_mt
    cands = list(r.config.groups[f"{BASE}-1000k"])
    assert int(r._canary_cold_pick(cands, 100, set())["order"]) == 20
    assert int(r._canary_cold_pick(cands, 100, {20})["order"]) == 50
    assert int(r._canary_cold_pick(cands, 100, {20, 50})["order"]) == 100
    assert int(r._canary_cold_pick(cands, 100, {20, 50, 100})["order"]) == 20


# ------------------------------------------------------ max_input 413
def test_extract_provider_max_input():
    from app.forwarder import extract_provider_max_input as ex
    assert ex('{"error":{"message":"This model\'s maximum context length is '
              '16384 tokens, however you requested 21500 tokens"}}') == 16384
    assert ex('{"error":{"message":"limit of 8192 tokens exceeded"}}') == 8192
    assert ex('{"error":{"message":"prompt is too long: 90000 tokens"}}') \
        == 90000
    assert ex('{"error":{"message":"bad schema"}}') is None
    assert ex('{"error":{"message":"maximum context length is 12 tokens"}}') \
        is None                                     # sotto la soglia credibile


def test_note_context_limit_ridimensiona(router_mt):
    from app.forwarder import note_context_limit
    r = router_mt
    big = _dep(r, f"{BASE}-1000k", "K-20A")
    lim = note_context_limit(
        r, big, 400,
        '{"error":{"message":"maximum context length is 2000 tokens"}}',
        ctx=100000)
    assert lim == 2000
    assert r._eff_max_input(big) == 2000
    assert not r._cap_fits(big, 5000) and r._cap_fits(big, 1500)
    # non deve MAI allargare oltre il dichiarato (numero assurdo ignorato)
    note_context_limit(r, big, 413, "context length is 9999999", ctx=100)
    assert r._eff_max_input(big) == 2000
    # 413 senza numero -> ctx*0.9
    d2 = {"unique": "ZZZ__m__0", "max_input_tokens": 100000}
    assert note_context_limit(r, d2, 413, "payload too large",
                              ctx=50000) == 45000
    # 400 NON context-length -> nessun tocco
    assert note_context_limit(r, big, 400, "invalid schema", ctx=1000) is None
    assert r._eff_max_input(big) == 2000


def test_discovered_max_input_persistenza(router_mt):
    r = router_mt
    r.note_discovered_max_input("dummy", 1234)
    dumped = r.dump_routing_state()
    assert dumped["discovered_max_input"] == {"dummy": 1234}
    r2 = Router(r.config, Policy.from_dict({}))
    rep = r2.load_routing_state(dumped)
    assert rep["discovered_max_input"] == 1
    assert r2._discovered()["dummy"] == 1234


# --------------------------------------------------------- coalescing
def test_coalesce_cache_non_mette_errori():
    import app.main as M
    from starlette.responses import JSONResponse
    M._coalesce_cache.clear()
    M._coalesce_cache_put("bad-envelope",
                          ({"error": {"message": "boom"}}, {}), 9e9)
    assert "bad-envelope" not in M._coalesce_cache
    M._coalesce_cache_put("bad-http",
                          JSONResponse(status_code=502, content={}), 9e9)
    assert "bad-http" not in M._coalesce_cache
    M._coalesce_cache_put("good", ({"choices": []}, {}), 9e9)
    assert "good" in M._coalesce_cache


def test_coalesce_hit_incrementa_metrica():
    import app.main as M
    from app import metrics
    pol = Policy.from_dict({})
    pol.request_coalescing_cache_sec = 3.0
    M._coalesce_cache.clear()
    payload = {"model": "m", "messages": [{"role": "user", "content": "z"}],
               "stream": False}
    key = M._coalesce_key(payload, "prof")
    M._coalesce_cache_put(key, ("R", {}), time.time() + 30)
    _b4 = metrics.snapshot(("nx_coalesce_total",)).get(
        "nx_coalesce_total", {}).get(("hit",), 0)

    async def fac():
        raise AssertionError("la cache non ha risposto: upstream chiamato")

    res = asyncio.run(M._forward_coalesced(pol, payload, "prof", fac))
    assert res == ("R", {})
    _af = metrics.snapshot(("nx_coalesce_total",)).get(
        "nx_coalesce_total", {}).get(("hit",), 0)
    assert _af - _b4 == 1


# ----------------------------------------------------------- shutdown
def test_drain_probe_tasks_cancella_e_attende():
    import app.main as M

    async def go():
        seen = []

        async def slow():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                seen.append("cancelled")
                raise
        t = asyncio.ensure_future(slow())
        M._PROBE_TASKS.add(t)
        t.add_done_callback(M._PROBE_TASKS.discard)
        await asyncio.sleep(0.01)
        n = await M._drain_probe_tasks()
        return n, seen, t.cancelled()

    n, seen, canc = asyncio.run(go())
    assert n == 1 and seen == ["cancelled"] and canc
