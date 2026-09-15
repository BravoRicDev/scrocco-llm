"""F3 — hedge del primo contenuto (main._hedge_peek): gara A/B SOLO pre-
commit, SOLO catena fredda, mai verso bucket pagati; il perdente e' annullato
senza punizione. Tutto monkeypatchato: niente rete, niente router vero.
Convenzione del repo: test sincroni che guidano coroutine con asyncio.run."""
import asyncio
import time
from types import SimpleNamespace

import pytest

import app.main as main

CONTENT = ("content", [b"d"], None, {})
TIMEOUT = ("timeout", [], None, {})
ERROR = ("error", [], None, {})


class FakeGen:
    """Solo aclose tracciata: un async-gen MAI avviato non esegue il finally
    alla aclose(), e il test deve vedere lo scarto del perdente."""
    def __init__(self, name, closed):
        self.name, self.closed = name, closed
        self.disposed = False

    async def aclose(self):
        self.disposed = True
        self.closed.append(self.name)


DEP_A = lambda: {"unique": "A__m1__0", "group": "scrocco-t-64k", "model": "m1"}
DEP_B = lambda: {"unique": "B__m2__1", "group": "scrocco-t-64k", "model": "m2"}


def _fake_router(B=None):
    notes = {"start": [], "end": [], "fail": [], "cool": []}
    r = SimpleNamespace(
        config=SimpleNamespace(go_suffix="-go", fallback_suffix="-fallback"),
        note_start=lambda u, ctx=None: notes["start"].append(u),
        note_end=lambda u, ctx=None: notes["end"].append(u),
        is_cooled_down=lambda u: False,
        clear_cooldown=lambda u: notes["cool"].append(u),
        first_content_deadline_ms=lambda u, ctx=None: 1000,
        mark_failed=lambda u, **kw: notes["fail"].append(u),
        note_rate_limit=lambda u, rl: None,
        # nuovo picker dei canary (tier crescenti) + warm ownership
        hedge_canaries=(lambda *a, **k: ([B] if B else [])),
        _sess_deps=lambda: {},
        note_warm_owner=lambda sid, u: None,
    )
    return r, notes


def _run_peek(peek, stream_response, router, *, closed_tag="A",
              hedge_ms=60):
    """Patcha i nomi di modulo attorno a _hedge_peek e lo esegue."""
    closed = []
    genA = FakeGen(closed_tag, closed)

    async def go():
        return await main._hedge_peek(
            DEP_A(), genA, time.monotonic(), 500, False, 40, 1000, 2048,
            payload={}, profile=None, need=frozenset(), scope="chain",
            ctx=1, tried_set=set(), attempts=[], requested_group=None,
            session=None, client_ip="", attribution=None,
            hedge_ms=hedge_ms, _tr_cfg=None,
            _tct_cfg=SimpleNamespace(cooldown_sec=1))
    old = (main._peek_stream, main.router, main.forwarder,
           main.inject_identity)
    main._peek_stream = peek
    main.router = router
    main.forwarder = SimpleNamespace(stream_response=stream_response)
    main.inject_identity = lambda p, d, router=None: None
    try:
        out = asyncio.run(go())
    finally:
        (main._peek_stream, main.router, main.forwarder,
         main.inject_identity) = old
    return out, closed


def _no_stream(*a, **k):
    raise AssertionError("stream_response non deve essere chiamato")


def test_a_rapido_nessun_canary():
    r, notes = _fake_router()

    async def peek(g, fcm, incl, mc, **kw):
        await asyncio.sleep(0.005)
        return CONTENT

    out, closed = _run_peek(peek, _no_stream, r)
    assert out[0]["unique"] == "A__m1__0"
    assert out[3] == "content"
    assert notes["start"] == []


def test_a_lento_b_vince_gara():
    B = DEP_B()
    r, notes = _fake_router(B=B)
    created = []

    async def sr(dep, payload, **kw):
        created.append(dep["unique"])
        return FakeGen("B", [])

    calls = {"n": 0}

    async def peek(g, fcm, incl, mc, **kw):
        calls["n"] += 1
        n = calls["n"]
        await asyncio.sleep(0.25 if n == 1 else 0.01)
        return TIMEOUT if n == 1 else CONTENT

    out, closed = _run_peek(peek, sr, r)
    assert out[0]["unique"] == "B__m2__1"
    assert out[3] == "content"
    assert created == ["B__m2__1"]
    assert "A" in closed                       # genA scartato, non punito
    assert notes["start"] == ["B__m2__1"]
    # A e' annullato e la sua contabilita' CHIUSA (niente inflight leak);
    # B resta aperto: impegna lui la risposta.
    assert notes["end"] == ["A__m1__0"]
    assert notes["fail"] == []


def test_bucket_pagato_mai_in_gara():
    """Il filtro dei bucket pagati vive in Router.hedge_canaries: qui si
    verifica che se il picker non offre nulla (come per il pagato) NON parte
    alcun canary. La copertura del filtro e' nei test del router."""
    B = None
    r, notes = _fake_router(B=B)

    async def peek(g, fcm, incl, mc, **kw):
        await asyncio.sleep(0.1)
        return TIMEOUT

    out, closed = _run_peek(peek, _no_stream, r)
    assert out[0]["unique"] == "A__m1__0"
    assert out[3] == "timeout"
    assert notes["start"] == []


def test_canary_impossibile_attende_A():
    B = DEP_B()
    r, notes = _fake_router(B=B)

    async def sr(*a, **k):
        raise RuntimeError("upstream refuse")

    async def peek(g, fcm, incl, mc, **kw):
        await asyncio.sleep(0.1)
        return TIMEOUT

    out, closed = _run_peek(peek, sr, r)
    assert out[0]["unique"] == "A__m1__0" and out[3] == "timeout"
    assert notes["start"] == ["B__m2__1"]
    assert notes["end"] == ["B__m2__1"]        # contabilita' chiusa


def test_nessun_contenuto_verdetto_di_A():
    """B produce solo 'error': vince comunque il verdetto di A (rotazione
    normale), B e' annullato e la sua contabilita' chiusa, nessuno punito."""
    B = DEP_B()
    r, notes = _fake_router(B=B)

    async def sr(dep, payload, **kw):
        return FakeGen("B", [])

    calls = {"n": 0}

    async def peek(g, fcm, incl, mc, **kw):
        calls["n"] += 1
        n = calls["n"]
        await asyncio.sleep(0.05 if n == 2 else 0.15)
        return TIMEOUT if n == 1 else ERROR

    out, closed = _run_peek(peek, sr, r)
    assert out[0]["unique"] == "A__m1__0"
    assert out[3] == "timeout"
    assert notes["end"] == ["B__m2__1"]
    assert notes["fail"] == []
