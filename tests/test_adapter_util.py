from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from taskboy.adapters._util import AccessDenied, wrap


class Adapter:
    def __init__(self):
        self.task = SimpleNamespace(task_id="t20260902-abcdef01")

    async def get_thing(self, args):
        raise AccessDenied("jira", "RISK", "denied")

    async def fail_thing(self, args):
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_wrap_logs_task_and_tool_for_access_denied():
    logger = MagicMock()

    result = await wrap(Adapter().get_thing, logger)({})

    assert result.get("isError") is True
    assert "request_permission" in result["content"][0]["text"]
    logger.warning.assert_called_once_with(
        "adapter access denied task=%s tool=%s system=%s scope=%s — %s",
        "t20260902-abcdef01",
        "get_thing",
        "jira",
        "RISK",
        "denied",
    )


@pytest.mark.asyncio
async def test_wrap_logs_task_and_tool_for_other_failure():
    logger = MagicMock()

    result = await wrap(Adapter().fail_thing, logger)({})

    assert result.get("isError") is True
    logger.exception.assert_called_once_with(
        "adapter tool failed task=%s tool=%s",
        "t20260902-abcdef01",
        "fail_thing",
    )


@pytest.mark.asyncio
async def test_wrap_logs_fallback_task_for_bare_function():
    async def get_thing(args):
        raise AccessDenied("jira", "RISK", "denied")

    logger = MagicMock()

    result = await wrap(get_thing, logger)({})

    assert result.get("isError") is True
    logger.warning.assert_called_once_with(
        "adapter access denied task=%s tool=%s system=%s scope=%s — %s",
        "-",
        "get_thing",
        "jira",
        "RISK",
        "denied",
    )
