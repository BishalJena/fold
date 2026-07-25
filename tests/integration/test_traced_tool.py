from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest

from foldos.core.session import Session
from foldos.core.types import ConcurrencyError, Event, Scope
from foldos.otel.traced_tool import traced_tool


class FakeStore:
    def __init__(
        self,
        events: dict[Scope, list[Event]] | None = None,
    ) -> None:
        self._events: dict[Scope, list[Event]] = events or {}

    async def append(self, scope: Scope, events: list[Event], expected_seq: int) -> int:
        current = self._events.get(scope, [])
        head = current[-1].seq if current else 0
        if expected_seq != head:
            raise ConcurrencyError(expected_seq, head)
        for event in events:
            head += 1
            stamped = event.stamp(seq=head, hash="")
            current.append(stamped)
        self._events[scope] = current
        return head

    async def head(self, scope: Scope) -> int:
        current = self._events.get(scope, [])
        return current[-1].seq if current else 0

    async def read_events(self, scope: Scope, after: int = 0) -> list[Event]:
        return [event for event in self._events.get(scope, []) if event.seq > after]

    async def get_state(self, scope: Scope, as_of: int | None = None) -> Any:
        return {"data": {}, "usage": {}, "messages": []}

    async def fork(self, scope: Scope, at_seq: int, new_thread: str) -> Scope:
        new_scope = Scope(scope.tenant, scope.agent, scope.session, new_thread)
        self._events[new_scope] = list(self._events.get(scope, [])[:at_seq])
        return new_scope

    async def scopes(self, session: str | None = None) -> list[Scope]:
        return list(self._events.keys())


@pytest.fixture
def scope() -> Scope:
    return Scope("tenant", "agent", "session", "thread")


@pytest.fixture
def sync_submit() -> Iterator[Callable[[Awaitable[Any]], Any]]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    def submit(coro: Awaitable[Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=5)

    yield submit
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=1)


def test_sync_traced_tool_records_name_args_result_latency(
    scope: Scope, sync_submit: Callable[[Awaitable[Any]], Any]
) -> None:
    store = FakeStore()
    session = Session(store, scope)

    @traced_tool(session=session, submit=sync_submit)
    def add(a: int, b: int) -> int:
        return a + b

    result = add(2, 3)

    assert result == 5
    events = sync_submit(store.read_events(scope))
    assert len(events) == 1
    assert events[0].kind == "tool_call"
    assert events[0].actor == "tool:add"
    payload = events[0].payload
    assert payload["name"] == "add"
    assert payload["args"] == {"a": 2, "b": 3}
    assert payload["result"] == 5
    assert payload["error"] is None
    assert payload["latency_ms"] >= 0.0


@pytest.mark.asyncio
async def test_async_traced_tool_records_name_args_result_latency(scope: Scope) -> None:
    store = FakeStore()
    session = Session(store, scope)

    @traced_tool(session=session)
    async def multiply(a: int, b: int) -> int:
        return a * b

    result = await multiply(3, 4)

    assert result == 12
    events = await store.read_events(scope)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["name"] == "multiply"
    assert payload["args"] == {"a": 3, "b": 4}
    assert payload["result"] == 12
    assert payload["error"] is None
    assert payload["latency_ms"] >= 0.0


def test_sync_traced_tool_records_error_and_reraises(
    scope: Scope, sync_submit: Callable[[Awaitable[Any]], Any]
) -> None:
    store = FakeStore()
    session = Session(store, scope)

    @traced_tool(session=session, submit=sync_submit)
    def boom(x: int) -> int:
        raise ValueError("fail")

    with pytest.raises(ValueError, match="fail"):
        boom(1)

    events = sync_submit(store.read_events(scope))
    assert len(events) == 1
    payload = events[0].payload
    assert payload["name"] == "boom"
    assert payload["args"] == {"x": 1}
    assert payload["result"] is None
    assert payload["error"] == "fail"
    assert payload["latency_ms"] >= 0.0


@pytest.mark.asyncio
async def test_async_traced_tool_records_error_and_reraises(scope: Scope) -> None:
    store = FakeStore()
    session = Session(store, scope)

    @traced_tool(session=session)
    async def aboom(x: int) -> int:
        raise RuntimeError("async fail")

    with pytest.raises(RuntimeError, match="async fail"):
        await aboom(2)

    events = await store.read_events(scope)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["name"] == "aboom"
    assert payload["args"] == {"x": 2}
    assert payload["result"] is None
    assert payload["error"] == "async fail"


def test_traced_tool_preserves_signature_and_metadata(
    scope: Scope, sync_submit: Callable[[Awaitable[Any]], Any]
) -> None:
    store = FakeStore()
    session = Session(store, scope)

    @traced_tool(session=session, submit=sync_submit)
    def greet(name: str, greeting: str = "hello") -> str:
        """Greet someone."""
        return f"{greeting}, {name}"

    assert greet.__name__ == "greet"
    assert greet.__doc__ == "Greet someone."
    sig = inspect.signature(greet)
    assert list(sig.parameters.keys()) == ["name", "greeting"]
    assert greet("world") == "hello, world"


def test_traced_tool_without_session_does_not_raise(scope: Scope, sync_submit: Callable[[Awaitable[Any]], Any]) -> None:
    @traced_tool(session=None, submit=sync_submit)
    def noop() -> int:
        return 42

    assert noop() == 42


def test_traced_tool_redacts_sensitive_arguments_and_result(
    scope: Scope, sync_submit: Callable[[Awaitable[Any]], Any]
) -> None:
    store = FakeStore()
    session = Session(store, scope)

    @traced_tool(session=session, submit=sync_submit)
    def connect(api_key: str, user: str) -> dict[str, Any]:
        return {"token": "secret-token", "user": user}

    connect(api_key="super-secret", user="alice")

    events = sync_submit(store.read_events(scope))
    assert len(events) == 1
    payload = events[0].payload
    assert payload["args"]["api_key"] == "[REDACTED]"
    assert payload["args"]["user"] == "alice"
    assert payload["result"]["token"] == "[REDACTED]"
    assert payload["result"]["user"] == "alice"
