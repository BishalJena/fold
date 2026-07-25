from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any, TypedDict

from foldos.core import usage
from foldos.core.types import Event


class Message(TypedDict):
    role: str
    content: str
    seq: int


class ToolRecord(TypedDict):
    name: str
    args: dict[str, Any]
    result: Any
    latency_ms: float | None
    error: str | None
    seq: int


class Span(TypedDict):
    name: str
    parent_id: str | None
    attrs: dict[str, Any]
    status: str
    duration_ms: float | None


class UsageByModel(TypedDict):
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms_total: float


class ToolUsage(TypedDict):
    calls: int
    errors: int
    latency_ms_total: float


class Usage(TypedDict):
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    uncosted_calls: int
    by_model: dict[str, UsageByModel]
    tools: dict[str, ToolUsage]


class State(TypedDict):
    messages: list[Message]
    tools: list[ToolRecord]
    spans: dict[str, Span]
    data: dict[str, Any]
    usage: Usage


def fresh_state() -> State:
    return {
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


def fold(events: Iterable[Event]) -> State:
    state = fresh_state()
    for event in events:
        _apply(state, event)
    return state


def apply_event(state: State, event: Event) -> State:
    advanced = copy.deepcopy(state)
    _apply(advanced, event)
    return advanced


def _apply(state: State, event: Event) -> None:
    kind = event.kind
    payload = event.payload
    if kind == "message":
        message_role: str = payload["role"]
        message_content: str = payload["content"]
        state["messages"].append({"role": message_role, "content": message_content, "seq": event.seq})
    elif kind == "tool_call":
        tool_name: str = payload["name"]
        tool_args: dict[str, Any] = payload.get("args", {})
        tool_result: Any = payload.get("result")
        tool_latency_ms: float | None = payload.get("latency_ms")
        tool_error: str | None = payload.get("error")
        record: ToolRecord = {
            "name": tool_name,
            "args": copy.deepcopy(tool_args),
            "result": copy.deepcopy(tool_result),
            "latency_ms": tool_latency_ms,
            "error": tool_error,
            "seq": event.seq,
        }
        state["tools"].append(record)
        tools_usage = state["usage"]["tools"]
        if tool_name not in tools_usage:
            tools_usage[tool_name] = {"calls": 0, "errors": 0, "latency_ms_total": 0.0}
        tool_usage = tools_usage[tool_name]
        tool_usage["calls"] += 1
        if tool_error is not None:
            tool_usage["errors"] += 1
        if tool_latency_ms is not None:
            tool_usage["latency_ms_total"] += tool_latency_ms
    elif kind == "llm_call":
        llm_model: str = payload["model"]
        llm_input_tokens: int = payload["input_tokens"]
        llm_output_tokens: int = payload["output_tokens"]
        llm_latency_ms: float | None = payload.get("latency_ms")
        llm_explicit_cost: float | None = payload.get("cost_usd")
        resolved_cost = usage.cost(llm_model, llm_input_tokens, llm_output_tokens, llm_explicit_cost)
        usage_state = state["usage"]
        usage_state["llm_calls"] += 1
        usage_state["input_tokens"] += llm_input_tokens
        usage_state["output_tokens"] += llm_output_tokens
        if resolved_cost is None:
            usage_state["uncosted_calls"] += 1
        else:
            usage_state["cost_usd"] += resolved_cost
        by_model = usage_state["by_model"]
        if llm_model not in by_model:
            by_model[llm_model] = {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "latency_ms_total": 0.0,
            }
        model_usage = by_model[llm_model]
        model_usage["calls"] += 1
        model_usage["input_tokens"] += llm_input_tokens
        model_usage["output_tokens"] += llm_output_tokens
        if resolved_cost is not None:
            model_usage["cost_usd"] += resolved_cost
        if llm_latency_ms is not None:
            model_usage["latency_ms_total"] += llm_latency_ms
    elif kind == "span_start":
        start_span_id: str = payload["span_id"]
        start_name: str = payload["name"]
        start_parent_id: str | None = payload.get("parent_id")
        start_attrs: dict[str, Any] = payload.get("attrs", {})
        state["spans"][start_span_id] = {
            "name": start_name,
            "parent_id": start_parent_id,
            "attrs": copy.deepcopy(start_attrs),
            "status": "open",
            "duration_ms": None,
        }
    elif kind == "span_end":
        end_span_id: str = payload["span_id"]
        span = state["spans"].get(end_span_id)
        if span is not None:
            end_status: str = payload["status"]
            end_duration_ms: float = payload["duration_ms"]
            span["status"] = end_status
            span["duration_ms"] = end_duration_ms
    elif kind == "state_delta":
        set_values: dict[str, Any] = payload.get("set", {})
        unset_keys: list[str] = payload.get("unset", [])
        for set_key, set_value in set_values.items():
            state["data"][set_key] = copy.deepcopy(set_value)
        for unset_key in unset_keys:
            state["data"].pop(unset_key, None)
    elif kind == "policy_set":
        policy_key: str = payload["key"]
        policy_value: Any = payload["value"]
        state["data"][policy_key] = copy.deepcopy(policy_value)
    elif kind == "agno_session":
        pass
    else:
        pass
