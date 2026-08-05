"""sentry diagnostics as read-only in-process mcp tools (SEN-001..006).

strictly read-only in v1 (SEN-003): the adapter implements no write operations at all.
event payloads are aggressively trimmed before entering model context (TOL-006, SEN-005).
every access is attributable via the tool-call audit trail (SEN-006).
"""

import json
import logging

from agent_harness.adapters._util import _error, _text, wrap
from agent_harness.models import Task
from agent_harness.redact import redactor
from agent_harness.store import Store

logger = logging.getLogger("agent_harness.sentry")

SENTRY_API = "https://sentry.io/api/0"


class SentryAdapter:
    def __init__(self, store: Store, task: Task, organization: str, token: str, projects: list[str]):
        self.store = store
        self.task = task
        self.organization = organization
        self.token = token
        self.projects = projects

    async def list_issues(self, args: dict) -> dict:
        project = str(args.get("project", "")).strip()
        if self.projects and project not in self.projects:
            return _error(f"project {project!r} is not on the approved list {self.projects}")
        params = {"query": str(args.get("query", "is:unresolved")), "limit": str(min(int(args.get("limit", 10)), 20)), "sort": "freq"}
        issues = await self._request(f"/projects/{self.organization}/{project}/issues/", params)
        lines = [f"{i.get('shortId')} [{i.get('id')}]: {str(i.get('title', ''))[:120]} | {i.get('count')} events, {i.get('userCount')} users | last {i.get('lastSeen')}" for i in issues[:20]]
        return _text("\n".join(lines) or "no issues matched")

    async def get_issue(self, args: dict) -> dict:
        issue = await self._request(f"/issues/{str(args.get('issue_id', '')).strip()}/", {})
        summary = {key: issue.get(key) for key in ("shortId", "title", "culprit", "level", "status", "count", "userCount", "firstSeen", "lastSeen", "permalink")}
        summary["project"] = (issue.get("project") or {}).get("slug")
        return _text(json.dumps(summary, ensure_ascii=False))

    async def get_latest_event(self, args: dict) -> dict:
        """the stack trace, trimmed to what an investigation needs (SEN-002, SEN-005)."""
        event = await self._request(f"/issues/{str(args.get('issue_id', '')).strip()}/events/latest/", {})
        lines = [f"title: {event.get('title')}", f"message: {str(event.get('message', ''))[:300]}"]
        for entry in event.get("entries") or []:
            if entry.get("type") != "exception":
                continue
            for value in (entry.get("data") or {}).get("values") or []:
                lines.append(f"exception: {value.get('type')}: {str(value.get('value', ''))[:200]}")
                frames = ((value.get("stacktrace") or {}).get("frames") or [])[-12:]
                for frame in frames:
                    marker = " (in app)" if frame.get("inApp") else ""
                    lines.append(f"  {frame.get('filename')}:{frame.get('lineNo')} in {frame.get('function')}{marker}")
        tags = {tag.get("key"): tag.get("value") for tag in (event.get("tags") or [])[:12]}
        lines.append(f"tags: {json.dumps(tags, ensure_ascii=False)}")
        return _text("\n".join(lines))

    async def _request(self, path: str, params: dict):
        """the http seam — patched in unit tests."""
        import aiohttp

        headers = {"Authorization": f"Bearer {self.token}"}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(SENTRY_API + path, params=params) as response:
                if response.status >= 300:
                    body = redactor.redact(await response.text())[:300]
                    raise RuntimeError(f"sentry api GET {path} failed: {response.status} — {body}")
                return await response.json()


def build_sentry_server(adapter: SentryAdapter):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tools = [
        tool("list_issues", "List Sentry issues in an approved project (query uses Sentry search syntax).", {"project": str, "query": str, "limit": int})(wrap(adapter.list_issues, logger)),
        tool("get_issue", "Get one Sentry issue's summary by numeric id.", {"issue_id": str})(wrap(adapter.get_issue, logger)),
        tool("get_latest_event", "Get the latest event for a Sentry issue: exception, stack trace, tags.", {"issue_id": str})(wrap(adapter.get_latest_event, logger)),
    ]
    return create_sdk_mcp_server(name="sentry", version="1.0.0", tools=tools)
