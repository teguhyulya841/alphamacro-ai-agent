"""Bridge allowing synchronous callers (telebot, CLI) to invoke async coroutines."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class LoopThread:
    """Dedicated background asyncio event loop running on a daemon thread."""

    def __init__(self, *, name: str = "alphamacro-event-loop") -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name=name, daemon=True)
        self._ready = threading.Event()
        self._closed = False
        self._lock = threading.Lock()
        self._thread.start()
        self._ready.wait()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, T], *, timeout: float | None = None) -> T:
        """Execute ``coro`` on the background loop and block until completion."""
        if self._closed:
            coro.close()
            raise RuntimeError("LoopThread has already been closed.")
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError(
                "LoopThread.run() cannot be called from its own event loop thread; "
                "use 'await' directly."
            )
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        """Stop the background event loop and join its thread."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if not self._loop.is_running():
            self._loop.close()
