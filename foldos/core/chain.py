from __future__ import annotations

import json
from hashlib import sha256
from typing import Protocol, TypedDict

from foldos.core.types import Event, Scope


class Verification(TypedDict):
    ok: bool
    broken_at: int | None
    events: int


class EventStore(Protocol):
    async def read_events(self, scope: Scope, after: int = 0) -> list[Event]: ...


def canonical(event: Event) -> bytes:
    return json.dumps(
        {
            "seq": event.seq,
            "kind": event.kind,
            "payload": event.payload,
            "actor": event.actor,
            "ts": event.ts,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def hash_event(prev_hash: str, event: Event) -> str:
    return sha256(prev_hash.encode() + canonical(event)).hexdigest()


async def verify(store: EventStore, scope: Scope) -> Verification:
    events = await store.read_events(scope)
    previous = ""
    for event in events:
        expected = hash_event(previous, event)
        if event.hash != expected:
            return {"ok": False, "broken_at": event.seq, "events": len(events)}
        previous = event.hash
    return {"ok": True, "broken_at": None, "events": len(events)}
