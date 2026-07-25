from __future__ import annotations

import asyncio
import functools
import inspect
import re
import time
from collections.abc import Callable, Coroutine
from typing import Any

from foldos.core.session import Session

SubmitFn = Callable[[Coroutine[Any, Any, Any]], Any]
SessionSource = Session | Callable[[], Session | None]


def _resolve_session(source: SessionSource | None) -> Session | None:
    if source is None:
        return None
    return source() if callable(source) else source


_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?<![a-zA-Z0-9])"
    r"(api[-_]?key|token|secret|password|auth[-_]?header|authorization)"
    r"(?![a-zA-Z0-9])",
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_PATTERN.search(key))


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if isinstance(k, str) and _is_sensitive_key(k) else _sanitize(v) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(v) for v in value)
    return value


def _run_sync(coro: Coroutine[Any, Any, Any], submit: SubmitFn | None) -> Any:
    if submit is not None:
        return submit(coro)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    else:
        return loop.create_task(coro)


def traced_tool(
    session: SessionSource | None = None,
    submit: SubmitFn | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                start = time.monotonic()
                error: str | None = None
                result: Any = None
                resolved = _resolve_session(session)
                try:
                    result = await fn(*args, **kwargs)
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    latency_ms = (time.monotonic() - start) * 1000.0
                    if resolved is not None:
                        await resolved.add_tool_call(
                            fn.__name__,
                            _sanitize(dict(bound.arguments)),
                            None,
                            latency_ms=latency_ms,
                            error=error,
                        )
                    raise
                latency_ms = (time.monotonic() - start) * 1000.0
                if resolved is not None:
                    await resolved.add_tool_call(
                        fn.__name__,
                        _sanitize(dict(bound.arguments)),
                        _sanitize(result),
                        latency_ms=latency_ms,
                        error=None,
                    )
                return result

            async_wrapper_any: Any = async_wrapper
            async_wrapper_any.__signature__ = sig
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            start = time.monotonic()
            error: str | None = None
            result: Any = None
            resolved = _resolve_session(session)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                error = str(exc) or type(exc).__name__
                latency_ms = (time.monotonic() - start) * 1000.0
                if resolved is not None:
                    _run_sync(
                        resolved.add_tool_call(
                            fn.__name__,
                            _sanitize(dict(bound.arguments)),
                            None,
                            latency_ms=latency_ms,
                            error=error,
                        ),
                        submit,
                    )
                raise
            latency_ms = (time.monotonic() - start) * 1000.0
            if resolved is not None:
                _run_sync(
                    resolved.add_tool_call(
                        fn.__name__,
                        _sanitize(dict(bound.arguments)),
                        _sanitize(result),
                        latency_ms=latency_ms,
                        error=None,
                    ),
                    submit,
                )
            return result

        sync_wrapper_any: Any = sync_wrapper
        sync_wrapper_any.__signature__ = sig
        return sync_wrapper

    return decorator
