import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from taskboy.adapters.aws_read import AwsReadAdapter
from taskboy.adapters.sentry import SentryAdapter


@pytest.fixture
def sentry(store, make_task):
    a = SentryAdapter(store, make_task(), "example-org", "sntrys_token", ["risk-nextgen"])
    a._request = AsyncMock()
    return a


@pytest.fixture
def aws(store, make_task):
    a = AwsReadAdapter(store, make_task(), ["logs", "cloudwatch"], ["us-east-1"])
    a._call = MagicMock(return_value={"events": [{"message": "log line"}]})
    return a


@pytest.mark.asyncio
async def test_sentry_project_allowlist(sentry):
    refused = await sentry.list_issues({"project": "other-project", "query": ""})
    assert refused.get("isError") is True
    sentry._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_sentry_latest_event_is_trimmed_to_stack_frames(sentry):
    sentry._request.side_effect = [
        {
            "title": "KeyError: 'exposure'",
            "message": "boom",
            "entries": [{"type": "exception", "data": {"values": [{"type": "KeyError", "value": "'exposure'", "stacktrace": {"frames": [{"filename": f"mod{i}.py", "lineNo": i, "function": f"fn{i}", "inApp": i > 17} for i in range(20)]}}]}}],
            "tags": [{"key": "release", "value": "1.2.3"}],
        }
    ]
    result = await sentry.get_latest_event({"issue_id": "123"})
    text = result["content"][0]["text"]
    assert "KeyError" in text
    assert "mod19.py:19 in fn19 (in app)" in text
    assert "mod0.py" not in text  # only the last frames survive trimming
    assert "release" in text
    assert len(text) <= 4000


@pytest.mark.asyncio
async def test_aws_read_denies_writes_and_offlist_targets(aws, store):
    denied = await aws.aws_read({"service": "logs", "operation": "DeleteLogGroup", "parameters": "{}"})
    assert denied.get("isError") is True
    aws._call.assert_not_called()
    events = store.events_for(aws.task.task_id)
    assert any(e["kind"] == "security_denial" for e in events)  # §10

    denied = await aws.aws_read({"service": "iam", "operation": "ListUsers", "parameters": "{}"})
    assert denied.get("isError") is True
    denied = await aws.aws_read({"service": "logs", "operation": "FilterLogEvents", "region": "eu-west-1", "parameters": "{}"})
    assert denied.get("isError") is True
    aws._call.assert_not_called()


@pytest.mark.asyncio
async def test_aws_read_happy_path_defaults_region(aws):
    result = await aws.aws_read({"service": "logs", "operation": "FilterLogEvents", "parameters": json.dumps({"logGroupName": "/svc"})})
    assert "log line" in result["content"][0]["text"]
    # no role arns configured -> local default credential chain (credentials=None)
    aws._call.assert_called_once_with("logs", "FilterLogEvents", "us-east-1", {"logGroupName": "/svc"}, None)


@pytest.fixture
def aws_cross_env(store, make_task):
    a = AwsReadAdapter(store, make_task(), ["logs"], ["us-east-1"], role_arns={"staging": "arn:aws:iam::1:role/example-staging-diagnostics", "production": "arn:aws:iam::2:role/example-production-diagnostics"})
    a._call = MagicMock(return_value={"ok": True})
    a._assume = MagicMock(side_effect=lambda env: {"aws_access_key_id": f"AKIA{env}", "aws_secret_access_key": "s", "aws_session_token": "t", "expiration_ts": 9999999999.0})
    return a


@pytest.mark.asyncio
async def test_aws_read_assumes_the_requested_environment_and_caches(aws_cross_env):
    await aws_cross_env.aws_read({"environment": "production", "service": "logs", "operation": "FilterLogEvents", "parameters": "{}"})
    await aws_cross_env.aws_read({"environment": "production", "service": "logs", "operation": "DescribeLogGroups", "parameters": "{}"})
    assert aws_cross_env._assume.call_count == 1  # cached until near expiry
    credentials = aws_cross_env._call.call_args.args[4]
    assert credentials["aws_access_key_id"] == "AKIAproduction"
    # default environment is staging
    await aws_cross_env.aws_read({"service": "logs", "operation": "DescribeLogGroups", "parameters": "{}"})
    assert aws_cross_env._call.call_args.args[4]["aws_access_key_id"] == "AKIAstaging"


@pytest.mark.asyncio
async def test_aws_read_refuses_unconfigured_environment(aws_cross_env):
    result = await aws_cross_env.aws_read({"environment": "sandbox", "service": "logs", "operation": "DescribeLogGroups", "parameters": "{}"})
    assert result.get("isError") is True
    aws_cross_env._call.assert_not_called()


@pytest.mark.asyncio
async def test_aws_read_rejects_malformed_parameters(aws):
    result = await aws.aws_read({"service": "logs", "operation": "FilterLogEvents", "parameters": "{not json"})
    assert result.get("isError") is True
    aws._call.assert_not_called()
