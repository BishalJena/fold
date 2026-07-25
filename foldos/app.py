"""FoldOS FastAPI application factory."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from agno.agent.agent import Agent
from agno.db.base import SessionType
from agno.models.openai import OpenAILike
from agno.os import AgentOS
from agno.os.settings import AgnoAPISettings
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry.sdk.resources import Resource

from foldos.control.routes import create_router, register_exception_handlers
from foldos.core import usage
from foldos.core.session import Session
from foldos.core.store import MemoryStore
from foldos.core.types import Event, Scope
from foldos.db import FoldosDb
from foldos.otel.config import build_providers
from foldos.otel.emitter import Emitter
from foldos.otel.instrument_openai import instrument_openai
from foldos.otel.traced_tool import traced_tool

load_dotenv(Path(__file__).parent.parent / ".env")

_FOLDOS_AGENT_ID = os.environ.get("FOLDOS_AGENT", "analyst")
_current_foldos_session: ContextVar[Session | None] = ContextVar("current_foldos_session", default=None)


def _load_env() -> dict[str, str]:
    return {
        "tenant": os.environ.get("FOLDOS_TENANT", "acme"),
        "agent": os.environ.get("FOLDOS_AGENT", "analyst"),
        "otlp_endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
    }


def _current_session_source() -> Session | None:
    return _current_foldos_session.get()


def create_app() -> FastAPI:
    env = _load_env()
    tenant = env["tenant"]
    agent = env["agent"]

    # Register illustrative pricing for the local Ollama model so budget
    # governance and backtesting produce meaningful results in demos.
    usage.register_pricing("llama3.2", 0.10, 0.20)

    resource = Resource.create({
        "service.name": "foldos",
        "foldos.tenant": tenant,
        "foldos.agent": agent,
    })
    providers = build_providers(resource=resource)

    emitters: dict[str, Emitter] = {}

    def event_sink(scope: Scope, event: Event) -> None:
        key = scope.key()
        emitter = emitters.get(key)
        if emitter is None:
            emitter = Emitter(scope, providers)
            emitters[key] = emitter
        emitter.emit_event(event)

    class _MultiEmitter:
        """Aggregates forward/inverse indexes from all per-scope emitters."""

        @property
        def forward_index(self) -> dict[tuple[str, int], tuple[str, str]]:
            merged: dict[tuple[str, int], tuple[str, str]] = {}
            for emitter in emitters.values():
                merged.update(emitter.forward_index)
            return merged

        @property
        def inverse_index(self) -> dict[tuple[str, str], tuple[str, int]]:
            merged: dict[tuple[str, str], tuple[str, int]] = {}
            for emitter in emitters.values():
                merged.update(emitter.inverse_index)
            return merged

        def emit_event(self, event: Event) -> None:
            pass

    store = MemoryStore(event_sink=event_sink)
    trace_index = _MultiEmitter()
    foldos_db = FoldosDb(store, tenant=tenant)

    async def _set_session_hook(agent: Any, session: Any, **kwargs: Any) -> None:
        session_id = getattr(session, "session_id", None)
        agent_id = getattr(session, "agent_id", None) or getattr(agent, "agent_id", None)
        if session_id is not None and agent_id is not None:
            scope = Scope(tenant, str(agent_id), str(session_id), "main")
            _current_foldos_session.set(Session(store, scope))

    async def _reset_session_hook(**kwargs: Any) -> None:
        _current_foldos_session.set(None)

    @traced_tool(session=_current_session_source)
    def get_foldos_status() -> str:
        """Return the FoldOS control plane status."""
        return "FoldOS is operational"

    model = OpenAILike(
        id="llama3.2",
        api_key="ollama",
        base_url="http://localhost:11434/v1",
    )

    agent_instance = Agent(
        id=_FOLDOS_AGENT_ID,
        name="FoldOS Agent",
        model=model,
        db=foldos_db,
        tools=[get_foldos_status],
        pre_hooks=[_set_session_hook],
        post_hooks=[_reset_session_hook],
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        yield
        providers.shutdown()
        foldos_db.close()

    application = FastAPI(
        title="FoldOS",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    register_exception_handlers(application)
    router = create_router(store, emitter=trace_index)
    application.include_router(router)

    static_dir = Path(__file__).parent / "console" / "static"
    application.mount("/console/static", StaticFiles(directory=str(static_dir)), name="console_static")

    @application.get("/console")
    async def console_root() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @application.get("/sessions/{session_id}/runs")
    async def get_session_runs(
        session_id: str,
        type: str | None = None,  # noqa: ARG001
        db_id: str | None = None,  # noqa: ARG001
    ) -> JSONResponse:
        """Return session history as "runs" for the Agno Agent UI."""
        session = foldos_db.get_session(session_id, session_type=SessionType.AGENT)
        if session is None:
            return JSONResponse([])
        if hasattr(session, "get_chat_history"):
            history = cast(list[Any], session.get_chat_history())
        else:
            history = cast(list[Any], getattr(session, "chat_history", None) or [])
        if not isinstance(history, list):
            return JSONResponse([])

        def _get(obj: Any, name: str, default: Any = None) -> Any:
            if isinstance(obj, dict):
                return obj.get(name, default)
            return getattr(obj, name, default)

        runs: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for msg in history:
            role = _get(msg, "role")
            if role == "user":
                if current is not None:
                    runs.append(current)
                current = {
                    "run_input": _get(msg, "content", ""),
                    "created_at": _get(msg, "created_at", 0),
                    "content": "",
                    "tools": [],
                    "extra_data": {},
                }
            elif role == "assistant" and current is not None:
                content = _get(msg, "content") or ""
                if content:
                    current["content"] = content
                for tc in _get(msg, "tool_calls") or []:
                    fn = _get(tc, "function") or {}
                    current["tools"].append(
                        {
                            "tool_call_id": _get(tc, "id", ""),
                            "tool_name": _get(fn, "name", ""),
                            "tool_args": _get(fn, "arguments") or {},
                            "role": "tool",
                            "content": "",
                            "metrics": {"time": 0},
                            "created_at": _get(msg, "created_at", 0),
                        }
                    )
        if current is not None:
            runs.append(current)

        return JSONResponse(runs)

    application.state.store = store
    application.state.providers = providers
    application.state.db = foldos_db

    agent_os = AgentOS(
        id="foldos-os",
        name="FoldOS",
        db=foldos_db,
        agents=[agent_instance],
        base_app=application,
        on_route_conflict="preserve_base_app",
        settings=AgnoAPISettings(docs_enabled=True),
    )
    agent_os.get_app()

    # Force model client creation so we can wrap chat.completions.create for
    # ledger capture. The resulting llm_call events are then exported to SigNoz
    # by the existing foldos.otel.emitter.Emitter pipeline.
    try:
        sync_client = model.get_client()
    except Exception:
        sync_client = None
    try:
        async_client = model.get_async_client()
    except Exception:
        async_client = None

    if sync_client is not None:
        instrument_openai(sync_client, session=_current_session_source)
    if async_client is not None:
        instrument_openai(async_client, session=_current_session_source)

    return application
