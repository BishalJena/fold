from __future__ import annotations

import asyncio
import functools
import inspect
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from typing import Any

from openai import AsyncOpenAI, OpenAI

from foldos.core import usage
from foldos.core.session import Session

SubmitFn = Callable[[Coroutine[Any, Any, Any]], Any]
SessionSource = Session | Callable[[], Session | None] | None
_INSTRUMENTED_ATTR = "_foldos_instrumented"


def _extract_usage(usage: Any) -> tuple[int, int]:
    if usage is None:
        return 0, 0
    if hasattr(usage, "input_tokens") and hasattr(usage, "output_tokens"):
        return int(usage.input_tokens or 0), int(usage.output_tokens or 0)
    if hasattr(usage, "prompt_tokens") and hasattr(usage, "completion_tokens"):
        return int(usage.prompt_tokens or 0), int(usage.completion_tokens or 0)
    return 0, 0


def _text_len(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list | tuple):
        return sum(_text_len(item) for item in value)
    if isinstance(value, dict):
        return _text_len(list(value.values()))
    return 0


def _estimate_input_tokens(kwargs: dict[str, Any]) -> int:
    # Rough heuristic: ~4 characters per token. Good enough for local demos
    # when the model does not return usage metadata.
    messages = kwargs.get("messages", [])
    chars = _text_len(messages)
    return max(1, chars // 4)


def _estimate_output_tokens(response: Any) -> int:
    if response is None:
        return 0
    try:
        content = response.choices[0].message.content or ""
    except Exception:
        content = ""
    return max(1, len(content) // 4)


def _resolve_session(source: SessionSource) -> Session | None:
    return source() if callable(source) else source


async def _record_llm_call(
    session: Session,
    model: str,
    usage_obj: Any,
    latency_ms: float,
    kwargs: dict[str, Any] | None = None,
    response: Any = None,
    output_text: str | None = None,
) -> None:
    input_tokens, output_tokens = _extract_usage(usage_obj)
    if input_tokens == 0 and kwargs is not None:
        input_tokens = _estimate_input_tokens(kwargs)
    if output_tokens == 0:
        if output_text:
            output_tokens = max(1, len(output_text) // 4)
        elif response is not None:
            output_tokens = _estimate_output_tokens(response)
    explicit_cost = getattr(usage_obj, "cost_usd", None)
    cost_usd = usage.cost(model, input_tokens, output_tokens, explicit_cost_usd=explicit_cost)
    await session.add_llm_call(model, input_tokens, output_tokens, latency_ms=latency_ms, cost_usd=cost_usd)


def _run_sync(coro: Coroutine[Any, Any, Any], submit: SubmitFn | None) -> Any:
    if submit is not None:
        return submit(coro)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    else:
        return loop.create_task(coro)


def _is_sync_stream(response: Any) -> bool:
    return hasattr(response, "__iter__") and hasattr(response, "__enter__") and hasattr(response, "__exit__")


def _is_async_stream(response: Any) -> bool:
    return hasattr(response, "__aiter__") and hasattr(response, "__aenter__") and hasattr(response, "__aexit__")


class _WrappedStream:
    def __init__(
        self,
        stream: Any,
        session: SessionSource,
        start: float,
        submit: SubmitFn | None,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._stream = stream
        self._session = session
        self._start = start
        self._submit = submit
        self._kwargs = kwargs or {}
        self._model: str | None = None
        self._usage: Any = None
        self._content_parts: list[str] = []
        self._recorded = False

    def __enter__(self) -> _WrappedStream:
        self._stream.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._stream.__exit__(exc_type, exc, tb)

    def __iter__(self) -> Iterator[Any]:
        for chunk in self._stream:
            if self._model is None:
                self._model = getattr(chunk, "model", None)
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                self._usage = usage
            try:
                delta = chunk.choices[0].delta.content
                if delta:
                    self._content_parts.append(delta)
            except Exception:
                pass
            yield chunk
        self._record()

    def _record(self) -> None:
        if self._recorded:
            return
        resolved = _resolve_session(self._session)
        if resolved is None:
            return
        self._recorded = True
        latency_ms = (time.monotonic() - self._start) * 1000.0
        model = self._model or "unknown"
        output_text = "".join(self._content_parts) if self._content_parts else None
        _run_sync(
            _record_llm_call(
                resolved,
                model,
                self._usage,
                latency_ms,
                kwargs=self._kwargs,
                output_text=output_text,
            ),
            self._submit,
        )


class _WrappedAsyncStream:
    def __init__(
        self,
        stream: Any,
        session: SessionSource,
        start: float,
        submit: SubmitFn | None,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._stream = stream
        self._session = session
        self._start = start
        self._submit = submit
        self._kwargs = kwargs or {}
        self._model: str | None = None
        self._usage: Any = None
        self._content_parts: list[str] = []
        self._recorded = False

    async def __aenter__(self) -> _WrappedAsyncStream:
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return await self._stream.__aexit__(exc_type, exc, tb)

    async def __aiter__(self) -> AsyncIterator[Any]:
        async for chunk in self._stream:
            if self._model is None:
                self._model = getattr(chunk, "model", None)
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                self._usage = usage
            try:
                delta = chunk.choices[0].delta.content
                if delta:
                    self._content_parts.append(delta)
            except Exception:
                pass
            yield chunk
        await self._record()

    async def _record(self) -> None:
        if self._recorded:
            return
        resolved = _resolve_session(self._session)
        if resolved is None:
            return
        self._recorded = True
        latency_ms = (time.monotonic() - self._start) * 1000.0
        model = self._model or "unknown"
        output_text = "".join(self._content_parts) if self._content_parts else None
        coro = _record_llm_call(
            resolved,
            model,
            self._usage,
            latency_ms,
            kwargs=self._kwargs,
            output_text=output_text,
        )
        if self._submit is not None:
            self._submit(coro)
        else:
            await coro


def instrument_openai(
    client: OpenAI | AsyncOpenAI,
    session: SessionSource = None,
    submit: SubmitFn | None = None,
) -> None:
    completions = client.chat.completions
    original = completions.create
    is_async_client = isinstance(client, AsyncOpenAI)
    if getattr(original, _INSTRUMENTED_ATTR, False):
        return

    if is_async_client or inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_wrapped(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            response = await original(*args, **kwargs)
            resolved = _resolve_session(session)
            if _is_async_stream(response):
                return _WrappedAsyncStream(response, session, start, None, kwargs=kwargs)
            latency_ms = (time.monotonic() - start) * 1000.0
            model = (
                getattr(response, "model", None)
                or kwargs.get("model")
                or kwargs.get("model_name")
                or (args[0] if args and isinstance(args[0], str) else None)
                or "unknown"
            )
            if resolved is not None:
                await _record_llm_call(
                    resolved,
                    model,
                    getattr(response, "usage", None),
                    latency_ms,
                    kwargs=kwargs,
                    response=response,
                )
            return response

        async_wrapped_any: Any = async_wrapped
        setattr(async_wrapped_any, _INSTRUMENTED_ATTR, True)
        completions_any: Any = completions
        completions_any.create = async_wrapped
    else:

        @functools.wraps(original)
        def sync_wrapped(*args: Any, **kwargs: Any) -> Any:
            start = time.monotonic()
            response = original(*args, **kwargs)
            resolved = _resolve_session(session)
            if _is_sync_stream(response):
                return _WrappedStream(response, session, start, submit, kwargs=kwargs)
            latency_ms = (time.monotonic() - start) * 1000.0
            model = (
                getattr(response, "model", None)
                or kwargs.get("model")
                or kwargs.get("model_name")
                or (args[0] if args and isinstance(args[0], str) else None)
                or "unknown"
            )
            if resolved is not None:
                _run_sync(
                    _record_llm_call(
                        resolved,
                        model,
                        getattr(response, "usage", None),
                        latency_ms,
                        kwargs=kwargs,
                        response=response,
                    ),
                    submit,
                )
            return response

        sync_wrapped_any: Any = sync_wrapped
        setattr(sync_wrapped_any, _INSTRUMENTED_ATTR, True)
        sync_completions_any: Any = completions
        sync_completions_any.create = sync_wrapped
