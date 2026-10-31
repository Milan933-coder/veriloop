"""Bounded thread pool for CPU-bound / blocking work (model inference, hashing, image decoding).

A small fixed pool keeps local models from oversubscribing the CPU when several jobs run at once
(torch already parallelises inside one call). The caller's contextvars (structlog context such as
job_id) are propagated into the worker thread.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

_pool: ThreadPoolExecutor | None = None
_max_workers = 0


def configure_executor(max_workers: int = 0) -> None:
    """Set the pool size (0 = auto). Takes effect for the next pool creation."""
    global _max_workers
    _max_workers = max_workers


def _get_pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        workers = _max_workers or min(4, os.cpu_count() or 2)
        _pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="veriloop-cpu")
    return _pool


async def run_cpu[**P, R](fn: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Await `fn(*args, **kwargs)` on the shared bounded pool."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(_get_pool(), functools.partial(ctx.run, fn, *args, **kwargs))


def shutdown_executor(wait: bool = False) -> None:
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=wait, cancel_futures=True)
        _pool = None
