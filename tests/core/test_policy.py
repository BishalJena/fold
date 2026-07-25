from __future__ import annotations

import pytest

from foldos.core.policy import backtest, budget_cap, clear_invariants, enforce_budget, invariant, validate_batch
from foldos.core.reducers import fresh_state
from foldos.core.store import MemoryStore
from foldos.core.types import Event, PolicyViolation, Scope


@pytest.fixture(autouse=True)
def reset_invariants() -> None:
    clear_invariants()


def test_invariant_receives_projected_state_and_candidate() -> None:
    seen: list[tuple[dict[str, object], Event]] = []

    @invariant("state_delta")
    def record(projected: dict[str, object], candidate: Event) -> None:
        seen.append((projected, candidate))

    events = [
        Event(kind="state_delta", payload={"set": {"first": 1}}),
        Event(kind="state_delta", payload={"set": {"second": 2}}),
    ]
    final = validate_batch(fresh_state(), events)

    assert seen[0][0]["data"] == {"first": 1}
    assert seen[1][0]["data"] == {"first": 1, "second": 2}
    assert final["data"] == {"first": 1, "second": 2}


@pytest.mark.asyncio
async def test_policy_veto_is_atomic() -> None:
    store = MemoryStore()
    scope = Scope("tenant", "agent", "session")

    @invariant("state_delta")
    def veto(_: dict[str, object], candidate: Event) -> None:
        if candidate.payload["set"].get("forbidden"):
            raise PolicyViolation("forbidden")

    with pytest.raises(PolicyViolation, match="forbidden"):
        await store.append(
            scope,
            [
                Event(kind="state_delta", payload={"set": {"accepted": True}}),
                Event(kind="state_delta", payload={"set": {"forbidden": True}}),
            ],
            0,
        )
    assert await store.head(scope) == 0
    assert await store.read_events(scope) == []


def test_budget_cap_rejects_a_threshold_crossing_candidate() -> None:
    invariant("llm_call")(budget_cap(0.5))
    event = Event(
        kind="llm_call",
        payload={"model": "known", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.6},
    )

    with pytest.raises(PolicyViolation, match="budget"):
        validate_batch(fresh_state(), [event])


def test_stream_budget_invariant_reads_projected_policy_state() -> None:
    events = [
        Event(kind="policy_set", payload={"key": "budget_usd", "value": 0.5, "reason": None}),
        Event(kind="llm_call", payload={"model": "known", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.6}),
    ]
    invariant("llm_call")(enforce_budget)

    with pytest.raises(PolicyViolation, match="budget"):
        validate_batch(fresh_state(), events)


@pytest.mark.asyncio
async def test_backtest_reports_violations_without_appending() -> None:
    store = MemoryStore()
    scope = Scope("tenant", "agent", "session")
    await store.append(
        scope,
        [
            Event(kind="llm_call", payload={"model": "known", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.2}),
            Event(kind="llm_call", payload={"model": "known", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.4}),
        ],
        0,
    )

    second_event = (await store.read_events(scope))[1]
    assert await backtest(store, scope, {"key": "budget_usd", "value": 0.5}) == {
        "evaluated": 2,
        "violations": [{"seq": 2, "ts": second_event.ts, "observed": 0.6000000000000001, "limit": 0.5}],
    }
    assert await store.head(scope) == 2
