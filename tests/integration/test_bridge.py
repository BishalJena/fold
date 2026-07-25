from __future__ import annotations

import asyncio

import pytest

from foldos.bridge import SyncBridge


def test_run_executes_coroutine_on_dedicated_background_loop() -> None:
    bridge = SyncBridge()
    try:
        thread_id, running = bridge.run(_loop_details())
        assert thread_id == bridge.thread.ident
        assert running
    finally:
        bridge.close()


def test_close_is_idempotent_and_stops_thread() -> None:
    bridge = SyncBridge()
    bridge.close()
    bridge.close()
    assert not bridge.thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        bridge.run(_value())


def test_run_from_bridge_loop_does_not_deadlock() -> None:
    bridge = SyncBridge()
    try:
        with pytest.raises(RuntimeError, match="bridge loop"):
            bridge.run(_run_from_bridge_loop(bridge))
    finally:
        bridge.close()


async def _loop_details() -> tuple[int | None, bool]:
    import threading

    return threading.get_ident(), asyncio.get_running_loop().is_running()


async def _value() -> int:
    return 1


async def _run_from_bridge_loop(bridge: SyncBridge) -> None:
    bridge.run(_value())
