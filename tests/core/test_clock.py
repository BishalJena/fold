from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from foldos.core import clock
from foldos.core.types import Event, Scope


class _ReadableStore:
    def __init__(self, events: dict[Scope, list[Event]]) -> None:
        self._events = events

    async def read_events(self, scope: Scope, after: int = 0) -> list[Event]:
        return [event for event in self._events.get(scope, []) if event.seq > after]


def _event(seq: int, ts: str) -> Event:
    return Event(kind="message", payload={"role": "user", "content": "x"}, ts=ts, seq=seq).stamp(seq=seq, hash="")


@pytest.mark.asyncio
async def test_parse_target_accepts_iso_string_with_and_without_offset() -> None:
    assert clock.parse_target("2026-01-01T00:00:00+00:00") == datetime(2026, 1, 1, tzinfo=UTC)
    assert clock.parse_target("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_parse_target_converts_datetime_to_utc() -> None:
    est = timezone(timedelta(hours=-5))
    assert clock.parse_target(datetime(2026, 1, 1, 5, 0, tzinfo=est)) == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert clock.parse_target(datetime(2026, 1, 1, 10, 0)) == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_pos_returns_zero_when_scope_is_empty() -> None:
    store = _ReadableStore({})
    scope = Scope("t", "a", "s")
    assert await clock.pos(store, scope, "2026-01-01T00:00:00+00:00") == 0


@pytest.mark.asyncio
async def test_pos_returns_max_seq_with_ts_less_than_or_equal_to_target() -> None:
    scope = Scope("t", "a", "s")
    events = [
        _event(1, "2026-01-01T10:00:00+00:00"),
        _event(2, "2026-01-01T11:00:00+00:00"),
        _event(3, "2026-01-01T12:00:00+00:00"),
    ]
    store = _ReadableStore({scope: events})
    assert await clock.pos(store, scope, "2026-01-01T10:00:00+00:00") == 1
    assert await clock.pos(store, scope, "2026-01-01T11:30:00+00:00") == 2
    assert await clock.pos(store, scope, "2026-01-01T13:00:00+00:00") == 3


@pytest.mark.asyncio
async def test_pos_is_monotone_and_total_in_target() -> None:
    scope = Scope("t", "a", "s")
    events = [
        _event(1, "2026-01-01T10:00:00+00:00"),
        _event(2, "2026-01-01T11:00:00+00:00"),
    ]
    store = _ReadableStore({scope: events})
    before = await clock.pos(store, scope, "2026-01-01T09:00:00+00:00")
    at_first = await clock.pos(store, scope, "2026-01-01T10:00:00+00:00")
    between = await clock.pos(store, scope, "2026-01-01T10:30:00+00:00")
    at_second = await clock.pos(store, scope, "2026-01-01T11:00:00+00:00")
    after = await clock.pos(store, scope, "2026-01-01T12:00:00+00:00")
    assert 0 == before <= at_first <= between <= at_second <= after


@pytest.mark.asyncio
async def test_cut_returns_dict_keyed_by_scope_key() -> None:
    s1 = Scope("t", "a", "s1")
    s2 = Scope("t", "a", "s2")
    store = _ReadableStore(
        {
            s1: [
                _event(1, "2026-01-01T10:00:00+00:00"),
                _event(2, "2026-01-01T12:00:00+00:00"),
            ],
            s2: [_event(1, "2026-01-01T11:00:00+00:00")],
        }
    )
    result = await clock.cut(store, [s1, s2], "2026-01-01T11:30:00+00:00")
    assert result == {s1.key(): 1, s2.key(): 1}
