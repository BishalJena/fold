from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import pytest

from foldos.core.chain import canonical, hash_event, verify
from foldos.core.types import Event, Scope


class MemoryStore:
    def __init__(self, events: list[Event]) -> None:
        self.events = events

    async def read_events(self, scope: Scope, after: int = 0) -> list[Event]:
        return [event for event in self.events if event.seq > after]


def test_canonical_json_has_exact_sorted_envelope_only() -> None:
    event = Event(
        kind="message",
        payload={"z": 1, "a": {"b": True}},
        actor="user",
        causation_id="span-1",
        id="ignored",
        ts="2026-07-25T12:00:00+00:00",
        seq=2,
        hash="ignored",
    )

    assert canonical(event) == (
        b'{"actor":"user","kind":"message","payload":{"a":{"b":true},"z":1},"seq":2,"ts":"2026-07-25T12:00:00+00:00"}'
    )


def test_hash_event_is_sha256_of_previous_hash_then_canonical_event() -> None:
    event = Event(kind="message", payload={"content": "hi"}, ts="2026-07-25T12:00:00+00:00", seq=1)

    assert hash_event("", event) == sha256(canonical(event)).hexdigest()
    assert hash_event("previous", event) == sha256(b"previous" + canonical(event)).hexdigest()


@pytest.mark.asyncio
async def test_verify_returns_exact_success_shape() -> None:
    scope = Scope("tenant", "agent", "session")
    first = Event(kind="message", payload={"content": "one"}, ts="2026-07-25T12:00:00+00:00", seq=1)
    first = first.stamp(seq=1, hash=hash_event("", first))
    second = Event(kind="message", payload={"content": "two"}, ts="2026-07-25T12:01:00+00:00", seq=2)
    second = second.stamp(seq=2, hash=hash_event(first.hash, second))

    assert await verify(MemoryStore([first, second]), scope) == {"ok": True, "broken_at": None, "events": 2}


@pytest.mark.asyncio
async def test_verify_reports_first_payload_or_hash_tamper() -> None:
    scope = Scope("tenant", "agent", "session")
    first = Event(kind="message", payload={"content": "one"}, ts="2026-07-25T12:00:00+00:00", seq=1)
    first = first.stamp(seq=1, hash=hash_event("", first))
    second = Event(kind="message", payload={"content": "two"}, ts="2026-07-25T12:01:00+00:00", seq=2)
    second = second.stamp(seq=2, hash=hash_event(first.hash, second))

    payload_tampered = replace(second, payload={"content": "changed"})
    hash_tampered = replace(second, hash="not-a-digest")

    assert await verify(MemoryStore([first, payload_tampered]), scope) == {"ok": False, "broken_at": 2, "events": 2}
    assert await verify(MemoryStore([first, hash_tampered]), scope) == {"ok": False, "broken_at": 2, "events": 2}
