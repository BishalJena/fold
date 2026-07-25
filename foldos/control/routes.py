from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from foldos.control.models import (
    AttestationResponse,
    BacktestRequest,
    BacktestResponse,
    BranchRequest,
    BranchResponse,
    CounterfactualRequest,
    CounterfactualResponse,
    CutResponse,
    ErrorBody,
    EventBody,
    EventListResponse,
    MessageRequest,
    MessageResponse,
    PolicyRequest,
    PolicyResponse,
    ReplayRequest,
    ReplayResponse,
    ScopeSelector,
    TraceIndexEntry,
    TraceIndexExactResponse,
    TraceIndexInexactResponse,
    VerificationResponse,
)
from foldos.control.scopes import AmbiguousScopeError, ScopeResolver
from foldos.core.chain import verify
from foldos.core.clock import cut as clock_cut
from foldos.core.counterfactual import counterfactual
from foldos.core.policy import backtest
from foldos.core.store import MemoryStore
from foldos.core.types import ConcurrencyError, Event, PolicyViolation

ReplayRunner = Callable[..., Coroutine[Any, Any, dict[str, Any]]]


class Emitter(Protocol):
    @property
    def forward_index(self) -> dict[tuple[str, int], tuple[str, str]]: ...

    @property
    def inverse_index(self) -> dict[tuple[str, str], tuple[str, int]]: ...

    def emit_event(self, event: Event) -> None: ...


def _error_response(
    status: int,
    error: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body = ErrorBody(error=error, message=message, details=details or {})
    return JSONResponse(status_code=status, content=body.model_dump())


def _inexact_trace() -> dict[str, Any]:
    return TraceIndexInexactResponse(
        stream=None, seq=None, exact=False, at=None, cut=None
    ).model_dump()


def _scope_selector(
    session: str,
    tenant: str | None,
    agent: str | None,
    thread: str | None,
) -> ScopeSelector:
    data: dict[str, Any] = {"session": session}
    if tenant is not None:
        data["tenant"] = tenant
    if agent is not None:
        data["agent"] = agent
    if thread is not None:
        data["thread"] = thread
    return ScopeSelector.model_validate(data)


def _source_selector_from_branch_or_counterfactual(
    req: BranchRequest | CounterfactualRequest,
) -> ScopeSelector:
    data: dict[str, Any] = {"session": req.session}
    if "tenant" in req.model_fields_set:
        data["tenant"] = req.tenant
    if "agent" in req.model_fields_set:
        data["agent"] = req.agent
    return ScopeSelector.model_validate(data)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request
        return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})


def create_router(
    store: MemoryStore,
    emitter: Emitter | None = None,
    replay_runner: ReplayRunner | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/foldos")
    resolver = ScopeResolver(store)

    @router.get("/cut", response_model=None)
    async def cut_route(
        session: str | None = None,
        at: str | None = None,
    ) -> Response:
        if not session:
            return _error_response(422, "validation_error", "session is required")
        at_str = at if at else datetime.now(UTC).isoformat()
        try:
            scopes = await store.scopes(session)
            result = await clock_cut(store, scopes, at_str)
        except ValueError as exc:
            return _error_response(422, "validation_error", str(exc))
        return JSONResponse(content=CutResponse(at=at_str, cut=result).model_dump())

    @router.get("/state/{session}", response_model=None)
    async def state_route(
        session: str,
        as_of: int | None = None,
        tenant: str | None = None,
        agent: str | None = None,
        thread: str | None = None,
    ) -> Response:
        try:
            selector = _scope_selector(session, tenant, agent, thread)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            scope = await resolver.resolve(selector)
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        state = await store.get_state(scope, as_of=as_of)
        return JSONResponse(content=state)

    @router.get("/events/{session}/stream", response_model=None)
    async def events_sse_route(
        request: Request,
        session: str,
        after: int = 0,
        follow: bool = True,
        tenant: str | None = None,
        agent: str | None = None,
        thread: str | None = None,
    ) -> Response:
        try:
            selector = _scope_selector(session, tenant, agent, thread)
            scope = await resolver.resolve(selector)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )

        last_event_id = request.headers.get("Last-Event-ID")
        if last_event_id is not None:
            try:
                after = max(after, int(last_event_id))
            except ValueError:
                pass

        async def sse_generator() -> AsyncGenerator[bytes, None]:
            cursor = after
            while True:
                if await request.is_disconnected():
                    break
                events = await store.read_events(scope, after=cursor)
                for e in events:
                    data = EventBody(
                        kind=e.kind,
                        payload=e.payload,
                        actor=e.actor,
                        causation_id=e.causation_id,
                        id=e.id,
                        ts=e.ts,
                        seq=e.seq,
                        hash=e.hash,
                    ).model_dump_json()
                    yield f"id: {e.seq}\nevent: foldos.event\ndata: {data}\n\n".encode()
                    cursor = e.seq
                if not follow:
                    break
                await asyncio.sleep(0.1)

        return StreamingResponse(sse_generator(), media_type="text/event-stream")

    @router.get("/events/{session}", response_model=None)
    async def events_route(
        session: str,
        after: int = 0,
        tenant: str | None = None,
        agent: str | None = None,
        thread: str | None = None,
    ) -> Response:
        try:
            selector = _scope_selector(session, tenant, agent, thread)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            scope = await resolver.resolve(selector)
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        events = await store.read_events(scope, after=after)
        head = await store.head(scope)
        event_bodies = [
            EventBody(
                kind=e.kind,
                payload=e.payload,
                actor=e.actor,
                causation_id=e.causation_id,
                id=e.id,
                ts=e.ts,
                seq=e.seq,
                hash=e.hash,
            )
            for e in events
        ]
        return JSONResponse(
            content=EventListResponse(stream=scope.key(), head=head, events=event_bodies).model_dump()
        )

    @router.get("/usage/{session}", response_model=None)
    async def usage_route(
        session: str,
        as_of: int | None = None,
        tenant: str | None = None,
        agent: str | None = None,
        thread: str | None = None,
    ) -> Response:
        try:
            selector = _scope_selector(session, tenant, agent, thread)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            scope = await resolver.resolve(selector)
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        state = await store.get_state(scope, as_of=as_of)
        return JSONResponse(content=state["usage"])

    @router.get("/verify/{session}", response_model=None)
    async def verify_route(
        session: str,
        tenant: str | None = None,
        agent: str | None = None,
        thread: str | None = None,
    ) -> Response:
        try:
            selector = _scope_selector(session, tenant, agent, thread)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            scope = await resolver.resolve(selector)
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        result = await verify(store, scope)
        return JSONResponse(content=VerificationResponse.model_validate(result).model_dump())

    @router.get("/attestation/{session}", response_model=None)
    async def attestation_route(
        session: str,
        tenant: str | None = None,
        agent: str | None = None,
        thread: str | None = None,
    ) -> Response:
        try:
            selector = _scope_selector(session, tenant, agent, thread)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            scope = await resolver.resolve(selector)
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        verification = await verify(store, scope)
        if not verification["ok"]:
            return _error_response(
                500,
                "chain_broken",
                f"chain broken at seq {verification['broken_at']}",
                {"broken_at": verification["broken_at"]},
            )
        events = await store.read_events(scope)
        head_seq = events[-1].seq if events else 0
        head_hash = events[-1].hash if events else ""
        return JSONResponse(
            content=AttestationResponse(
                stream=scope.key(),
                events=len(events),
                head_seq=head_seq,
                head_hash=head_hash,
                verified_at=datetime.now(UTC).isoformat(),
                ok=True,
            ).model_dump()
        )

    @router.get("/trace-index", response_model=None)
    async def trace_index_route(
        session: str | None = None,
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> Response:
        if session is not None and trace_id is None and span_id is None:
            if emitter is None:
                return JSONResponse(content=[])
            scopes = await store.scopes(session)
            stream_set = {scope.key() for scope in scopes}
            entries: list[dict[str, Any]] = []
            for (stream, seq), (tid, sid) in emitter.forward_index.items():
                if stream in stream_set:
                    entries.append(
                        TraceIndexEntry(
                            stream=stream, seq=seq, trace_id=tid, span_id=sid
                        ).model_dump()
                    )
            entries.sort(key=lambda e: (e["stream"], e["seq"]))
            return JSONResponse(content=entries)

        if trace_id is not None and span_id is not None:
            if emitter is None:
                return JSONResponse(content=_inexact_trace())
            anchor = emitter.inverse_index.get((trace_id, span_id))
            if anchor is None:
                return JSONResponse(content=_inexact_trace())
            stream, seq = anchor
            scopes = await store.scopes()
            target_scope = next((s for s in scopes if s.key() == stream), None)
            if target_scope is None:
                return JSONResponse(content=_inexact_trace())
            events = await store.read_events(target_scope)
            event = next((e for e in events if e.seq == seq), None)
            if event is None:
                return JSONResponse(content=_inexact_trace())
            try:
                cut_result = await clock_cut(store, scopes, event.ts)
            except ValueError as exc:
                return _error_response(422, "validation_error", str(exc))
            exact = TraceIndexExactResponse(
                stream=stream,
                seq=seq,
                exact=True,
                at=event.ts,
                cut=cut_result,
            )
            return JSONResponse(content=exact.model_dump())

        return _error_response(
            422, "validation_error", "provide session, or both trace_id and span_id"
        )

    @router.post("/policy", response_model=None)
    async def policy_route(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _error_response(422, "validation_error", "invalid JSON body")
        if not isinstance(body, dict):
            return _error_response(422, "validation_error", "invalid JSON body")
        try:
            req = PolicyRequest.model_validate(body)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            scope = await resolver.resolve(req)
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        event = Event(
            kind="policy_set",
            payload={"key": req.key, "value": req.value, "reason": req.reason},
            actor="policy",
        )
        head_before = await store.head(scope)
        try:
            new_head = await store.append(scope, [event], head_before)
        except ConcurrencyError as exc:
            return _error_response(
                409, "concurrency_error", str(exc), {"expected": exc.expected, "actual": exc.actual}
            )
        except PolicyViolation as exc:
            return _error_response(422, "policy_violation", exc.reason, {})
        stored_events = await store.read_events(scope, after=new_head - 1)
        event_id = stored_events[0].id if stored_events else event.id
        if emitter is not None:
            emitter.emit_event(stored_events[0] if stored_events else event)
        return JSONResponse(content=PolicyResponse(seq=new_head, event_id=event_id).model_dump())

    @router.post("/message", response_model=None)
    async def message_route(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _error_response(422, "validation_error", "invalid JSON body")
        if not isinstance(body, dict):
            return _error_response(422, "validation_error", "invalid JSON body")
        try:
            req = MessageRequest.model_validate(body)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            scope = await resolver.resolve(req)
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        event = Event(
            kind="message",
            payload={"role": req.role, "content": req.content},
            actor=req.role,
        )
        head_before = await store.head(scope)
        try:
            new_head = await store.append(scope, [event], head_before)
        except ConcurrencyError as exc:
            return _error_response(
                409, "concurrency_error", str(exc), {"expected": exc.expected, "actual": exc.actual}
            )
        except PolicyViolation as exc:
            return _error_response(422, "policy_violation", exc.reason, {})
        stored_events = await store.read_events(scope, after=new_head - 1)
        event_id = stored_events[0].id if stored_events else event.id
        if emitter is not None:
            emitter.emit_event(stored_events[0] if stored_events else event)
        return JSONResponse(content=MessageResponse(seq=new_head, event_id=event_id).model_dump())

    @router.post("/backtest", response_model=None)
    async def backtest_route(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _error_response(422, "validation_error", "invalid JSON body")
        if not isinstance(body, dict):
            return _error_response(422, "validation_error", "invalid JSON body")
        try:
            req = BacktestRequest.model_validate(body)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            scope = await resolver.resolve(req)
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        result = await backtest(store, scope, {"key": req.policy.key, "value": req.policy.value})
        return JSONResponse(content=BacktestResponse.model_validate(result).model_dump())

    @router.post("/branch", response_model=None)
    async def branch_route(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _error_response(422, "validation_error", "invalid JSON body")
        if not isinstance(body, dict):
            return _error_response(422, "validation_error", "invalid JSON body")
        try:
            req = BranchRequest.model_validate(body)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            source = await resolver.resolve(_source_selector_from_branch_or_counterfactual(req))
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        new_thread = req.thread
        try:
            new_scope = await store.fork(source, req.at_seq, new_thread)
        except ValueError as exc:
            return _error_response(422, "validation_error", str(exc), {})
        return JSONResponse(content=BranchResponse(scope=new_scope.key(), from_seq=req.at_seq).model_dump())

    @router.post("/counterfactual", response_model=None)
    async def counterfactual_route(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return _error_response(422, "validation_error", "invalid JSON body")
        if not isinstance(body, dict):
            return _error_response(422, "validation_error", "invalid JSON body")
        try:
            req = CounterfactualRequest.model_validate(body)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            source = await resolver.resolve(_source_selector_from_branch_or_counterfactual(req))
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        new_thread = req.thread
        try:
            result = await counterfactual(store, source, req.event_seq, new_thread, req.payload_overrides)
        except ValueError as exc:
            return _error_response(422, "validation_error", str(exc), {})
        except ConcurrencyError as exc:
            return _error_response(
                409, "concurrency_error", str(exc), {"expected": exc.expected, "actual": exc.actual}
            )
        except PolicyViolation as exc:
            return _error_response(422, "policy_violation", exc.reason, {})
        return JSONResponse(
            content=CounterfactualResponse(
                original_scope=result["original_scope"].key(),
                branch_scope=result["branch_scope"].key(),
                replaced_seq=result["replaced_seq"],
                original_state=cast(dict[str, Any], result["original_state"]),
                branch_state=cast(dict[str, Any], result["branch_state"]),
            ).model_dump()
        )

    @router.post("/replay", response_model=None)
    async def replay_route(request: Request) -> Response:
        if replay_runner is None:
            return _error_response(422, "validation_error", "replay_runner not configured", {})
        try:
            body = await request.json()
        except Exception:
            return _error_response(422, "validation_error", "invalid JSON body")
        if not isinstance(body, dict):
            return _error_response(422, "validation_error", "invalid JSON body")
        try:
            req = ReplayRequest.model_validate(body)
        except ValidationError as exc:
            return _error_response(422, "validation_error", str(exc), {"errors": exc.errors()})
        try:
            scope = await resolver.resolve(req)
        except AmbiguousScopeError as exc:
            return _error_response(
                409, "ambiguous_scope", str(exc), {"available": list(exc.available)}
            )
        result = await replay_runner(store=store, scope=scope, prompt=req.prompt, thread=req.thread)
        return JSONResponse(content=ReplayResponse.model_validate(result).model_dump())

    return router
