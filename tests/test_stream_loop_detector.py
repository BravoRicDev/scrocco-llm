"""Streaming loop detector con kill precoce.

Il loop detector NON deve aspettare l'intera risposta: lavora on-the-fly su
un buffer circolare delle ultime parole di contenuto estratte dai chunk SSE.
Un modello in loop degenere viene killato subito (StreamLoopDetected), il
chiamante ruota come per un normale errore upstream."""
import asyncio
import json

import pytest

from app.forwarder import StreamLoopDetected, _stream_loop_guard
from app.sampling import LoopConfig, stream_loop_reason


def _ssel(content: str) -> bytes:
    obj = {"choices": [{"delta": {"content": content}}]}
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def test_stream_loop_reason_detects_repeat():
    lc = LoopConfig(ngram_size=2, repeats=3, min_tokens=4)
    assert stream_loop_reason(["x"] * 7, lc) == "repeated_ngram"


def test_stream_loop_reason_noop_short_or_clean():
    lc = LoopConfig(ngram_size=2, repeats=3, min_tokens=4)
    assert stream_loop_reason(["one", "two", "three"], lc) is None
    assert stream_loop_reason(
        ["one", "two", "three", "four", "five", "six", "seven"], lc) is None


def test_guard_kills_repeated_content():
    async def src():
        yield _ssel("x x x x x x x")
        yield b'data: {"choices":[{"delta":{"content":"still loop x x"}}]}\n\n'

    lc = LoopConfig(ngram_size=2, repeats=3, min_tokens=4)

    async def _run():
        gen = _stream_loop_guard(src(), lc, 200, "u1", "m1")
        out = []
        with pytest.raises(StreamLoopDetected) as ei:
            async for chunk in gen:
                out.append(chunk)
        assert "loop" in str(ei.value.detail).lower()
        # nessun chunk e' passato (il kill e' avvenuto PRIMA del yield)
        assert out == []

    asyncio.run(_run())


def test_guard_passes_clean_stream():
    words = " ".join(f"w{i}" for i in range(50))

    async def src():
        for i in range(0, 50, 10):
            yield _ssel(" ".join(words.split()[i:i + 10]))

    lc = LoopConfig(ngram_size=3, repeats=4, min_tokens=16)

    async def _run():
        gen = _stream_loop_guard(src(), lc, 200, "u1", "m1")
        out = [chunk async for chunk in gen]
        assert len(out) == 5

    asyncio.run(_run())


def test_guard_split_across_chunks_detected():
    """Il loop scatta anche quando le parole arrivano frammentate in piu'
    chunk (contenuto ripartito sui chunk SSE, come nei provider reali)."""
    async def src():
        yield _ssel("x x")
        yield _ssel("x x")
        yield _ssel("x x")
        yield _ssel("x x")          # cumulativo: >= 2*3=6 parole ripetute

    lc = LoopConfig(ngram_size=2, repeats=3, min_tokens=6)

    async def _run():
        gen = _stream_loop_guard(src(), lc, 200, "u1", "m1")
        got = 0
        with pytest.raises(StreamLoopDetected):
            async for _ in gen:
                got += 1
        assert got >= 1             # i primi chunk passano, poi il kill

    asyncio.run(_run())