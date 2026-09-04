"""jira cloud operations as permission-aware in-process mcp tools (JIR-001..010).

a dedicated service-account api token (no delete permissions in jira's own scheme — JIR-006/007)
never reaches the model. created issues always carry the `taskboy` + `agent-task-{id}` labels and
a task footer (JIR-008); creation checks the artifacts table and a jql label search first (JIR-009).
projects and issue types are validated against config (JIR-010).
"""

import json
import logging
import re

from taskboy.adapters._util import AccessDenied, _error, _text, wrap
from taskboy.models import Task
from taskboy.redact import redactor
from taskboy.store import Store

logger = logging.getLogger("taskboy.jira")


class JiraAdapter:
    def __init__(self, store: Store, task: Task, site: str, email: str, api_token: str, projects: list[str], issue_types: list[str], on_milestone=None, story_points_field: str = "", bot_name: str = "Agent"):
        self.store = store
        self.task = task
        self.site = site.rstrip("/")
        self.email = email
        self.api_token = api_token
        self.projects = projects
        self.issue_types = issue_types
        self.on_milestone = on_milestone
        self.story_points_field = story_points_field
        self.bot_name = bot_name

    # -- tools -------------------------------------------------------------------

    async def search_issues(self, args: dict) -> dict:
        data = await self._request("GET", "/rest/api/3/search/jql", params={"jql": str(args.get("jql", "")), "maxResults": min(int(args.get("max_results", 10)), 20), "fields": "summary,status,issuetype,assignee"})
        lines = []
        for issue in data.get("issues") or []:
            fields = issue.get("fields") or {}
            lines.append(f"{issue.get('key')}: {str(fields.get('summary', ''))[:120]} [{(fields.get('status') or {}).get('name')}] ({(fields.get('issuetype') or {}).get('name')})")
        return _text("\n".join(lines) or "no issues matched")

    async def get_issue(self, args: dict) -> dict:
        key = str(args.get("key", "")).strip().upper()
        data = await self._request("GET", f"/rest/api/3/issue/{key}", params={"fields": "summary,status,issuetype,assignee,labels,description,priority"})
        fields = data.get("fields") or {}
        summary = {
            "key": data.get("key"),
            "summary": fields.get("summary"),
            "status": (fields.get("status") or {}).get("name"),
            "type": (fields.get("issuetype") or {}).get("name"),
            "assignee": (fields.get("assignee") or {}).get("displayName"),
            "labels": fields.get("labels"),
            "priority": (fields.get("priority") or {}).get("name"),
            "description": _adf_to_text(fields.get("description"))[:2000],
        }
        return _text(json.dumps(summary, ensure_ascii=False))

    async def search_users(self, args: dict) -> dict:
        users = await self._request("GET", "/rest/api/3/user/search", params={"query": str(args.get("query", "")), "maxResults": 10})
        lines = [f"{user.get('accountId')}: {user.get('displayName')} ({user.get('emailAddress') or 'hidden'})" for user in users]
        return _text("\n".join(lines) or "no users matched")

    async def list_boards(self, args: dict) -> dict:
        params: dict = {"maxResults": 20}
        project = str(args.get("project") or "").strip().upper()
        if project:
            params["projectKeyOrId"] = project
        data = await self._request("GET", "/rest/agile/1.0/board", params=params)
        lines = [f"{board.get('id')}: {board.get('name')} ({board.get('type')})" for board in data.get("values") or []]
        return _text("\n".join(lines) or "no boards matched")

    async def list_sprints(self, args: dict) -> dict:
        board_id = int(args.get("board_id") or 0)
        data = await self._request("GET", f"/rest/agile/1.0/board/{board_id}/sprint", params={"state": "active,future", "maxResults": 20})
        lines = [f"{sprint.get('id')}: {sprint.get('name')} [{sprint.get('state')}]" for sprint in data.get("values") or []]
        return _text("\n".join(lines) or "no active or future sprints matched")

    async def create_issue(self, args: dict) -> dict:
        project = str(args.get("project", "")).strip().upper()
        issue_type = str(args.get("issue_type", "")).strip()
        summary = str(args.get("summary", "")).strip()
        if self.projects and project not in self.projects:
            return _error(f"project {project!r} is not on the approved list {self.projects}")
        if self.issue_types and issue_type not in self.issue_types:
            return _error(f"issue type {issue_type!r} is not permitted; use one of {self.issue_types}")
        if not summary:
            return _error("summary is required")

        # idempotency layer 1: this task already created an issue with this summary (JIR-009, ORC-012)
        for artifact in self.store.artifacts_for(self.task.task_id):
            if artifact["kind"] == "jira_issue":
                return _text(f"this task already created {artifact['external_id']} ({artifact['url']}) — comment on or update it instead of creating another")
        # idempotency layer 2: jql on the task label catches the crash-after-create window
        existing = await self._request("GET", "/rest/api/3/search/jql", params={"jql": f'labels = "agent-task-{self.task.task_id}"', "maxResults": 1, "fields": "summary"})
        if existing.get("issues"):
            issue = existing["issues"][0]
            self._record_issue(issue["key"])
            return _text(f"an issue already exists for this task: {issue['key']} — update it instead of creating another")

        description = str(args.get("description", "")).rstrip()
        footer = f"Created by {self.bot_name} (taskboy) for Slack task {self.task.task_id} in <#{self.task.slack_channel_id}>."
        fields: dict = {
            "project": {"key": project},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": _adf(f"{description}\n\n{footer}" if description else footer),
            "labels": ["taskboy", f"agent-task-{self.task.task_id}"],  # JIR-008
        }
        assignee = str(args.get("assignee_account_id") or "").strip()
        parent_key = str(args.get("parent_key") or "").strip()
        story_points = float(args.get("story_points") or 0)
        if assignee:
            fields["assignee"] = {"accountId": assignee}
        if parent_key:
            fields["parent"] = {"key": parent_key.upper()}
        if story_points > 0 and self.story_points_field:
            fields[self.story_points_field] = story_points
        payload = {"fields": fields}
        created = await self._request("POST", "/rest/api/3/issue", payload)
        key = str(created["key"])
        points_note = ""
        if story_points > 0 and not self.story_points_field:
            points_note = f" — story points were not set: an operator must set story_points_field in services/jira.yaml. this is an access problem an operator can fix: call request_permission with kind='access' and target='jira:story_points_field', quoting this message as the reason, then stop working. Once granted, call set_story_points with key {key} and points {story_points:g} instead of calling create_issue again; the issue already exists."
        self._record_issue(key)
        if self.on_milestone:
            await self.on_milestone(f"Created Jira issue {key}")
        return _text(f"created {key}: {self.site}/browse/{key}{points_note}")

    async def add_comment(self, args: dict) -> dict:
        key = str(args.get("key", "")).strip().upper()
        comment = await self._request("POST", f"/rest/api/3/issue/{key}/comment", {"body": _adf(str(args.get("body", "")))})
        self.store.add_artifact(self.task.task_id, "jira_comment", f"{key}/comment/{comment.get('id')}", f"{self.site}/browse/{key}")
        return _text(f"commented on {key}")

    async def assign_issue(self, args: dict) -> dict:
        key = str(args.get("key", "")).strip().upper()
        account_id = str(args.get("account_id", "")).strip()
        await self._request("PUT", f"/rest/api/3/issue/{key}/assignee", {"accountId": account_id})
        self.store.add_event(self.task.task_id, "tool_call", {"jira_assignment": f"{key} -> {account_id}"}, tool_name="mcp__jira__assign_issue", is_write=True)
        return _text(f"assigned {key} to account {account_id}")

    async def move_to_sprint(self, args: dict) -> dict:
        key = str(args.get("key", "")).strip().upper()
        sprint_id = int(args.get("sprint_id") or 0)
        await self._request("POST", f"/rest/agile/1.0/sprint/{sprint_id}/issue", {"issues": [key]})
        self.store.add_event(self.task.task_id, "tool_call", {"jira_sprint": f"{key} -> {sprint_id}"}, tool_name="mcp__jira__move_to_sprint", is_write=True)
        return _text(f"moved {key} to sprint {sprint_id}")

    async def set_epic(self, args: dict) -> dict:
        key = str(args.get("key", "")).strip().upper()
        epic_key = str(args.get("epic_key", "")).strip().upper()
        await self._request("PUT", f"/rest/api/3/issue/{key}", {"fields": {"parent": {"key": epic_key}}})
        self.store.add_event(self.task.task_id, "tool_call", {"jira_epic": f"{key} -> {epic_key}"}, tool_name="mcp__jira__set_epic", is_write=True)
        return _text(f"set {epic_key} as the epic for {key}")

    async def set_story_points(self, args: dict) -> dict:
        if not self.story_points_field:
            raise AccessDenied("jira", "story_points_field", "story points field is not configured — an operator must set story_points_field in services/jira.yaml (the Story Points custom field id)")
        key = str(args.get("key", "")).strip().upper()
        points = float(args.get("points") or 0)
        await self._request("PUT", f"/rest/api/3/issue/{key}", {"fields": {self.story_points_field: points}})
        self.store.add_event(self.task.task_id, "tool_call", {"jira_story_points": f"{key} -> {points}"}, tool_name="mcp__jira__set_story_points", is_write=True)
        return _text(f"set {key} story points to {points:g}")

    async def transition_issue(self, args: dict) -> dict:
        key = str(args.get("key", "")).strip().upper()
        wanted = str(args.get("transition", "")).strip().lower()
        data = await self._request("GET", f"/rest/api/3/issue/{key}/transitions")
        match = next((t for t in data.get("transitions") or [] if str(t.get("name", "")).lower() == wanted), None)
        if match is None:
            names = [t.get("name") for t in data.get("transitions") or []]
            return _error(f"transition {wanted!r} is not available for {key}; available: {names}")
        await self._request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": match["id"]}})
        self.store.add_event(self.task.task_id, "tool_call", {"jira_transition": f"{key} -> {match.get('name')}"}, tool_name="mcp__jira__transition_issue", is_write=True)
        return _text(f"transitioned {key} to {match.get('name')}")

    async def link_pr(self, args: dict) -> dict:
        """attach a github pull request to a jira issue as a remote link (JIR-005, GIT-013)."""
        key = str(args.get("key", "")).strip().upper()
        url = str(args.get("pr_url", "")).strip()
        if not url.startswith("https://github.com/"):
            return _error("pr_url must be a github pull request url")
        await self._request("POST", f"/rest/api/3/issue/{key}/remotelink", {"object": {"url": url, "title": str(args.get("title", "")) or url}})
        self.store.add_artifact(self.task.task_id, "jira_link", f"{key} -> {url}", url)
        return _text(f"linked {url} to {key}")

    # -- plumbing ------------------------------------------------------------------

    def _record_issue(self, key: str) -> None:
        self.store.add_artifact(self.task.task_id, "jira_issue", key, f"{self.site}/browse/{key}")

    async def _request(self, method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
        """the http seam — patched in unit tests. basic auth with the service-account token (TOL-007)."""
        import aiohttp

        auth = aiohttp.BasicAuth(self.email, self.api_token)
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.request(method, self.site + path, json=payload, params=params, headers={"Accept": "application/json"}) as response:
                if response.status in (401, 403):
                    body = redactor.redact(await response.text())[:300]
                    raise AccessDenied("jira", _project_scope(path, payload), f"jira api {method} {path} denied: {response.status} — {body}")
                if response.status >= 300:
                    body = redactor.redact(await response.text())[:300]
                    raise RuntimeError(f"jira api {method} {path} failed: {response.status} — {body}")
                return await response.json() if response.status != 204 else {}


def _adf(text: str) -> dict:
    """minimal atlassian document format: one paragraph per text block."""
    paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": block}]} for block in text.split("\n\n") if block.strip()]
    return {"type": "doc", "version": 1, "content": paragraphs or [{"type": "paragraph", "content": [{"type": "text", "text": " "}]}]}


def _adf_to_text(node) -> str:
    """flatten adf to plain text for bounded model context (TOL-006)."""
    if node is None:
        return ""
    if isinstance(node, dict):
        own = node.get("text", "")
        children = "".join(_adf_to_text(child) for child in node.get("content") or [])
        suffix = "\n" if node.get("type") in ("paragraph", "heading") else ""
        return f"{own}{children}{suffix}"
    return ""


def build_jira_server(adapter: JiraAdapter):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tools = [
        tool("search_issues", "Search Jira issues with JQL.", {"jql": str, "max_results": int})(wrap(adapter.search_issues, logger)),
        tool("search_users", "Search Jira users and return account ids for assignment.", {"query": str})(wrap(adapter.search_users, logger)),
        tool("get_issue", "Get one Jira issue (summary, status, description, labels).", {"key": str})(wrap(adapter.get_issue, logger)),
        tool(
            "list_boards",
            "List Jira boards, optionally for one project.",
            {"type": "object", "properties": {"project": {"type": "string"}}, "required": []},
        )(wrap(adapter.list_boards, logger)),
        tool("list_sprints", "List active and future sprints on a Jira board.", {"board_id": int})(wrap(adapter.list_sprints, logger)),
        tool(
            "create_issue",
            "Create a Jira issue in an approved project. Search for duplicates first.",
            {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "issue_type": {"type": "string"},
                    "summary": {"type": "string"},
                    "description": {"type": "string"},
                    "assignee_account_id": {"type": "string"},
                    "parent_key": {"type": "string"},
                    "story_points": {"type": "number"},
                },
                "required": ["project", "issue_type", "summary", "description"],
            },
        )(wrap(adapter.create_issue, logger)),
        tool("add_comment", "Add a comment to a Jira issue.", {"key": str, "body": str})(wrap(adapter.add_comment, logger)),
        tool("assign_issue", "Assign a Jira issue using an account id from search_users.", {"key": str, "account_id": str})(wrap(adapter.assign_issue, logger)),
        tool("transition_issue", "Move a Jira issue through its workflow (e.g. 'In Progress', 'Done').", {"key": str, "transition": str})(wrap(adapter.transition_issue, logger)),
        tool("move_to_sprint", "Move a Jira issue into a sprint.", {"key": str, "sprint_id": int})(wrap(adapter.move_to_sprint, logger)),
        tool("set_epic", "Set the epic parent for a Jira story.", {"key": str, "epic_key": str})(wrap(adapter.set_epic, logger)),
        tool("set_story_points", "Set the story point estimate on a Jira issue.", {"key": str, "points": float})(wrap(adapter.set_story_points, logger)),
        tool("link_pr", "Attach a GitHub pull request link to a Jira issue.", {"key": str, "pr_url": str, "title": str})(wrap(adapter.link_pr, logger)),
    ]
    return create_sdk_mcp_server(name="jira", version="1.0.0", tools=tools)


def _project_scope(path: str, payload: dict | None) -> str:
    """best-effort project key for an access target: from the create payload, else the issue key in the path, else the api."""
    project = ((payload or {}).get("fields") or {}).get("project") or {}
    if isinstance(project, dict) and project.get("key"):
        return str(project["key"])
    match = re.search(r"/issue/([A-Za-z][A-Za-z0-9_]+)-\d+", path)
    return match.group(1).upper() if match else "api"
