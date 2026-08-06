"""aws diagnostics as one read-only in-process mcp tool (AWS-001..007).

IAM is the real enforcement (AWS-005) — this adapter is the belt on those suspenders:
service and region allowlists from config (AWS-003) and a read-verb operation gate.
red is deployed once (staging account) and reads other environments by assuming that
environment's configured per-environment diagnostics role per task, with session name
ar-<task_id> so every cloudtrail entry is task-attributable (AWS-006).
"""

import asyncio
import json
import logging
import re
import time

from taskboy.adapters._util import _error, _text, wrap
from taskboy.models import Task
from taskboy.redact import redactor
from taskboy.store import Store

logger = logging.getLogger("taskboy.aws")

READ_VERBS = re.compile(r"^(Get|List|Describe|Lookup|Search|Filter|BatchGet|Head)")
ASSUME_EXTERNAL_ID = "taskboy"  # matches the trust policy in the shell repo's infrastructure/iam.py


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
        environment = str(args.get("environment", "")).strip().lower() or "staging"
        region = str(args.get("region", "")).strip() or (self.allowed_regions[0] if self.allowed_regions else "us-east-1")
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
                return _error(f"environment {environment!r} is not configured; available: {sorted(self.role_arns)}")
            credentials = await asyncio.to_thread(self._credentials_for, environment)
        self.store.add_event(self.task.task_id, "tool_call", {"aws": f"{environment}:{region} {service}.{operation}"}, tool_name="mcp__aws__aws_read", is_write=False)  # AWS-006
        result = await asyncio.to_thread(self._call, service, operation, region, parameters, credentials)
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


def build_aws_server(adapter: AwsReadAdapter):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tools = [
        tool(
            "aws_read",
            "Run a read-only AWS API call in a specific environment (staging|sandbox|production), e.g. environment=production service=logs operation=FilterLogEvents parameters='{\"logGroupName\": ...}'. Writes are denied by IAM and by this tool.",
            {"environment": str, "service": str, "operation": str, "region": str, "parameters": str},
        )(wrap(adapter.aws_read, logger)),
    ]
    return create_sdk_mcp_server(name="aws", version="1.0.0", tools=tools)
