"""WARM-REFILL a cascata: finche' la sessione ha meno di `warm_ready_min`
caldi che possono EFFETTIVAMENTE consegnare (need + ctx + output assicurato),
ogni richiesta reale lancia gare 2-alla-volta (A + 1 canary) percorrendo il
ladder -dim ASCENDENTE (free only, MAI -go/-fallback). Il perdente non viene
mai cancellato: finisce come PROBE REALE -> se serve, entra in warm; se
sbaglia, cooldown con le solite logiche. Candidati esclusi: api_key gia' in
warm, dep owner di qualsiasi sessione, dep gia' testati/sondati.

`app.main` va importato SOLO dentro funzioni/fixture (convenzione del repo).
"""
import asyncio
import os
import tempfile
import time

import pytest

from app.config import GatewayConfig
from app.forwarder import Forwarder, UpstreamError
from app.policy import Policy
from app.router import Router

BASE = "scrocco-llm-test"
CONTENT = b'data: {"choices":[{"delta":{"content":"ciao mondo"}}]}\n\n'
FAST = b'data: {"choices":[{"delta":{"content":"VELOCE"}}]}\n\n'
SLOW = b'data: {"choices":[{"delta":{"content":"LENTO"}}]}\n\n'
STOP = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
DONE = b"data: [DONE]\n\n"

CSV_CAP = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/rf-small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5
t@x.com,m/rf-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-M,,4
t@x.com,m/rf-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B,,10
t@x.com,m/rf-g1,groq-go,https://api.groq.com/openai/v1,,0,0,5,K-GO,,
t@x.com,m/rf-f1,groq,https://api.groq.com/openai/v1,paid,0,0,5,K-FB,,
"""


@pytest.fixture()
def router():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_CAP)
    pol = Policy.from_dict({})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def _dep(r, gname, key):
    return next(d for d in r.config.groups[gname] if d.get("api_key") == key)


# ------------------------------------------------------- dep_deliverable
def test_dep_deliverable_bordi(router):
    big = _dep(router, f"{BASE}-1000k", "K-B")
    assert router.dep_deliverable(big, None, 0, 4096)
    assert not router.dep_deliverable(big, None, 995_000, 4096)
    # output grande: la finestra non basta + safety 5%
    assert not router.dep_deliverable(big, None, 500_000, 460_000)
    # effort_capable: la riserva di reasoning (30%) rende non-valido
    effort = dict(big)
    effort["effort_capable"] = True
    assert router.dep_deliverable(effort, None, 0, 4096)
    assert not router.dep_deliverable(effort, None, 0, 650_001)
    # max_input 0 = nessun guardo (come _cap_fits)
    assert router.dep_deliverable({"max_input_tokens": 0}, None, 9_999_999,
                                  999_999)


def test_warm_valid_for_counta_solo_deliverabili(router):
    big = _dep(router, f"{BASE}-1000k", "K-B")
    mid = _dep(router, f"{BASE}-200k", "K-M")
    router.note_session_success("rf-sess", big["unique"], 100, ctx_est=100)
    router.note_session_success("rf-sess", mid["unique"], 100, ctx_est=100)
    pool = router.warm_valid_for("rf-sess", "test", f"{BASE}-32k",
                                 frozenset(), 100, 4096)
    assert {d["unique"] for d in pool} == {big["unique"], mid["unique"]}
    # con un budget di output che SOLO il big regge, il mid non conta
    pool2 = router.warm_valid_for("rf-sess", "test", f"{BASE}-32k",
                                  frozenset(), 100, 190_000)
    assert [d["unique"] for d in pool2] == [big["unique"]]


# ------------------------------------------------ warm_wake_canary (SVEglia)
def test_wake_canary_solo_429_maturi(router):
    """La SVEglia pesca SOLO un dep in cooldown da 429 da >=1h; i 429 freschi,
    gli altri motivi (403/ban) e i non-dormienti non sono candidabili."""
    import time as _t
    small = _dep(router, f"{BASE}-32k", "K-S")
    mid = _dep(router, f"{BASE}-200k", "K-M")
    big = _dep(router, f"{BASE}-1000k", "K-B")
    now = _t.time()
    # mid: 429 dormiente da 2h -> maiuscola candidata
    router._cooldown[mid["unique"]] = now + 600
    router._cooldown_since[mid["unique"]] = now - 7200
    router.stats_for(mid["unique"]).last_reason = "http_429"
    w = router.warm_wake_canary("test", small, frozenset(), 100, 4096,
                                tried={small["unique"]},
                                requested_group=f"{BASE}-32k")
    assert w and w["unique"] == mid["unique"]
    # 429 troppo FRESCO (10 min): non si sveglia
    router._cooldown_since[mid["unique"]] = now - 600
    assert router.warm_wake_canary("test", small, frozenset(), 100, 4096,
                                   tried={small["unique"]},
                                   requested_group=f"{BASE}-32k") is None
    # motivo diverso (403/ban): MAI svegliato
    router._cooldown_since[mid["unique"]] = now - 7200
    router.stats_for(mid["unique"]).last_reason = "upstream_403"
    assert router.warm_wake_canary("test", small, frozenset(), 100, 4096,
                                   tried={small["unique"]},
                                   requested_group=f"{BASE}-32k") is None
    # 429 maturo ma host in quarantena: escluso
    router.stats_for(mid["unique"]).last_reason = "http_429"
    router.quarantine_endpoint("api.groq.com", 3600)
    assert router.warm_wake_canary("test", small, frozenset(), 100, 4096,
                                   tried={small["unique"]},
                                   requested_group=f"{BASE}-32k") is None
    router._endpoint_quarantine.clear()
    # due 429 maturi (mid e big): vince il piu' vicino nel ladder -dim
    router._cooldown[big["unique"]] = now + 600
    router._cooldown_since[big["unique"]] = now - 7200
    router.stats_for(big["unique"]).last_reason = "http_429"
    w2 = router.warm_wake_canary("test", small, frozenset(), 100, 4096,
                                 tried={small["unique"]},
                                 requested_group=f"{BASE}-32k")
    assert w2 and w2["unique"] == mid["unique"]   # il piu' vicino nel ladder


def test_wake_canary_esclude_chiavi_di_tutte_le_sessioni(router):
    """Chiavi DIVERSE da TUTTE LE SESSIONI: se la key del dormiente e' in uso
    da un'altra sessione, la Sveglia non la tocca (e session_api_keys la
    espone)."""
    mid = _dep(router, f"{BASE}-200k", "K-M")
    small = _dep(router, f"{BASE}-32k", "K-S")
    router.note_session_success("altra-sess", mid["unique"], 100, ctx_est=100)
    assert str(mid.get("api_key")) in router.session_api_keys()
    assert router.warm_wake_canary("test", small, frozenset(), 100, 4096,
                                   tried={small["unique"]},
                                   requested_group=f"{BASE}-32k") is None


def test_wake_sweep_raddoppia_cooldown(router, monkeypatch):
    """Il giro di Sveglia prova fino a N dormienti (chiavi diverse fra loro),
    e chi KO vede il cooldown residuo RADDOPPIATO."""
    import app.main as M
    from app import autoprobe as AP
    small = _dep(router, f"{BASE}-32k", "K-S")
    mid = _dep(router, f"{BASE}-200k", "K-M")
    big = _dep(router, f"{BASE}-1000k", "K-B")
    now = time.time()
    for d in (mid, big):
        router._cooldown[d["unique"]] = now + 600
        router._cooldown_since[d["unique"]] = now - 7200
        router.stats_for(d["unique"]).last_reason = "http_429"
    router.policy.warm_refill_wake_max_attempts = 10
    calls = []

    async def fake_probe(fwd, dep, timeout):
        calls.append(dep["unique"])
        return (False, 5.0, 429, "")

    monkeypatch.setattr(AP, "_probe_one", fake_probe)
    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "forwarder", object())
    from app import metrics
    _b4 = dict(metrics.snapshot(("nx_wake_sweep_total",)).get(
        "nx_wake_sweep_total", {}))
    asyncio.run(M._wake_sweep({"model": "m", "messages": []}, "test", small,
                              frozenset(), 100, 4096, None, "sess", {}))
    assert sorted(calls) == sorted([mid["unique"], big["unique"]])
    for u in (mid["unique"], big["unique"]):
        assert router._cooldown[u] - time.time() > 900   # ~1200 (raddoppiato)
    _af = metrics.snapshot(("nx_wake_sweep_total",)).get(
        "nx_wake_sweep_total", {})
    assert _af.get(("ko",), 0) - _b4.get(("ko",), 0) == 2
    assert _af.get(("exhausted",), 0) - _b4.get(("exhausted",), 0) == 1


def test_wake_sweep_successo_torna_caldo(router, monkeypatch):
    """Chi risponde al primo tentativo torna caldo e il giro si ferma."""
    import app.main as M
    from app import autoprobe as AP
    small = _dep(router, f"{BASE}-32k", "K-S")
    mid = _dep(router, f"{BASE}-200k", "K-M")
    now = time.time()
    router._cooldown[mid["unique"]] = now + 600
    router._cooldown_since[mid["unique"]] = now - 7200
    router.stats_for(mid["unique"]).last_reason = "http_429"
    calls = []

    async def fake_probe(fwd, dep, timeout):
        calls.append(dep["unique"])
        return (True, 12.0, 200, "")

    monkeypatch.setattr(AP, "_probe_one", fake_probe)
    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "forwarder", object())
    from app import metrics
    _b4 = dict(metrics.snapshot(("nx_wake_sweep_total",)).get(
        "nx_wake_sweep_total", {}))
    asyncio.run(M._wake_sweep({"model": "m", "messages": []}, "test", small,
                              frozenset(), 100, 4096, None, "sess",
                              {"uniq": set(), "keys": set()}))
    assert calls == [mid["unique"]]
    assert not router.is_cooled_down(mid["unique"])   # svegliato davvero
    _af = metrics.snapshot(("nx_wake_sweep_total",)).get(
        "nx_wake_sweep_total", {})
    assert _af.get(("ok",), 0) - _b4.get(("ok",), 0) == 1
    assert _af.get(("exhausted",), 0) - _b4.get(("exhausted",), 0) == 0


# ------------------------------------------------------- warm_fill_canary
def test_canary_dim_ascendente_e_free_only(router):
    small = _dep(router, f"{BASE}-32k", "K-S")
    mid = _dep(router, f"{BASE}-200k", "K-M")
    c = router.warm_fill_canary("test", small, frozenset(), 100, 4096,
                                tried=set(), requested_group=None,
                                exclude_keys=set(), exclude_uniq=set())
    assert c and c["unique"] == mid["unique"]      # il -dim subito sopra
    # escluso il mid -> il big... ma se lo scartiamo restano solo i PAGATI:
    # la cascata deve fermarsi (None), mai salire su -go/-fallback
    c2 = router.warm_fill_canary("test", small, frozenset(), 100, 4096,
                                 tried=set(), requested_group=None,
                                 exclude_keys=set(),
                                 exclude_uniq={mid["unique"]})
    assert c2 and c2["unique"] == _dep(router, f"{BASE}-1000k",
                                       "K-B")["unique"]
    c3 = router.warm_fill_canary("test", small, frozenset(), 100, 4096,
                                 tried=set(), requested_group=None,
                                 exclude_keys=set(),
                                 exclude_uniq={mid["unique"], c2["unique"]})
    assert c3 is None


def test_canary_esclude_owner_api_key_e_non_deliverabili(router):
    small = _dep(router, f"{BASE}-32k", "K-S")
    mid = _dep(router, f"{BASE}-200k", "K-M")
    big = _dep(router, f"{BASE}-1000k", "K-B")
    # owner vivo DI QUALSIASI SESSIONE: non si tocca
    router.note_session_success("ALTRO", mid["unique"], 100, ctx_est=100)
    c = router.warm_fill_canary("test", small, frozenset(), 100, 4096,
                                tried=set(), requested_group=None,
                                exclude_keys=set(), exclude_uniq=set())
    assert c and c["unique"] == big["unique"]
    # api_key gia' rappresentata in warm (stessa chiave di un caldo)
    router.note_session_success("rf-sess", big["unique"], 100, ctx_est=100)
    wk = router.warm_api_keys("rf-sess", "test", f"{BASE}-32k")
    assert "K-B" in wk
    c2 = router.warm_fill_canary("test", small, frozenset(), 100, 4096,
                                 tried=set(), requested_group=None,
                                 exclude_keys=wk, exclude_uniq=set())
    assert c2 is None                              # mid owner + big key in
    # budget di output fuori scala: niente di consegnabile
    c3 = router.warm_fill_canary("test", small, frozenset(), 100, 250_000,
                                 tried=set(), requested_group=None,
                                 exclude_keys=set(), exclude_uniq=set())
    assert c3 is None                              # solo big potrebbe, ma e'
    # ... owner-altre-sessioni? no: big e' owner sX -> ancora occupato


# ------------------------------- scelta "come a freddo" e scavo del -dim
CSV_DIG = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score,model_preference,order
t@x.com,m/rf-small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5,0,0
t@x.com,m/rf-mid-a,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-MA,,5,0,0
t@x.com,m/rf-mid-b,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-MB,,5,50,0
t@x.com,m/rf-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B,,5,0,0
"""


@pytest.fixture()
def router_dig():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_DIG)
    pol = Policy.from_dict({})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def test_canary_scava_il_dim_col_migliore_a_freddo(router_dig):
    """Regola utente: il canary sceglie il -dim piu' vicino "come a freddo"
    (vince il modello preferito, non il primo per ordine CSV) e SCAVA quel
    -dim coi tentativi successivi prima di salire al -dim superiore."""
    r = router_dig
    small = _dep(r, f"{BASE}-32k", "K-S")
    mid_a = _dep(r, f"{BASE}-200k", "K-MA")
    mid_b = _dep(r, f"{BASE}-200k", "K-MB")       # model_preference=50
    big = _dep(r, f"{BASE}-1000k", "K-B")
    c = r.warm_fill_canary("test", small, frozenset(), 100, 4096,
                           tried=set(), requested_group=None,
                           exclude_keys=set(), exclude_uniq=set())
    assert c and c["unique"] == mid_b["unique"]   # il migliore del 200k
    c2 = r.warm_fill_canary("test", small, frozenset(), 100, 4096,
                            tried=set(), requested_group=None,
                            exclude_keys=set(),
                            exclude_uniq={mid_b["unique"]})
    assert c2 and c2["unique"] == mid_a["unique"]  # scava lo STESSO -dim
    c3 = r.warm_fill_canary("test", small, frozenset(), 100, 4096,
                            tried=set(), requested_group=None,
                            exclude_keys=set(),
                            exclude_uniq={mid_b["unique"], mid_a["unique"]})
    assert c3 and c3["unique"] == big["unique"]    # -dim esaurito: sale


def test_canary_cold_pick_preferisce_il_tier_order_minimo(router):
    """Come a freddo: a parita' di tutto vince il tier `order` minore."""
    r = router
    d_t0 = {"unique": "T0__m__0", "group": f"{BASE}-32k", "order": 0,
            "priority": 0, "model_preference": 0}
    d_t5 = {"unique": "T5__m__0", "group": f"{BASE}-32k", "order": 5,
            "priority": 0, "model_preference": 0}
    assert r._canary_cold_pick([d_t5, d_t0], 100)["unique"] == "T0__m__0"


def test_wake_canary_scava_il_dim_col_migliore(router_dig):
    r = router_dig
    small = _dep(r, f"{BASE}-32k", "K-S")
    mid_a = _dep(r, f"{BASE}-200k", "K-MA")
    mid_b = _dep(r, f"{BASE}-200k", "K-MB")
    now = time.time()
    for d in (mid_a, mid_b):
        r._cooldown[d["unique"]] = now + 600
        r._cooldown_since[d["unique"]] = now - 7200
        r.stats_for(d["unique"]).last_reason = "http_429"
    w = r.warm_wake_canary("test", small, frozenset(), 100, 4096,
                           tried=set(), requested_group=None,
                           exclude_keys=set(), exclude_uniq=set())
    assert w and w["unique"] == mid_b["unique"]
    w2 = r.warm_wake_canary("test", small, frozenset(), 100, 4096,
                            tried=set(), requested_group=None,
                            exclude_keys=set(),
                            exclude_uniq={mid_b["unique"]})
    assert w2 and w2["unique"] == mid_a["unique"]


# ---------------------------------------------------- probe (main, stream)
@pytest.fixture()
def M():
    import app.main as _M
    return _M


def _fake_peel_router(notes):
    from types import SimpleNamespace
    r = SimpleNamespace(
        config=SimpleNamespace(go_suffix="-go",
                               fallback_suffix="-fallback"),
        policy=SimpleNamespace(qc_json=SimpleNamespace(
            watchdog_cooldown_sec=90, stream_total_deadline_ms=180000)),
        stats_for=lambda u: SimpleNamespace(fail_count_24h=0),
        escalate_cooldown=lambda base, f: base,
        note_start=lambda u, ctx=None: notes["start"].append(u),
        note_end=lambda u, ctx=None: notes["end"].append(u),
        is_cooled_down=lambda u: False,
        clear_cooldown=lambda u: None,
        first_content_deadline_ms=lambda u, ctx=None: 5000,
        mark_failed=lambda u, **kw: notes["fail"].append(u),
        note_rate_limit=lambda u, rl: None,
        hedge_canaries=lambda *a, **k: [],
        warm_api_keys=lambda *a, **k: {"K-W"},
        _sess_deps=lambda: {},
        note_warm_owner=lambda sid, u: notes["warm"].append((sid, u)),
    )
    return r


async def _join_probes(M):
    while M._PROBE_TASKS:
        await asyncio.gather(*list(M._PROBE_TASKS), return_exceptions=True)


def test_hedge_refill_gara_a_coppie_e_probe_in_warm(M, monkeypatch):
    """refill=True: anche se A STA gia' fluendo (hold), parte UN solo canary
    (2 in flight); il canary che chiude prima vince e A, finita comunque,
    entra in warm come probe reale."""
    from types import SimpleNamespace
    DEP_A = {"unique": "A__m1__0", "group": "g-32k", "model": "m1",
             "api_key": "K-A"}
    DEP_C = {"unique": "C__m9__9", "group": "g-200k", "model": "m9",
             "api_key": "K-C"}
    notes = {"start": [], "fail": [], "end": [], "warm": []}
    r = _fake_peel_router(notes)
    picked = {}

    def warm_fill(profile, cur, need, ctx, out, **kw):
        picked["exclude_keys"] = set(kw.get("exclude_keys") or ())
        picked["exclude_uniq"] = set(kw.get("exclude_uniq") or ())
        return DEP_C
    r.warm_fill_canary = warm_fill

    async def genA():
        yield SLOW
        await asyncio.sleep(0.3)
        yield STOP

    async def sr(dep, payload, **kw):
        async def gen():
            yield FAST
            yield STOP
        return gen()
    monkeypatch.setattr(M.forwarder, "stream_response", sr)
    monkeypatch.setattr(M, "inject_identity",
                        lambda p, d, router=None: None)

    async def go():
        old = (M.router, M.inject_identity)
        M.router = r
        try:
            raced = {"uniq": {"A__m1__0"}, "keys": {"K-A"}}
            out = await M._hedge_peek(
                dict(DEP_A), genA(), 0.0, 5000, False, 40, 60000, 2048,
                payload={}, profile="test", need=frozenset(), scope="chain",
                ctx=100, tried_set=set(), attempts=[], requested_group=None,
                session="rf-sess", client_ip="", attribution=None,
                hedge_ms=30, _tr_cfg=None, _tct_cfg=None,
                hold=True, refill=True, out_tokens=4096, raced=raced)
            await _join_probes(M)
            return out, raced
        finally:
            M.router, M.inject_identity = old
    (dep, gen, t_att, verdict, prebuf, pending, meta), raced = asyncio.run(go())
    assert verdict == "content" and dep["unique"] == "C__m9__9"
    # il picker ha visto escluse: chiave di A + chiavi warm +uniq corsi
    assert "K-A" in picked["exclude_keys"] and "K-W" in picked["exclude_keys"]
    assert "A__m1__0" in picked["exclude_uniq"]
    assert "C__m9__9" in raced["uniq"] and "K-C" in raced["keys"]
    # A NON cancellata: probe completato con contenuto -> warm
    assert ("rf-sess", "A__m1__0") in notes["warm"]
    assert notes["fail"] == []


def test_hedge_refill_canary_che_sbaglia_apertura_va_in_cooldown(M,
                                                                 monkeypatch):
    """REGE (utente): errore durante il canary -> cooldown come al solito.
    A (muto->poi chiude) serve comunque; il canary che solleva all'apertura
    viene punito, non solo 'notato'."""
    from types import SimpleNamespace
    from app.forwarder import UpstreamError
    DEP_A = {"unique": "A__m1__0", "group": "g-32k", "model": "m1",
             "api_key": "K-A"}
    DEP_C = {"unique": "C__m9__9", "group": "g-200k", "model": "m9",
             "api_key": "K-C"}
    notes = {"start": [], "fail": [], "end": [], "warm": []}
    fails = {}

    def mark_failed(u, **kw):
        notes["fail"].append(u)
        fails[u] = kw

    r = _fake_peel_router(notes)
    r.mark_failed = mark_failed
    r.warm_fill_canary = lambda *a, **k: DEP_C

    async def genA():
        yield SLOW
        await asyncio.sleep(0.05)
        yield STOP

    async def sr(dep, payload, **kw):
        if dep["unique"] == "C__m9__9":
            raise UpstreamError(503, "endpoint unavailable")

        async def gen():
            yield FAST
            yield STOP
        return gen()
    monkeypatch.setattr(M.forwarder, "stream_response", sr)
    monkeypatch.setattr(M, "inject_identity",
                        lambda p, d, router=None: None)

    async def go():
        old = (M.router, M.inject_identity)
        M.router = r
        try:
            raced = {"uniq": {"A__m1__0"}, "keys": {"K-A"}}
            out = await M._hedge_peek(
                dict(DEP_A), genA(), 0.0, 5000, False, 40, 60000, 2048,
                payload={}, profile="test", need=frozenset(), scope="chain",
                ctx=100, tried_set=set(), attempts=[], requested_group=None,
                session="rf-sess", client_ip="", attribution=None,
                hedge_ms=20, _tr_cfg=None, _tct_cfg=None,
                hold=True, refill=True, out_tokens=4096, raced=raced)
            await _join_probes(M)
            return out
        finally:
            M.router, M.inject_identity = old
    (dep, gen, t_att, verdict, prebuf, pending, meta) = asyncio.run(go())
    assert verdict == "content" and dep["unique"] == "A__m1__0"
    assert "C__m9__9" in notes["fail"]
    assert fails["C__m9__9"].get("reason") == "canary_error"
    assert "A__m1__0" not in notes["fail"]


# --------------------------------------- loop streaming: refill end-to-end
CSV_LOOP = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/rf-small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5
t@x.com,m/rf-mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-M,,4
t@x.com,m/rf-big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B,,10
t@x.com,m/rf-g1,groq-go,https://api.groq.com/openai/v1,,0,0,5,K-GO,,
t@x.com,m/rf-f1,groq,https://api.groq.com/openai/v1,paid,0,0,5,K-FB,,
"""


@pytest.fixture()
def ML(tmp_path, monkeypatch):
    import app.main as _M
    csv = tmp_path / "k.csv"
    csv.write_text(CSV_LOOP)
    orig_csv = _M.config.csv_path
    qj = _M.router.policy.qc_json
    pol = _M.router.policy
    snap = (qj.stream_hedge_delay_ms, qj.stream_first_content_ms,
            qj.stream_hold_until_finish, pol.warm_refill_enabled,
            pol.warm_ready_min, pol.warm_ready_rpm_adaptive,
            pol.warm_ready_rpm_window_sec, pol.warm_ready_rpm_base,
            pol.warm_ready_rpm_step, pol.warm_ready_min_max)
    cooled = set(_M.router._cooldown)
    owned = dict(_M.router._dep_last_session)
    sdeps = {k: set(v) for k, v in _M.router._session_deps.items()}
    slok = dict(_M.router._session_last_ok)
    probes = {k: dict(v) for k, v in _M.router._probes().items()}
    _M.config.csv_path = csv
    _M.config.reload()
    qj.stream_hedge_delay_ms = 50
    qj.stream_first_content_ms = 5000
    qj.stream_hold_until_finish = True
    pol.warm_refill_enabled = True
    pol.warm_ready_min = 3
    yield _M
    (qj.stream_hedge_delay_ms, qj.stream_first_content_ms,
     qj.stream_hold_until_finish, pol.warm_refill_enabled,
     pol.warm_ready_min, pol.warm_ready_rpm_adaptive,
     pol.warm_ready_rpm_window_sec, pol.warm_ready_rpm_base,
     pol.warm_ready_rpm_step, pol.warm_ready_min_max) = snap
    _M.config.csv_path = orig_csv
    _M.config.reload()
    for k in list(_M.router._cooldown):
        if k not in cooled:
            _M.router._cooldown.pop(k, None)
    _M.router._dep_last_session.clear()
    _M.router._dep_last_session.update(owned)
    _M.router._session_deps.clear()
    _M.router._session_deps.update(sdeps)
    _M.router._session_last_ok.clear()
    _M.router._session_last_ok.update(slok)
    _M.router._probes().clear()
    _M.router._probes().update(probes)


def test_streaming_refill_riscalda_il_warm_a_3(ML, monkeypatch):
    """Sessione con UN solo caldo valido (il big/holder): la richiesta parte
    sul small, il refill lancia UN canary (il mid), il piu' veloce consegna,
    e A finisce come probe -> warm a 3. Nessun -go/-fallback chiamato."""
    from app.router import set_current_session
    small = ML.config.groups[f"{BASE}-32k"][0]
    mid = ML.config.groups[f"{BASE}-200k"][0]
    big = ML.config.groups[f"{BASE}-1000k"][0]
    ML.router.note_session_success("rf-sess", big["unique"], 100, ctx_est=100)
    calls = []

    async def sr(dep, payload, **kw):
        calls.append(dep["unique"])
        u = dep["unique"]

        async def gen():
            if u == small["unique"]:
                yield SLOW
                await asyncio.sleep(0.3)
                yield STOP
            else:
                yield FAST
                yield STOP
        return gen()
    monkeypatch.setattr(ML.forwarder, "stream_response", sr)

    async def go():
        set_current_session("rf-sess")
        try:
            payload = {"model": small["model"],
                       "messages": [{"role": "user", "content": "ciao"}]}
            resp = await ML._stream_with_fallback(
                "test", small, payload, scope="chain", session="rf-sess",
                ses="rf-sess", ctx=100)
            body = b""
            if hasattr(resp, "body_iterator"):
                async for c in resp.body_iterator:
                    body += c
            await _join_probes(ML)
            return resp, body
        finally:
            set_current_session(None)
    resp, body = asyncio.run(go())
    assert isinstance(resp, ML.StreamingResponse)
    assert b"VELOCE" in body and b"LENTO" not in body
    # solo A + UN canary: mai i pagati, mai il big (chiave gia' in warm)
    assert sorted(calls) == sorted([small["unique"], mid["unique"]])
    # warm completo: holder + winner + probe
    pool = ML.router.warm_valid_for("rf-sess", "test", f"{BASE}-32k",
                                    frozenset(), 100, 4096)
    assert {d["unique"] for d in pool} == {small["unique"], mid["unique"],
                                           big["unique"]}
    assert small["unique"] not in ML.router._cooldown


# ---------------------------------------------- non-streaming: race 2/2
def _ns_payload():
    return {"model": "m", "messages": [{"role": "user", "content": "ciao"}]}


def _ns_resp(txt):
    return {"choices": [{"message": {"role": "assistant",
                                     "content": txt},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 2}}


@pytest.fixture()
def FW():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_LOOP)
    pol = Policy.from_dict({})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    r = Router(cfg, pol)
    yield r
    os.unlink(path)


async def _join_ns():
    import app.forwarder as _F
    while _F._NS_PROBES:
        await asyncio.gather(*list(_F._NS_PROBES), return_exceptions=True)


def test_nonstream_refill_consegna_la_piu_veloce(FW, monkeypatch):
    import app.forwarder as F
    small = _dep(FW, f"{BASE}-32k", "K-S")
    mid = _dep(FW, f"{BASE}-200k", "K-M")
    calls = []

    async def fake_call(self, dep, payload, **kw):
        calls.append(dep["unique"])
        if dep["unique"] == small["unique"]:
            await asyncio.sleep(0.05)
            return _ns_resp("LENTO")
        return _ns_resp("VELOCE")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        data, used = await fwd.call_with_fallback(
            FW, "test", small, _ns_payload(), need=frozenset(),
            scope="chain", ctx=100, attempts_box=[], session="rf-sess",
            ses="rf-sess", client_ip="", attribution=None,
            requested_group=None)
        await _join_ns()
        return data, used
    data, used = asyncio.run(go())
    assert used["unique"] == mid["unique"]
    assert data["choices"][0]["message"]["content"] == "VELOCE"
    assert sorted(calls) == sorted([small["unique"], mid["unique"]])
    # A (lenta ma sana) e' finita come probe: in warm, nessuna penale
    ent = FW._dep_last_session.get(small["unique"])
    assert ent and ent[0] == "rf-sess"
    assert small["unique"] not in FW._cooldown


def test_nonstream_refill_errori_vanno_in_cooldown(FW, monkeypatch):
    import app.forwarder as F
    small = _dep(FW, f"{BASE}-32k", "K-S")
    mid = _dep(FW, f"{BASE}-200k", "K-M")
    from app.forwarder import UpstreamError

    async def fake_call(self, dep, payload, **kw):
        raise UpstreamError(503, "boom")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        try:
            await fwd.call_with_fallback(
                FW, "test", small, _ns_payload(), need=frozenset(),
                scope="chain", ctx=100, attempts_box=[], session="rf-sess",
                ses="rf-sess", client_ip="", attribution=None,
                requested_group=None)
        finally:
            await _join_ns()
    with pytest.raises(UpstreamError):
        asyncio.run(go())
    # il probe fallito va in cooldown COME IL SOLITO tentativo servito
    assert small["unique"] in FW._cooldown
    assert mid["unique"] in FW._cooldown


def test_nonstream_slow_race_parte_anche_con_refill_in_volo(FW, monkeypatch):
    """Il timer lento e' indipendente dal refill: a soglia scaduta apre un
    canario ANCHE se un canario di refill e' gia' in volo. Nessuna penale."""
    import app.forwarder as F
    small = _dep(FW, f"{BASE}-32k", "K-S")
    big = _dep(FW, f"{BASE}-1000k", "K-B")
    FW.policy.nonstream_slow_race_after_ms = 100
    FW.policy.slow_canary_after_ms = 100
    calls = []

    async def fake_call(self, dep, payload, **kw):
        calls.append(dep["unique"])
        if dep["unique"] == big["unique"]:
            await asyncio.sleep(0.2)
            return _ns_resp("TERZO")
        await asyncio.sleep(0.6)
        return _ns_resp("LENTO")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        data, used = await fwd.call_with_fallback(
            FW, "test", small, _ns_payload(), need=frozenset(),
            scope="chain", ctx=100, attempts_box=[], session="rf-sess",
            ses="rf-sess", client_ip="", attribution=None,
            requested_group=None)
        await _join_ns()
        return data, used
    data, used = asyncio.run(go())
    assert big["unique"] in calls                      # canario lento partito
    assert used["unique"] == big["unique"]             # e ha vinto
    assert data["choices"][0]["message"]["content"] == "TERZO"
    assert not FW.is_cooled_down(small["unique"])
    assert not FW.is_cooled_down(big["unique"])


def test_nonstream_slow_race_parte_senza_refill(FW, monkeypatch):
    """Senza refill (nessun canario in volo) il timer lento apre comunque il
    suo canario a soglia scaduta."""
    import app.forwarder as F
    small = _dep(FW, f"{BASE}-32k", "K-S")
    big = _dep(FW, f"{BASE}-1000k", "K-B")
    FW.policy.warm_refill_enabled = False
    FW.policy.nonstream_slow_race_after_ms = 100
    FW.policy.slow_canary_after_ms = 100
    calls = []

    async def fake_call(self, dep, payload, **kw):
        calls.append(dep["unique"])
        if dep["unique"] == big["unique"]:
            return _ns_resp("TERZO")
        await asyncio.sleep(0.6)
        return _ns_resp("LENTO")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        data, used = await fwd.call_with_fallback(
            FW, "test", small, _ns_payload(), need=frozenset(),
            scope="chain", ctx=100, attempts_box=[], session="rf-sess",
            ses="rf-sess", client_ip="", attribution=None,
            requested_group=None)
        await _join_ns()
        return data, used
    data, used = asyncio.run(go())
    assert big["unique"] in calls
    assert used["unique"] == big["unique"]
    assert not FW.is_cooled_down(big["unique"])


def test_nonstream_canary_anticipa_il_flag(FW, monkeypatch):
    """`slow_canary_after_ms` << `nonstream_slow_race_after_ms`: il canario
    parte al proprio tempo SENZA marcare il dep lento (flag a 45s)."""
    import app.forwarder as F
    small = _dep(FW, f"{BASE}-32k", "K-S")
    big = _dep(FW, f"{BASE}-1000k", "K-B")
    FW.policy.warm_refill_enabled = False
    FW.policy.nonstream_slow_race_after_ms = 100000
    FW.policy.slow_canary_after_ms = 100
    marked = []
    monkeypatch.setattr(FW, "mark_session_slow",
                        lambda sid, u: marked.append(u))
    calls = []

    async def fake_call(self, dep, payload, **kw):
        calls.append(dep["unique"])
        if dep["unique"] == big["unique"]:
            return _ns_resp("VELOCE")
        await asyncio.sleep(0.6)
        return _ns_resp("LENTO")
    monkeypatch.setattr(F.Forwarder, "call", fake_call)
    fwd = F.Forwarder()

    async def go():
        data, used = await fwd.call_with_fallback(
            FW, "test", small, _ns_payload(), need=frozenset(),
            scope="chain", ctx=100, attempts_box=[], session="rf-sess",
            ses="rf-sess", client_ip="", attribution=None,
            requested_group=None)
        await _join_ns()
        return data, used
    data, used = asyncio.run(go())
    assert big["unique"] in calls, "canario aperto al proprio timing"
    assert used["unique"] == big["unique"]
    assert marked == [], "flag lento NON scatta (soglia ancora lontana)"


# ------------------------------------------------ histnorm: testa sporca
def test_scrub_assistant_vuote_anche_in_testa():
    from app.histnorm import normalize_messages
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": None},          # in HEAD
        {"role": "user", "content": "b"},
        {"role": "assistant", "tool_calls": [{"id": "t1",
                                              "type": "function",
                                              "function": {"name": "f",
                                                           "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ok"},
        {"role": "assistant", "content": "   "},          # coda vuota
        {"role": "user", "content": "c"},
    ]
    out, rep = normalize_messages(msgs, tail_floor=3)     # tail=[3..]
    assert rep["empty_assistant"] == 2                    # testa + coda
    roles = [(m.get("role"), m.get("tool_calls") is not None) for m in out]
    assert ("assistant", True) in roles                   # intatta
    assert all(not (m.get("role") == "assistant" and not m.get("tool_calls")
                    and not str(m.get("content") or "").strip())
               for m in out)


def test_policy_knob_warm_refill():
    p = Policy.from_dict({"warm_pool": {"refill_enabled": False,
                                        "ready_min": 5,
                                        "ready_min_adaptive": False,
                                        "ready_min_rpm_window_sec": 60,
                                        "ready_min_rpm_base": 8,
                                        "ready_min_rpm_step": 4,
                                        "ready_min_max": 9,
                                        "refill_default_out_tokens": 1000,
                                        "max_inflight": 2,
                                        "wake_max_attempts": 5}})
    assert p.warm_refill_enabled is False
    assert p.warm_ready_min == 5
    assert p.warm_ready_rpm_adaptive is False
    assert p.warm_ready_rpm_window_sec == 60
    assert p.warm_ready_rpm_base == 8.0
    assert p.warm_ready_rpm_step == 4.0
    assert p.warm_ready_min_max == 9
    assert p.warm_refill_default_out_tokens == 1000
    assert p.warm_refill_max_inflight == 2
    assert p.warm_refill_wake_max_attempts == 5
    d = Policy.from_dict({})
    assert d.warm_refill_enabled is True and d.warm_ready_min == 3
    assert d.warm_refill_max_inflight == 6
    assert d.warm_refill_wake_max_attempts == 10
    # default adattivo: base 5 rpm, step 5, cap 6, finestra 180s
    assert d.warm_ready_rpm_adaptive is True
    assert d.warm_ready_rpm_window_sec == 180
    assert d.warm_ready_rpm_base == 5.0
    assert d.warm_ready_rpm_step == 5.0
    assert d.warm_ready_min_max == 6


# ----------------------------------------------- tetto 4 in volo PER SESSIONE
def test_registro_probe_in_volo(router):
    import time as _t
    r = router
    assert r.probes_in_flight("s1") == 0
    r.note_probe_started("s1", "u-a")
    r.note_probe_started("s1", "u-b")
    r.note_probe_started("s1", "u-a")          # idempotente: stesso uniq
    assert r.probes_in_flight("s1") == 2
    assert r.probes_in_flight("s2") == 0
    r.note_probe_done("s1", "u-a")
    assert r.probes_in_flight("s1") == 1
    r._probes_flight["s1"]["u-b"] = _t.time() - 2000   # rete TTL
    assert r.probes_in_flight("s1") == 0


def test_streaming_refill_bloccato_a_4_in_volo(ML, monkeypatch):
    """Con 4 probe gia' in volo per la sessione il gate NON accende il
    canario (anche se i validi sono 1/3): A viene servita da sola, e i
    registri restanti si liberano con note_probe_done."""
    from app.router import set_current_session
    small = ML.config.groups[f"{BASE}-32k"][0]
    big = ML.config.groups[f"{BASE}-1000k"][0]
    ML.router.note_session_success("rf-sess", big["unique"], 100, ctx_est=100)
    for i in range(6):
        ML.router.note_probe_started("rf-sess", f"phantom-{i}")
    calls = []

    async def sr(dep, payload, **kw):
        calls.append(dep["unique"])

        async def gen():
            yield SLOW
            yield STOP
        return gen()
    monkeypatch.setattr(ML.forwarder, "stream_response", sr)

    async def go():
        set_current_session("rf-sess")
        try:
            payload = {"model": small["model"],
                       "messages": [{"role": "user", "content": "ciao"}]}
            resp = await ML._stream_with_fallback(
                "test", small, payload, scope="chain", session="rf-sess",
                ses="rf-sess", ctx=100)
            body = b""
            if hasattr(resp, "body_iterator"):
                async for c in resp.body_iterator:
                    body += c
            await _join_probes(ML)
            return resp, body
        finally:
            set_current_session(None)
    resp, body = asyncio.run(go())
    assert isinstance(resp, ML.StreamingResponse)
    assert b"LENTO" in body
    assert calls == [small["unique"]]           # nessun canario: tetto saturo
    assert ML.router.probes_in_flight("rf-sess") == 6   # phantom non toccati
    ML.router.note_probe_done("rf-sess", "phantom-0")
    assert ML.router.probes_in_flight("rf-sess") == 5


def test_streaming_refill_libera_il_tetto_quando_i_probe_finiscono(ML,
                                                                   monkeypatch):
    """0 phantom, 1 valido -> puo' riaccedere FINO a 4 in volo: il gate spara
    a ogni round finche' il tetto non e' saturo; il probe di A si scarica da
    solo nel finally (contatore a 0 a gara finita)."""
    from app.router import set_current_session
    small = ML.config.groups[f"{BASE}-32k"][0]
    mid = ML.config.groups[f"{BASE}-200k"][0]
    big = ML.config.groups[f"{BASE}-1000k"][0]
    ML.router.note_session_success("rf-sess", big["unique"], 100, ctx_est=100)
    calls = []

    async def sr(dep, payload, **kw):
        calls.append(dep["unique"])
        u = dep["unique"]

        async def gen():
            if u == small["unique"]:
                yield SLOW
                await asyncio.sleep(0.3)
                yield STOP
            else:
                yield FAST
                yield STOP
        return gen()
    monkeypatch.setattr(ML.forwarder, "stream_response", sr)

    async def go():
        set_current_session("rf-sess")
        try:
            payload = {"model": small["model"],
                       "messages": [{"role": "user", "content": "ciao"}]}
            resp = await ML._stream_with_fallback(
                "test", small, payload, scope="chain", session="rf-sess",
                ses="rf-sess", ctx=100)
            body = b""
            if hasattr(resp, "body_iterator"):
                async for c in resp.body_iterator:
                    body += c
            await _join_probes(ML)
            return resp, body
        finally:
            set_current_session(None)
    resp, body = asyncio.run(go())
    assert b"VELOCE" in body
    assert sorted(calls) == sorted([small["unique"], mid["unique"]])
    # probe conclusi -> registro vuoto (nessun contatore perso)
    assert ML.router.probes_in_flight("rf-sess") == 0


def test_metrics_wake_sweep_espone_label_result():
    """Il counter nx_wake_sweep_total deve renderizzare la label result
    (Prometheus): senza declare() le serie uscivano senza nome label."""
    from app import metrics
    metrics.inc("nx_wake_sweep_total", ("ok",))
    out = metrics.render()
    assert 'nx_wake_sweep_total{result="ok"}' in out


# ------------------------------- WARM-READY ADATTIVO AL RATE DI SESSIONE
def _seed_rate(r, sid, n):
    """Popola la finestra di rate della sessione con n arrivi 'adesso'."""
    from collections import deque
    r._sess_rate()[sid] = deque([time.time()] * n)


def test_session_rpm_finestra(router):
    from collections import deque
    r = router
    assert r.session_rpm("s") == 0.0
    t = time.time()
    r._sess_rate()["s"] = deque([t - 400, t - 200, t - 10, t - 5])
    # finestra default 180s: contano solo gli ultimi due -> 2/(180/60)
    assert abs(r.session_rpm("s") - 2 / 3.0) < 1e-6
    # finestra esplicita 60s: conta solo t-10 e t-5
    assert abs(r.session_rpm("s", 60) - 2.0) < 1e-6


def test_note_session_request_conta_e_ignora_vuoti(router):
    r = router
    r.note_session_request(None)               # no-op: non esplode
    r.note_session_request("")
    for _ in range(16):
        r.note_session_request("s")
    assert abs(r.session_rpm("s") - 16 / 3.0) < 0.2
    assert r.session_rpm(None) == 0.0


def test_warm_ready_effective_soglie(router):
    """Default: base 3, >5rpm -> 4, >10 -> 5, >15 -> 6 (cap)."""
    r = router
    pol = r.policy
    assert pol.warm_ready_min == 3 and pol.warm_ready_min_max == 6
    cases = [(15, 3),     # rpm 5.00  -> base (non > base)
             (16, 4),     # rpm 5.33  -> +1
             (30, 4),     # rpm 10.00 -> +1
             (31, 5),     # rpm 10.33 -> +2
             (45, 5),     # rpm 15.00 -> +2
             (46, 6),     # rpm 15.33 -> +3
             (300, 6)]    # tappato al cap
    for n, exp in cases:
        _seed_rate(r, "s", n)
        assert r.warm_ready_effective("s", pol) == exp, (n, exp)


def test_warm_ready_effective_off_e_senza_sessione(router):
    r = router
    _seed_rate(r, "s", 300)
    assert r.warm_ready_effective(None, r.policy) == 3
    assert r.warm_ready_effective("", r.policy) == 3
    r.policy.warm_ready_rpm_adaptive = False
    assert r.warm_ready_effective("s", r.policy) == 3


def test_warm_ready_effective_nel_gate_streaming(ML):
    """Nel punto di decisione del refill il valore arriva dal router (stessa
    policy): a rpm alto la soglia sale a 5, a feature off torna 3."""
    from collections import deque
    r = ML.router
    pol = r.policy
    pol.warm_refill_enabled = True
    pol.warm_ready_min = 3
    pol.warm_ready_rpm_adaptive = True
    r._sess_rate()["rf-sess"] = deque([time.time()] * 31)   # rpm ~10.3
    assert r.warm_ready_effective("rf-sess", pol) == 5
    pol.warm_ready_rpm_adaptive = False
    assert r.warm_ready_effective("rf-sess", pol) == 3


def test_prestito_conta_nel_valido(router):
    """(A) I warm PRESTABILI contano nel conteggio del gate: finché la
    sessione ha prestati utili il refill NON deve partire."""
    from app.router import set_current_session
    r = router
    big = _dep(r, f"{BASE}-1000k", "K-B")
    r._dep_last_session[big["unique"]] = ("other", time.time())
    r.stats_for(big["unique"]).last_used = time.time() - 300
    set_current_session("me")
    try:
        own = r.warm_valid_for("me", "test", f"{BASE}-1000k", frozenset(),
                               100, 4096, include_borrowed=False)
        borrow = r.warm_valid_for("me", "test", f"{BASE}-1000k", frozenset(),
                                  100, 4096, include_borrowed=True)
    finally:
        set_current_session(None)
    assert {d["unique"] for d in own} == set()
    assert {d["unique"] for d in borrow} == {big["unique"]}


# ------------------------------------------ CODA DEI PROVIDER GIA' IN USO
CSV_PROV = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score,model_preference,order
t@x.com,m/rf-small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5,0,0
t@x.com,m/rf-mg,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-MG,,5,0,0
t@x.com,m/rf-mo,openrouter,https://openrouter.ai/api/v1,free,200,200000,5,K-MO,,5,0,9
"""


@pytest.fixture()
def router_prov():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_PROV)
    pol = Policy.from_dict({})
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    yield Router(cfg, pol)
    os.unlink(path)


def test_warm_providers_espone_i_provider_in_warm(router_prov):
    r = router_prov
    small = _dep(r, f"{BASE}-32k", "K-S")
    assert r._warm_providers() == set()
    r.note_session_success("altra", small["unique"], 100, ctx_est=100)
    assert r._warm_providers() == {"groq"}


def test_canary_provider_in_warm_va_in_coda(router_prov):
    """Se un provider e' gia' in warm, i suoi candidati vanno in CODA: il
    canary prende prima un ALTRO provider, anche se nel cold-pick quello in
    warm sarebbe il tier piu' basso. Con il knob OFF torna l'ordine legacy."""
    r = router_prov
    small = _dep(r, f"{BASE}-32k", "K-S")      # groq (dep corrente)
    mg = _dep(r, f"{BASE}-200k", "K-MG")       # groq, tier order=0
    mo = _dep(r, f"{BASE}-200k", "K-MO")       # openrouter, tier order=9
    # nessun provider in warm: come a freddo vince il tier piu' basso (groq)
    c = r.warm_fill_canary("test", small, frozenset(), 100, 4096, tried=set(),
                           requested_group=None, exclude_keys=set(),
                           exclude_uniq=set())
    assert c and c["unique"] == mg["unique"]
    # groq ora e' in warm (altra sessione): il canary preferisce openrouter
    r.note_session_success("altra", small["unique"], 100, ctx_est=100)
    c2 = r.warm_fill_canary("test", small, frozenset(), 100, 4096, tried=set(),
                            requested_group=None, exclude_keys=set(),
                            exclude_uniq=set())
    assert c2 and c2["unique"] == mo["unique"]
    # knob OFF: legacy, nessuna coda -> torna il tier piu' basso (groq)
    r.policy.canary_warm_last = False
    c3 = r.warm_fill_canary("test", small, frozenset(), 100, 4096, tried=set(),
                            requested_group=None, exclude_keys=set(),
                            exclude_uniq=set())
    assert c3 and c3["unique"] == mg["unique"]


def test_canary_provider_in_warm_non_escluso_se_unico(router_prov):
    """Il provider in warm NON viene escluso: se resta l'unico candidato
    (l'altro provider e' gia' tentato in questa richiesta) viene scelto."""
    r = router_prov
    small = _dep(r, f"{BASE}-32k", "K-S")
    mg = _dep(r, f"{BASE}-200k", "K-MG")
    mo = _dep(r, f"{BASE}-200k", "K-MO")
    r.note_session_success("altra", small["unique"], 100, ctx_est=100)
    c = r.warm_fill_canary("test", small, frozenset(), 100, 4096, tried=set(),
                           requested_group=None, exclude_keys=set(),
                           exclude_uniq={mo["unique"]})
    assert c and c["unique"] == mg["unique"]


def test_canary_chiave_in_warm_resta_esclusa(router_prov):
    """La chiave gia' in warm resta ESCLUSA SEMPRE, anche se il provider e'
    in coda: passata in `exclude_keys` (come fanno i chiamanti) il candidato
    sparisce del tutto."""
    r = router_prov
    small = _dep(r, f"{BASE}-32k", "K-S")
    mg = _dep(r, f"{BASE}-200k", "K-MG")
    mo = _dep(r, f"{BASE}-200k", "K-MO")
    r.note_session_success("altra", small["unique"], 100, ctx_est=100)
    # groq in warm (coda) ma la chiave di mg e' esclusa: resta solo openrouter
    c = r.warm_fill_canary("test", small, frozenset(), 100, 4096, tried=set(),
                           requested_group=None, exclude_keys={"K-MG"},
                           exclude_uniq=set())
    assert c and c["unique"] == mo["unique"]
    # escludo ANCHE la chiave di openrouter: niente piu' candidati
    c2 = r.warm_fill_canary("test", small, frozenset(), 100, 4096, tried=set(),
                            requested_group=None,
                            exclude_keys={"K-MG", "K-MO"}, exclude_uniq=set())
    assert c2 is None


def test_wake_provider_in_warm_va_in_coda(router_prov):
    """Anche la SVEglia mette in coda i dormienti di un provider gia' in
    warm, preferendo un altro provider; con il knob OFF torna legacy."""
    r = router_prov
    small = _dep(r, f"{BASE}-32k", "K-S")
    mg = _dep(r, f"{BASE}-200k", "K-MG")
    mo = _dep(r, f"{BASE}-200k", "K-MO")
    now = time.time()
    for d in (mg, mo):
        r._cooldown[d["unique"]] = now + 600
        r._cooldown_since[d["unique"]] = now - 7200
        r.stats_for(d["unique"]).last_reason = "http_429"
    # nessun provider in warm: vince il tier piu' basso (groq)
    w = r.warm_wake_canary("test", small, frozenset(), 100, 4096,
                           tried={small["unique"]}, requested_group=None,
                           exclude_keys=set(), exclude_uniq=set(),
                           min_age_sec=3600)
    assert w and w["unique"] == mg["unique"]
    # groq in warm -> la Sveglia preferisce openrouter
    r.note_session_success("altra", small["unique"], 100, ctx_est=100)
    w2 = r.warm_wake_canary("test", small, frozenset(), 100, 4096,
                            tried={small["unique"]}, requested_group=None,
                            exclude_keys=set(), exclude_uniq=set(),
                            min_age_sec=3600)
    assert w2 and w2["unique"] == mo["unique"]
    # knob OFF -> legacy
    r.policy.canary_warm_last = False
    w3 = r.warm_wake_canary("test", small, frozenset(), 100, 4096,
                            tried={small["unique"]}, requested_group=None,
                            exclude_keys=set(), exclude_uniq=set(),
                            min_age_sec=3600)
    assert w3 and w3["unique"] == mg["unique"]


# --------------------------------- CODA PROVIDER "IN USO" (warm + in volo)
def test_warm_providers_include_probe_in_volo(router_prov):
    """Un probe/canaro IN VOLO (non e' ancora owner: l'ownership si acquista
    al successo) marca il provider come 'in uso', anche se di un'ALTRA
    sessione; scaduto oltre il TTL di sicurezza non conta piu'."""
    r = router_prov
    mg = _dep(r, f"{BASE}-200k", "K-MG")       # groq
    assert r._warm_providers() == set()
    r.note_probe_started("altra", mg["unique"])
    assert r._warm_providers() == {"groq"}
    r.note_probe_done("altra", mg["unique"])
    assert r._warm_providers() == set()
    r._probes()["altra"] = {mg["unique"]: time.time() - 1000.0}
    assert r._warm_providers() == set()


def test_warm_providers_include_chiamata_reale_in_corso(router_prov):
    """Una chiamata REALE in corso (inflight>0) marca il provider come 'in
    uso': quel provider sta gia' popolando la cache con lo stesso contenuto."""
    r = router_prov
    mg = _dep(r, f"{BASE}-200k", "K-MG")       # groq
    assert r._warm_providers() == set()
    r.note_start(mg["unique"], ctx_est=100)
    assert r._warm_providers() == {"groq"}
    r.note_end(mg["unique"], ctx_est=100)
    assert r._warm_providers() == set()


def test_canary_provider_in_volo_va_in_coda(router_prov):
    """Un probe in volo su groq (sessione 'altra') mette groq in CODA: il
    canary della sessione corrente preferisce openrouter anche se groq ha il
    tier `order` piu' basso."""
    r = router_prov
    small = _dep(r, f"{BASE}-32k", "K-S")      # groq (dep corrente)
    mg = _dep(r, f"{BASE}-200k", "K-MG")       # groq, tier order=0
    mo = _dep(r, f"{BASE}-200k", "K-MO")       # openrouter, tier order=9
    r.note_probe_started("altra", mg["unique"])
    c = r.warm_fill_canary("test", small, frozenset(), 100, 4096, tried=set(),
                           requested_group=None, exclude_keys=set(),
                           exclude_uniq=set())
    assert c and c["unique"] == mo["unique"]


def test_canary_provider_in_volo_soft_ripiega(router_prov):
    """La chiave in uso NON viene MAI riciclata: se resta solo un provider il
    cui unico dep ha la chiave in volo, il canary non lo riusa (None). Se esiste
    una SECONDA chiave dello stesso provider, quella rientra (fascia 2)."""
    r = router_prov
    small = _dep(r, f"{BASE}-32k", "K-S")
    mg = _dep(r, f"{BASE}-200k", "K-MG")
    mo = _dep(r, f"{BASE}-200k", "K-MO")
    r.note_probe_started("altra", mg["unique"])
    c = r.warm_fill_canary("test", small, frozenset(), 100, 4096, tried=set(),
                           requested_group=None, exclude_keys=set(),
                           exclude_uniq={mo["unique"]})
    assert c is None, "chiave in volo: mai riciclata, nessun altro candidato"


def test_sweep_alterna_provider_e_chiave(router_prov):
    """Sweep anti-raffica: prov1/k1, prov2/k1, prov1/k2, prov2/k2 — mai due
    volte di fila lo stesso provider, e le chiavi di un provider ruotano."""
    r = router_prov
    ds = [
        {"unique": "g1__m__0", "group": f"{BASE}-200k", "provider": "groq",
         "api_key": "G1", "order": 0, "priority": 5, "model_preference": 0,
         "max_input_tokens": 200000},
        {"unique": "g2__m__0", "group": f"{BASE}-200k", "provider": "groq",
         "api_key": "G2", "order": 0, "priority": 5, "model_preference": 0,
         "max_input_tokens": 200000},
        {"unique": "o1__m__0", "group": f"{BASE}-200k", "provider": "openrouter",
         "api_key": "O1", "order": 0, "priority": 5, "model_preference": 0,
         "max_input_tokens": 200000},
        {"unique": "o2__m__0", "group": f"{BASE}-200k", "provider": "openrouter",
         "api_key": "O2", "order": 0, "priority": 5, "model_preference": 0,
         "max_input_tokens": 200000},
    ]
    seq = [d["unique"] for d in r._sweep_provider_key(ds)]
    assert seq == ["g1__m__0", "o1__m__0", "g2__m__0", "o2__m__0"]


def test_wake_provider_in_volo_va_in_coda(router_prov):
    """Anche la SVEglia mette in coda (soft) i dormienti di un provider con un
    probe in volo, preferendo un altro provider."""
    r = router_prov
    small = _dep(r, f"{BASE}-32k", "K-S")
    mg = _dep(r, f"{BASE}-200k", "K-MG")
    mo = _dep(r, f"{BASE}-200k", "K-MO")
    now = time.time()
    for d in (mg, mo):
        r._cooldown[d["unique"]] = now + 600
        r._cooldown_since[d["unique"]] = now - 7200
        r.stats_for(d["unique"]).last_reason = "http_429"
    r.note_probe_started("altra", mg["unique"])
    w = r.warm_wake_canary("test", small, frozenset(), 100, 4096,
                           tried={small["unique"]}, requested_group=None,
                           exclude_keys=set(), exclude_uniq=set(),
                           min_age_sec=3600)
    assert w and w["unique"] == mo["unique"]
