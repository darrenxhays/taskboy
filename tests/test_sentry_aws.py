import json
import logging
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from taskboy.adapters import aws_read
from taskboy.adapters._util import AccessDenied, wrap
from taskboy.adapters.aws_read import AwsReadAdapter
from taskboy.adapters.sentry import SentryAdapter
from taskboy.redact import redactor


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


@pytest.mark.asyncio
async def test_aws_read_requires_an_environment_when_roles_are_configured(aws_cross_env):
    # no silent staging default: a production incident read against staging looks like "no data" (task t20260901-6e676906)
    result = await aws_cross_env.aws_read({"service": "logs", "operation": "DescribeLogGroups", "parameters": "{}"})
    assert result.get("isError") is True
    assert "environment is required" in result["content"][0]["text"]
    assert "production" in result["content"][0]["text"] and "staging" in result["content"][0]["text"]
    aws_cross_env._call.assert_not_called()


@pytest.mark.asyncio
async def test_aws_read_unconfigured_environment_points_at_request_permission(aws_cross_env):
    tool = wrap(aws_cross_env.aws_read, logging.getLogger("test"))
    result = await tool({"environment": "sandbox", "service": "logs", "operation": "DescribeLogGroups", "parameters": "{}"})
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert "no diagnostics role configured" in text
    assert "kind='access'" in text and "target='aws:sandbox'" in text
    aws_cross_env._call.assert_not_called()


@pytest.mark.asyncio
async def test_aws_read_assume_role_failure_points_at_request_permission(aws_cross_env):
    # the evidence case: the role existed in config but AssumeRole was denied — the model must request access, not report blocked
    aws_cross_env._assume = MagicMock(side_effect=RuntimeError("An error occurred (AccessDenied) when calling the AssumeRole operation"))
    tool = wrap(aws_cross_env.aws_read, logging.getLogger("test"))
    result = await tool({"environment": "production", "service": "logs", "operation": "FilterLogEvents", "parameters": "{}"})
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert "could not assume the production diagnostics role" in text
    assert "AccessDenied" in text
    assert "request_permission with kind='access' and target='aws:production'" in text
    aws_cross_env._call.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception_name", "expected_text"),
    [
        ("ReadTimeoutError", "error: timed out"),
        ("ConnectTimeoutError", "error: timed out"),
        ("EndpointConnectionError", "error: timed out"),
        ("ConnectionClosedError", "error: timed out"),
        ("ProxyConnectionError", "error: timed out"),
        ("SSLError", "error: timed out"),
        ("HTTPClientError", "error: timed out"),
        ("TimeoutError", "error: timed out"),
    ],
)
async def test_aws_read_assume_role_timeout_classes_stay_generic(aws_cross_env, exception_name, expected_text):
    error_type = type(exception_name, (Exception,), {})
    aws_cross_env._assume = MagicMock(side_effect=error_type("timed out"))
    tool = wrap(aws_cross_env.aws_read, logging.getLogger("test"))

    result = await tool({"environment": "production", "service": "logs", "operation": "FilterLogEvents", "parameters": "{}"})

    assert result.get("isError") is True
    assert result["content"][0]["text"] == expected_text
    assert "request_permission" not in result["content"][0]["text"]
    aws_cross_env._call.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "InternalError",
        "InternalFailure",
        "RequestTimeout",
        "RequestTimeoutException",
    ],
)
async def test_aws_read_assume_role_transient_codes_stay_generic(aws_cross_env, code):
    class ClientError(Exception):
        def __init__(self):
            super().__init__("throttled")
            self.response = {"Error": {"Code": code}}

    aws_cross_env._assume = MagicMock(side_effect=ClientError())
    tool = wrap(aws_cross_env.aws_read, logging.getLogger("test"))

    result = await tool({"environment": "production", "service": "logs", "operation": "FilterLogEvents", "parameters": "{}"})

    assert result.get("isError") is True
    assert result["content"][0]["text"] == "error: throttled"
    assert "request_permission" not in result["content"][0]["text"]
    aws_cross_env._call.assert_not_called()


@pytest.mark.asyncio
async def test_aws_read_assume_role_server_status_stays_generic(aws_cross_env):
    class ClientError(Exception):
        def __init__(self):
            super().__init__("service unavailable")
            self.response = {"Error": {"Code": "UnknownError"}, "ResponseMetadata": {"HTTPStatusCode": 503}}

    aws_cross_env._assume = MagicMock(side_effect=ClientError())
    tool = wrap(aws_cross_env.aws_read, logging.getLogger("test"))

    result = await tool({"environment": "production", "service": "logs", "operation": "FilterLogEvents", "parameters": "{}"})

    assert result.get("isError") is True
    assert result["content"][0]["text"] == "error: service unavailable"
    assert "request_permission" not in result["content"][0]["text"]
    aws_cross_env._call.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("AccessDenied", 400),
        ("AccessDeniedException", 400),
        ("UnauthorizedOperation", 400),
        ("UnauthorizedAccess", 400),
        ("AuthorizationError", 400),
        ("NotAuthorized", 400),
        ("NotAuthorizedException", 400),
        ("Forbidden", 400),
        ("ForbiddenException", 400),
        ("UnknownError", 403),
    ],
)
async def test_aws_read_service_access_denial_points_at_request_permission(aws_cross_env, code, status):
    error = RuntimeError("not authorized to filter log events")
    error.response = {"Error": {"Code": code, "Message": "not authorized"}, "ResponseMetadata": {"HTTPStatusCode": status}}
    aws_cross_env._call = MagicMock(side_effect=error)
    tool = wrap(aws_cross_env.aws_read, logging.getLogger("test"))

    result = await tool({"environment": "production", "service": "logs", "operation": "FilterLogEvents", "parameters": "{}"})

    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert "request_permission" in text
    assert "kind='access'" in text
    assert "target='aws:production'" in text


@pytest.mark.asyncio
async def test_aws_read_non_access_service_failure_stays_generic(aws_cross_env):
    aws_cross_env._call = MagicMock(side_effect=RuntimeError("throttled"))
    tool = wrap(aws_cross_env.aws_read, logging.getLogger("test"))

    result = await tool({"environment": "production", "service": "logs", "operation": "FilterLogEvents", "parameters": "{}"})

    assert result.get("isError") is True
    assert result["content"][0]["text"] == "error: throttled"
    assert "request_permission" not in result["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "status", "message"), [("InternalFailure", 503, "internal failure"), ("Throttling", 400, "throttled")])
async def test_aws_read_structured_non_access_service_failure_stays_generic(aws_cross_env, code, status, message):
    error = RuntimeError(message)
    error.response = {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}
    aws_cross_env._call = MagicMock(side_effect=error)
    tool = wrap(aws_cross_env.aws_read, logging.getLogger("test"))

    result = await tool({"environment": "production", "service": "logs", "operation": "FilterLogEvents", "parameters": "{}"})

    assert result.get("isError") is True
    assert result["content"][0]["text"] == f"error: {message}"
    assert "request_permission" not in result["content"][0]["text"]


def test_check_environments_reports_ok_or_the_error_and_caches(monkeypatch):
    aws_read._self_check_cache.clear()
    calls: list[str] = []

    def probe(role_arn):
        calls.append(role_arn)
        if "production" in role_arn:
            raise RuntimeError("AccessDenied: User is not authorized to perform: sts:AssumeRole\nsecond line")

    monkeypatch.setattr(aws_read, "_probe_assume", probe)
    roles = {"staging": "arn:staging", "production": "arn:production"}
    assert aws_read.check_environments(roles) == {"production": "AccessDenied: User is not authorized to perform: sts:AssumeRole", "staging": "ok"}
    assert aws_read.check_environments(roles) == {"production": "AccessDenied: User is not authorized to perform: sts:AssumeRole", "staging": "ok"}
    assert len(calls) == 2  # second call served from the cache
    assert aws_read.check_environments(roles, ttl_seconds=0)["staging"] == "ok" and len(calls) == 4
    aws_read._self_check_cache.clear()


def test_probe_assume_does_not_register_discarded_credentials(monkeypatch):
    secret = "probe-secret-xyz"
    token = "probe-token-xyz"
    assume_calls = []
    client_calls = []
    config_calls = []

    class FakeSts:
        def assume_role(self, **kwargs):
            assume_calls.append(kwargs)
            return {
                "Credentials": {
                    "AccessKeyId": "AKIAEXAMPLE",
                    "SecretAccessKey": secret,
                    "SessionToken": token,
                    "Expiration": datetime(2026, 1, 1),
                }
            }

    def client(*args, **kwargs):
        client_calls.append((args, kwargs))
        return FakeSts()

    def config(**kwargs):
        config_calls.append(kwargs)
        return SimpleNamespace(**kwargs)

    fake = SimpleNamespace(client=client)
    fake_config = SimpleNamespace(Config=config)
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setitem(sys.modules, "botocore.config", fake_config)
    redactor.unregister(secret)
    redactor.unregister(token)

    try:
        role_arn = "arn:aws:iam::1:role/x"
        aws_read._probe_assume(role_arn)
        assert assume_calls == [
            {
                "RoleArn": role_arn,
                "RoleSessionName": "ar-selfcheck",
                "ExternalId": aws_read.ASSUME_EXTERNAL_ID,
                "DurationSeconds": 900,
            }
        ]
        assert config_calls == [{"connect_timeout": 3, "read_timeout": 5, "retries": {"max_attempts": 1}}]
        assert len(client_calls) == 1
        assert client_calls[0][0] == ("sts",)
        client_config = client_calls[0][1]["config"]
        assert client_config.connect_timeout == 3
        assert client_config.read_timeout == 5
        assert client_config.retries == {"max_attempts": 1}
        assert redactor.redact(f"{secret} {token}") == f"{secret} {token}"
    finally:
        redactor.unregister(secret)
        redactor.unregister(token)


@pytest.mark.asyncio
async def test_wrap_turns_access_denied_into_a_request_permission_hint():
    async def tool(args):
        raise AccessDenied("jira", "RISK", "jira api POST /rest/api/3/issue denied: 403")

    result = await wrap(tool, logging.getLogger("test"))({})
    assert result.get("isError") is True
    text = result["content"][0]["text"]
    assert text.startswith("error: jira api POST /rest/api/3/issue denied: 403")
    assert "kind='access'" in text and "target='jira:RISK'" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (401, 403))
async def test_sentry_request_maps_forbidden_to_organization_access_denied_and_permission_hint(sentry, fake_aiohttp, status):
    del sentry._request
    fake_aiohttp(status, body="forbidden")

    with pytest.raises(AccessDenied) as raised:
        await sentry._request("/projects/example-org/risk-nextgen/issues/", {})

    assert raised.value.system == "sentry"
    assert raised.value.scope == "example-org"

    result = await wrap(sentry.list_issues, logging.getLogger("test"))({"project": "risk-nextgen", "query": ""})
    text = result["content"][0]["text"]
    assert "target='sentry:example-org'" in text


@pytest.mark.asyncio
async def test_sentry_request_server_error_stays_generic(sentry, fake_aiohttp):
    del sentry._request
    fake_aiohttp(500, body="server unavailable")

    with pytest.raises(RuntimeError, match="sentry api GET .* failed: 500"):
        await sentry._request("/projects/example-org/risk-nextgen/issues/", {})

    result = await wrap(sentry.list_issues, logging.getLogger("test"))({"project": "risk-nextgen", "query": ""})
    text = result["content"][0]["text"]
    assert result.get("isError") is True
    assert "request_permission" not in text


@pytest.mark.asyncio
async def test_aws_read_rejects_malformed_parameters(aws):
    result = await aws.aws_read({"service": "logs", "operation": "FilterLogEvents", "parameters": "{not json"})
    assert result.get("isError") is True
    aws._call.assert_not_called()
