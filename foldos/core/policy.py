from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from foldos.core.reducers import State, apply_event
from foldos.core.types import Event, PolicyViolation, Scope

Invariant = Callable[[State, Event], None]


class EventReader(Protocol):
    async def read_events(self, scope: Scope, after: int = 0) -> list[Event]: ...


_invariants: dict[str, list[Invariant]] = {}


def invariant(kind: str) -> Callable[[Invariant], Invariant]:
    def register(check: Invariant) -> Invariant:
        _invariants.setdefault(kind, []).append(check)
        return check

    return register


def clear_invariants() -> None:
    _invariants.clear()


def validate_batch(state: State, events: list[Event]) -> State:
    projected = state
    for candidate in events:
        projected = apply_event(projected, candidate)
        for check in _invariants.get(candidate.kind, []):
            check(projected, candidate)
    return projected


def budget_cap(limit: float) -> Invariant:
    def check(state: State, candidate: Event) -> None:
        observed = state["usage"]["cost_usd"]
        if observed > limit:
            raise PolicyViolation(f"budget_usd limit {limit} exceeded by {observed}")

    return check


@invariant("llm_call")
@invariant("tool_call")
@invariant("message")
@invariant("policy_set")
def enforce_budget(state: State, candidate: Event) -> None:
    limit = state["data"].get("budget_usd")
    if isinstance(limit, int | float) and not isinstance(limit, bool):
        budget_cap(float(limit))(state, candidate)


async def backtest(store: EventReader, scope: Scope, policy: Mapping[str, Any]) -> dict[str, Any]:
    if policy.get("key") != "budget_usd":
        return {"evaluated": 0, "violations": []}
    limit = policy.get("value")
    if not isinstance(limit, int | float) or isinstance(limit, bool):
        raise ValueError("budget_usd must be numeric")
    state: State = {
        "messages": [],
        "tools": [],
        "spans": {},
        "data": {},
        "usage": {
            "llm_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "uncosted_calls": 0,
            "by_model": {},
            "tools": {},
        },
    }
    violations: list[dict[str, Any]] = []
    events = await store.read_events(scope)
    check = budget_cap(float(limit))
    for event in events:
        state = apply_event(state, event)
        try:
            check(state, event)
        except PolicyViolation:
            violations.append(
                {
                    "seq": event.seq,
                    "ts": event.ts,
                    "observed": state["usage"]["cost_usd"],
                    "limit": limit,
                }
            )
    return {"evaluated": len(events), "violations": violations}
