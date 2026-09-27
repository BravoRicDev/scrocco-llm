"""Task di background "fire-and-forget" che non spariscono a meta'.

[IT] asyncio tiene solo un riferimento DEBOLE ai task: un task creato con
`loop.create_task(...)` e poi dimenticato puo' essere raccolto dal garbage
collector prima di finire (documentazione di `asyncio.create_task`). Qui il
riferimento resta in un set finche' il task non termina.

[EN] Keep a strong reference to fire-and-forget tasks until they finish.
"""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

_TASKS: set[asyncio.Task] = set()


def spawn(loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, Any], *,
          registry: set[asyncio.Task] | None = None) -> asyncio.Task:
    """`loop.create_task(coro)` trattenendo il task in `registry` (default:
    set di modulo) fino al suo completamento."""
    tasks = _TASKS if registry is None else registry
    task = loop.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task
