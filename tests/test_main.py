import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from taskboy.main import serve_slack


class _HangingHandler:
    """simulates slack_sdk's real connect(): returns once connected, otherwise retries forever without raising."""

    def __init__(self, connected: bool):
        self.cancelled = False
        self.started = False
        self._connected = connected

    async def connect_async(self):
        self.started = True
        if self._connected:
            return
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


async def _wait_until(predicate, timeout=1):
    async def _poll():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


@pytest.mark.asyncio
async def test_serve_slack_without_debug_notifier_still_records_error(store):
    notifier = SimpleNamespace()  # e.g. StdoutNotifier — no .debug attribute
    handler = _HangingHandler(connected=False)

    task = asyncio.ensure_future(serve_slack(handler, store, notifier, connect_timeout=0))
    await _wait_until(lambda: len(store.recent_errors()) == 1)

    errors = store.recent_errors()
    assert len(errors) == 1
    assert errors[0]["component"] == "slack"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_serve_slack_detects_a_connection_that_never_comes_up(store):
    debug = SimpleNamespace(system_error=AsyncMock())
    notifier = SimpleNamespace(debug=debug)
    handler = _HangingHandler(connected=False)

    task = asyncio.ensure_future(serve_slack(handler, store, notifier, connect_timeout=0))
    await _wait_until(lambda: len(store.recent_errors()) == 1)

    errors = store.recent_errors()
    assert len(errors) == 1
    assert errors[0]["component"] == "slack"
    assert "did not connect" in errors[0]["message"]
    debug.system_error.assert_awaited_once()
    assert handler.cancelled is False  # the still-pending connect attempt survives the timeout error

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_serve_slack_records_nothing_while_connected(store):
    debug = SimpleNamespace(system_error=AsyncMock())
    notifier = SimpleNamespace(debug=debug)
    handler = _HangingHandler(connected=True)

    await serve_slack(handler, store, notifier)

    assert store.recent_errors() == []
    debug.system_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_serve_slack_propagates_cancellation(store):
    notifier = SimpleNamespace(debug=SimpleNamespace(system_error=AsyncMock()))
    handler = _HangingHandler(connected=False)
    task = asyncio.ensure_future(serve_slack(handler, store, notifier))
    await _wait_until(lambda: handler.started)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store.recent_errors() == []
    assert handler.cancelled is True
