from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class ControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_by_name=True)


class ScopeSelector(ControlModel):
    tenant: str = "acme"
    agent: str = "analyst"
    session: str
    thread: str = "main"


class PolicyRequest(ScopeSelector):
    key: str
    value: Any
    reason: str | None = None


class PolicyResponse(ControlModel):
    seq: int
    event_id: str


class MessageRequest(ScopeSelector):
    role: Literal["user", "assistant", "system"] = "user"
    content: str


class MessageResponse(ControlModel):
    seq: int
    event_id: str


class PolicySpec(ControlModel):
    key: str
    value: Any


class BacktestRequest(ScopeSelector):
    policy: PolicySpec


class Violation(ControlModel):
    seq: int
    ts: str
    observed: Any
    limit: Any


class BacktestResponse(ControlModel):
    evaluated: int
    violations: list[Violation]


class BranchRequest(ScopeSelector):
    at_seq: int = Field(validation_alias=AliasChoices("at_seq", "atSeq"))


class BranchResponse(ControlModel):
    scope: str
    from_seq: int


class CounterfactualRequest(ScopeSelector):
    event_seq: int = Field(validation_alias=AliasChoices("event_seq", "eventSeq"))
    payload_overrides: dict[str, Any] = Field(validation_alias=AliasChoices("payload_overrides", "payloadOverrides"))


class CounterfactualResponse(ControlModel):
    original_scope: str
    branch_scope: str
    replaced_seq: int
    original_state: dict[str, Any]
    branch_state: dict[str, Any]


class ReplayRequest(ScopeSelector):
    prompt: str


class ReplayResponse(ControlModel):
    scope: str
    agno_session_id: str
    run_id: str
    trace_id: str
    replay_of: str
    replay_at_seq: int


class EventBody(ControlModel):
    kind: str
    payload: dict[str, Any]
    actor: str
    causation_id: str | None
    id: str
    ts: str
    seq: int
    hash: str


class EventListResponse(ControlModel):
    stream: str
    head: int
    events: list[EventBody]


class VerificationResponse(ControlModel):
    ok: bool
    broken_at: int | None
    events: int


class AttestationResponse(ControlModel):
    stream: str
    events: int
    head_seq: int
    head_hash: str
    verified_at: str
    ok: bool


class TraceIndexEntry(ControlModel):
    stream: str
    seq: int
    trace_id: str
    span_id: str


class TraceIndexExactResponse(ControlModel):
    stream: str
    seq: int
    exact: Literal[True]
    at: str
    cut: dict[str, int]


class TraceIndexInexactResponse(ControlModel):
    stream: None = None
    seq: None = None
    exact: Literal[False]
    at: None = None
    cut: None = None


TraceIndexResponse = TraceIndexExactResponse | TraceIndexInexactResponse


class CutResponse(ControlModel):
    at: str
    cut: dict[str, int]


class StateResponse(ControlModel):
    state: dict[str, Any]


class UsageResponse(ControlModel):
    usage: dict[str, Any]


class ErrorBody(ControlModel):
    error: str
    message: str
    details: dict[str, Any]
