"""Lavoro CPU-bound fuori dall'event loop (base64, transcodifica, multipart).

[IT] Il gateway e' un solo processo con un solo event loop: una decodifica
base64 di qualche megabyte (immagini, audio) o una transcodifica audio
eseguita sul loop ferma TUTTE le altre richieste, stream compresi. Qui quel
lavoro va su un thread (`asyncio.to_thread`); sotto `OFFLOAD_MIN_BYTES` resta
inline, dove il salto di thread costerebbe piu' del lavoro. Risultati ed
eccezioni sono quelli della chiamata diretta.

[EN] Run CPU-bound work (base64, audio transcoding) off the event loop.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

OFFLOAD_MIN_BYTES = 256 * 1024


async def run(fn: Callable[..., T], *args: Any, size: int = OFFLOAD_MIN_BYTES, **kwargs: Any) -> T:
    """`fn(*args, **kwargs)`, su un thread se `size` (byte da elaborare) e'
    almeno `OFFLOAD_MIN_BYTES`, altrimenti inline."""
    if size >= OFFLOAD_MIN_BYTES:
        return await asyncio.to_thread(fn, *args, **kwargs)
    return fn(*args, **kwargs)


def b64encode_str(data: bytes) -> str:
    return base64.b64encode(data).decode()
