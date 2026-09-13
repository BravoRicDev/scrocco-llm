"""Autoprobe dei cooldown triggerato da chiamata (solo gruppi -dim testo)."""
import asyncio
import os
import tempfile
import time

import pytest

from app import autoprobe
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text
t@x.com,m-b,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-B,text
t@x.com,m-c,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-C,text
t@x.com,mgo,groq,https://api.groq.com/openai/v1,,32,8000,0,K-G,text
"""
DIM = "scrocco-llm-test-32k"
GO = "scrocco-llm-test-go"


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"choices": [{}]}

    def json(self):
        return self._payload


class _Cli:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self.resp


class _Fwd:
    def __init__(self, resp):
        self.cli = _Cli(resp)

    def _client_for(self, url):
        return self.cli


@pytest.fixture(autouse=True)
def _reset():
    autoprobe._last_probe.clear()
    autoprobe._running = False
    yield
    autoprobe._last_probe.clear()
    autoprobe._running = False


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_ROWS)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, Policy.from_dict({}))
    yield r
    os.unlink(path)


def _dep(router, group, key):
    return next(d for d in router.config.groups[group] if d["api_key"] == key)


def _cool(router, dep, remaining=3600.0, age=600.0):
    now = time.time()
    router._cooldown[dep["unique"]] = now + remaining
    router._cooldown_since[dep["unique"]] = now - age


def test_probe_ok_wakes(router):
    d = _dep(router, DIM, "K-A")
    _cool(router, d)
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert not router.is_cooled_down(d["unique"])
    assert len(fwd.cli.calls) == 1
    assert fwd.cli.calls[0]["json"]["max_tokens"] == 1


def test_probe_ko_grows_cooldown(router):
    d = _dep(router, DIM, "K-A")
    _cool(router, d, remaining=3600.0)
    fwd = _Fwd(_Resp(503))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    rem = router._cooldown[d["unique"]] - time.time()
    assert rem > 3600.0 + 100          # 3600 + grow(120)
    assert router.is_cooled_down(d["unique"])


def test_per_dim_cap(router):
    for k in ("K-A", "K-B", "K-C"):
        _cool(router, _dep(router, DIM, k), remaining=3000.0 + (0 if k == "K-A" else 500))
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    # 3/3 cooled = 100% > soglia crisis (0.30) -> per_dim 2x2=4 -> tutti e 3
    assert len(fwd.cli.calls) == 3


def test_crisis_doubles_per_dim(router):
    """1/3 cooled = 33% < soglia 0.50 -> nessun boost: per_dim resta 1."""
    router.policy.cooldown_autoprobe_crisis_ratio = 0.50
    _cool(router, _dep(router, DIM, "K-A"), remaining=3000.0)
    router.policy.cooldown_autoprobe_per_dim = 1
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1          # per_dim normale (1)


def test_crisis_above_ratio_boosts(router):
    """2/3 cooled = 67% > 0.50 -> per_dim 1x2=2 -> entrambi sondati."""
    router.policy.cooldown_autoprobe_crisis_ratio = 0.50
    for k in ("K-A", "K-B"):
        _cool(router, _dep(router, DIM, k), remaining=3000.0)
    router.policy.cooldown_autoprobe_per_dim = 1
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 2          # per_dim 1x2=2


def test_crisis_disabled_no_boost(router):
    router.policy.cooldown_autoprobe_crisis_enabled = False
    for k in ("K-A", "K-B", "K-C"):
        _cool(router, _dep(router, DIM, k), remaining=3000.0)
    router.policy.cooldown_autoprobe_per_dim = 1
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1          # senza crisis resta per_dim 1


def test_crisis_below_ratio_no_boost(router):
    router.policy.cooldown_autoprobe_crisis_ratio = 0.9   # soglia altissima
    _cool(router, _dep(router, DIM, "K-A"), remaining=3000.0)
    router.policy.cooldown_autoprobe_per_dim = 1
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1          # 1/3 = 33% < 90% -> nessun boost


def test_hotreload_probe_ok_warms(router):
    """Probe su deployment nuovo (hot-reload): OK -> note_result (caldo)."""
    d = _dep(router, DIM, "K-A")
    fwd = _Fwd(_Resp(200))
    _drain_spawn(router, fwd, [d["unique"]])
    assert len(fwd.cli.calls) == 1
    s = router.stats_for(d["unique"])
    assert s.ok_count == 1                   # successo registrato
    assert s.fail_streak == 0


def test_hotreload_probe_ko_cooldowns(router):
    """Probe KO -> cooldown breve, deployment escluso dal traffico."""
    d = _dep(router, DIM, "K-A")
    fwd = _Fwd(_Resp(503))
    _drain_spawn(router, fwd, [d["unique"]])
    assert len(fwd.cli.calls) == 1
    assert router.is_cooled_down(d["unique"])


def test_hotreload_probe_disabled(router):
    router.policy.hotreload_probe_enabled = False
    d = _dep(router, DIM, "K-A")
    fwd = _Fwd(_Resp(503))
    _drain_spawn(router, fwd, [d["unique"]])
    assert fwd.cli.calls == []               # nessun probe


def _drain_spawn(router, fwd, uniques):
    async def _go():
        autoprobe.spawn_hotreload_probe(router, fwd, uniques)
        while any(t is not asyncio.current_task()
                  for t in asyncio.all_tasks()):
            await asyncio.sleep(0.01)
    asyncio.run(_go())


def test_min_age_skips_fresh(router):
    d = _dep(router, DIM, "K-A")
    _cool(router, d, age=10.0)          # < min_age 300
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert fwd.cli.calls == []


def test_min_gap_skips_recently_probed(router):
    d = _dep(router, DIM, "K-A")
    _cool(router, d)
    autoprobe._last_probe[d["unique"]] = time.time()   # appena sondato
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert fwd.cli.calls == []


def test_only_dim_groups(router):
    g = _dep(router, GO, "K-G")
    _cool(router, g)
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert fwd.cli.calls == []
    assert router.is_cooled_down(g["unique"])           # intatto


def test_select_targets_orders_by_remaining(router):
    a = _dep(router, DIM, "K-A")
    b = _dep(router, DIM, "K-B")
    _cool(router, a, remaining=1000.0)
    _cool(router, b, remaining=100.0)
    t = autoprobe._select_targets(router, "test", 1, 300.0, 60.0, 6)
    assert [u for _g, u in t] == [b["unique"]]          # il piu' pronto prima


def test_max_total_cap(router):
    for k in ("K-A", "K-B", "K-C"):
        _cool(router, _dep(router, DIM, k))
    t = autoprobe._select_targets(router, "test", 5, 300.0, 60.0, 2)
    assert len(t) == 2


def test_disabled_no_spawn(router):
    router.policy.cooldown_autoprobe_enabled = False
    autoprobe.maybe_spawn(router, _Fwd(_Resp(200)), "test")
    assert autoprobe._running is False


def test_parsing_knobs():
    pol = Policy.from_dict({
        "cooldown_autoprobe_enabled": False,
        "cooldown_autoprobe_per_dim": 4,
        "cooldown_autoprobe_min_age_sec": 90,
        "cooldown_autoprobe_grow_sec": 30,
        "cooldown_autoprobe_min_gap_sec": 10,
        "cooldown_autoprobe_max_total": 9,
        "cooldown_autoprobe_timeout_sec": 5,
    })
    assert pol.cooldown_autoprobe_enabled is False
    assert pol.cooldown_autoprobe_per_dim == 4
    assert pol.cooldown_autoprobe_min_age_sec == 90.0
    assert pol.cooldown_autoprobe_grow_sec == 30.0
    assert pol.cooldown_autoprobe_min_gap_sec == 10.0
    assert pol.cooldown_autoprobe_max_total == 9
    assert pol.cooldown_autoprobe_timeout_sec == 5.0
    d = Policy.from_dict({})
    assert d.cooldown_autoprobe_enabled is True
    assert d.cooldown_autoprobe_per_dim == 2
    assert d.cooldown_autoprobe_grow_sec == 120.0
