"""GARA LENTA (slow-race) + ELEZIONE per TEMPO DI TENTATIVO.

Regole utente:
- se il primo tentativo (A) sta ancora generando dopo N ms, parte UN canario
  "senza buttare via la risposta": vince chi consegna per primo al client;
- l'ELEZIONE (chi diventa holder per la richiesta successiva) va pero' a chi
  ha impiegato MENO tempo nel proprio tentativo (anche se ha consegnato
  dopo), e il piu' lento viene marcato "lento per la sessione";
- qualsiasi canary che consegna qualcosa finisce in warm (anche troncato).

Convenzione del repo: test sincroni che guidano coroutine con asyncio.run.
NB: `_spawn_probe` e `_hedge_peek` usano il `router` GLOBALE di app.main.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

import app.main as main
from app import forwarder as fwd_mod

CONTENT = ("content", [b"d"], None, {})
TIMEOUT = ("timeout", [], None, {})
ERROR = ("error", [], None, {})
LENGTH_TRUNC = ("length_truncated", [b"d"], None, {})

WINNER = "A__m1__0"
SESSION = "s1"


class FakeGen:
    def __init__(self, name, chunks=None):
        self.name = name
        self.chunks = list(chunks or [])
        self.disposed = False
        self.closed = []

    async def aclose(self):
        self.disposed = True
        self.closed.append(self.name)

    def __aiter__(self):
        async def _it():
            for c in self.chunks:
                yield c
        return _it()


def DEP_A():
    return {"unique": WINNER, "group": "scrocco-t-64k", "model": "m1"}


def DEP_B():
    return {"unique": "B__m2__1", "group": "scrocco-t-64k", "model": "m2"}


def _fake_router(B=None, holder_u=WINNER):
    """Router finto con cio' che serve a _hedge_peek/_spawn_probe."""
    notes = {"start": [], "end": [], "fail": [], "cool": [], "warm": [],
             "success": [], "slow": [], "probe": []}
    r = SimpleNamespace(
        config=SimpleNamespace(go_suffix="-go", fallback_suffix="-fallback"),
        policy=SimpleNamespace(
            qc_json=SimpleNamespace(watchdog_cooldown_sec=90,
                                    stream_total_deadline_ms=180000),
            stream_slow_race_after_ms=120000,
            nonstream_slow_race_after_ms=120000,
            stream_slow_race_canaries=1,
            warm_refill_max_inflight=6),
        stats_for=lambda u: SimpleNamespace(fail_count_24h=0),
        escalate_cooldown=lambda base, f: base,
        note_start=lambda u, ctx=None: notes["start"].append(u),
        note_end=lambda u, ctx=None: notes["end"].append(u),
        note_result=lambda *a, **k: None,
        is_cooled_down=lambda u: False,
        clear_cooldown=lambda u: notes["cool"].append(u),
        first_content_deadline_ms=lambda u, ctx=None: 1000,
        mark_failed=lambda u, **kw: notes["fail"].append(u),
        note_rate_limit=lambda u, rl: None,
        note_warm_owner=lambda sid, u: notes["warm"].append(u),
        note_probe_started=lambda sid, u: notes["probe"].append(("start", u)),
        note_probe_done=lambda sid, u: notes["probe"].append(("done", u)),
        probes_in_flight=lambda sid: 0,
        degraded_active=lambda: False,
        hedge_canaries=(lambda *a, **k: ([B] if B else [])),
        warm_fill_canary=(lambda *a, **k: B),
        warm_wake_canary=(lambda *a, **k: None),
        warm_api_keys=(lambda *a, **k: set()),
        _sess_deps=lambda: {},
        _cache_ok=(lambda: ({SESSION: (holder_u, 0.0)} if holder_u else {})),
        note_session_success=lambda sid, u, **kw: notes["success"].append((u, kw)),
        _note_session_slow=lambda sid, u, **kw: notes["slow"].append((u, kw)),
        mark_session_slow=lambda sid, u: notes["slow"].append((u, {"hard": True})),
        slow_race_allowed=lambda *a, **k: True,
    )
    return r, notes


async def _join_probes():
    while main._PROBE_TASKS:
        await asyncio.gather(*list(main._PROBE_TASKS), return_exceptions=True)


async def _join_ns():
    while getattr(fwd_mod, "_NS_PROBES", None):
        await asyncio.gather(*list(fwd_mod._NS_PROBES), return_exceptions=True)


def _no_stream(*a, **k):
    raise AssertionError("stream_response non deve essere chiamato")


def _run_probe(dep, gen, res, router, *, t0=None, race=None, ctx=100):
    old = main.router
    main.router = router

    async def go():
        main._spawn_probe(dep, gen, None, res, SESSION, ctx, t0=t0, race=race)
        await _join_probes()
    try:
        asyncio.run(go())
    finally:
        main.router = old


def _run_peek(peek, stream_response, router, *, slow_race_ms=0,
              slow_canary_ms=0, hedge_ms=60,
              t_att_offset=0.0, session=SESSION, hold=False, refill=False):
    closed = []
    genA = FakeGen(WINNER)
    old = (main._peek_stream, main.router, main.forwarder, main.inject_identity)

    async def go():
        out = await main._hedge_peek(
            DEP_A(), genA, time.monotonic() - t_att_offset, 500, False, 40,
            1000, 2048, payload={}, profile=None, need=frozenset(),
            scope="chain", ctx=1, tried_set=set(), attempts=[],
            requested_group=None, session=session, client_ip="",
            attribution=None, hedge_ms=hedge_ms, _tr_cfg=None,
            _tct_cfg=SimpleNamespace(cooldown_sec=1), slow_race_ms=slow_race_ms,
            slow_canary_ms=slow_canary_ms,
            hold=hold, refill=refill)
        await _join_probes()
        return out, closed

    main._peek_stream = peek
    main.router = router
    main.forwarder = SimpleNamespace(stream_response=stream_response)
    main.inject_identity = lambda p, d, router=None: None
    try:
        return asyncio.run(go())
    finally:
        main._peek_stream, main.router, main.forwarder, main.inject_identity = old


# ------------------------------------------------------------------ trigger
def test_slow_race_apre_il_canario_e_consegna_il_primo():
    """A lento oltre la soglia -> parte il canario; vince chi consegna prima."""
    B = DEP_B()
    r, notes = _fake_router(B=B)
    opened = []

    async def sr(dep, payload, **kw):
        opened.append(dep["unique"])
        return FakeGen(dep["unique"])

    async def peek(gen, fcm, incl_reason=None, min_ch=None, **kw):
        if gen.name == B["unique"]:
            await asyncio.sleep(0.02)
        else:
            await asyncio.sleep(0.4)          # A e' ancora in generazione
        return CONTENT

    out, _closed = _run_peek(peek, sr, r, slow_race_ms=100)
    assert opened == [B["unique"]], "il canario deve partire una volta sola"
    assert out[0]["unique"] == B["unique"]
    assert out[3] == "content"


def test_slow_race_a_veloce_non_apre_nulla():
    r, notes = _fake_router(B=DEP_B())

    async def peek(gen, fcm, incl_reason=None, min_ch=None, **kw):
        await asyncio.sleep(0.01)
        return CONTENT

    out, _ = _run_peek(peek, _no_stream, r, slow_race_ms=100)
    assert out[0]["unique"] == WINNER
    assert notes["start"] == [], "nessun canario se A consegna subito"


def test_slow_race_off_nessun_canario_extra():
    """slow_race_ms=0 -> nessun canario per lentezza (comportamento di oggi)."""
    r, notes = _fake_router(B=None)

    async def peek(gen, fcm, incl_reason=None, min_ch=None, **kw):
        await asyncio.sleep(0.05)
        return CONTENT

    out, _ = _run_peek(peek, _no_stream, r, slow_race_ms=0)
    assert out[0]["unique"] == WINNER
    assert notes["start"] == []


def test_slow_race_rispetta_il_tetto_in_volo():
    """Se non c'e' un canario apribile, A resta l'unico (nessun crash)."""
    r, notes = _fake_router(B=None)

    async def peek(gen, fcm, incl_reason=None, min_ch=None, **kw):
        await asyncio.sleep(0.001)
        return CONTENT

    out, _ = _run_peek(peek, _no_stream, r, slow_race_ms=100)
    assert out[0]["unique"] == WINNER


# ---------------------------------------------------- slow-race + hedge (stream)
def test_slow_race_scatta_con_a_gia_in_streaming():
    """Con HOLD: A ha gia' emesso byte ma non chiude -> al timer parte il
    canario lento e vince. L'hedge classico NON apre nulla (A sta streammando).
    Nessuna penalita' per il lento."""
    B = DEP_B()
    r, notes = _fake_router(B=None)
    r.hedge_canaries = lambda *a, **k: [B]
    opened = []

    async def sr(dep, payload, **kw):
        opened.append(dep["unique"])
        return FakeGen(dep["unique"])

    async def peek(gen, fcm, incl_reason=None, min_ch=None, **kw):
        fb = kw.get("first_byte")
        if fb is not None:
            fb.set()                      # il primo byte di A e' arrivato
        if gen.name == B["unique"]:
            await asyncio.sleep(0.02)
        else:
            await asyncio.sleep(0.6)      # A streamma lento, non chiude
        return CONTENT

    out, _closed = _run_peek(peek, sr, r, slow_race_ms=100, hedge_ms=50,
                             hold=True)
    assert opened == [B["unique"]], "un solo canario lento"
    assert out[0]["unique"] == B["unique"] and out[3] == "content"
    assert notes["fail"] == [], "nessuna cooldown per il lento"


def test_slow_canary_anticipa_il_flag():
    """`slow_canary_ms` << `slow_race_ms`: il canario lento parte al PROPRIO
    tempo, senza marcare il dep "lento per la sessione" (flag a 45s)."""
    B = DEP_B()
    r, notes = _fake_router(B=None)
    r.hedge_canaries = lambda *a, **k: [B]
    opened = []

    async def sr(dep, payload, **kw):
        opened.append(dep["unique"])
        return FakeGen(dep["unique"])

    async def peek(gen, fcm, incl_reason=None, min_ch=None, **kw):
        fb = kw.get("first_byte")
        if fb is not None:
            fb.set()
        if gen.name == B["unique"]:
            await asyncio.sleep(0.02)
        else:
            await asyncio.sleep(0.6)
        return CONTENT

    out, _ = _run_peek(peek, sr, r, slow_race_ms=100000,
                       slow_canary_ms=100, hedge_ms=50, hold=True)
    assert opened == [B["unique"]], "canario aperto al suo timing"
    assert out[0]["unique"] == B["unique"] and out[3] == "content"
    assert all(kw.get("hard") is not True for _u, kw in notes["slow"]), \
        "flag lento NON scatta: soglia del flag ancora lontana"


def test_slow_flag_scatta_col_proprio_timer():
    """`slow_canary_ms` >> `slow_race_ms`: alla soglia del flag il dep e'
    marcato lento, ma il canario NON parte (timer proprio ancora lontano)."""
    r, notes = _fake_router(B=None)
    r.hedge_canaries = lambda *a, **k: [DEP_B()]

    async def peek(gen, fcm, incl_reason=None, min_ch=None, **kw):
        fb = kw.get("first_byte")
        if fb is not None:
            fb.set()
        await asyncio.sleep(0.5)
        return CONTENT

    out, _ = _run_peek(peek, _no_stream, r, slow_race_ms=100,
                       slow_canary_ms=100000, hedge_ms=50, hold=True)
    assert out[0]["unique"] == WINNER
    assert (WINNER, {"hard": True}) in notes["slow"], \
        "flag lento scattato al proprio timer"


def test_slow_race_vale_anche_in_refill():
    """In refill la cascata non trova canari -> il timer lento ne apre uno
    comunque (fuori dal tetto per-sessione)."""
    B = DEP_B()
    r, notes = _fake_router(B=None)          # warm_fill_canary -> None
    r.hedge_canaries = lambda *a, **k: [B]   # il picker lento trova B
    opened = []

    async def sr(dep, payload, **kw):
        opened.append(dep["unique"])
        return FakeGen(dep["unique"])

    async def peek(gen, fcm, incl_reason=None, min_ch=None, **kw):
        if gen.name == B["unique"]:
            await asyncio.sleep(0.02)
        else:
            await asyncio.sleep(0.6)
        return CONTENT

    out, _ = _run_peek(peek, sr, r, slow_race_ms=100, hedge_ms=50, refill=True)
    assert opened == [B["unique"]]
    assert out[0]["unique"] == B["unique"]


def test_slow_race_off_a_streaming_si_aspetta():
    """slow_race_ms=0 e A in streaming: si aspetta A (nessun canario)."""
    r, notes = _fake_router(B=DEP_B())

    async def peek(gen, fcm, incl_reason=None, min_ch=None, **kw):
        fb = kw.get("first_byte")
        if fb is not None:
            fb.set()
        await asyncio.sleep(0.05)
        return CONTENT

    out, _ = _run_peek(peek, _no_stream, r, slow_race_ms=0, hedge_ms=50,
                       hold=True)
    assert out[0]["unique"] == WINNER
    assert notes["start"] == []


# ----------------------------------------------------------------- elezione
def test_elezione_probe_piu_veloce_diventa_holder():
    """Probe piu' veloce del vincitore -> diventa holder, il vincitore slow."""
    r, notes = _fake_router(holder_u=WINNER)
    gen = FakeGen("B__m2__1", [b'data: {"choices":[{"delta":{"content":"x"}}]}'])
    _run_probe(DEP_B(), gen, CONTENT, r,
               t0=time.monotonic() - 1.2,        # probe: 1.2s
               race=(WINNER, 1800.0))            # vincitore: 1.8s
    assert notes["warm"] == ["B__m2__1"]
    assert [u for u, _ in notes["success"]] == ["B__m2__1"]
    assert notes["success"][0][1]["latency_ms"] == pytest.approx(1200.0, abs=80)
    assert notes["slow"] and notes["slow"][0][0] == WINNER
    assert notes["slow"][0][1]["latency_ms"] == pytest.approx(1800.0)


def test_elezione_probe_piu_lento_non_promuove():
    r, notes = _fake_router(holder_u=WINNER)
    gen = FakeGen("B__m2__1", [b'data: {"choices":[{"delta":{"content":"x"}}]}'])
    _run_probe(DEP_B(), gen, CONTENT, r,
               t0=time.monotonic() - 2.5,        # probe: 2.5s
               race=(WINNER, 1000.0))            # vincitore: 1.0s
    assert notes["warm"] == ["B__m2__1"]
    assert notes["success"] == [], "il piu' lento non diventa holder"
    assert notes["slow"] and notes["slow"][0][0] == "B__m2__1"


def test_elezione_non_calpesta_un_holder_piu_recente():
    r, notes = _fake_router(holder_u="ALTRO__x__9")
    gen = FakeGen("B__m2__1", [b'data: {"choices":[{"delta":{"content":"x"}}]}'])
    _run_probe(DEP_B(), gen, CONTENT, r,
               t0=time.monotonic() - 1.0, race=(WINNER, 1800.0))
    assert notes["success"] == []
    assert notes["slow"] == []


def test_probe_troncato_finisce_comunque_in_warm():
    """Regola utente: QUALSIASI canary che consegna qualcosa va in warm."""
    r, notes = _fake_router()
    gen = FakeGen("B__m2__1", [
        b'data: {"choices":[{"delta":{"content":"parziale"}}]}',
        b'data: {"choices":[{"finish_reason":"length","delta":{}}]}',
    ])
    _run_probe(DEP_B(), gen, LENGTH_TRUNC, r)
    assert notes["warm"] == ["B__m2__1"], "troncato ma consegnato -> warm"
    assert notes["fail"] == [], "nessuna penale per un troncamento"


# -------------------------------------------------------------- non-stream
def _run_ns(router, *, t0=None, race=None, content="ok", fr="stop"):
    async def go():
        fut = asyncio.Future()
        fut.set_result({"choices": [{"message": {"content": content},
                                     "finish_reason": fr}]})
        fwd_mod._spawn_ns_probe(router, DEP_B(), fut,
                                t0 if t0 is not None else time.monotonic(),
                                100, SESSION, race=race)
        await _join_ns()
    asyncio.run(go())


def test_elezione_nonstream_probe_piu_veloce():
    r, notes = _fake_router(holder_u=WINNER)
    _run_ns(r, t0=time.monotonic() - 0.8, race=(WINNER, 1500.0))
    assert notes["warm"] == ["B__m2__1"]
    assert [u for u, _ in notes["success"]] == ["B__m2__1"]
    assert notes["slow"] and notes["slow"][0][0] == WINNER


def test_elezione_nonstream_probe_piu_lento():
    r, notes = _fake_router(holder_u=WINNER)
    _run_ns(r, t0=time.monotonic() - 3.0, race=(WINNER, 900.0))
    assert notes["success"] == []
    assert notes["slow"] and notes["slow"][0][0] == "B__m2__1"


def test_nonstream_troncato_finisce_comunque_in_warm():
    r, notes = _fake_router()
    _run_ns(r, content="parziale", fr="length")
    assert notes["warm"] == ["B__m2__1"]
