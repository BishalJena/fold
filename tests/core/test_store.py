from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from foldos.core.chain import verify
from foldos.core.reducers import fold
from foldos.core.store import MemoryStore
from foldos.core.types import ConcurrencyError, Event, Scope


@st.composite
def events(draw: st.DrawFn) -> list[Event]:
    values = draw(st.lists(st.integers(), max_size=8))
    return [Event(kind="state_delta", payload={"set": {"value": value}}) for value in values]


@given(events())
@pytest.mark.asyncio
async def test_each_prefix_state_matches_pure_fold(event_batch: list[Event]) -> None:
    store = MemoryStore()
    scope = Scope("tenant", "agent", "session")
    for expected, event in enumerate(event_batch):
        assert await store.append(scope, [event], expected) == expected + 1
    recorded = await store.read_events(scope)
    for sequence in range(len(recorded) + 1):
        assert await store.get_state(scope, as_of=sequence) == fold(recorded[:sequence])


@pytest.mark.asyncio
async def test_concurrent_same_head_has_exactly_one_winner() -> None:
    store = MemoryStore()
    scope = Scope("tenant", "agent", "session")

    outcomes = await asyncio.gather(
        store.append(scope, [Event(kind="state_delta", payload={"set": {"one": 1}})], 0),
        store.append(scope, [Event(kind="state_delta", payload={"set": {"two": 2}})], 0),
        return_exceptions=True,
    )

    outcome_types = sorted((type(outcome) for outcome in outcomes), key=lambda result: result.__name__)
    assert outcome_types == [ConcurrencyError, int]
    assert await store.head(scope) == 1


@pytest.mark.asyncio
async def test_append_clones_input_and_reads_are_immutable() -> None:
    store = MemoryStore()
    scope = Scope("tenant", "agent", "session")
    event = Event(kind="state_delta", payload={"set": {"nested": {"value": 1}}})
    await store.append(scope, [event], 0)
    event.payload["set"]["nested"]["value"] = 2

    read = await store.read_events(scope)
    state = await store.get_state(scope)
    read[0].payload["set"]["nested"]["value"] = 3
    state["data"]["nested"]["value"] = 4

    assert (await store.read_events(scope))[0].payload["set"]["nested"]["value"] == 1
    assert (await store.get_state(scope))["data"]["nested"]["value"] == 1


@pytest.mark.asyncio
async def test_event_sink_receives_only_successfully_persisted_events_and_cannot_break_writes() -> None:
    received: list[tuple[Scope, Event]] = []

    def sink(scope: Scope, event: Event) -> None:
        received.append((scope, event))
        raise RuntimeError("telemetry unavailable")

    store = MemoryStore(event_sink=sink)
    scope = Scope("tenant", "agent", "session")
    head = await store.append(scope, [Event(kind="state_delta", payload={"set": {"a": 1}})], 0)

    assert head == 1
    assert received[0][0] == scope
    assert received[0][1].seq == 1
    assert await store.head(scope) == 1


@pytest.mark.asyncio
async def test_append_has_gap_free_hashes_and_rejects_stale_heads() -> None:
    store = MemoryStore()
    scope = Scope("tenant", "agent", "session")
    await store.append(scope, [Event(kind="state_delta", payload={"set": {"a": 1}})], 0)
    await store.append(scope, [Event(kind="state_delta", payload={"set": {"b": 2}})], 1)

    assert [event.seq for event in await store.read_events(scope)] == [1, 2]
    assert await verify(store, scope) == {"ok": True, "broken_at": None, "events": 2}
    with pytest.raises(ConcurrencyError):
        await store.append(scope, [], 0)


@pytest.mark.asyncio
async def test_fork_copies_a_verified_prefix_and_is_isolated() -> None:
    store = MemoryStore()
    source = Scope("tenant", "agent", "session")
    branch = Scope("tenant", "agent", "session", "branch")
    await store.append(source, [Event(kind="state_delta", payload={"set": {"a": 1}})], 0)
    await store.append(source, [Event(kind="state_delta", payload={"set": {"b": 2}})], 1)

    assert await store.fork(source, 1, branch) == branch
    await store.append(branch, [Event(kind="state_delta", payload={"set": {"c": 3}})], 1)

    assert (await store.get_state(source))["data"] == {"a": 1, "b": 2}
    assert (await store.get_state(branch))["data"] == {"a": 1, "c": 3}
    assert (await store.read_events(source))[0].id != (await store.read_events(branch))[0].id
    assert await verify(store, branch) == {"ok": True, "broken_at": None, "events": 2}
    assert set(await store.scopes()) == {source, branch}
