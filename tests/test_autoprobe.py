"""Autoprobe dei cooldown triggerato da chiamata (solo gruppi -dim testo)."""
import asyncio
import os
import tempfile
import time
from collections import deque

import pytest

from app import autoprobe
from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router


@pytest.fixture(autouse=True)
def _reset_probe_state():
    """Stato module-level di autoprobe isolato per test."""
    autoprobe._last_probe.clear()
    autoprobe._key_last_probe.clear()
    autoprobe._probe_times.clear()
    yield

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-a,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-A,text
t@x.com,m-b,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-B,text
t@x.com,m-c,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-C,text
t@x.com,mgo,groq,https://api.groq.com/openai/v1,,32,8000,0,K-G,text
"""
DIM = "scrocco-llm-test-32k"
GO = "scrocco-llm-test-go"


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {"choices": [{}]}
        self.text = text

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
    autoprobe._key_last_probe.clear()
    autoprobe._running = False
    yield
    autoprobe._last_probe.clear()
    autoprobe._key_last_probe.clear()
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


def _used_all(router):
    """Marca tutti i deployment dim come USATI (non freschi), cosi' il pass
    ripiega sul modo COOLED (fallback)."""
    now = time.time()
    for d in router.config.groups[DIM]:
        router.stats_for(d["unique"]).last_used = now


def test_probe_ok_wakes(router):
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d)
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert not router.is_cooled_down(d["unique"])
    assert len(fwd.cli.calls) == 1
    assert fwd.cli.calls[0]["json"]["max_tokens"] == 1


def test_probe_ko_grows_cooldown(router):
    """KO 429 sul cooled: il residuo ALMENO RADDOPPIA (backoff), cosi'
    l'autoprobe non insiste con richieste ravvicinate inutili."""
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d, remaining=3600.0)
    fwd = _Fwd(_Resp(429))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    rem = router._cooldown[d["unique"]] - time.time()
    assert 7150.0 <= rem <= 7250.0     # 3600 -> almeno 7200 (2x)
    assert router.is_cooled_down(d["unique"])


def test_probe_ko_transient_modest_cooldown(router):
    """Quando il residuo e' PICCOLO (< incremento) l'aggiunta e' il MODESTO
    transient (30s): si ruota via dal dep flaky senza bruciare il grow pieno."""
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d, remaining=5.0)
    fwd = _Fwd(_Resp(503))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1
    assert router.is_cooled_down(d["unique"])
    rem = router._cooldown[d["unique"]] - time.time()
    assert 30.0 <= rem <= 45.0     # 5 + transient(30) ~= 35


def test_cooled_ko_doubles_residual(router):
    """Backoff: il residuo cooled almeno raddoppia a ogni KO."""
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d, remaining=1800.0)
    fwd = _Fwd(_Resp(429))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    rem = router._cooldown[d["unique"]] - time.time()
    assert 3550.0 <= rem <= 3650.0     # 1800 -> almeno 3600


def test_per_dim_cap(router):
    _used_all(router)
    for k in ("K-A", "K-B", "K-C"):
        _cool(router, _dep(router, DIM, k), remaining=3000.0 + (0 if k == "K-A" else 500))
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    # 3/3 cooled = 100% > soglia crisis (0.30) -> per_dim 2x2=4 -> tutti e 3
    assert len(fwd.cli.calls) == 3


def test_crisis_doubles_per_dim(router):
    """1/3 cooled = 33% < soglia 0.50 -> nessun boost: per_dim resta 1."""
    _used_all(router)
    router.policy.cooldown_autoprobe_crisis_ratio = 0.50
    _cool(router, _dep(router, DIM, "K-A"), remaining=3000.0)
    router.policy.cooldown_autoprobe_per_dim = 1
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1          # per_dim normale (1)


def test_crisis_above_ratio_boosts(router):
    """2/3 cooled = 67% > 0.50 -> per_dim 1x2=2 -> entrambi sondati."""
    _used_all(router)
    router.policy.cooldown_autoprobe_crisis_ratio = 0.50
    for k in ("K-A", "K-B"):
        _cool(router, _dep(router, DIM, k), remaining=3000.0)
    router.policy.cooldown_autoprobe_per_dim = 1
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 2          # per_dim 1x2=2


def test_crisis_disabled_no_boost(router):
    _used_all(router)
    router.policy.cooldown_autoprobe_crisis_enabled = False
    for k in ("K-A", "K-B", "K-C"):
        _cool(router, _dep(router, DIM, k), remaining=3000.0)
    router.policy.cooldown_autoprobe_per_dim = 1
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1          # senza crisis resta per_dim 1


def test_crisis_below_ratio_no_boost(router):
    _used_all(router)
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
    _used_all(router)
    _cool(router, d, age=10.0)          # < min_age 300
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert fwd.cli.calls == []


def test_min_gap_skips_recently_probed(router):
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d)
    autoprobe._last_probe[d["unique"]] = time.time()   # appena sondato
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert fwd.cli.calls == []


def test_only_dim_groups(router):
    g = _dep(router, GO, "K-G")
    _used_all(router)
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


def test_select_targets_orders_by_probe_count_24h(router):
    """Vince il deployment con MENO tentativi nelle 24h, anche se il suo
    cooldown residuo e' maggiore."""
    a = _dep(router, DIM, "K-A")
    b = _dep(router, DIM, "K-B")
    _cool(router, a, remaining=100.0)     # piu' pronto, ma gia' martellato
    _cool(router, b, remaining=5000.0)    # residuo alto, MA quasi mai tentato
    now = time.time()
    autoprobe._probe_times.setdefault(a["unique"], deque()).extend(
        [now - 10, now - 20, now - 30])   # 3 probe nelle ultime 24h
    autoprobe._probe_times.setdefault(b["unique"], deque()).append(now - 50)
    t = autoprobe._select_targets(router, "test", 1, 300.0, 60.0, 6)
    assert [u for _g, u in t] == [b["unique"]]
    assert _probe_count(autoprobe._probe_times, a["unique"]) == 3
    assert _probe_count(autoprobe._probe_times, b["unique"]) == 1


def test_select_targets_tie_by_remaining(router):
    """A parita' di tentativi 24h vince il residuo cooldown minore."""
    a = _dep(router, DIM, "K-A")
    b = _dep(router, DIM, "K-B")
    _cool(router, a, remaining=1000.0)
    _cool(router, b, remaining=100.0)
    t = autoprobe._select_targets(router, "test", 1, 300.0, 60.0, 6)
    assert [u for _g, u in t] == [b["unique"]]


def test_probe_count_24h_window(router):
    """Un probe piu' vecchio di 24h non conta (e viene potato)."""
    d = _dep(router, DIM, "K-A")
    now = time.time()
    dq = autoprobe._probe_times.setdefault(d["unique"], deque())
    dq.append(now - 90000)          # > 24h fa
    dq.append(now - 3600)           # entro le 24h
    assert autoprobe._probe_count_24h(d["unique"], now) == 1
    assert len(dq) == 1             # il vecchio e' stato potato
    assert autoprobe._probe_count_24h(d["unique"], now + 86400) == 0


def test_probe_times_recorded_in_pass(router):
    """Il pass registra il timestamp in _probe_times (fresh e cooled)."""
    router.policy.cooldown_autoprobe_per_dim = 1
    d = _dep(router, DIM, "K-A")
    fwd = _Fwd(_Resp(200))
    autoprobe._probe_times.clear()
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(autoprobe._probe_times.get(d["unique"], [])) == 1


def _probe_count(times, unique):
    dq = times.get(unique)
    return len(dq) if dq else 0


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
    assert d.cooldown_autoprobe_fresh_age_sec == 86400.0
    assert Policy.from_dict({"cooldown_autoprobe_fresh_age_sec": 7200}) \
        .cooldown_autoprobe_fresh_age_sec == 7200.0


# ---------------------------------------------------------------- MODO FRESH

def test_probe_ko_classification():
    """Classificazione KO del probe: 429/401/403/model-missing -> cooldown
    pieno (grow); transitori (5xx/timeout/rete/altro 4xx) -> cooldown MODESTO
    (transient)."""
    G, T = 120.0, 30.0
    assert autoprobe._probe_ko_cooldown(429, "", G, T) == G
    assert autoprobe._probe_ko_cooldown(401, "", G, T) == G
    assert autoprobe._probe_ko_cooldown(403, "", G, T) == G
    assert autoprobe._probe_ko_cooldown(
        400, "The requested model does not exist.", G, T) == G
    assert autoprobe._probe_ko_cooldown(503, "", G, T) == T
    assert autoprobe._probe_ko_cooldown(502, "", G, T) == T
    assert autoprobe._probe_ko_cooldown(0, "", G, T) == T       # timeout/rete
    assert autoprobe._probe_ko_cooldown(400, "", G, T) == T     # altro 4xx


def test_probe_ko_transient_escalates_on_streak(router):
    """KO transitori consecutivi: l'incremento sale a `grow` dopo la streak E
    viene moltiplicato per il numero di probe/24h. Pass1: 30 x1 = +30.
    Pass2 (streak cap -> grow 120) x2 = +240. MAI retire dall'autoprobe."""
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d, remaining=5.0)
    router.policy.cooldown_autoprobe_min_gap_sec = 0
    router.policy.probe_retire_after = 2
    fwd = _Fwd(_Resp(503))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))   # streak 1 -> +30
    # F32: il gap per-chiave fermerebbe il secondo giro nella stessa manciata
    # di secondi; qui si testa la streak, non il gap -> azzera il registro.
    autoprobe._key_last_probe.clear()
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))   # streak 2 -> +240
    rem = router._cooldown[d["unique"]] - time.time()
    assert 250.0 <= rem <= 300.0       # 5 + 30 + 240 = 275
    assert router.stats_for(d["unique"]).probe_fail_streak == 2
    assert not router.is_retired(d["unique"])

def test_fresh_probe_ok_promotes(router):
    """Senza cooled e con deployment mai usati: probe "normale" (note_result)
    -> il deployment fresco sale in classifica con un successo."""
    router.policy.cooldown_autoprobe_per_dim = 1
    d = _dep(router, DIM, "K-A")
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1
    s = router.stats_for(d["unique"])
    assert s.ok_count == 1
    assert not router.is_cooled_down(d["unique"])


def test_fresh_probe_ko_cooldowns(router):
    """Probe KO su fresco con 429 -> mark_failed: cooldown breve (120s)."""
    router.policy.cooldown_autoprobe_per_dim = 1
    d = _dep(router, DIM, "K-A")
    fwd = _Fwd(_Resp(429))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1
    assert router.is_cooled_down(d["unique"])
    assert router.stats_for(d["unique"]).last_fail_ts > 0


def test_fresh_probe_ko_transient_modest_cooldown(router):
    """KO transitorio (503) su fresco: mark_failed col cooldown MODESTO (30s):
    si ruota via ma non si spende il grow pieno."""
    router.policy.cooldown_autoprobe_per_dim = 1
    d = _dep(router, DIM, "K-A")
    fwd = _Fwd(_Resp(503))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1
    assert router.is_cooled_down(d["unique"])
    s = router.stats_for(d["unique"])
    assert s.ok_count == 0
    assert s.last_fail_ts > 0
    assert s.probe_fail_streak == 1
    rem = router._cooldown[d["unique"]] - time.time()
    assert 20.0 <= rem <= 40.0        # transient(30)


def test_fresh_probe_ko_definitive_cooldowns(router):
    """KO definitivo (401/403/model-missing) su fresco -> cooldown."""
    router.policy.cooldown_autoprobe_per_dim = 1
    d = _dep(router, DIM, "K-A")
    fwd = _Fwd(_Resp(403))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert router.is_cooled_down(d["unique"])
    assert router.stats_for(d["unique"]).last_fail_ts > 0


def test_fresh_preferred_over_cooled(router):
    """Se esistono freschi, si sondano quelli: il cooled NON viene insistito."""
    router.policy.cooldown_autoprobe_per_dim = 2
    a = _dep(router, DIM, "K-A")
    _cool(router, a)                          # K-A fallito di recente
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 2            # K-B e K-C (freschi), non K-A
    assert router.is_cooled_down(a["unique"])  # K-A intatto


def test_fresh_skips_recently_probed(router):
    router.policy.cooldown_autoprobe_per_dim = 1
    for d in router.config.groups[DIM]:
        autoprobe._last_probe[d["unique"]] = time.time()   # appena sondati
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert fwd.cli.calls == []


def test_fresh_uses_fresh_age(router):
    """Fresco = nessuna attivita' nelle ultime fresh_age (default 24h)."""
    router.policy.cooldown_autoprobe_per_dim = 1
    a = _dep(router, DIM, "K-A")
    b = _dep(router, DIM, "K-B")
    c = _dep(router, DIM, "K-C")
    router.stats_for(a["unique"]).last_used = time.time() - 90000   # 25h fa
    router.stats_for(b["unique"]).last_used = time.time() - 3600    # 1h fa
    router.stats_for(c["unique"]).last_used = time.time() - 3600    # 1h fa
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1
    assert router.stats_for(a["unique"]).ok_count == 1   # A fresco -> sondato
    assert router.stats_for(b["unique"]).ok_count == 0   # B recente -> no


def test_no_fresh_falls_back_to_cooled(router):
    """Niente freschi -> ripiega sul modo COOLED classico (risveglio)."""
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d)
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1
    assert not router.is_cooled_down(d["unique"])       # risvegliato
    assert router.stats_for(d["unique"]).ok_count == 0  # NESSUN note_result


def test_probe_cd_multiplied_by_24h_count(router):
    """Il cooldown di un KO del probe e' MOLTIPLICATO per i probe/24h: con 3
    probe storici (piu' il corrente = 4) un 429 aggiunge 120*4=480. Residuo
    piccolo -> domina il prodotto."""
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d, remaining=5.0)
    dq = autoprobe._probe_times.setdefault(d["unique"], deque())
    for _ in range(3):
        dq.append(time.time() - 10)
    fwd = _Fwd(_Resp(429))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    rem = router._cooldown[d["unique"]] - time.time()
    assert 5.0 + 480.0 - 25 <= rem <= 5.0 + 480.0 + 25


def test_probe_cd_multiply_can_be_disabled(router):
    router.policy.cooldown_autoprobe_multiply_24h = False
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d, remaining=5.0)
    dq = autoprobe._probe_times.setdefault(d["unique"], deque())
    for _ in range(5):
        dq.append(time.time() - 10)
    fwd = _Fwd(_Resp(429))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    rem = router._cooldown[d["unique"]] - time.time()
    assert 5.0 + 120.0 - 25 <= rem <= 5.0 + 120.0 + 25


def test_cooled_over_2h_skipped(router):
    """Deployment con cooldown residuo > 2h: NIENTE probe (lo salvano il tempo
    o la ULTIMA SPIAGGIA della scala)."""
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d, remaining=9000.0)          # 2.5h > 7200
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert fwd.cli.calls == []
    assert router.is_cooled_down(d["unique"])   # intatto


def test_cooled_under_2h_probed(router):
    """Sotto soglia (es. 6000s) il probe avviene normalmente."""
    d = _dep(router, DIM, "K-A")
    _used_all(router)
    _cool(router, d, remaining=6000.0)
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == 1
    assert not router.is_cooled_down(d["unique"])


def test_mark_failed_additive_extends_not_resets(router):
    """mark_failed(additive=True) ESTENDE il residuo invece di resettarlo."""
    router.policy.cooldown_jitter_ratio = 0.0
    d = _dep(router, DIM, "K-A")
    router.mark_failed(d["unique"], seconds=100)
    exp1 = router._cooldown[d["unique"]]
    router.mark_failed(d["unique"], seconds=50, additive=True)
    exp2 = router._cooldown[d["unique"]]
    assert abs((exp2 - exp1) - 50) <= 2          # +50, additivo
    router.mark_failed(d["unique"], seconds=10)  # non-additivo -> reset
    rem = router._cooldown[d["unique"]] - time.time()
    assert 8 <= rem <= 12
