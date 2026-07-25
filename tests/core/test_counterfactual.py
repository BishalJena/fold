from __future__ import annotations

import pytest

from foldos.core.chain import verify
from foldos.core.counterfactual import counterfactual
from foldos.core.store import MemoryStore
from foldos.core.types import Event, Scope


async def _seed(store: MemoryStore, scope: Scope) -> None:
    await store.append(
        scope,
        [
            Event(
                kind="message",
                payload={"role": "user", "content": "hello"},
                actor="user",
                causation_id="c1",
                ts="2026-01-01T00:00:00+00:00",
            ),
            Event(
                kind="message",
                payload={"role": "assistant", "content": "world"},
                actor="assistant",
                causation_id="c2",
                ts="2026-01-01T00:00:01+00:00",
            ),
            Event(
                kind="state_delta",
                payload={"set": {"flag": True}},
                actor="agent",
                causation_id="c3",
                ts="2026-01-01T00:00:02+00:00",
            ),
        ],
        0,
    )


async def test_suffix_is_copied_with_unchanged_fields() -> None:
    store = MemoryStore()
    source = Scope("t", "a", "s")
    await _seed(store, source)
    original = await store.read_events(source)

    result = await counterfactual(
        store,
        source,
        event_seq=2,
        new_thread="branch",
        payload_overrides={"content": "altered"},
    )

    branch_events = await store.read_events(result["branch_scope"])
    assert len(branch_events) == len(original)
    assert branch_events[0].kind == original[0].kind
    assert branch_events[1].payload["content"] == "altered"
    assert branch_events[2].payload == original[2].payload
    assert branch_events[2].kind == original[2].kind
    assert branch_events[2].actor == original[2].actor
    assert branch_events[2].causation_id == original[2].causation_id
    assert branch_events[2].ts == original[2].ts
    assert branch_events[2].seq == original[2].seq
    assert branch_events[2].id == original[2].id


async def test_source_stream_is_isolated() -> None:
    store = MemoryStore()
    source = Scope("t", "a", "s")
    await _seed(store, source)
    before = await store.read_events(source)
    before_state = await store.get_state(source)

    await counterfactual(
        store,
        source,
        event_seq=1,
        new_thread="branch",
        payload_overrides={"content": "changed"},
    )

    after = await store.read_events(source)
    after_state = await store.get_state(source)
    assert after == before
    assert after_state == before_state


async def test_branch_chain_is_valid() -> None:
    store = MemoryStore()
    source = Scope("t", "a", "s")
    await _seed(store, source)

    result = await counterfactual(
        store,
        source,
        event_seq=2,
        new_thread="branch",
        payload_overrides={"content": "x"},
    )

    assert await verify(store, result["branch_scope"]) == {
        "ok": True,
        "broken_at": None,
        "events": 3,
    }


async def test_folded_result_is_deterministic_across_branch_names() -> None:
    store = MemoryStore()
    source = Scope("t", "a", "s")
    await _seed(store, source)

    first = await counterfactual(
        store,
        source,
        event_seq=2,
        new_thread="b1",
        payload_overrides={"content": "same"},
    )
    second = await counterfactual(
        store,
        source,
        event_seq=2,
        new_thread="b2",
        payload_overrides={"content": "same"},
    )

    assert first["replaced_seq"] == second["replaced_seq"]
    assert first["branch_state"] == second["branch_state"]
    assert first["original_state"] == second["original_state"]
    assert first["branch_scope"] != second["branch_scope"]


async def test_no_model_integration_occurs() -> None:
    store = MemoryStore()
    source = Scope("t", "a", "s")
    await _seed(store, source)

    result = await counterfactual(
        store,
        source,
        event_seq=2,
        new_thread="branch",
        payload_overrides={"content": "only payload"},
    )

    branch_events = await store.read_events(result["branch_scope"])
    assert len(branch_events) == 3
    assert all(event.kind != "llm_call" for event in branch_events)
    assert result["branch_state"] == await store.get_state(result["branch_scope"])


async def test_invalid_sequence_raises() -> None:
    store = MemoryStore()
    source = Scope("t", "a", "s")
    await _seed(store, source)

    with pytest.raises(ValueError):
        await counterfactual(store, source, event_seq=0, new_thread="branch", payload_overrides={})
    with pytest.raises(ValueError):
        await counterfactual(store, source, event_seq=4, new_thread="branch", payload_overrides={})
    with pytest.raises(ValueError):
        await counterfactual(store, source, event_seq=-1, new_thread="branch", payload_overrides={})


async def test_same_or_existing_thread_raises() -> None:
    store = MemoryStore()
    source = Scope("t", "a", "s", "main")
    await _seed(store, source)

    with pytest.raises(ValueError):
        await counterfactual(store, source, event_seq=1, new_thread="main", payload_overrides={})

    await counterfactual(store, source, event_seq=1, new_thread="used", payload_overrides={})
    with pytest.raises(ValueError):
        await counterfactual(store, source, event_seq=1, new_thread="used", payload_overrides={})
