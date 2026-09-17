"""Mai rispondere troncato: hold-until-finish come GARANZIA (post-incidente
incidente in produzione 2026-09-15).

In hold mode `finish_reason=length` NON e' mai 'content' (nemmeno con 0
caratteri: il reasoning si era mangiato tutto il budget clampato) e una
chiusura pulita senza risposta (stop/[DONE] a 0 caratteri) e' un fallimento
RUOTABILE senza penale: il gateway prova il candidato PIU' CAPACE
(finestra maggiore, poi intelligence) e solo a catena esaurita consegna 503
retryable. L'hedge resta attivo anche in hold, ma e' hold-aware: gara solo
su upstream MUTO (nessun byte), mai su una risposta lunga che sta fluendo.

`app.main` va importato SOLO dentro funzioni/fixture (mai a livello modulo).
"""
import asyncio
import os
import tempfile
import time as _time

import pytest
from fastapi.responses import JSONResponse

from app.config import GatewayConfig
from app.policy import Policy
from app.router import Router

CONTENT = b'data: {"choices":[{"delta":{"content":"ciao mondo"}}]}\n\n'
STOP = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
LENGTH = b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
DONE = b"data: [DONE]\n\n"
REASON = b'data: {"choices":[{"delta":{"reasoning_content":"penso"}}]}\n\n'


@pytest.fixture()
def M():
    import app.main as _M
    return _M


def _run(M, chunks, fc=500, **kw):
    async def gen():
        for c in chunks:
            yield c
    return asyncio.run(M._peek_stream(gen(), fc, kw.pop("incl", False),
                                      kw.pop("min_chars", 40),
                                      hold_until_finish=True, **kw))


def _run_silent(M, fc=60, **kw):
    async def gen():
        await asyncio.sleep(10)
        yield CONTENT
    out = {}

    async def go():
        v, _buf, task, _m = await M._peek_stream(gen(), fc, False, 40,
                                                  hold_until_finish=True, **kw)
        out["v"] = v
        if task is not None:
            task.cancel()
    asyncio.run(go())
    return out["v"]


# ------------------------------------------- classificazione hold (peek)
def test_length_senza_answer_non_e_mai_content(M):
    v, _b, _p, meta = _run(M, [LENGTH])
    assert v == "length_truncated"
    assert meta.get("no_rotate") is False


def test_stop_senza_answer_ruota_senza_punire(M):
    v, _b, _p, meta = _run(M, [STOP])
    assert v == "empty_eof"
    assert meta.get("empty_clean") is True
    assert meta.get("no_rotate") is False


def test_done_senza_answer_e_pulito_vuoto(M):
    v, _b, _p, meta = _run(M, [DONE])
    assert v == "empty_eof" and meta.get("empty_clean") is True


def test_reasoning_only_senza_include_e_vuoto_ruotabile(M):
    v, _b, _p, meta = _run(M, [REASON, STOP])
    assert v == "empty_eof" and meta.get("empty_clean") is True


def test_reasoning_only_con_include_e_contenuto(M):
    v, _b, _p, meta = _run(M, [REASON, STOP], incl=True)
    assert v == "content"


def test_muto_in_hold_rispetta_il_deadline_primo_byte(M):
    # hold non significa aspettare 120s un upstream morto: prima del primo
    # byte vale il deadline primo-contenuto -> timeout (ruotabile/paracadute).
    assert _run_silent(M, fc=60, hold_idle_ms=60000) == "timeout"


def test_troncamento_spurio_senza_terminatore_resto_truncated(M):
    v, _b, _p, _m = _run(M, [CONTENT])
    assert v == "truncated"


def test_clean_stop_con_answer_ancora_content(M):
    v, _b, _p, _m = _run(M, [CONTENT, STOP])
    assert v == "content"


# ------------------------------------------------ Router: ordine "capable"
CSV_CAP = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5
t@x.com,m/mid,groq,https://api.groq.com/openai/v1,free,200,200000,5,K-M,,4
t@x.com,m/big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B,,10
t@x.com,m/g1,groq-go,https://api.groq.com/openai/v1,,0,0,5,K-GO,,
t@x.com,m/f1,groq,https://api.groq.com/openai/v1,paid,0,0,5,K-FB,,
"""
BASE = "scrocco-llm-test"


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


def test_capable_first_sopra_la_corrente_per_intelligence(router):
    lad = router._ladder_for_group(f"{BASE}-32k")
    small = _dep(router, f"{BASE}-32k", "K-S")
    ordered = router._capable_first(lad, small)
    groups = [router.config.deployment_by_unique(u)["group"] for u in ordered]
    # la camminata parte DOPO il corrente: tra i successivi vince la finestra
    # maggiore, poi l'intelligence (big 1000k/intel10 prima di mid 200k/intel4).
    assert groups[0] == f"{BASE}-32k"               # prefisso invariato
    assert groups[1:3] == [f"{BASE}-1000k", f"{BASE}-200k"]
    assert groups[-2:] == [f"{BASE}-go", f"{BASE}-fallback"]


def test_fallback_next_prefer_capable_salta_a_1000k(router):
    small = _dep(router, f"{BASE}-32k", "K-S")
    plain = router.fallback_next("test", small, scope="chain", ctx=100,
                                 tried={small["unique"]})
    cap = router.fallback_next("test", small, scope="chain", ctx=100,
                               tried={small["unique"]}, prefer_capable=True)
    assert plain["group"] == f"{BASE}-200k"        # salita normale di dim
    assert cap["group"] == f"{BASE}-1000k"         # il piu' capace prima


# ------------------------------------------- normalizzazione dim >1000k
CSV_DIM = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps
t@x.com,m.a,groq,https://api.groq.com/openai/v1,free,1049,,5,K1,
t@x.com,m.b,groq,https://api.groq.com/openai/v1,free,1049,1049000,5,K2,
"""


def test_dim_sopra_1000_normalizzata_a_1000k():
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(CSV_DIM)
    cfg = GatewayConfig(path, proxy_prefix="scrocco-llm-", seed=1)
    try:
        assert f"{BASE}-1049k" not in cfg.groups
        deps = cfg.groups[f"{BASE}-1000k"]
        assert len(deps) == 2
        assert all(int(d["max_input_tokens"]) == 1_000_000 for d in deps)
        assert sorted(cfg.profile_dims["test"]) == [1000]
    finally:
        os.unlink(path)


# ------------------------------- loop: rotazione senza penale + preferenza
CSV_LOOP = """commento,modello,provider,endpoint,data,context,max_input,priority,scrocco-llm-test,caps,intelligence_score
t@x.com,m/small,groq,https://api.groq.com/openai/v1,free,32,32000,5,K-S,,5
t@x.com,m/big,groq,https://api.groq.com/openai/v1,free,1000,1000000,5,K-B,,9
"""


@pytest.fixture()
def ML(tmp_path, monkeypatch):
    import app.main as _M
    csv = tmp_path / "k.csv"
    csv.write_text(CSV_LOOP)
    orig_csv = _M.config.csv_path
    qj = _M.router.policy.qc_json
    snap = (qj.stream_hedge_delay_ms, qj.stream_first_content_ms,
            qj.stream_hold_until_finish)
    cooled = set(_M.router._cooldown)
    _M.config.csv_path = csv
    _M.config.reload()
    qj.stream_hedge_delay_ms = 0
    qj.stream_first_content_ms = 5000
    yield _M
    qj.stream_hedge_delay_ms, qj.stream_first_content_ms, \
        qj.stream_hold_until_finish = snap
    _M.config.csv_path = orig_csv
    _M.config.reload()
    for k in list(_M.router._cooldown):
        if k not in cooled:
            _M.router._cooldown.pop(k, None)


def _fake_stream(M, monkeypatch, chunks):
    calls = []

    async def stream_response(dep, payload, **kwargs):
        calls.append(dep["unique"])

        async def gen():
            for c in chunks:
                yield c
        return gen()
    monkeypatch.setattr(M.forwarder, "stream_response", stream_response)
    return calls


async def _drain(resp):
    out = b""
    if hasattr(resp, "body_iterator"):
        async for c in resp.body_iterator:
            out += c
    return out


def test_length_vuoto_ruota_senza_cooldown_e_cerca_il_piu_capace(ML, monkeypatch):
    """Incidente in produzione a valle del fix clamp: se IL MODELLO si trunca da
    solo (length a 0 answer) il gateway ruota pre-byte sul piu' capace senza
    consegnare il moncone e senza mettere in cooldown il free-troncatore."""
    calls = _fake_stream(ML, monkeypatch, [LENGTH])
    seen = {}
    orig = ML.router.fallback_next

    def wrap(*a, **k):
        seen.setdefault("flags", []).append(k.get("prefer_capable"))
        return orig(*a, **k)
    monkeypatch.setattr(ML.router, "fallback_next", wrap)

    small = ML.config.groups[f"{BASE}-32k"][0]
    big = ML.config.groups[f"{BASE}-1000k"][0]

    async def go():
        payload = {"model": small["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await ML._stream_with_fallback("test", small, payload,
                                              scope="chain")
    resp = asyncio.run(go())
    asyncio.run(_drain(resp))
    assert isinstance(resp, JSONResponse) and resp.status_code == 503
    assert calls == [small["unique"], big["unique"]]   # tentato il piu' capace
    assert seen["flags"] and all(seen["flags"])        # richiesta capace
    # NESSUNA penale: il troncamento da budget non e' colpa del deployment
    assert small["unique"] not in ML.router._cooldown
    assert big["unique"] not in ML.router._cooldown


def test_clean_stop_vuoto_ruota_senza_cooldown(ML, monkeypatch):
    calls = _fake_stream(ML, monkeypatch, [STOP])
    small = ML.config.groups[f"{BASE}-32k"][0]

    async def go():
        payload = {"model": small["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await ML._stream_with_fallback("test", small, payload,
                                              scope="chain")
    resp = asyncio.run(go())
    asyncio.run(_drain(resp))
    assert isinstance(resp, JSONResponse) and resp.status_code == 503
    assert calls == [small["unique"],
                     ML.config.groups[f"{BASE}-1000k"][0]["unique"]]
    assert small["unique"] not in ML.router._cooldown


def test_contenuto_completa_passa_normalmente(ML, monkeypatch):
    calls = _fake_stream(ML, monkeypatch, [CONTENT, STOP, DONE])
    small = ML.config.groups[f"{BASE}-32k"][0]

    async def go():
        payload = {"model": small["model"],
                   "messages": [{"role": "user", "content": "ciao"}]}
        return await ML._stream_with_fallback("test", small, payload,
                                              scope="chain")
    resp = asyncio.run(go())
    out = asyncio.run(_drain(resp))
    assert b"ciao mondo" in out
    assert len(calls) == 1


# ---------------------------------------- hedge hold-aware (main._hedge_peek)
DEP_A = lambda: {"unique": "A__m1__0", "group": "scrocco-t-32k", "model": "m1"}
DEP_B = {"unique": "B__m2__1", "group": "scrocco-t-32k", "model": "m2"}


def _fake_router(B=None):
    from types import SimpleNamespace
    notes = {"start": [], "fail": [], "canary": 0, "end": [], "warm": []}
    r = SimpleNamespace(
        config=SimpleNamespace(go_suffix="-go", fallback_suffix="-fallback"),
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
        hedge_canaries=(lambda *a, **k: (notes.__setitem__(
            "canary", notes["canary"] + 1), [B] if B else [])[1]),
        _sess_deps=lambda: {},
        note_warm_owner=lambda sid, u: notes["warm"].append(u),
    )
    return r, notes


async def _join_probes(M):
    while M._PROBE_TASKS:
        await asyncio.gather(*list(M._PROBE_TASKS), return_exceptions=True)


def _drive_hedge(M, monkeypatch, genA, stream_response, router, *,
                 hold=True, hedge_ms=30, fc=5000):
    import time as _t

    async def go():
        out = await M._hedge_peek(
            DEP_A(), genA, _t.monotonic(), fc, False, 40, 60000, 2048,
            payload={}, profile=None, need=frozenset(), scope="chain",
            ctx=1, tried_set=set(), attempts=[], requested_group=None,
            session=None, client_ip="", attribution=None,
            hedge_ms=hedge_ms, _tr_cfg=None,
            _tct_cfg=None, hold=hold)
        await _join_probes(M)
        return out
    monkeypatch.setattr(M, "router", router)
    monkeypatch.setattr(M, "forwarder",
                        __import__("types").SimpleNamespace(
                            stream_response=stream_response))
    monkeypatch.setattr(M, "inject_identity", lambda p, d, router=None: None)
    return asyncio.run(go())


async def _genA_slow_then_finish():
    yield CONTENT
    await asyncio.sleep(0.15)
    yield STOP


async def _genA_mute():
    await asyncio.sleep(0.35)
    raise ConnectionError("monta-gna: upstream muto e poi crepato")
    yield  # pragma: no cover (serve a renderlo un async generator)


async def _streamB_ok(dep, payload, **kwargs):
    async def gen():
        yield CONTENT
        yield STOP
    return gen()


def _streamB_forbidden(dep, payload, **kwargs):
    raise AssertionError("nessun canary: A sta gia' streammando")


def test_hedge_hold_non_gareggia_su_A_che_fluisce(M, monkeypatch):
    """Hold: A emette byte ma non ha ancora chiuso -> NON e' cold-start muto:
    nessun canary, si aspetta la sua chiusura pulita (verdetto content)."""
    r, notes = _fake_router(B=DEP_B)
    out = _drive_hedge(M, monkeypatch, _genA_slow_then_finish(),
                       _streamB_forbidden, r)
    dep, gen, t_att, verdict, prebuf, pending, meta = out
    assert verdict == "content" and dep["unique"] == "A__m1__0"
    assert notes["canary"] == 0


def test_hedge_hold_gareggia_su_A_muto_e_vince_canary(M, monkeypatch):
    """Hold: A muto oltre il ritardo hedge -> gara; il canary che chiude
    pulito vince e consegna. A NON viene cancellata: finisce come probe
    reale e, essendoci davvero morta, prende il cooldown solito."""
    r, notes = _fake_router(B=DEP_B)
    out = _drive_hedge(M, monkeypatch, _genA_mute(),
                       _streamB_ok, r, hedge_ms=30, fc=300)
    dep, gen, t_att, verdict, prebuf, pending, meta = out
    assert verdict == "content" and dep["unique"] == "B__m2__1"
    assert notes["canary"] >= 1
    assert notes["fail"] == ["A__m1__0"]      # probe: timeout -> cooldown
