from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from foldos.control.models import (
    AttestationResponse,
    BacktestRequest,
    BacktestResponse,
    BranchRequest,
    BranchResponse,
    CounterfactualRequest,
    CounterfactualResponse,
    ErrorBody,
    EventListResponse,
    PolicyRequest,
    PolicyResponse,
    ReplayRequest,
    ReplayResponse,
    ScopeSelector,
    TraceIndexExactResponse,
    TraceIndexInexactResponse,
    VerificationResponse,
)


def test_scope_selector_defaults_and_session_is_required() -> None:
    selector = ScopeSelector(session="run-1")

    assert selector.model_dump() == {"tenant": "acme", "agent": "analyst", "session": "run-1", "thread": "main"}
    with pytest.raises(ValidationError):
        ScopeSelector()


def test_policy_request_serializes_contract_names_and_rejects_coercion() -> None:
    request = PolicyRequest(session="run-1", key="budget_usd", value=0.5, reason=None)

    assert request.model_dump(by_alias=True) == {
        "tenant": "acme",
        "agent": "analyst",
        "session": "run-1",
        "thread": "main",
        "key": "budget_usd",
        "value": 0.5,
        "reason": None,
    }
    with pytest.raises(ValidationError):
        PolicyRequest(session=1, key="budget_usd", value=0.5)
    with pytest.raises(ValidationError):
        PolicyRequest(session="run-1", key="budget_usd", value=0.5, unexpected=True)


def test_request_aliases_defaults_and_schema() -> None:
    assert BranchRequest.model_validate({"session": "run-1", "atSeq": 3, "thread": "trial"}).at_seq == 3
    assert BacktestRequest(session="run-1", policy={"key": "budget_usd", "value": 0.5}).policy.key == "budget_usd"
    assert CounterfactualRequest(session="run-1", event_seq=3, thread="trial", payload_overrides={}).tenant == "acme"
    assert ReplayRequest(session="run-1", thread="trial", prompt="continue").agent == "analyst"
    assert "at_seq" in BranchRequest.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        BranchRequest(session="run-1", at_seq="3", thread="trial")


def test_responses_preserve_contract_shapes() -> None:
    backtest = BacktestResponse(evaluated=1, violations=[{"seq": 1, "ts": "now", "observed": 1, "limit": 0}])
    events = EventListResponse(stream="acme/analyst/run-1/main", head=1, events=[event()])
    verification = VerificationResponse(ok=True, broken_at=None, events=1)
    attestation = AttestationResponse(
        stream="acme/analyst/run-1/main",
        events=1,
        head_seq=1,
        head_hash="hash",
        verified_at="2026-01-01T00:00:00+00:00",
        ok=True,
    )

    assert PolicyResponse(seq=1, event_id="event-1").model_dump() == {"seq": 1, "event_id": "event-1"}
    assert BranchResponse(scope="acme/analyst/run-1/trial", from_seq=3).from_seq == 3
    assert (
        CounterfactualResponse(
            original_scope="acme/analyst/run-1/main",
            branch_scope="acme/analyst/run-1/trial",
            replaced_seq=3,
            original_state={},
            branch_state={},
        ).replaced_seq
        == 3
    )
    assert (
        ReplayResponse(
            scope="acme/analyst/run-1/trial",
            agno_session_id="run-1--trial",
            run_id="run-1",
            trace_id="trace-1",
            replay_of="acme/analyst/run-1/main",
            replay_at_seq=3,
        ).replay_at_seq
        == 3
    )
    assert backtest.model_dump()["violations"][0]["observed"] == 1
    assert events.model_dump()["events"][0]["causation_id"] is None
    assert verification.model_dump() == {"ok": True, "broken_at": None, "events": 1}
    assert attestation.model_dump()["head_hash"] == "hash"


def test_trace_index_exact_and_inexact_are_distinct() -> None:
    exact = TraceIndexExactResponse(
        stream="acme/analyst/run-1/main",
        seq=4,
        exact=True,
        at="2026-01-01T00:00:00+00:00",
        cut={"acme/analyst/run-1/main": 4},
    )
    inexact = TraceIndexInexactResponse(stream=None, seq=None, exact=False, at=None, cut=None)

    assert exact.model_dump()["exact"] is True
    assert inexact.model_dump() == {"stream": None, "seq": None, "exact": False, "at": None, "cut": None}


def test_error_body_is_stable_and_strict() -> None:
    error = ErrorBody(error="ambiguous_scope", message="choose a stream", details={"available": ["a/b/c/d"]})

    assert error.model_dump() == {
        "error": "ambiguous_scope",
        "message": "choose a stream",
        "details": {"available": ["a/b/c/d"]},
    }
    with pytest.raises(ValidationError):
        ErrorBody(error="x", message="y", details={}, extra="z")


def event() -> dict[str, Any]:
    return {
        "kind": "message",
        "payload": {"role": "user", "content": "hello"},
        "actor": "user",
        "causation_id": None,
        "id": "event-1",
        "ts": "2026-01-01T00:00:00+00:00",
        "seq": 1,
        "hash": "hash",
    }
