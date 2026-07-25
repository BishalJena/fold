from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from foldos.core.types import ChainBroken, ConcurrencyError, Event, PolicyViolation, Scope


def test_scope_is_frozen_hashable_and_has_exact_key() -> None:
    scope = Scope("tenant", "agent", "session", "thread")

    assert scope.key() == "tenant/agent/session/thread"
    assert {scope: "value"}[scope] == "value"
    with pytest.raises(AttributeError):
        scope.tenant = "other"


def test_event_defaults_are_sortable_utc_and_unassigned() -> None:
    event = Event(kind="message", payload={"content": "hello"})

    assert event.id[:13].isdigit()
    assert len(event.id) == 30
    assert datetime.fromisoformat(event.ts).utcoffset() == timedelta(0)
    assert event.seq == 0
    assert event.hash == ""


def test_event_clone_deep_copies_and_stamp_does_not_mutate_caller() -> None:
    event = Event(kind="message", payload={"meta": {"tags": ["one"]}})

    clone = event.clone()
    stamped = event.stamp(seq=3, hash="digest")
    clone.payload["meta"]["tags"].append("two")

    assert event.payload == {"meta": {"tags": ["one"]}}
    assert clone.payload == {"meta": {"tags": ["one", "two"]}}
    assert stamped.seq == 3
    assert stamped.hash == "digest"
    assert event.seq == 0
    assert event.hash == ""


def test_domain_errors_expose_their_contract_values() -> None:
    concurrency = ConcurrencyError(2, 4)
    violation = PolicyViolation("budget exceeded")
    broken = ChainBroken(7)

    assert concurrency.expected == 2
    assert concurrency.actual == 4
    assert str(concurrency) == "expected 2, got 4"
    assert violation.reason == "budget exceeded"
    assert str(violation) == "budget exceeded"
    assert broken.seq == 7
    assert str(broken) == "chain broken at seq 7"
