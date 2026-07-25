from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, StatusCode, TraceFlags, set_span_in_context

from foldos.core.types import Event, Scope
from foldos.otel.config import OtelProviders, build_providers
from foldos.otel.emitter import Emitter


def _make_providers() -> tuple[OtelProviders, InMemorySpanExporter, InMemoryLogRecordExporter, InMemoryMetricReader]:
    span_exporter = InMemorySpanExporter()
    log_exporter = InMemoryLogRecordExporter()
    metric_reader = InMemoryMetricReader()
    providers = build_providers(
        span_exporter=span_exporter,
        log_exporter=log_exporter,
        metric_reader=metric_reader,
    )
    return providers, span_exporter, log_exporter, metric_reader


def _points(metrics_data: Any, name: str) -> list[Any]:
    return [
        point
        for resource_metrics in metrics_data.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def _event(kind: str, payload: dict[str, Any], *, seq: int = 7, actor: str = "agent") -> Event:
    return Event(kind=kind, payload=payload, id=f"event-{seq}", seq=seq, hash=f"hash-{seq}", actor=actor)


def test_every_event_has_exactly_one_contract_log_and_no_noncontract_span() -> None:
    providers, span_exporter, log_exporter, _ = _make_providers()
    emitter = Emitter(Scope("tenant", "agent", "session"), providers)
    event = _event("message", {"role": "user", "content": "hello"})
    emitter.emit_event(event)
    log = log_exporter.get_finished_logs()[0].log_record
    assert len(log_exporter.get_finished_logs()) == 1
    assert span_exporter.get_finished_spans() == ()
    assert log.body == "message seq=7 hello"
    assert log.severity_number.name == "INFO"
    assert log.attributes == {
        "foldos.tenant": "tenant",
        "foldos.agent": "agent",
        "foldos.session": "session",
        "foldos.thread": "main",
        "foldos.stream": "tenant/agent/session/main",
        "foldos.seq": 7,
        "foldos.event_kind": "message",
        "foldos.actor": "agent",
        "foldos.payload": json.dumps(event.payload, separators=(",", ":")),
    }


def test_log_payload_is_json_truncated_at_4000_and_errors_are_error_severity() -> None:
    providers, _, log_exporter, _ = _make_providers()
    emitter = Emitter(Scope("tenant", "agent", "session"), providers)
    event = _event("tool_call", {"name": "search", "error": "boom", "result": "x" * 5000})
    emitter.emit_event(event)
    log = log_exporter.get_finished_logs()[0].log_record
    assert len(log.attributes["foldos.payload"]) == 4000
    assert log.severity_number.name == "ERROR"


def test_only_contract_events_create_exact_spans_and_attributes() -> None:
    providers, span_exporter, _, _ = _make_providers()
    emitter = Emitter(Scope("tenant", "agent", "session"), providers)
    tool = _event("tool_call", {"name": "search", "latency_ms": 12.5, "error": "boom"}, seq=1)
    llm = _event(
        "llm_call",
        {"model": "llama3.2", "input_tokens": 3, "output_tokens": 5, "cost_usd": None, "latency_ms": 20},
        seq=2,
    )
    emitter.emit_event(tool)
    emitter.emit_event(llm)
    spans = span_exporter.get_finished_spans()
    assert [span.name for span in spans] == ["foldos.tool.search", "foldos.llm.llama3.2"]
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].status.description == "boom"
    assert spans[0].attributes["tool.name"] == "search"
    assert spans[0].attributes["tool.latency_ms"] == 12.5
    assert spans[0].attributes["foldos.error"] == "boom"
    assert spans[1].attributes["gen_ai.request.model"] == "llama3.2"
    assert spans[1].attributes["gen_ai.usage.input_tokens"] == 3
    assert spans[1].attributes["gen_ai.usage.output_tokens"] == 5
    assert spans[1].attributes["gen_ai.usage.cost_usd"] == 0.0
    assert spans[1].attributes["foldos.uncosted"] is True
    assert all(span.attributes["foldos.stream"] == "tenant/agent/session/main" for span in spans)


def test_span_start_is_held_until_matching_end_and_has_exact_attributes() -> None:
    providers, span_exporter, log_exporter, _ = _make_providers()
    emitter = Emitter(Scope("tenant", "agent", "session"), providers)
    start = _event("span_start", {"span_id": "logical-1", "name": "work", "attrs": {"count": 2}}, seq=3, actor="trace")
    end = _event(
        "span_end",
        {"span_id": "logical-1", "status": "error", "error": "failed", "duration_ms": 42},
        seq=4,
        actor="trace",
    )
    emitter.emit_event(start)
    assert span_exporter.get_finished_spans() == ()
    emitter.emit_event(end)
    span = span_exporter.get_finished_spans()[0]
    assert span.name == "foldos.work"
    assert span.attributes["foldos.span_id"] == "logical-1"
    assert span.attributes["foldos.duration_ms"] == 42.0
    assert span.attributes["foldos.attr.count"] == "2"
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "failed"
    assert len(log_exporter.get_finished_logs()) == 2


def test_indexes_are_bidirectional_by_stream_and_sequence() -> None:
    providers, span_exporter, _, _ = _make_providers()
    emitter = Emitter(Scope("tenant", "agent", "session"), providers)
    event = _event("tool_call", {"name": "search"})
    emitter.emit_event(event)
    context = span_exporter.get_finished_spans()[0].context
    anchor = ("tenant/agent/session/main", 7)
    trace_span = (f"{context.trace_id:032x}", f"{context.span_id:016x}")
    assert emitter.forward_index[anchor] == trace_span
    assert emitter.inverse_index[trace_span] == anchor


def test_exact_contract_metrics_include_chain_head_gauges() -> None:
    providers, _, _, metric_reader = _make_providers()
    emitter = Emitter(Scope("tenant", "agent", "session"), providers)
    emitter.emit_event(
        _event("llm_call", {"model": "m", "input_tokens": 3, "output_tokens": 5, "cost_usd": 1.25, "latency_ms": 6})
    )
    emitter.emit_event(_event("tool_call", {"name": "search", "error": "no"}, seq=8))
    metrics = metric_reader.get_metrics_data()
    names = {
        metric.name
        for resource_metrics in metrics.resource_metrics
        for scope_metrics in resource_metrics.scope_metrics
        for metric in scope_metrics.metrics
    }
    assert names == {
        "foldos.llm.calls",
        "foldos.llm.input_tokens",
        "foldos.llm.output_tokens",
        "foldos.llm.cost_usd",
        "foldos.llm.latency_ms",
        "foldos.tool.calls",
        "foldos.chain.head_seq",
        "foldos.chain.head_hash",
    }
    assert _points(metrics, "foldos.llm.cost_usd")[0].value == 1.25
    assert _points(metrics, "foldos.tool.calls")[0].attributes["error"] == "true"
    assert _points(metrics, "foldos.chain.head_seq")[0].value == 8
    head_hash = _points(metrics, "foldos.chain.head_hash")[0]
    assert head_hash.value == 1
    assert head_hash.attributes["hash"] == "hash-8"


def test_policy_violation_emits_only_contract_span_and_counter() -> None:
    providers, span_exporter, log_exporter, metric_reader = _make_providers()
    emitter = Emitter(Scope("tenant", "agent", "session"), providers)
    emitter.note_policy_violation("budget_usd", 0.5, 0.7, "budget exceeded")
    span = span_exporter.get_finished_spans()[0]
    assert span.name == "foldos.policy.violation"
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "budget exceeded"
    assert span.attributes["foldos.policy.key"] == "budget_usd"
    assert span.attributes["foldos.policy.limit"] == "0.5"
    assert span.attributes["foldos.policy.observed"] == "0.7"
    assert span.attributes["foldos.reason"] == "budget exceeded"
    assert log_exporter.get_finished_logs() == ()
    point = _points(metric_reader.get_metrics_data(), "foldos.policy.violations")[0]
    assert point.value == 1
    assert point.attributes == {"tenant": "tenant", "agent": "agent", "session": "session", "policy": "budget_usd"}


def test_orphans_are_ended_with_error_without_creating_noncontract_metrics() -> None:
    providers, span_exporter, _, metric_reader = _make_providers()
    emitter = Emitter(Scope("tenant", "agent", "session"), providers)
    emitter.emit_event(_event("span_start", {"span_id": "logical-1", "name": "work"}))
    time.sleep(0.001)
    emitter.sweep_orphans(max_age_seconds=0)
    span = span_exporter.get_finished_spans()[0]
    assert span.status.status_code == StatusCode.ERROR
    assert span.status.description == "orphan span swept"
    assert _points(metric_reader.get_metrics_data(), "foldos.chain.head_seq")[0].value == 7


def test_caller_context_is_used_for_log_and_synthesized_span() -> None:
    providers, span_exporter, log_exporter, _ = _make_providers()
    emitter = Emitter(Scope("tenant", "agent", "session"), providers)
    parent = SpanContext(0x11111111111111111111111111111111, 0x2222222222222222, False, TraceFlags(1))
    context = set_span_in_context(NonRecordingSpan(parent))
    emitter.emit_event(_event("tool_call", {"name": "search"}), context=context)
    span = span_exporter.get_finished_spans()[0]
    log = log_exporter.get_finished_logs()[0].log_record
    assert span.parent == parent
    assert log.trace_id == parent.trace_id
    assert log.span_id == parent.span_id


def test_event_is_not_mutated_and_concurrent_emission_is_safe() -> None:
    providers, span_exporter, _, _ = _make_providers()
    emitter = Emitter(Scope("tenant", "agent", "session"), providers)
    event = _event("llm_call", {"model": "m", "input_tokens": 1, "output_tokens": 1})
    original = copy.deepcopy(event)
    threads = [threading.Thread(target=emitter.emit_event, args=(event,)) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert event == original
    assert len(span_exporter.get_finished_spans()) == 10
