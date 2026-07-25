"""StateStoreExporter — the deliverable, in its smallest honest form.

Wraps any statefold StateStore and delegates every Protocol method. On append()
it turns statefold events into OpenTelemetry spans and metrics. It never mutates
an event, so the hash chain still verifies afterwards.

One integration point catches everything: SessionState, AgentStateDb/SyncBridge
and statefold.instrument all funnel through store.append().
"""
from __future__ import annotations

import time
from typing import AsyncIterator

from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

from statefold.types import Event, Scope

_MS = 1_000_000  # ns per ms


class OtelStateStore:
    """StateStore decorator that emits OTel telemetry as events are appended."""

    def __init__(self, inner, tracer=None, meter=None):
        self._inner = inner
        self._tracer = tracer or trace.get_tracer("foldos.statefold")
        m = meter or metrics.get_meter("foldos.statefold")
        self._c_llm = m.create_counter("statefold.llm.calls")
        self._c_in = m.create_counter("statefold.llm.input_tokens")
        self._c_out = m.create_counter("statefold.llm.output_tokens")
        self._c_cost = m.create_counter("statefold.llm.cost_usd")
        self._h_lat = m.create_histogram("statefold.llm.latency_ms")
        self._c_tool = m.create_counter("statefold.tool.calls")
        self._c_viol = m.create_counter("statefold.invariant.violations")

        self._open: dict[str, trace.Span] = {}      # statefold span_id -> live OTel span
        self.index: dict[tuple[str, int], tuple[str, str]] = {}  # (stream, seq) -> ids

    # --- the one interesting method -----------------------------------------

    async def append(self, scope: Scope, events: list[Event], expected_seq: int) -> int:
        # Capture ambient context on the CALLER's thread, before anything awaits.
        ambient = trace.set_span_in_context(trace.get_current_span())
        stamped = [(e, time.time_ns()) for e in events]
        head = await self._inner.append(scope, events, expected_seq=expected_seq)
        for ev, now in stamped:
            try:
                self._emit(scope, ev, now, ambient)
            except Exception:  # telemetry must never break the write path
                pass
        return head

    def _parent_ctx(self, ev: Event, ambient):
        """A statefold span wins over the ambient OTel span, if we have it live."""
        if ev.causation_id and ev.causation_id in self._open:
            return trace.set_span_in_context(self._open[ev.causation_id])
        return ambient

    def _record(self, scope: Scope, ev: Event, span: trace.Span) -> None:
        sc = span.get_span_context()
        self.index[(scope.flatten(), ev.seq)] = (
            format(sc.trace_id, "032x"), format(sc.span_id, "016x"))

    def _attrs(self, scope: Scope, ev: Event) -> dict:
        return {"statefold.tenant": scope.tenant, "statefold.agent": scope.agent,
                "statefold.session": scope.session, "statefold.thread": scope.thread,
                "statefold.stream": scope.flatten(), "statefold.seq": ev.seq,
                "statefold.event_kind": ev.kind, "statefold.actor": ev.actor}

    def _emit(self, scope: Scope, ev: Event, now: int, ambient) -> None:
        p = ev.payload
        base = self._attrs(scope, ev)
        tags = {"tenant": scope.tenant, "agent": scope.agent, "session": scope.session}

        if ev.kind == "span_start":
            sid = p["span_id"]
            parent = (trace.set_span_in_context(self._open[p["parent_id"]])
                      if p.get("parent_id") in self._open else ambient)
            span = self._tracer.start_span(
                f"statefold.{p['name']}", context=parent, start_time=now,
                attributes={**base, "statefold.span_id": sid,
                            **{f"statefold.attr.{k}": str(v) for k, v in (p.get("attrs") or {}).items()}})
            self._open[sid] = span
            self._record(scope, ev, span)

        elif ev.kind == "span_end":
            span = self._open.pop(p["span_id"], None)
            if span is None:
                return
            if p.get("status") == "error":
                span.set_status(Status(StatusCode.ERROR, p.get("error") or ""))
                span.set_attribute("statefold.error", p.get("error") or "")
            span.set_attribute("statefold.duration_ms", p.get("duration_ms") or 0.0)
            span.end(end_time=now)

        elif ev.kind == "tool_call":
            lat = float(p.get("latency_ms") or 0.0)
            span = self._tracer.start_span(
                f"statefold.tool.{p.get('name','unknown')}", context=self._parent_ctx(ev, ambient),
                start_time=now - int(lat * _MS),
                attributes={**base, "tool.name": p.get("name"), "tool.latency_ms": lat})
            if p.get("error"):
                span.set_status(Status(StatusCode.ERROR, str(p["error"])))
            span.end(end_time=now)
            self._record(scope, ev, span)
            self._c_tool.add(1, {**tags, "tool": str(p.get("name")),
                                 "error": str(bool(p.get("error")))})

        elif ev.kind == "llm_call":
            lat = float(p.get("latency_ms") or 0.0)
            model = p.get("model", "unknown")
            itok, otok = int(p.get("input_tokens", 0)), int(p.get("output_tokens", 0))
            cost = p.get("cost_usd")
            if cost is None:
                from statefold.telemetry import _cost
                cost = _cost(model, itok, otok)
            span = self._tracer.start_span(
                f"statefold.llm.{model}", context=self._parent_ctx(ev, ambient),
                start_time=now - int(lat * _MS),
                attributes={**base, "gen_ai.request.model": model,
                            "gen_ai.usage.input_tokens": itok,
                            "gen_ai.usage.output_tokens": otok,
                            "gen_ai.usage.cost_usd": cost or 0.0,
                            "statefold.uncosted": cost is None})
            span.end(end_time=now)
            self._record(scope, ev, span)
            mt = {**tags, "model": model}
            self._c_llm.add(1, mt); self._c_in.add(itok, mt); self._c_out.add(otok, mt)
            if cost is not None:
                self._c_cost.add(cost, mt)
            if lat:
                self._h_lat.record(lat, mt)

    def note_violation(self, scope: Scope, reason: str, ambient=None) -> None:
        """Called when an invariant vetoes a write — nothing was persisted."""
        span = self._tracer.start_span(
            "statefold.invariant.violation",
            attributes={"statefold.stream": scope.flatten(), "statefold.reason": reason})
        span.set_status(Status(StatusCode.ERROR, reason))
        span.end()
        self._c_viol.add(1, {"tenant": scope.tenant, "agent": scope.agent,
                             "session": scope.session})

    # --- pure delegation -----------------------------------------------------

    async def head(self, scope: Scope) -> int:
        return await self._inner.head(scope)

    async def get_state(self, scope: Scope, as_of: int | None = None) -> dict:
        return await self._inner.get_state(scope, as_of=as_of)

    def read_events(self, scope: Scope, after: int = 0) -> AsyncIterator[Event]:
        return self._inner.read_events(scope, after=after)

    async def checkpoint(self, scope: Scope, label: str | None = None) -> str:
        return await self._inner.checkpoint(scope, label=label)

    async def fork(self, scope: Scope, at_seq: int, new_thread: str) -> Scope:
        return await self._inner.fork(scope, at_seq, new_thread)

    def __getattr__(self, name):  # memory API (remember/recall/forget/...)
        return getattr(self._inner, name)
