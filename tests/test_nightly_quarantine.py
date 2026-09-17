"""Autoprobe NOTTURNI-only + QUARANTENA ENDPOINT da ban/ToS del provider.

Regola utente (post-ban llm7.io): mai probe scatenati dalle
richieste, solo il giro delle 00:00 locali; 1 probe/giorno per CHIAVE; se un
provider risponde "ip_banned"/"policy_review"/"Terms of Service" l'HOST va in
quarantena 24h ed e' escluso da TUTTI i gate di eligibilita' (rotazione, warm,
canary, ultima spiaggia), scadendo da solo.

`app.main` va importato SOLO dentro funzioni (convenzione del repo).
"""
import asyncio
import os
import tempfile
import time

import pytest

from app import autoprobe
from app.config import GatewayConfig
from app.forwarder import ban_signature_hit, maybe_quarantine_ban
from app.policy import Policy
from app.router import Router

CSV_ROWS = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m-a,llm7,https://api.llm7.io/v1,free,32,8000,0,K-A,text
t@x.com,m-b,llm7,https://api.llm7.io/v1,free,32,8000,0,K-B,text
t@x.com,m-c,groq,https://api.groq.com/openai/v1,free,32,8000,0,K-C,text
"""
DIM = "scrocco-llm-test-32k"

LLM7_BAN = ('{"error":{"message":"Access from this IP address is restricted '
            'because it may violate the Terms of Service: '
            'https://github.com/chigwell/llm7.io/blob/main/TERMS.md",'
            '"type":"permission_error","code":"ip_banned"}}')
OR_POLICY = ('{"error":{"message":"Access is temporarily unavailable for '
             'this client due to policy review. See the Terms of Service",'
             '"type":"access_restricted","code":"policy_review_required"}}')
BENIGN_429 = '{"error":{"message":"Rate limit reached. Resets in 60s"}}'


@pytest.fixture(autouse=True)
def _reset():
    autoprobe._last_probe.clear()
    autoprobe._key_last_probe.clear()
    autoprobe._key_probe_day.clear()
    autoprobe._key_quota_day.clear()
    autoprobe._probe_times.clear()
    autoprobe._running = False
    yield
    autoprobe._last_probe.clear()
    autoprobe._key_last_probe.clear()
    autoprobe._key_probe_day.clear()
    autoprobe._key_quota_day.clear()
    autoprobe._probe_times.clear()
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


def _dep(router, key):
    return next(d for d in router.config.groups[DIM] if d["api_key"] == key)


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text

    def json(self):
        return {"choices": [{}]}


class _Cli:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(url)
        return self.resp


class _Fwd:
    def __init__(self, resp):
        self.cli = _Cli(resp)

    def _client_for(self, url):
        return self.cli


# ------------------------------------------------------ schedule nightly
def test_maybe_spawn_muto_in_nightly(router):
    router.policy.cooldown_autoprobe_schedule = "nightly"
    autoprobe.maybe_spawn(router, _Fwd(_Resp(200)), "test")
    assert autoprobe._running is False          # nessun task creato


def test_maybe_spawn_vivo_in_request_mode(router):
    router.policy.cooldown_autoprobe_schedule = "request"
    router.policy.cooldown_autoprobe_per_dim = 1

    async def go():
        autoprobe.maybe_spawn(router, _Fwd(_Resp(200)), "test")
        assert autoprobe._running is True
        for _ in range(50):
            if not autoprobe._running:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("il pass non e' terminato")
    asyncio.run(go())


# ------------------------------------------------------- budget 1/giorno
def test_budget_un_giro_per_chiaveanche_sul_fresh(router):
    d = _dep(router, "K-A")
    router.policy.cooldown_autoprobe_per_dim = 3
    router.policy.cooldown_autoprobe_key_day_max = 1
    router.policy.cooldown_autoprobe_key_gap_sec = 0.0
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    n1 = len(fwd.cli.calls)
    assert n1 >= 1
    # secondo giro stessa notte: budget chiavi esaurito -> nessun probe
    asyncio.run(autoprobe._probe_pass(router, fwd, "test"))
    assert len(fwd.cli.calls) == n1
    assert d["unique"]


# ------------------------------------------- matcher firme ban / ToS
def test_matcher_firme():
    assert ban_signature_hit(LLM7_BAN)
    assert ban_signature_hit(OR_POLICY)
    assert not ban_signature_hit(BENIGN_429)
    assert not ban_signature_hit("")
    assert not ban_signature_hit(None)


def _fake_router_cooler():
    class R:
        quarantined = {}

        def quarantine_endpoint(self, host, seconds=86400.0):
            R.quarantined[host] = seconds
    return R()


def test_quarantine_da_403_ip_banned():
    r = _fake_router_cooler()
    dep = {"unique": "u", "api_base": "https://api.llm7.io/v1"}
    assert maybe_quarantine_ban(r, dep, -403, LLM7_BAN) is True
    assert R_host(r) == "api.llm7.io"


def R_host(r):
    return next(iter(r.quarantined))


def test_quarantine_non_scatta_su_429_benigno():
    r = _fake_router_cooler()
    dep = {"unique": "u", "api_base": "https://api.llm7.io/v1"}
    assert maybe_quarantine_ban(r, dep, -429, BENIGN_429) is False
    assert not r.quarantined


def test_quarantine_ignora_status_non_403_429():
    r = _fake_router_cooler()
    dep = {"unique": "u", "api_base": "https://api.llm7.io/v1"}
    assert maybe_quarantine_ban(r, dep, -400, LLM7_BAN) is False
    assert not r.quarantined


# ------------------------------- host-cooldown BREVE su 502 mid-stream
MIDSTREAM_502 = ('{"error":{"origin":"router","message":"The provider sent '
                 'an error mid-stream. The request was retried where '
                 'possible"}}')


def test_host_cooldown_su_502_midstream(router):
    from app.forwarder import (maybe_host_transient_cooldown,
                               host_transient_signature_hit)
    assert host_transient_signature_hit(MIDSTREAM_502)
    assert not host_transient_signature_hit(BENIGN_429)
    dep = _dep(router, "K-A")
    router.policy.cooldown_host_midstream_502_sec = 120.0
    assert maybe_host_transient_cooldown(router, dep, -502, MIDSTREAM_502)
    v = router.endpoint_quarantine_view()
    assert "api.llm7.io" in v and v["api.llm7.io"] <= 120
    # e' una pausa BREVE: non e' la quarantena ban/ToS da 24h
    assert v["api.llm7.io"] < 86400
    # un 502 SENZA la firma non punisce l'host
    router._endpoint_quarantine.clear()
    assert not maybe_host_transient_cooldown(router, dep, -502, '{"e":"x"}')
    assert not router.endpoint_quarantine_view()
    # un 400 con firma non e' un errore host
    assert not maybe_host_transient_cooldown(router, dep, -400, MIDSTREAM_502)


def test_host_cooldown_non_accorcia_la_quarantena_ban(router):
    from app.forwarder import maybe_host_transient_cooldown
    dep = _dep(router, "K-A")
    router.quarantine_endpoint("api.llm7.io", 86400.0)
    router.policy.cooldown_host_midstream_502_sec = 120.0
    maybe_host_transient_cooldown(router, dep, -502, MIDSTREAM_502)
    v = router.endpoint_quarantine_view()
    assert v["api.llm7.io"] > 80000       # resta la quarantena lunga


# --------------------------------------------- quarantena nei gate router
def test_quarantine_blocca_pick_warm_canary_e_scade(router):
    from app.router import set_current_session
    set_current_session("s1")           # la ownership deve essere NOSTRA
    try:
        da, db = _dep(router, "K-A"), _dep(router, "K-B")  # stesso host llm7
        dc = _dep(router, "K-C")                           # groq, indenne
        router.note_probe_started = lambda *a, **k: None   # non serve qui
        router.quarantine_endpoint("api.llm7.io", 86400.0)
        # 1) pick_deployment: solo il groq
        need = frozenset({"text"})
        for _ in range(3):
            d = router.pick_deployment(DIM, need=need, ctx=1000)
            assert d is not None
            assert d["unique"] == dc["unique"]
        # 2) warm pool: i due llm7 non sono caldi nemmeno se li marchiamo
        for d in (da, db, dc):
            router.note_session_success("s1", d["unique"], 100, ctx_est=100)
        pool = {x["unique"] for x in router._warm_pool("s1", None)}
        assert da["unique"] not in pool and db["unique"] not in pool
        # 3) canary refill: il picker non deve MAI pescare su host quarantenato
        for i in range(5):
            c = router.warm_fill_canary("test", dc, need, 1000, 4096,
                                        tried=set(), requested_group=DIM)
            if c is None:
                continue
            assert "llm7" not in c["unique"] and "llm7" not in str(
                c.get("api_base"))
        # 4) scade: dopo il TTL tornano eleggibili
        router._endpoint_quarantine["api.llm7.io"] = time.time() - 1
        assert router._endpoint_quarantined(da) is False
        d = router.pick_deployment(DIM, need=need, ctx=1000)
        assert d is not None
    finally:
        set_current_session(None)


def test_endpoint_quarantine_view(router):
    router.quarantine_endpoint("api.llm7.io", 3600.0)
    v = router.endpoint_quarantine_view()
    assert "api.llm7.io" in v and 3500 <= v["api.llm7.io"] <= 3600
    router._endpoint_quarantine["api.llm7.io"] = time.time() - 5
    assert "api.llm7.io" not in router.endpoint_quarantine_view()


# -------------------------------------------------- seconds_to_midnight
def test_seconds_to_midnight():
    from app.main import seconds_to_midnight
    s = seconds_to_midnight()
    assert 0 < s <= 86401.0
    # a mezzogiorno locale mancano ~12h
    import datetime as dt
    noon = dt.datetime.now().replace(hour=12, minute=0, second=0,
                                     microsecond=0)
    assert abs(seconds_to_midnight(noon.timestamp()) - 43200.0) <= 2.0


def test_nightly_pass_gira_in_cooled_e_rispetta_budget(router):
    """nightly_pass = il vecchio _probe_pass per profilo, con il budget per
    chiave (1/giorno) che limita il giro: mai piu' 50 chiavi same-host a
    raffica."""
    router.policy.cooldown_autoprobe_schedule = "nightly"
    router.policy.cooldown_autoprobe_key_day_max = 1
    router.policy.cooldown_autoprobe_key_gap_sec = 0.0
    router.policy.cooldown_autoprobe_per_dim = 5
    router.policy.cooldown_autoprobe_max_total = 5
    fwd = _Fwd(_Resp(200))
    asyncio.run(autoprobe.nightly_pass(router, fwd, profiles=["test"]))
    n = len(fwd.cli.calls)
    assert n >= 1
    asyncio.run(autoprobe.nightly_pass(router, fwd, profiles=["test"]))
    assert len(fwd.cli.calls) == n     # secondo giro: chiavi a riposo
