"""Lavoro CPU-bound fuori dall'event loop (app/offload.py)."""
import asyncio
import base64
import threading

import pytest

from app import offload


def test_small_inline_big_on_thread_same_result():
    loop_thread = []

    def work(x):
        return (threading.get_ident(), base64.b64encode(x).decode())

    async def main():
        loop_thread.append(threading.get_ident())
        small = await offload.run(work, b"abc", size=3)
        big_data = b"x" * (offload.OFFLOAD_MIN_BYTES + 1)
        big = await offload.run(work, big_data, size=len(big_data))
        return small, big, big_data

    small, big, big_data = asyncio.run(main())
    assert small[0] == loop_thread[0] and small[1] == "YWJj"
    assert big[0] != loop_thread[0] and big[1] == base64.b64encode(big_data).decode()


def test_exceptions_propagate_unchanged():
    def boom():
        raise ValueError("x")

    async def main():
        with pytest.raises(ValueError):
            await offload.run(boom, size=0)
        with pytest.raises(ValueError):
            await offload.run(boom, size=offload.OFFLOAD_MIN_BYTES)
    asyncio.run(main())


def test_stt_transcoding_never_on_the_loop(monkeypatch):
    """La transcodifica audio della chat gira su un thread, non sul loop."""
    from app import audioconvert, sttchat

    seen = {}

    def fake_chunks(raw, **kw):
        seen["thread"] = threading.get_ident()
        raise audioconvert.AudioConversionError("stop qui")

    monkeypatch.setattr(audioconvert, "to_ogg_chunks", fake_chunks)
    part = {"type": "input_audio",
            "input_audio": {"data": base64.b64encode(b"RIFF....WAVE").decode(), "format": "wav"}}
    payload = {"messages": [{"role": "user", "content": [part]}]}

    class Pol:
        stt_chat_enabled = True

    async def transcript_one(*a, **k):
        return "trascrizione"

    async def main():
        seen["loop"] = threading.get_ident()
        return await sttchat.resolve_audio_in_payload(
            payload, boundary=None, transcript_one=transcript_one, policy=Pol())

    asyncio.run(main())
    assert "thread" in seen and seen["thread"] != seen["loop"]
