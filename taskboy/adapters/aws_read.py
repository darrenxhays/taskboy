"""aws diagnostics as one read-only in-process mcp tool (AWS-001..007).

IAM is the real enforcement (AWS-005) — this adapter is the belt on those suspenders:
service and region allowlists from config (AWS-003) and a read-verb operation gate.
the harness is deployed once (host account) and reads other environments by assuming that
environment's configured diagnostics role. per-task aws_read calls use session name
ar-<task_id>, making their cloudtrail entries task-attributable; startup/dashboard health
probes use ar-selfcheck and are not tied to a task (AWS-006).
"""

import asyncio
import json
import logging
import re
import time

from taskboy.adapters._util import AccessDenied, _error, _text, wrap
from taskboy.models import Task
from taskboy.redact import redactor
from taskboy.store import Store

logger = logging.getLogger("taskboy.aws")

READ_VERBS = re.compile(r"^(Get|List|Describe|Lookup|Search|Filter|BatchGet|Head)")
ASSUME_EXTERNAL_ID = "taskboy"  # matches the trust policy in the shell repo's infrastructure/iam.py
SELF_CHECK_TTL_SECONDS = 300  # the dashboard re-probes each environment at most this often
_self_check_cache: dict[str, tuple[float, str]] = {}  # environment -> (checked_at, status)


def _is_access_denied(error: Exception) -> bool:
    """return whether an AWS service response reports an authorization denial."""
    response = getattr(error, "response", {})
    code = response.get("Error", {}).get("Code")
    return (
        code
        in {
            "AccessDenied",
            "AccessDeniedException",
            "UnauthorizedOperation",
            "UnauthorizedAccess",
            "AuthorizationError",
            "NotAuthorized",
            "NotAuthorizedException",
            "Forbidden",
            "ForbiddenException",
        }
        or response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 403
    )


def _is_transient(error: Exception) -> bool:
    """return whether an AWS failure is likely to succeed when retried."""
    if type(error).__name__ in {
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "EndpointConnectionError",
        "ConnectionClosedError",
        "ProxyConnectionError",
        "SSLError",
        "HTTPClientError",
        "TimeoutError",
    }:
        return True
    response = getattr(error, "response", {})
    code = response.get("Error", {}).get("Code")
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {
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
    } or (isinstance(status, int) and status >= 500)


class AwsReadAdapter:
    def __init__(self, store: Store, task: Task, allowed_services: list[str], allowed_regions: list[str], role_arns: dict[str, str] | None = None):
        self.store = store
        self.task = task
        self.allowed_services = allowed_services
        self.allowed_regions = allowed_regions
        self.role_arns = role_arns or {}  # environment -> diagnostics role arn; empty = local dev default chain
        self._credentials: dict[str, dict] = {}

    async def aws_read(self, args: dict) -> dict:
        service = str(args.get("service", "")).strip().lower()
        operation = str(args.get("operation", "")).strip()
        environment = str(args.get("environment", "")).strip().lower()
        region = str(args.get("region", "")).strip() or (self.allowed_regions[0] if self.allowed_regions else "us-east-1")
        if self.role_arns and not environment:
            # no silent default: a production incident read against staging looks like "no data" and misleads the investigation
            return _error(f"environment is required; configured environments: {sorted(self.role_arns)}. take it from the alert's environment tag or the request.")
        environment = environment or "local"
        if service not in self.allowed_services:
            return _error(f"service {service!r} is not on the approved list {self.allowed_services}")
        if self.allowed_regions and region not in self.allowed_regions:
            return _error(f"region {region!r} is not on the approved list {self.allowed_regions}")
        if not READ_VERBS.match(operation):
            self.store.add_event(self.task.task_id, "security_denial", {"reason": "non-read aws operation", "service": service, "operation": operation}, tool_name="mcp__aws__aws_read", is_write=True)
            return _error(f"operation {operation!r} is not a read operation; only Get/List/Describe/... are permitted")
        try:
            parameters = json.loads(str(args.get("parameters") or "{}"))
        except json.JSONDecodeError as e:
            return _error(f"parameters must be a json object: {e}")

        credentials = None
        if self.role_arns:
            if environment not in self.role_arns:
                raise AccessDenied("aws", environment, f"environment {environment!r} has no diagnostics role configured (services/aws.yaml diagnostics_role_arns has {sorted(self.role_arns)})")
            try:
                credentials = await asyncio.to_thread(self._credentials_for, environment)
            except Exception as e:
                if _is_transient(e):
                    raise
                # trust policy, orchestrator policy, and missing roles are operator fixes
                raise AccessDenied("aws", environment, f"could not assume the {environment} diagnostics role {self.role_arns[environment]}: {e}") from e
        self.store.add_event(self.task.task_id, "tool_call", {"aws": f"{environment}:{region} {service}.{operation}"}, tool_name="mcp__aws__aws_read", is_write=False)  # AWS-006
        try:
            result = await asyncio.to_thread(self._call, service, operation, region, parameters, credentials)
        except Exception as e:
            if _is_access_denied(e):
                raise AccessDenied("aws", environment, f"{service}.{operation} in {environment} was denied for the diagnostics role: {e}") from e
            raise
        return _text(json.dumps(result, default=str, ensure_ascii=False))

    def _credentials_for(self, environment: str) -> dict:
        cached = self._credentials.get(environment)
        if cached and cached["expiration_ts"] - time.time() > 300:
            return cached
        self._credentials[environment] = self._assume(environment)
        return self._credentials[environment]

    def _assume(self, environment: str) -> dict:
        """the sts seam — patched in unit tests."""
        import boto3

        sts = boto3.client("sts")
        response = sts.assume_role(RoleArn=self.role_arns[environment], RoleSessionName=f"ar-{self.task.task_id}"[:64], ExternalId=ASSUME_EXTERNAL_ID, DurationSeconds=3600)
        credentials = response["Credentials"]
        redactor.register(credentials["SecretAccessKey"])
        redactor.register(credentials["SessionToken"])
        return {
            "aws_access_key_id": credentials["AccessKeyId"],
            "aws_secret_access_key": credentials["SecretAccessKey"],
            "aws_session_token": credentials["SessionToken"],
            "expiration_ts": credentials["Expiration"].timestamp(),
        }

    def _call(self, service: str, operation: str, region: str, parameters: dict, credentials: dict | None):
        """the boto3 seam — patched in unit tests. credentials never reach the model (TOL-007)."""
        import boto3
        from botocore import xform_name

        kwargs = {key: credentials[key] for key in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")} if credentials else {}
        client = boto3.client(service, region_name=region, **kwargs)
        response = getattr(client, xform_name(operation))(**parameters)
        response.pop("ResponseMetadata", None)
        return response


def check_environments(role_arns: dict[str, str], ttl_seconds: float = SELF_CHECK_TTL_SECONDS) -> dict[str, str]:
    """probe every configured environment's diagnostics role: "ok" or the assume-role error. runs at startup so a
    misplaced role (created in the wrong account, or a broken trust policy) is a log line and a dashboard row, not a
    blocked task months later. cached per environment so the dashboard can call it freely."""
    now = time.time()
    statuses: dict[str, str] = {}
    for environment, role_arn in sorted(role_arns.items()):
        cached = _self_check_cache.get(environment)
        if cached and now - cached[0] < ttl_seconds:
            statuses[environment] = cached[1]
            continue
        try:
            _probe_assume(role_arn)
            status = "ok"
        except Exception as e:
            status = str(e).splitlines()[0][:200] or type(e).__name__
        _self_check_cache[environment] = (now, status)
        statuses[environment] = status
    return statuses


def _probe_assume(role_arn: str) -> None:
    """the sts seam for the self-check — patched in unit tests."""
    import boto3
    from botocore.config import Config

    # a self-check must fail fast; the per-task _assume keeps defaults
    sts = boto3.client("sts", config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}))
    # the probe discards the credentials, so registering them would only grow the redactor on every re-probe
    sts.assume_role(RoleArn=role_arn, RoleSessionName="ar-selfcheck", ExternalId=ASSUME_EXTERNAL_ID, DurationSeconds=900)


def build_aws_server(adapter: AwsReadAdapter):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    # environments come from config, never a hardcoded list
    environments = "|".join(sorted(adapter.role_arns)) or "default credentials"
    example_environment = sorted(adapter.role_arns)[0] if adapter.role_arns else "local"
    tools = [
        tool(
            "aws_read",
            f"Run a read-only AWS API call in a specific environment ({environments}). environment is required when diagnostics roles are configured: take it from the alert's environment tag or the requester's wording — an incident in one environment must be read in that environment. e.g. environment={example_environment} service=logs operation=FilterLogEvents parameters='{{\"logGroupName\": ...}}'. Writes are denied by IAM and by this tool. If the call fails for an access reason the error tells you which request_permission access target to ask for.",
            {"environment": str, "service": str, "operation": str, "region": str, "parameters": str},
        )(wrap(adapter.aws_read, logger)),
    ]
    return create_sdk_mcp_server(name="aws", version="1.0.0", tools=tools)
