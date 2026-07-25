from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from foldos.core.session import Session
from foldos.core.types import ConcurrencyError, Event, Scope
from foldos.otel.instrument_openai import instrument_openai


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


class FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[Any]:
        yield from self._chunks

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        pass


class FakeAsyncStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[Any]:
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self) -> FakeAsyncStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        pass


class FakeSyncCompletions:
    def __init__(self, client: FakeSyncOpenAIClient) -> None:
        self._client = client

    def create(self, *, model: str, messages: list[dict[str, Any]], stream: bool = False, **kwargs: Any) -> Any:
        if stream:
            return FakeStream(self._client._chunks)
        return SimpleNamespace(model=model, usage=self._client._usage)


class FakeSyncChat:
    def __init__(self, client: FakeSyncOpenAIClient) -> None:
        self.completions = FakeSyncCompletions(client)


class FakeSyncOpenAIClient:
    def __init__(self, chunks: list[Any] | None = None) -> None:
        self.chat = FakeSyncChat(self)
        self._usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        self._chunks = chunks or []


class FakeAsyncCompletions:
    def __init__(self, client: FakeAsyncOpenAIClient) -> None:
        self._client = client

    async def create(self, *, model: str, messages: list[dict[str, Any]], stream: bool = False, **kwargs: Any) -> Any:
        if stream:
            return FakeAsyncStream(self._client._chunks)
        return SimpleNamespace(model=model, usage=self._client._usage)


class FakeAsyncChat:
    def __init__(self, client: FakeAsyncOpenAIClient) -> None:
        self.completions = FakeAsyncCompletions(client)


class FakeAsyncOpenAIClient:
    def __init__(self, chunks: list[Any] | None = None) -> None:
        self.chat = FakeAsyncChat(self)
        self._usage = SimpleNamespace(prompt_tokens=4, completion_tokens=2)
        self._chunks = chunks or []


def make_chunk(model: str, usage: Any) -> Any:
    return SimpleNamespace(model=model, usage=usage)


def test_sync_non_stream_records_llm_call(scope: Scope, sync_submit: Callable[[Awaitable[Any]], Any]) -> None:
    store = FakeStore()
    session = Session(store, scope)
    client = FakeSyncOpenAIClient()
    instrument_openai(client, session=session, submit=sync_submit)

    response = client.chat.completions.create(model="m1", messages=[{"role": "user", "content": "hi"}])

    assert response.model == "m1"
    events = sync_submit(store.read_events(scope))
    assert len(events) == 1
    assert events[0].kind == "llm_call"
    assert events[0].actor == "llm:m1"
    payload = events[0].payload
    assert payload["model"] == "m1"
    assert payload["input_tokens"] == 10
    assert payload["output_tokens"] == 5
    assert payload["latency_ms"] >= 0.0
    assert "messages" not in payload


def test_sync_non_stream_without_session_does_not_raise(scope: Scope) -> None:
    client = FakeSyncOpenAIClient()
    instrument_openai(client)
    response = client.chat.completions.create(model="m1", messages=[{"role": "user", "content": "hi"}])
    assert response.model == "m1"


def test_double_wrap_does_not_duplicate_events(scope: Scope, sync_submit: Callable[[Awaitable[Any]], Any]) -> None:
    store = FakeStore()
    session = Session(store, scope)
    client = FakeSyncOpenAIClient()
    instrument_openai(client, session=session, submit=sync_submit)
    instrument_openai(client, session=session, submit=sync_submit)

    client.chat.completions.create(model="m1", messages=[])

    events = sync_submit(store.read_events(scope))
    assert len(events) == 1


def test_sync_stream_full_consumption_records_llm_call(
    scope: Scope, sync_submit: Callable[[Awaitable[Any]], Any]
) -> None:
    store = FakeStore()
    session = Session(store, scope)
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2)
    chunks = [make_chunk("m1", None), make_chunk("m1", usage)]
    client = FakeSyncOpenAIClient(chunks=chunks)
    instrument_openai(client, session=session, submit=sync_submit)

    with client.chat.completions.create(model="m1", messages=[], stream=True) as stream:
        assert [chunk for chunk in stream] == chunks

    events = sync_submit(store.read_events(scope))
    assert len(events) == 1
    payload = events[0].payload
    assert payload["model"] == "m1"
    assert payload["input_tokens"] == 3
    assert payload["output_tokens"] == 2
    assert payload["latency_ms"] >= 0.0


def test_sync_stream_partial_consumption_does_not_record(
    scope: Scope, sync_submit: Callable[[Awaitable[Any]], Any]
) -> None:
    store = FakeStore()
    session = Session(store, scope)
    usage = SimpleNamespace(prompt_tokens=3, completion_tokens=2)
    chunks = [make_chunk("m1", None), make_chunk("m1", usage)]
    client = FakeSyncOpenAIClient(chunks=chunks)
    instrument_openai(client, session=session, submit=sync_submit)

    stream = client.chat.completions.create(model="m1", messages=[], stream=True)
    with stream:
        _ = next(iter(stream))

    events = sync_submit(store.read_events(scope))
    assert len(events) == 0


@pytest.mark.asyncio
async def test_dynamic_session_resolver_records_to_current_session(scope: Scope) -> None:
    first_store = FakeStore()
    second_store = FakeStore()
    current = Session(first_store, scope)
    client = FakeAsyncOpenAIClient()
    instrument_openai(client, session=lambda: current)

    await client.chat.completions.create(model="m2", messages=[])
    current = Session(second_store, scope)
    await client.chat.completions.create(model="m2", messages=[])

    assert len(await first_store.read_events(scope)) == 1
    assert len(await second_store.read_events(scope)) == 1


@pytest.mark.asyncio
async def test_async_non_stream_records_llm_call(scope: Scope) -> None:
    store = FakeStore()
    session = Session(store, scope)
    client = FakeAsyncOpenAIClient()
    instrument_openai(client, session=session)

    response = await client.chat.completions.create(model="m2", messages=[])

    assert response.model == "m2"
    events = await store.read_events(scope)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["model"] == "m2"
    assert payload["input_tokens"] == 4
    assert payload["output_tokens"] == 2
    assert payload["latency_ms"] >= 0.0


@pytest.mark.asyncio
async def test_async_stream_full_consumption_records_llm_call(scope: Scope) -> None:
    store = FakeStore()
    session = Session(store, scope)
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=3)
    chunks = [make_chunk("m2", None), make_chunk("m2", usage)]
    client = FakeAsyncOpenAIClient(chunks=chunks)
    instrument_openai(client, session=session)

    async with await client.chat.completions.create(model="m2", messages=[], stream=True) as stream:
        consumed = [chunk async for chunk in stream]
        assert consumed == chunks

    events = await store.read_events(scope)
    assert len(events) == 1
    payload = events[0].payload
    assert payload["model"] == "m2"
    assert payload["input_tokens"] == 6
    assert payload["output_tokens"] == 3


@pytest.mark.asyncio
async def test_async_stream_partial_consumption_does_not_record(scope: Scope) -> None:
    store = FakeStore()
    session = Session(store, scope)
    usage = SimpleNamespace(prompt_tokens=6, completion_tokens=3)
    chunks = [make_chunk("m2", None), make_chunk("m2", usage)]
    client = FakeAsyncOpenAIClient(chunks=chunks)
    instrument_openai(client, session=session)

    stream = await client.chat.completions.create(model="m2", messages=[], stream=True)
    async with stream:
        async for _chunk in stream:
            break

    events = await store.read_events(scope)
    assert len(events) == 0


@pytest.mark.asyncio
async def test_latency_is_measured_for_non_stream_call(scope: Scope) -> None:
    store = FakeStore()
    session = Session(store, scope)
    client = FakeAsyncOpenAIClient()
    instrument_openai(client, session=session)

    await client.chat.completions.create(model="m3", messages=[])

    events = await store.read_events(scope)
    assert events[0].payload["latency_ms"] >= 0.0
