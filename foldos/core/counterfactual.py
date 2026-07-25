from __future__ import annotations

from typing import Any, TypedDict

from foldos.core.reducers import State
from foldos.core.store import MemoryStore
from foldos.core.types import Event, Scope


class CounterfactualResult(TypedDict):
    original_scope: Scope
    branch_scope: Scope
    replaced_seq: int
    original_state: State
    branch_state: State


async def counterfactual(
    store: MemoryStore,
    source_scope: Scope,
    event_seq: int,
    new_thread: str,
    payload_overrides: dict[str, Any],
) -> CounterfactualResult:
    source_events = await store.read_events(source_scope)
    source_head = len(source_events)
    if event_seq < 1 or event_seq > source_head:
        raise ValueError("event_seq must point to an existing source event")

    if new_thread == source_scope.thread:
        raise ValueError("branch thread must differ from source thread")

    branch_scope = Scope(
        source_scope.tenant,
        source_scope.agent,
        source_scope.session,
        new_thread,
    )
    if branch_scope in await store.scopes(source_scope.session):
        raise ValueError("branch scope already exists")

    original_state = await store.get_state(source_scope)
    branch = await store.fork(source_scope, event_seq - 1, new_thread)

    source_event = source_events[event_seq - 1]
    merged_payload = {**source_event.payload, **payload_overrides}
    replacement = Event(
        kind=source_event.kind,
        payload=merged_payload,
        actor=source_event.actor,
        causation_id=source_event.causation_id,
        ts=source_event.ts,
    )

    suffix = [event.clone() for event in source_events[event_seq:]]
    batch = [replacement, *suffix]

    await store.append(branch, batch, event_seq - 1)
    branch_state = await store.get_state(branch)

    return {
        "original_scope": source_scope,
        "branch_scope": branch,
        "replaced_seq": event_seq,
        "original_state": original_state,
        "branch_state": branch_state,
    }
