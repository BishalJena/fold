from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from foldos.control.routes import create_router, register_exception_handlers
from foldos.core.store import MemoryStore
from foldos.core.types import Event, Scope


def _scope() -> Scope:
    return Scope("acme", "analyst", "run-1", "main")


def _app(store: MemoryStore, **kwargs: Any) -> FastAPI:
    application = FastAPI()
    register_exception_handlers(application)
    router = create_router(store, **kwargs)
    application.include_router(router)
    return application


async def _client(app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _seeded_store() -> MemoryStore:
    store = MemoryStore()
    scope = _scope()
    events = [
        Event(
            kind="message",
            payload={"role": "user", "content": "hello"},
            actor="user",
            ts="2026-01-01T00:00:00+00:00",
        ),
        Event(
            kind="llm_call",
            payload={"model": "gpt-4", "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
            actor="llm:gpt-4",
            ts="2026-01-01T00:00:01+00:00",
        ),
    ]
    await store.append(scope, events, 0)
    return store


@pytest.mark.asyncio
async def test_cut_returns_at_and_cut() -> None:
    store = MemoryStore()
    scope = _scope()
    await store.append(
        scope,
        [
            Event(
                kind="message",
                payload={"role": "user", "content": "hi"},
                actor="user",
                ts="2026-01-01T00:00:00+00:00",
            ),
        ],
        0,
    )
    client = await _client(_app(store))
    resp = await client.get("/foldos/cut", params={"session": "run-1"})
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert "at" in data
    assert "cut" in data
    assert isinstance(data["cut"], dict)
    assert data["cut"]["acme/analyst/run-1/main"] == 1


@pytest.mark.asyncio
async def test_cut_with_at_query() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.get("/foldos/cut", params={"session": "run-1", "at": "2026-01-01T00:00:00+00:00"})
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["cut"]["acme/analyst/run-1/main"] == 1


@pytest.mark.asyncio
async def test_cut_missing_session_returns_422() -> None:
    store = MemoryStore()
    client = await _client(_app(store))
    resp = await client.get("/foldos/cut")
    await client.aclose()
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"] == "validation_error"
    assert "message" in data
    assert "details" in data


@pytest.mark.asyncio
async def test_state_returns_state_directly() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.get("/foldos/state/run-1")
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert "state" not in data
    assert "messages" in data
    assert "usage" in data
    assert data["messages"][0]["content"] == "hello"
    assert data["usage"]["llm_calls"] == 1


@pytest.mark.asyncio
async def test_state_as_of_filters() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.get("/foldos/state/run-1", params={"as_of": 1})
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["messages"]) == 1
    assert data["usage"]["llm_calls"] == 0


@pytest.mark.asyncio
async def test_events_returns_list() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.get("/foldos/events/run-1")
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["stream"] == "acme/analyst/run-1/main"
    assert data["head"] == 2
    assert len(data["events"]) == 2
    assert data["events"][0]["kind"] == "message"
    assert "id" in data["events"][0]
    assert "seq" in data["events"][0]
    assert "hash" in data["events"][0]


@pytest.mark.asyncio
async def test_events_after_filters() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.get("/foldos/events/run-1", params={"after": 1})
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["events"]) == 1
    assert data["events"][0]["seq"] == 2


@pytest.mark.asyncio
async def test_events_sse_streams_and_cancels() -> None:
    store = await _seeded_store()
    app = _app(store)
    transport = ASGITransport(app=app)

    async def _read() -> list[str]:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream("GET", "/foldos/events/run-1/stream", params={"follow": "false"}) as resp:
                assert resp.status_code == 200
                lines: list[str] = []
                async for line in resp.aiter_lines():
                    lines.append(line)
                    data_lines = [line for line in lines if line.startswith("data:")]
                    if len(data_lines) >= 2:
                        return lines
        return []

    lines = await asyncio.wait_for(_read(), timeout=5.0)
    data_lines = [line for line in lines if line.startswith("data:")]
    assert len(data_lines) >= 2
    for i, line in enumerate(lines):
        if line.startswith("data:"):
            assert lines[i - 1] == "event: foldos.event"
    assert any(line.startswith("id: 1") for line in lines)
    assert any(line.startswith("id: 2") for line in lines)


@pytest.mark.asyncio
async def test_events_sse_last_event_id() -> None:
    store = await _seeded_store()
    app = _app(store)
    transport = ASGITransport(app=app)

    async def _read() -> list[str]:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            async with client.stream(
                "GET",
                "/foldos/events/run-1/stream",
                params={"follow": "false"},
                headers={"Last-Event-ID": "1"},
            ) as resp:
                assert resp.status_code == 200
                lines: list[str] = []
                async for line in resp.aiter_lines():
                    lines.append(line)
                    data_lines = [line for line in lines if line.startswith("data:")]
                    if len(data_lines) >= 1:
                        return data_lines
        return []

    data_lines = await asyncio.wait_for(_read(), timeout=5.0)
    assert len(data_lines) >= 1
    parsed = json.loads(data_lines[0].removeprefix("data:").strip())
    assert parsed["seq"] == 2


@pytest.mark.asyncio
async def test_usage_returns_usage_directly() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.get("/foldos/usage/run-1")
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert "usage" not in data
    assert data["llm_calls"] == 1
    assert data["cost_usd"] == 0.01


@pytest.mark.asyncio
async def test_usage_as_of_filters() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.get("/foldos/usage/run-1", params={"as_of": 1})
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["llm_calls"] == 0
    assert data["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_verify_ok() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.get("/foldos/verify/run-1")
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["broken_at"] is None
    assert data["events"] == 2


@pytest.mark.asyncio
async def test_attestation_ok() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.get("/foldos/attestation/run-1")
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["stream"] == "acme/analyst/run-1/main"
    assert data["events"] == 2
    assert data["head_seq"] == 2
    assert "head_hash" in data
    assert "verified_at" in data


@pytest.mark.asyncio
async def test_policy_appends_and_returns_seq() -> None:
    store = MemoryStore()
    scope = _scope()
    await store.append(
        scope,
        [
            Event(
                kind="message",
                payload={"role": "user", "content": "hi"},
                actor="user",
                ts="2026-01-01T00:00:00+00:00",
            ),
        ],
        0,
    )
    client = await _client(_app(store))
    resp = await client.post(
        "/foldos/policy",
        json={"session": "run-1", "key": "budget_usd", "value": 1.0},
    )
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["seq"] == 2
    assert "event_id" in data


@pytest.mark.asyncio
async def test_policy_emits_normative_event_with_reason() -> None:
    store = MemoryStore()
    scope = _scope()
    await store.append(
        scope,
        [
            Event(
                kind="message",
                payload={"role": "user", "content": "hi"},
                actor="user",
                ts="2026-01-01T00:00:00+00:00",
            ),
        ],
        0,
    )

    class FakeEmitter:
        def __init__(self) -> None:
            self.emitted: list[Event] = []

        def emit_event(self, event: Event) -> None:
            self.emitted.append(event)

    fake_emitter = FakeEmitter()
    app = _app(store, emitter=fake_emitter)
    client = await _client(app)
    resp = await client.post(
        "/foldos/policy",
        json={"session": "run-1", "key": "budget_usd", "value": 1.0, "reason": "cap"},
    )
    await client.aclose()
    assert resp.status_code == 200
    assert len(fake_emitter.emitted) == 1
    emitted = fake_emitter.emitted[0]
    assert emitted.kind == "policy_set"
    assert emitted.actor == "policy"
    assert emitted.payload == {"key": "budget_usd", "value": 1.0, "reason": "cap"}


@pytest.mark.asyncio
async def test_policy_violation_returns_422_and_leaves_head() -> None:
    from foldos.core.policy import clear_invariants, invariant

    clear_invariants()

    @invariant("policy_set")
    def reject_all(state: Any, event: Event) -> None:
        from foldos.core.types import PolicyViolation

        raise PolicyViolation("denied")

    store = MemoryStore()
    scope = _scope()
    await store.append(
        scope,
        [
            Event(
                kind="message",
                payload={"role": "user", "content": "hi"},
                actor="user",
                ts="2026-01-01T00:00:00+00:00",
            ),
        ],
        0,
    )
    app = _app(store)
    client = await _client(app)
    head_before = await store.head(scope)
    resp = await client.post(
        "/foldos/policy",
        json={"session": "run-1", "key": "budget_usd", "value": 0.5},
    )
    head_after = await store.head(scope)
    await client.aclose()
    assert resp.status_code == 422
    assert resp.json()["error"] == "policy_violation"
    assert head_after == head_before
    clear_invariants()


@pytest.mark.asyncio
async def test_message_appends_and_returns() -> None:
    store = MemoryStore()
    client = await _client(_app(store))
    resp = await client.post(
        "/foldos/message",
        json={"session": "run-1", "role": "user", "content": "hello"},
    )
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["seq"] == 1
    assert data["event_id"]


@pytest.mark.asyncio
async def test_backtest_read_only() -> None:
    store = await _seeded_store()
    scope = _scope()
    head_before = await store.head(scope)
    client = await _client(_app(store))
    resp = await client.post(
        "/foldos/backtest",
        json={"session": "run-1", "policy": {"key": "budget_usd", "value": 0.005}},
    )
    head_after = await store.head(scope)
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["evaluated"] == 2
    assert len(data["violations"]) >= 1
    assert head_after == head_before


@pytest.mark.asyncio
async def test_branch_creates_fork_using_req_thread() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.post(
        "/foldos/branch",
        json={"session": "run-1", "at_seq": 1, "thread": "new-thread"},
    )
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["scope"] == "acme/analyst/run-1/new-thread"
    assert data["from_seq"] == 1


@pytest.mark.asyncio
async def test_branch_invalid_seq() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.post(
        "/foldos/branch",
        json={"session": "run-1", "at_seq": 100, "thread": "new-thread"},
    )
    await client.aclose()
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_counterfactual_creates_branch_using_req_thread() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.post(
        "/foldos/counterfactual",
        json={
            "session": "run-1",
            "event_seq": 1,
            "thread": "cf-thread",
            "payload_overrides": {"content": "bye"},
        },
    )
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["replaced_seq"] == 1
    assert data["original_scope"] == "acme/analyst/run-1/main"
    assert data["branch_scope"] == "acme/analyst/run-1/cf-thread"
    assert "original_state" in data
    assert "branch_state" in data
    assert data["branch_state"]["messages"][0]["content"] == "bye"


@pytest.mark.asyncio
async def test_counterfactual_invalid_seq() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.post(
        "/foldos/counterfactual",
        json={"session": "run-1", "event_seq": 100, "thread": "cf-thread", "payload_overrides": {}},
    )
    await client.aclose()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_replay_without_runner_returns_422() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.post("/foldos/replay", json={"session": "run-1", "thread": "main", "prompt": "continue"})
    await client.aclose()
    assert resp.status_code == 422
    assert resp.json()["error"] == "validation_error"


@pytest.mark.asyncio
async def test_replay_with_runner() -> None:
    store = await _seeded_store()
    captured: dict[str, Any] = {}

    async def fake_runner(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "scope": "acme/analyst/run-1/main",
            "agno_session_id": "run-1--main",
            "run_id": "run-1",
            "trace_id": "trace-abc",
            "replay_of": "acme/analyst/run-1/main",
            "replay_at_seq": 2,
        }

    app = _app(store, replay_runner=fake_runner)
    client = await _client(app)
    resp = await client.post(
        "/foldos/replay",
        json={"session": "run-1", "thread": "main", "prompt": "continue"},
    )
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["trace_id"] == "trace-abc"
    assert data["replay_at_seq"] == 2
    assert captured.get("thread") == "main"


@pytest.mark.asyncio
async def test_trace_index_list_without_emitter() -> None:
    store = await _seeded_store()
    client = await _client(_app(store))
    resp = await client.get("/foldos/trace-index", params={"session": "run-1"})
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data == []


@pytest.mark.asyncio
async def test_trace_index_list_with_emitter() -> None:
    store = await _seeded_store()
    stream = "acme/analyst/run-1/main"

    class FakeEmitter:
        @property
        def forward_index(self) -> dict[tuple[str, int], tuple[str, str]]:
            return {(stream, 1): ("trace-abc", "span-abc")}

        @property
        def inverse_index(self) -> dict[tuple[str, str], tuple[str, int]]:
            return {("trace-abc", "span-abc"): (stream, 1)}

    app = _app(store, emitter=FakeEmitter())
    client = await _client(app)
    resp = await client.get("/foldos/trace-index", params={"session": "run-1"})
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0] == {"stream": stream, "seq": 1, "trace_id": "trace-abc", "span_id": "span-abc"}


@pytest.mark.asyncio
async def test_trace_index_exact_with_emitter() -> None:
    store = await _seeded_store()
    scope = _scope()
    events = await store.read_events(scope)
    event = events[0]
    stream = scope.key()

    class FakeEmitter:
        @property
        def forward_index(self) -> dict[tuple[str, int], tuple[str, str]]:
            return {(stream, 1): ("trace-abc", "span-abc")}

        @property
        def inverse_index(self) -> dict[tuple[str, str], tuple[str, int]]:
            return {("trace-abc", "span-abc"): (stream, 1)}

    app = _app(store, emitter=FakeEmitter())
    client = await _client(app)
    resp = await client.get("/foldos/trace-index", params={"trace_id": "trace-abc", "span_id": "span-abc"})
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["exact"] is True
    assert data["stream"] == stream
    assert data["seq"] == 1
    assert data["at"] == event.ts
    assert data["cut"] == {stream: 1}


@pytest.mark.asyncio
async def test_trace_index_inexact_with_emitter() -> None:
    store = await _seeded_store()

    class FakeEmitter:
        @property
        def forward_index(self) -> dict[tuple[str, int], tuple[str, str]]:
            return {}

        @property
        def inverse_index(self) -> dict[tuple[str, str], tuple[str, int]]:
            return {}

    app = _app(store, emitter=FakeEmitter())
    client = await _client(app)
    resp = await client.get("/foldos/trace-index", params={"trace_id": "x", "span_id": "y"})
    await client.aclose()
    assert resp.status_code == 200
    data = resp.json()
    assert data["exact"] is False
    assert data["stream"] is None
    assert data["seq"] is None
    assert data["at"] is None
    assert data["cut"] is None


@pytest.mark.asyncio
async def test_ambiguous_scope_returns_409() -> None:
    store = MemoryStore()
    scope_a = Scope("tenant-a", "agent", "run-1", "one")
    scope_b = Scope("tenant-b", "agent", "run-1", "two")
    await store.append(
        scope_a,
        [Event(kind="state_delta", payload={"set": {"x": 1}}, ts="2026-01-01T00:00:00+00:00")],
        0,
    )
    await store.append(
        scope_b,
        [Event(kind="state_delta", payload={"set": {"x": 2}}, ts="2026-01-01T00:00:00+00:00")],
        0,
    )
    client = await _client(_app(store))
    resp = await client.get("/foldos/state/run-1")
    await client.aclose()
    assert resp.status_code == 409
    data = resp.json()
    assert data["error"] == "ambiguous_scope"
    assert "available" in data["details"]


@pytest.mark.asyncio
async def test_policy_creates_scope_on_first_append() -> None:
    store = MemoryStore()
    client = await _client(_app(store))
    resp = await client.post(
        "/foldos/policy",
        json={"session": "run-1", "key": "budget_usd", "value": 1.0},
    )
    await client.aclose()
    assert resp.status_code == 200
    assert resp.json()["seq"] == 1
