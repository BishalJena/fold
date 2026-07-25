from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Protocol

from foldos.core.reducers import State, Usage
from foldos.core.types import ConcurrencyError, Event, Scope


class Store(Protocol):
    async def append(self, scope: Scope, events: list[Event], expected_seq: int) -> int: ...
    async def head(self, scope: Scope) -> int: ...
    async def read_events(self, scope: Scope, after: int = 0) -> list[Event]: ...
    async def get_state(self, scope: Scope, as_of: int | None = None) -> State: ...
    async def fork(self, scope: Scope, at_seq: int, new_thread: str) -> Scope: ...
    async def scopes(self, session: str | None = None) -> list[Scope]: ...


_MAX_APPEND_ATTEMPTS = 8
_active_span_id: ContextVar[str | None] = ContextVar("active_span_id", default=None)


def _new_span_id() -> str:
    return f"{time.time_ns():020d}-{os.urandom(8).hex()}"


class Session:
    def __init__(self, store: Store, scope: Scope) -> None:
        self._store = store
        self._scope = scope

    async def _append(self, events: list[Event]) -> int:
        for attempt in range(_MAX_APPEND_ATTEMPTS):
            expected = await self._store.head(self._scope)
            try:
                return await self._store.append(self._scope, events, expected)
            except ConcurrencyError:
                if attempt == _MAX_APPEND_ATTEMPTS - 1:
                    raise
        raise ConcurrencyError(-1, -1)

    async def add_message(self, role: str, content: str) -> int:
        event = Event(
            kind="message",
            payload={"role": role, "content": content},
            actor=role,
            causation_id=_active_span_id.get(),
        )
        return await self._append([event])

    async def add_tool_call(
        self,
        name: str,
        args: dict[str, Any],
        result: Any,
        latency_ms: float | None = None,
        error: str | None = None,
    ) -> int:
        event = Event(
            kind="tool_call",
            payload={
                "name": name,
                "args": args,
                "result": result,
                "latency_ms": latency_ms,
                "error": error,
            },
            actor=f"tool:{name}",
            causation_id=_active_span_id.get(),
        )
        return await self._append([event])

    async def add_llm_call(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float | None = None,
        cost_usd: float | None = None,
    ) -> int:
        event = Event(
            kind="llm_call",
            payload={
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "cost_usd": cost_usd,
            },
            actor=f"llm:{model}",
            causation_id=_active_span_id.get(),
        )
        return await self._append([event])

    async def set(self, key: str, value: Any) -> int:
        event = Event(
            kind="state_delta",
            payload={"set": {key: value}},
            actor="agent",
            causation_id=_active_span_id.get(),
        )
        return await self._append([event])

    async def unset(self, key: str) -> int:
        event = Event(
            kind="state_delta",
            payload={"unset": [key]},
            actor="agent",
            causation_id=_active_span_id.get(),
        )
        return await self._append([event])

    async def get(self, key: str) -> Any:
        state = await self.state()
        return state["data"].get(key)

    @property
    def scope(self) -> Scope:
        return self._scope

    async def usage(self) -> Usage:
        state = await self.state()
        return state["usage"]

    async def state(self) -> State:
        return await self._store.get_state(self._scope)

    async def trace(self) -> list[Event]:
        return await self._store.read_events(self._scope)

    async def head(self) -> int:
        return await self._store.head(self._scope)

    async def fork(self, at_seq: int, new_thread: str) -> Session:
        new_scope = await self._store.fork(self._scope, at_seq, new_thread)
        return Session(self._store, new_scope)

    @asynccontextmanager
    async def span(self, name: str, **attrs: Any) -> AsyncIterator[str]:
        span_id = _new_span_id()
        parent_id = _active_span_id.get()
        start_event = Event(
            kind="span_start",
            payload={"span_id": span_id, "name": name, "parent_id": parent_id, "attrs": attrs},
            actor="trace",
            causation_id=parent_id,
        )
        token = _active_span_id.set(span_id)
        started_at = time.monotonic()
        status = "ok"
        error: str | None = None
        started = False
        try:
            await self._append([start_event])
            started = True
            yield span_id
        except Exception as exc:
            status = "error"
            error = str(exc) or type(exc).__name__
            raise
        finally:
            if started:
                duration_ms = (time.monotonic() - started_at) * 1000.0
                end_event = Event(
                    kind="span_end",
                    payload={"span_id": span_id, "status": status, "error": error, "duration_ms": duration_ms},
                    actor="trace",
                    causation_id=span_id,
                )
                await self._append([end_event])
            _active_span_id.reset(token)
