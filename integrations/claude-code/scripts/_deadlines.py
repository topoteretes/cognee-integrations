"""Elapsed-time bounds for read-only calls, without blocking interpreter exit."""

import math
import queue
import threading
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def bounded_call(call: Callable[[], T], timeout: float) -> T:
    """Wait at most timeout seconds; a late daemon result is discarded.

    This does not cancel the underlying socket. Only use it for reads: a
    timed-out mutation could still commit after its caller has moved on.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise TimeoutError("read deadline exhausted")
    result: queue.Queue = queue.Queue(maxsize=1)

    def run():
        try:
            result.put((True, call()))
        except BaseException as exc:
            result.put((False, exc))

    threading.Thread(target=run, name="cognee-bounded-read", daemon=True).start()
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError("read deadline exhausted") from exc
    if not ok:
        raise value
    return value
