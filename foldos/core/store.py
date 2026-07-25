from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy

from foldos.core.chain import hash_event
from foldos.core.policy import validate_batch
from foldos.core.reducers import State, fold
from foldos.core.types import ConcurrencyError, Event, Scope

EventSink = Callable[[Scope, Event], None]


class MemoryStore:
    def __init__(self, event_sink: EventSink | None = None) -> None:
        self._events: dict[Scope, list[Event]] = {}
        self._lock = asyncio.Lock()
        self._event_sink = event_sink

    async def append(self, scope: Scope, events: list[Event], expected_seq: int) -> int:
        async with self._lock:
            current = self._events.get(scope, [])
            actual = len(current)
            if expected_seq != actual:
                raise ConcurrencyError(expected_seq, actual)
            previous_hash = current[-1].hash if current else ""
            prospective: list[Event] = []
            for offset, event in enumerate(events, start=1):
                sequence = actual + offset
                unstamped = event.clone(seq=sequence, hash="")
                candidate = unstamped.stamp(seq=sequence, hash=hash_event(previous_hash, unstamped))
                previous_hash = candidate.hash
                prospective.append(candidate)
            validate_batch(fold(current), prospective)
            if prospective:
                self._events[scope] = [*current, *prospective]
            head = actual + len(prospective)
        if self._event_sink is not None:
            for event in prospective:
                try:
                    self._event_sink(scope, event.clone())
                except Exception:
                    pass
        return head

    async def head(self, scope: Scope) -> int:
        async with self._lock:
            return len(self._events.get(scope, []))

    async def read_events(self, scope: Scope, after: int = 0) -> list[Event]:
        async with self._lock:
            return [event.clone() for event in self._events.get(scope, []) if event.seq > after]

    async def get_state(self, scope: Scope, as_of: int | None = None) -> State:
        async with self._lock:
            events = self._events.get(scope, [])
            selected = events if as_of is None else events[:as_of]
            return deepcopy(fold(selected))

    async def fork(self, source: Scope, at_seq: int, new_thread: str | Scope) -> Scope:
        target = (
            new_thread
            if isinstance(new_thread, Scope)
            else Scope(source.tenant, source.agent, source.session, new_thread)
        )
        async with self._lock:
            source_events = self._events.get(source, [])
            if at_seq < 0 or at_seq > len(source_events):
                raise ValueError("at_seq must be within the source stream")
            if target in self._events:
                raise ValueError("target scope already exists")
            copied: list[Event] = []
            previous_hash = ""
            for event in source_events[:at_seq]:
                unstamped = event.clone(id="", hash="")
                candidate = unstamped.stamp(seq=event.seq, hash=hash_event(previous_hash, unstamped))
                copied.append(candidate)
                previous_hash = candidate.hash
            self._events[target] = copied
            return target

    async def scopes(self, session: str | None = None) -> list[Scope]:
        async with self._lock:
            scopes = self._events if session is None else (scope for scope in self._events if scope.session == session)
            return sorted(scopes, key=Scope.key)


InMemoryStore = MemoryStore
