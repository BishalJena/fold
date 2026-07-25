from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from foldos.core.types import Event, Scope


class ReadableStore(Protocol):
    async def read_events(self, scope: Scope, after: int = 0) -> list[Event]: ...


def parse_target(target: str | datetime) -> datetime:
    if isinstance(target, datetime):
        dt = target
    else:
        dt = datetime.fromisoformat(target)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def pos(store: ReadableStore, scope: Scope, target: str | datetime) -> int:
    t = parse_target(target)
    events = await store.read_events(scope)
    best = 0
    for event in events:
        ts = datetime.fromisoformat(event.ts).astimezone(UTC)
        if ts <= t and event.seq > best:
            best = event.seq
    return best


async def cut(store: ReadableStore, scopes: list[Scope], target: str | datetime) -> dict[str, int]:
    result: dict[str, int] = {}
    for scope in scopes:
        result[scope.key()] = await pos(store, scope, target)
    return result
