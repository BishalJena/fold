from __future__ import annotations

import copy
from collections.abc import Iterator

import pytest

from foldos.core import usage
from foldos.core.reducers import fold, fresh_state
from foldos.core.types import Event


@pytest.fixture(autouse=True)
def _reset_pricing() -> Iterator[None]:
    usage.reset_pricing()
    yield


def test_fresh_state_has_exact_empty_shape() -> None:
    assert fresh_state() == {
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


def test_fresh_state_returned_shape_is_independent() -> None:
    first = fresh_state()
    second = fresh_state()
    first["messages"].append({"role": "x", "content": "y", "seq": 1})
    assert second["messages"] == []


def test_fold_does_not_mutate_inputs() -> None:
    payload = {"role": "user", "content": "hello"}
    events = [Event(kind="message", payload=payload, seq=1)]
    original = copy.deepcopy(events)
    fold(events)
    assert events == original
    assert events[0].payload == payload


def test_message_reducer_appends_message() -> None:
    event = Event(kind="message", payload={"role": "assistant", "content": "ok"}, seq=1)
    state = fold([event])
    assert state["messages"] == [{"role": "assistant", "content": "ok", "seq": 1}]


def test_message_ignores_extra_meta() -> None:
    event = Event(kind="message", payload={"role": "user", "content": "hello", "tags": ["a"]}, seq=2)
    state = fold([event])
    assert state["messages"] == [{"role": "user", "content": "hello", "seq": 2}]


def test_tool_call_appends_and_tracks_usage() -> None:
    event = Event(
        kind="tool_call",
        payload={"name": "search", "args": {"q": "x"}, "result": {"ok": True}, "latency_ms": 12.5, "error": None},
        seq=1,
    )
    state = fold([event])
    assert state["tools"] == [
        {"name": "search", "args": {"q": "x"}, "result": {"ok": True}, "latency_ms": 12.5, "error": None, "seq": 1}
    ]
    assert state["usage"]["tools"]["search"] == {"calls": 1, "errors": 0, "latency_ms_total": 12.5}


def test_tool_call_with_error_increments_error_count() -> None:
    event = Event(
        kind="tool_call",
        payload={"name": "search", "args": {}, "result": None, "latency_ms": 5.0, "error": "boom"},
        seq=1,
    )
    state = fold([event])
    assert state["usage"]["tools"]["search"]["errors"] == 1


def test_multiple_tool_calls_aggregate() -> None:
    events = [
        Event(
            kind="tool_call",
            payload={"name": "a", "args": {}, "result": None, "latency_ms": 10.0, "error": None},
            seq=1,
        ),
        Event(
            kind="tool_call",
            payload={"name": "a", "args": {}, "result": None, "latency_ms": 20.0, "error": "fail"},
            seq=2,
        ),
        Event(
            kind="tool_call", payload={"name": "b", "args": {}, "result": None, "latency_ms": 5.0, "error": None}, seq=3
        ),
    ]
    state = fold(events)
    assert state["usage"]["tools"]["a"] == {"calls": 2, "errors": 1, "latency_ms_total": 30.0}
    assert state["usage"]["tools"]["b"] == {"calls": 1, "errors": 0, "latency_ms_total": 5.0}


def test_tool_call_does_not_mutate_payload() -> None:
    payload = {"name": "t", "args": {"list": [1]}, "result": {"list": [2]}, "latency_ms": 1.0, "error": None}
    event = Event(kind="tool_call", payload=payload, seq=1)
    state = fold([event])
    state["tools"][0]["args"]["list"].append(3)
    state["tools"][0]["result"]["list"].append(4)
    assert payload["args"]["list"] == [1]
    assert payload["result"]["list"] == [2]


def test_llm_call_computes_cost_and_usage() -> None:
    usage.register_pricing("llama3.2", 3.0, 15.0)
    event = Event(
        kind="llm_call",
        payload={
            "model": "llama3.2",
            "input_tokens": 1_000_000,
            "output_tokens": 2_000_000,
            "latency_ms": 100.0,
            "cost_usd": None,
        },
        seq=1,
    )
    state = fold([event])
    assert state["usage"]["llm_calls"] == 1
    assert state["usage"]["input_tokens"] == 1_000_000
    assert state["usage"]["output_tokens"] == 2_000_000
    assert state["usage"]["cost_usd"] == 33.0
    assert state["usage"]["uncosted_calls"] == 0
    assert state["usage"]["by_model"]["llama3.2"] == {
        "calls": 1,
        "input_tokens": 1_000_000,
        "output_tokens": 2_000_000,
        "cost_usd": 33.0,
        "latency_ms_total": 100.0,
    }


def test_llm_call_explicit_cost_overrides_registry() -> None:
    usage.register_pricing("llama3.2", 3.0, 15.0)
    event = Event(
        kind="llm_call",
        payload={
            "model": "llama3.2",
            "input_tokens": 1_000_000,
            "output_tokens": 1_000_000,
            "latency_ms": 50.0,
            "cost_usd": 9.0,
        },
        seq=1,
    )
    state = fold([event])
    assert state["usage"]["cost_usd"] == 9.0
    assert state["usage"]["by_model"]["llama3.2"]["cost_usd"] == 9.0


def test_llm_call_without_pricing_increments_uncosted() -> None:
    event = Event(
        kind="llm_call",
        payload={"model": "unknown", "input_tokens": 100, "output_tokens": 50, "latency_ms": None, "cost_usd": None},
        seq=1,
    )
    state = fold([event])
    assert state["usage"]["llm_calls"] == 1
    assert state["usage"]["cost_usd"] == 0.0
    assert state["usage"]["uncosted_calls"] == 1
    assert state["usage"]["by_model"]["unknown"] == {
        "calls": 1,
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.0,
        "latency_ms_total": 0.0,
    }


def test_multiple_llm_calls_aggregate_by_model_and_total() -> None:
    usage.register_pricing("m1", 1.0, 2.0)
    events = [
        Event(
            kind="llm_call",
            payload={
                "model": "m1",
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "latency_ms": 10.0,
                "cost_usd": None,
            },
            seq=1,
        ),
        Event(
            kind="llm_call",
            payload={
                "model": "m1",
                "input_tokens": 0,
                "output_tokens": 1_000_000,
                "latency_ms": 20.0,
                "cost_usd": None,
            },
            seq=2,
        ),
        Event(
            kind="llm_call",
            payload={"model": "m2", "input_tokens": 100, "output_tokens": 50, "latency_ms": 5.0, "cost_usd": None},
            seq=3,
        ),
    ]
    state = fold(events)
    assert state["usage"]["cost_usd"] == 3.0
    assert state["usage"]["uncosted_calls"] == 1
    assert state["usage"]["by_model"]["m1"] == {
        "calls": 2,
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cost_usd": 3.0,
        "latency_ms_total": 30.0,
    }
    assert state["usage"]["by_model"]["m2"]["calls"] == 1


def test_span_start_opens_span() -> None:
    event = Event(
        kind="span_start",
        payload={"span_id": "s1", "parent_id": "p1", "name": "work", "attrs": {"x": 1}},
        seq=1,
    )
    state = fold([event])
    assert state["spans"]["s1"] == {
        "name": "work",
        "parent_id": "p1",
        "attrs": {"x": 1},
        "status": "open",
        "duration_ms": None,
    }


def test_span_end_closes_matching_span() -> None:
    start = Event(kind="span_start", payload={"span_id": "s1", "parent_id": None, "name": "work", "attrs": {}}, seq=1)
    end = Event(kind="span_end", payload={"span_id": "s1", "status": "ok", "error": None, "duration_ms": 42.0}, seq=2)
    state = fold([start, end])
    assert state["spans"]["s1"]["status"] == "ok"
    assert state["spans"]["s1"]["duration_ms"] == 42.0


def test_span_end_records_error_status() -> None:
    start = Event(kind="span_start", payload={"span_id": "s1", "parent_id": None, "name": "work", "attrs": {}}, seq=1)
    end = Event(
        kind="span_end", payload={"span_id": "s1", "status": "error", "error": "boom", "duration_ms": 10.0}, seq=2
    )
    state = fold([start, end])
    assert state["spans"]["s1"]["status"] == "error"


def test_span_end_without_match_is_noop() -> None:
    end = Event(
        kind="span_end", payload={"span_id": "missing", "status": "ok", "error": None, "duration_ms": 1.0}, seq=1
    )
    state = fold([end])
    assert state["spans"] == {}


def test_state_delta_sets_values() -> None:
    event = Event(kind="state_delta", payload={"set": {"key1": "value1", "key2": 2}}, seq=1)
    state = fold([event])
    assert state["data"] == {"key1": "value1", "key2": 2}


def test_state_delta_unsets_values() -> None:
    events = [
        Event(kind="state_delta", payload={"set": {"a": 1, "b": 2}}, seq=1),
        Event(kind="state_delta", payload={"unset": ["a"]}, seq=2),
    ]
    state = fold(events)
    assert state["data"] == {"b": 2}


def test_state_delta_set_and_unset_in_same_event() -> None:
    event = Event(kind="state_delta", payload={"set": {"a": 1}, "unset": ["a"]}, seq=1)
    state = fold([event])
    assert state["data"] == {}


def test_state_delta_does_not_mutate_input_payload() -> None:
    payload = {"set": {"a": ["one"]}}
    event = Event(kind="state_delta", payload=payload, seq=1)
    state = fold([event])
    assert event.payload == payload
    state["data"]["a"].append("two")
    assert payload["set"]["a"] == ["one"]


def test_policy_set_stores_value_in_data() -> None:
    event = Event(kind="policy_set", payload={"key": "budget_usd", "value": 0.5, "reason": "limit"}, seq=1)
    state = fold([event])
    assert state["data"] == {"budget_usd": 0.5}


def test_agno_session_is_noop() -> None:
    event = Event(kind="agno_session", payload={"session": {}, "type": "agent"}, seq=1)
    state = fold([event])
    assert state == fresh_state()


def test_unknown_kind_is_noop() -> None:
    event = Event(kind="custom", payload={"x": 1}, seq=1)
    state = fold([event])
    assert state == fresh_state()


def test_apply_event_advances_a_copy_of_existing_state() -> None:
    from foldos.core.reducers import apply_event

    state = fold([Event(kind="state_delta", payload={"set": {"a": 1}}, seq=1)])
    advanced = apply_event(state, Event(kind="state_delta", payload={"set": {"b": 2}}, seq=2))

    assert state["data"] == {"a": 1}
    assert advanced["data"] == {"a": 1, "b": 2}


def test_fold_is_deterministic_and_pure() -> None:
    events = [
        Event(kind="message", payload={"role": "user", "content": "a"}, seq=1),
        Event(kind="message", payload={"role": "assistant", "content": "b"}, seq=2),
    ]
    first = fold(events[:1])
    full = fold(events)
    assert first == fold(events[:1])
    assert full["messages"] == [
        {"role": "user", "content": "a", "seq": 1},
        {"role": "assistant", "content": "b", "seq": 2},
    ]
