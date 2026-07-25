from __future__ import annotations

import pytest

from foldos.core.reducers import State, fold
from foldos.core.session import Session
from foldos.core.types import ConcurrencyError, Event, Scope


class FakeStore:
    def __init__(
        self,
        events: dict[Scope, list[Event]] | None = None,
        states: dict[Scope, State] | None = None,
    ) -> None:
        self._events: dict[Scope, list[Event]] = events or {}
        self._states: dict[Scope, State] = states or {}

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

    async def get_state(self, scope: Scope, as_of: int | None = None) -> State:
        if scope in self._states:
            return self._states[scope]
        events = self._events.get(scope, [])
        return fold(events if as_of is None else events[:as_of])

    async def fork(self, scope: Scope, at_seq: int, new_thread: str) -> Scope:
        new_scope = Scope(scope.tenant, scope.agent, scope.session, new_thread)
        self._events[new_scope] = list(self._events.get(scope, [])[:at_seq])
        return new_scope

    async def scopes(self) -> list[Scope]:
        return list(self._events.keys())


class FlakyStore:
    def __init__(self, underlying: FakeStore, failures_before_success: int) -> None:
        self._underlying = underlying
        self._failures = failures_before_success
        self._attempts = 0

    async def append(self, scope: Scope, events: list[Event], expected_seq: int) -> int:
        self._attempts += 1
        if self._attempts <= self._failures:
            actual = await self._underlying.head(scope)
            raise ConcurrencyError(expected_seq, actual)
        return await self._underlying.append(scope, events, expected_seq)

    async def head(self, scope: Scope) -> int:
        return await self._underlying.head(scope)

    async def read_events(self, scope: Scope, after: int = 0) -> list[Event]:
        return await self._underlying.read_events(scope, after)

    async def get_state(self, scope: Scope, as_of: int | None = None) -> State:
        return await self._underlying.get_state(scope, as_of)

    async def fork(self, scope: Scope, at_seq: int, new_thread: str) -> Scope:
        return await self._underlying.fork(scope, at_seq, new_thread)

    async def scopes(self) -> list[Scope]:
        return await self._underlying.scopes()


@pytest.fixture
def scope() -> Scope:
    return Scope("tenant", "agent", "session", "thread")


@pytest.mark.asyncio
async def test_add_message_appends_exact_payload_and_actor(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    head = await session_obj.add_message("user", "hello")
    assert head == 1
    events = await store.read_events(scope)
    assert len(events) == 1
    assert events[0].kind == "message"
    assert events[0].actor == "user"
    assert events[0].payload == {"role": "user", "content": "hello"}
    assert events[0].causation_id is None


@pytest.mark.asyncio
async def test_add_tool_call_appends_exact_payload(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    await session_obj.add_tool_call("search", {"q": "x"}, {"ok": True}, latency_ms=12.5, error=None)
    events = await store.read_events(scope)
    assert events[0].kind == "tool_call"
    assert events[0].actor == "tool:search"
    assert events[0].payload == {
        "name": "search",
        "args": {"q": "x"},
        "result": {"ok": True},
        "latency_ms": 12.5,
        "error": None,
    }


@pytest.mark.asyncio
async def test_add_llm_call_appends_exact_payload(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    await session_obj.add_llm_call("m1", 100, 50, latency_ms=200.0, cost_usd=1.5)
    events = await store.read_events(scope)
    assert events[0].kind == "llm_call"
    assert events[0].actor == "llm:m1"
    assert events[0].payload == {
        "model": "m1",
        "input_tokens": 100,
        "output_tokens": 50,
        "latency_ms": 200.0,
        "cost_usd": 1.5,
    }


@pytest.mark.asyncio
async def test_set_and_unset_manipulate_data(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    await session_obj.set("a", 1)
    await session_obj.set("b", 2)
    assert await session_obj.get("a") == 1
    assert await session_obj.get("b") == 2
    await session_obj.unset("a")
    assert await session_obj.get("a") is None
    state = await session_obj.state()
    assert state["data"] == {"b": 2}


@pytest.mark.asyncio
async def test_usage_reflects_llm_and_tool_calls(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    await session_obj.add_message("user", "hi")
    await session_obj.add_tool_call("search", {}, None, latency_ms=10.0, error=None)
    await session_obj.add_llm_call("m1", 10, 5, latency_ms=100.0, cost_usd=1.0)
    usage = await session_obj.usage()
    assert usage["llm_calls"] == 1
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["cost_usd"] == 1.0
    assert usage["tools"]["search"]["calls"] == 1


@pytest.mark.asyncio
async def test_state_and_trace_and_head_helpers(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    assert await session_obj.head() == 0
    await session_obj.add_message("user", "a")
    await session_obj.add_message("assistant", "b")
    events = await session_obj.trace()
    assert len(events) == 2
    assert [event.kind for event in events] == ["message", "message"]
    assert await session_obj.head() == 2
    state = await session_obj.state()
    assert state["messages"] == [
        {"role": "user", "content": "a", "seq": 1},
        {"role": "assistant", "content": "b", "seq": 2},
    ]


@pytest.mark.asyncio
async def test_fork_creates_independent_session(scope: Scope) -> None:
    new_scope = Scope("tenant", "agent", "session", "other")
    store = FakeStore()
    session_obj = Session(store, scope)
    await session_obj.add_message("user", "a")
    forked = await session_obj.fork(1, "other")
    assert forked._scope == new_scope
    await forked.add_message("assistant", "b")
    assert await store.head(scope) == 1
    assert await store.head(new_scope) == 2


@pytest.mark.asyncio
async def test_append_retries_until_success_within_eight_attempts(scope: Scope) -> None:
    store = FakeStore()
    flaky = FlakyStore(store, failures_before_success=7)
    session_obj = Session(flaky, scope)
    head = await session_obj.add_message("user", "hello")
    assert head == 1
    assert await store.head(scope) == 1
    assert flaky._attempts == 8


@pytest.mark.asyncio
async def test_append_raises_after_exactly_eight_attempts(scope: Scope) -> None:
    store = FakeStore()
    flaky = FlakyStore(store, failures_before_success=8)
    session_obj = Session(flaky, scope)
    with pytest.raises(ConcurrencyError):
        await session_obj.add_message("user", "hello")
    assert await store.head(scope) == 0
    assert flaky._attempts == 8


@pytest.mark.asyncio
async def test_span_appends_start_and_end_with_ok(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    async with session_obj.span("work", x=1) as span_id:
        pass
    events = await store.read_events(scope)
    assert len(events) == 2
    start, end = events
    assert start.kind == "span_start"
    assert start.actor == "trace"
    assert start.payload["name"] == "work"
    assert start.payload["attrs"] == {"x": 1}
    assert start.payload["parent_id"] is None
    assert start.payload["span_id"] == span_id
    assert end.kind == "span_end"
    assert end.payload["span_id"] == span_id
    assert end.payload["status"] == "ok"
    assert end.payload["error"] is None
    assert end.payload["duration_ms"] >= 0.0


@pytest.mark.asyncio
async def test_span_appends_end_with_error_on_body_exception(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    with pytest.raises(ValueError, match="boom"):
        async with session_obj.span("work") as span_id:
            raise ValueError("boom")
    events = await store.read_events(scope)
    assert events[1].kind == "span_end"
    assert events[1].payload["span_id"] == span_id
    assert events[1].payload["status"] == "error"
    assert events[1].payload["error"] == "boom"


@pytest.mark.asyncio
async def test_span_nesting_preserves_parent_ids(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    async with session_obj.span("outer") as outer_id:
        async with session_obj.span("inner") as inner_id:
            pass
    events = await store.read_events(scope)
    assert len(events) == 4
    outer_start, inner_start, inner_end, outer_end = events
    assert outer_start.payload["parent_id"] is None
    assert outer_start.payload["span_id"] == outer_id
    assert inner_start.payload["parent_id"] == outer_id
    assert inner_start.payload["span_id"] == inner_id
    assert inner_end.payload["span_id"] == inner_id
    assert outer_end.payload["span_id"] == outer_id
    assert events.index(outer_end) > events.index(inner_end)


@pytest.mark.asyncio
async def test_nested_writes_set_causation_id_to_current_span(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    async with session_obj.span("outer") as outer_id:
        await session_obj.add_message("user", "a")
        async with session_obj.span("inner") as inner_id:
            await session_obj.add_message("user", "b")
    events = await store.read_events(scope)
    message_a = next(event for event in events if event.kind == "message" and event.payload["content"] == "a")
    message_b = next(event for event in events if event.kind == "message" and event.payload["content"] == "b")
    assert message_a.causation_id == outer_id
    assert message_b.causation_id == inner_id


@pytest.mark.asyncio
async def test_span_id_is_sortable(scope: Scope) -> None:
    store = FakeStore()
    session_obj = Session(store, scope)
    async with session_obj.span("first") as first_id:
        async with session_obj.span("second") as second_id:
            pass
    assert first_id < second_id
