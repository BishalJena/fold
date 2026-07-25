from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar, cast

T = TypeVar("T")


class SyncBridge:
    def __init__(self) -> None:
        self._ready = threading.Event()
        self._closed = False
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.thread = threading.Thread(target=self._serve, name="foldos-sync-bridge", daemon=True)
        self.thread.start()
        self._ready.wait()

    def _serve(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        if not inspect.iscoroutine(coro):
            raise TypeError("run requires a coroutine")
        with self._lock:
            closed = self._closed
            loop = self._loop
        if closed or loop is None:
            coro.close()
            raise RuntimeError("SyncBridge is closed")
        if threading.current_thread() is self.thread:
            coro.close()
            raise RuntimeError("SyncBridge.run cannot be called from the bridge loop")
        return cast(T, asyncio.run_coroutine_threadsafe(coro, loop).result())

    def _cancel_and_stop(self) -> None:
        loop = self._loop
        if loop is None:
            return
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.stop()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(self._cancel_and_stop)
        if threading.current_thread() is not self.thread:
            self.thread.join()

    def __enter__(self) -> SyncBridge:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
