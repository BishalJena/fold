from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any

from opentelemetry._logs import Logger, SeverityNumber
from opentelemetry.context import Context, get_current
from opentelemetry.metrics import CallbackOptions, Counter, Histogram, Meter, Observation
from opentelemetry.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Tracer,
    format_span_id,
    format_trace_id,
    set_span_in_context,
)

from foldos.core.types import Event, Scope
from foldos.otel.config import OtelProviders


@dataclass
class _OpenSpan:
    span: Span
    start_ns: int
    started_at: float


class Emitter:
    def __init__(self, scope: Scope, providers: OtelProviders) -> None:
        self._scope = scope
        self._providers = providers
        self._tracer: Tracer = providers.tracer_provider.get_tracer("foldos.emitter")
        self._meter: Meter = providers.meter_provider.get_meter("foldos.emitter")
        self._logger: Logger = providers.logger_provider.get_logger("foldos.emitter")
        self._lock = threading.RLock()
        self._open_spans: dict[str, _OpenSpan] = {}
        self._forward: dict[tuple[str, int], tuple[str, str]] = {}
        self._inverse: dict[tuple[str, str], tuple[str, int]] = {}
        self._error_count = 0
        self._head_seq = 0
        self._head_hash = ""
        self._llm_calls: Counter = self._meter.create_counter("foldos.llm.calls", unit="1")
        self._llm_input_tokens: Counter = self._meter.create_counter("foldos.llm.input_tokens", unit="{token}")
        self._llm_output_tokens: Counter = self._meter.create_counter("foldos.llm.output_tokens", unit="{token}")
        self._llm_cost: Counter = self._meter.create_counter("foldos.llm.cost_usd", unit="USD")
        self._llm_latency: Histogram = self._meter.create_histogram("foldos.llm.latency_ms", unit="ms")
        self._tool_calls: Counter = self._meter.create_counter("foldos.tool.calls", unit="1")
        self._policy_violations: Counter = self._meter.create_counter("foldos.policy.violations", unit="1")
        self._meter.create_observable_gauge("foldos.chain.head_seq", callbacks=[self._observe_head_seq], unit="1")
        self._meter.create_observable_gauge("foldos.chain.head_hash", callbacks=[self._observe_head_hash], unit="1")

    @property
    def error_count(self) -> int:
        with self._lock:
            return self._error_count

    @property
    def forward_index(self) -> dict[tuple[str, int], tuple[str, str]]:
        with self._lock:
            return dict(self._forward)

    @property
    def inverse_index(self) -> dict[tuple[str, str], tuple[str, int]]:
        with self._lock:
            return dict(self._inverse)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self._providers.force_flush(timeout_millis=timeout_millis)
        except Exception:
            self._inc_error()
            return False

    def shutdown(self) -> None:
        self.sweep_orphans()
        try:
            self._providers.shutdown()
        except Exception:
            self._inc_error()

    def emit_event(self, event: Event, context: Context | None = None) -> None:
        ctx = context if context is not None else get_current()
        try:
            payload = json.dumps(event.payload, default=str, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            self._inc_error()
            payload = "{}"
        attributes = self._base_attributes(event)
        self._emit_span(event, attributes, ctx)
        self._emit_log(event, payload, attributes, ctx)
        self._emit_metrics(event)
        self._update_head(event)

    def note_policy_violation(
        self,
        policy: str,
        limit: Any = None,
        observed: Any = None,
        reason: str | None = None,
        context: Context | None = None,
    ) -> None:
        ctx = context if context is not None else get_current()
        message = reason if reason is not None else policy
        attributes = self._policy_attributes(policy, limit, observed, message)
        try:
            span = self._tracer.start_span(
                "foldos.policy.violation",
                context=ctx,
                kind=SpanKind.INTERNAL,
                attributes=attributes,
            )
            span.set_status(Status(StatusCode.ERROR, message))
            span.end()
        except Exception:
            self._inc_error()
        try:
            self._policy_violations.add(1, self._policy_metric_attributes(policy))
        except Exception:
            self._inc_error()

    def sweep_orphans(self, max_age_seconds: float = 0.0) -> None:
        expired: list[_OpenSpan] = []
        now = time.monotonic()
        with self._lock:
            for logical_id, open_span in list(self._open_spans.items()):
                if now - open_span.started_at >= max_age_seconds:
                    expired.append(open_span)
                    del self._open_spans[logical_id]
        for open_span in expired:
            try:
                open_span.span.set_status(Status(StatusCode.ERROR, "orphan span swept"))
                open_span.span.end()
            except Exception:
                self._inc_error()

    def _inc_error(self) -> None:
        with self._lock:
            self._error_count += 1

    def _base_attributes(self, event: Event) -> dict[str, Any]:
        return {
            "foldos.tenant": self._scope.tenant,
            "foldos.agent": self._scope.agent,
            "foldos.session": self._scope.session,
            "foldos.thread": self._scope.thread,
            "foldos.stream": self._scope.key(),
            "foldos.seq": event.seq,
            "foldos.event_kind": event.kind,
            "foldos.actor": event.actor,
        }

    def _metric_attributes(self) -> dict[str, str]:
        return {
            "tenant": self._scope.tenant,
            "agent": self._scope.agent,
            "session": self._scope.session,
        }

    def _policy_metric_attributes(self, policy: str) -> dict[str, str]:
        return {**self._metric_attributes(), "policy": policy}

    def _policy_attributes(self, policy: str, limit: Any, observed: Any, reason: str) -> dict[str, Any]:
        return {
            "foldos.tenant": self._scope.tenant,
            "foldos.agent": self._scope.agent,
            "foldos.session": self._scope.session,
            "foldos.thread": self._scope.thread,
            "foldos.stream": self._scope.key(),
            "foldos.seq": 0,
            "foldos.event_kind": "policy_violation",
            "foldos.actor": "policy",
            "foldos.policy.key": policy,
            "foldos.policy.limit": str(limit),
            "foldos.policy.observed": str(observed),
            "foldos.reason": reason,
        }

    def _emit_span(self, event: Event, attributes: dict[str, Any], context: Context) -> None:
        try:
            if event.kind == "span_start":
                self._start_span(event, attributes, context)
            elif event.kind == "span_end":
                self._end_span(event)
            elif event.kind in {"tool_call", "llm_call"}:
                self._emit_synthetic_span(event, attributes, context)
        except Exception:
            self._inc_error()

    def _start_span(self, event: Event, attributes: dict[str, Any], context: Context) -> None:
        logical_id = str(event.payload["span_id"])
        name = str(event.payload["name"])
        span_attributes = {
            **attributes,
            "foldos.span_id": logical_id,
        }
        for key, value in event.payload.get("attrs", {}).items():
            span_attributes[f"foldos.attr.{key}"] = str(value)
        parent_id = event.payload.get("parent_id")
        with self._lock:
            parent = self._open_spans.get(str(parent_id)) if parent_id is not None else None
        parent_context = set_span_in_context(parent.span) if parent is not None else context
        start_ns = time.time_ns()
        span = self._tracer.start_span(
            f"foldos.{name}",
            context=parent_context,
            kind=SpanKind.INTERNAL,
            attributes=span_attributes,
            start_time=start_ns,
        )
        self._index(event, span)
        with self._lock:
            self._open_spans[logical_id] = _OpenSpan(span, start_ns, time.monotonic())

    def _end_span(self, event: Event) -> None:
        logical_id = str(event.payload.get("span_id", ""))
        with self._lock:
            open_span = self._open_spans.pop(logical_id, None)
        if open_span is None:
            return
        duration = float(event.payload.get("duration_ms", 0.0))
        open_span.span.set_attribute("foldos.duration_ms", duration)
        error = event.payload.get("error")
        if event.payload.get("status") == "error":
            open_span.span.set_status(Status(StatusCode.ERROR, str(error or "error")))
        else:
            open_span.span.set_status(Status(StatusCode.OK))
        open_span.span.end(end_time=open_span.start_ns + int(duration * 1_000_000))

    def _emit_synthetic_span(self, event: Event, attributes: dict[str, Any], context: Context) -> None:
        payload = event.payload
        if event.kind == "tool_call":
            name = str(payload["name"])
            span_name = f"foldos.tool.{name}"
            span_attributes = {
                **attributes,
                "tool.name": name,
                "tool.latency_ms": float(payload.get("latency_ms") or 0.0),
            }
            if payload.get("error") is not None:
                span_attributes["foldos.error"] = str(payload["error"])
        else:
            model = str(payload["model"])
            cost = payload.get("cost_usd")
            span_name = f"foldos.llm.{model}"
            span_attributes = {
                **attributes,
                "gen_ai.request.model": model,
                "gen_ai.usage.input_tokens": int(payload["input_tokens"]),
                "gen_ai.usage.output_tokens": int(payload["output_tokens"]),
                "gen_ai.usage.cost_usd": float(cost or 0.0),
                "foldos.uncosted": cost is None,
            }
        start = time.time_ns()
        span = self._tracer.start_span(
            span_name, context=context, kind=SpanKind.INTERNAL, attributes=span_attributes, start_time=start
        )
        self._index(event, span)
        error = payload.get("error")
        if error is not None:
            span.set_status(Status(StatusCode.ERROR, str(error)))
        else:
            span.set_status(Status(StatusCode.OK))
        latency = payload.get("latency_ms")
        end = start + int(float(latency) * 1_000_000) if latency is not None else None
        span.end(end_time=end)

    def _index(self, event: Event, span: Span) -> None:
        context = span.get_span_context()
        anchor = (self._scope.key(), event.seq)
        trace_span = (format_trace_id(context.trace_id), format_span_id(context.span_id))
        with self._lock:
            self._forward[anchor] = trace_span
            self._inverse[trace_span] = anchor

    def _emit_log(self, event: Event, payload: str, attributes: dict[str, Any], context: Context) -> None:
        try:
            error = event.payload.get("error")
            is_error = error is not None or event.payload.get("status") == "error"
            severity = SeverityNumber.ERROR if is_error else SeverityNumber.INFO
            self._logger.emit(
                body=f"{event.kind} seq={event.seq} {self._summary(event)}",
                severity_number=severity,
                severity_text="ERROR" if is_error else "INFO",
                attributes={**attributes, "foldos.payload": payload[:4000]},
                context=context,
            )
        except Exception:
            self._inc_error()

    def _summary(self, event: Event) -> str:
        payload = event.payload
        if event.kind == "message":
            return str(payload.get("content", ""))
        if event.kind in {"tool_call", "llm_call"}:
            return str(payload.get("name", payload.get("model", "")))
        if event.kind in {"span_start", "span_end"}:
            return str(payload.get("name", payload.get("span_id", "")))
        return event.kind

    def _emit_metrics(self, event: Event) -> None:
        try:
            if event.kind == "llm_call":
                self._emit_llm_metrics(event)
            elif event.kind == "tool_call":
                self._emit_tool_metrics(event)
        except Exception:
            self._inc_error()

    def _emit_llm_metrics(self, event: Event) -> None:
        payload = event.payload
        attributes = {**self._metric_attributes(), "model": str(payload["model"])}
        self._llm_calls.add(1, attributes)
        self._llm_input_tokens.add(int(payload["input_tokens"]), attributes)
        self._llm_output_tokens.add(int(payload["output_tokens"]), attributes)
        if payload.get("cost_usd") is not None:
            self._llm_cost.add(float(payload["cost_usd"]), attributes)
        if payload.get("latency_ms") is not None:
            self._llm_latency.record(float(payload["latency_ms"]), attributes)

    def _emit_tool_metrics(self, event: Event) -> None:
        payload = event.payload
        attributes = {
            **self._metric_attributes(),
            "tool": str(payload["name"]),
            "error": str(payload.get("error") is not None).lower(),
        }
        self._tool_calls.add(1, attributes)

    def _update_head(self, event: Event) -> None:
        with self._lock:
            if event.seq >= self._head_seq:
                self._head_seq = event.seq
                self._head_hash = event.hash

    def _observe_head_seq(self, options: CallbackOptions) -> list[Observation]:
        del options
        with self._lock:
            return [Observation(self._head_seq, self._metric_attributes())]

    def _observe_head_hash(self, options: CallbackOptions) -> list[Observation]:
        del options
        with self._lock:
            if not self._head_hash:
                return []
            return [Observation(1, {**self._metric_attributes(), "hash": self._head_hash})]
