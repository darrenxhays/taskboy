"""issues as in-process mcp tools.

the discovery skill reads past tasks/feedback/errors and records proposed issues; the
implementation skill reads approved issues and enqueues a spec-driven PR task per one; the
spec2pr skill reads its assigned spec and records the outcome. these are gated to skills that opt in
via `internal_tools:` frontmatter, and every call is on the tool-call audit trail like any other tool.
"""

import json
import logging

from taskboy.adapters._util import _error, _text, wrap
from taskboy.models import Task
from taskboy.store import ISSUE_STATUSES, Store

logger = logging.getLogger("taskboy.issues")

# tool names the runner adds to a session's allowlist when its skill opts into each capability server
ISSUES_TOOLS = [
    "mcp__issues__list_task_feedback",
    "mcp__issues__list_failed_tasks",
    "mcp__issues__list_recent_errors",
    "mcp__issues__record_issue",
    "mcp__issues__list_accepted_issues",
    "mcp__issues__list_existing_issues",
    "mcp__issues__get_issue",
    "mcp__issues__update_issue",
    "mcp__issues__list_issue_comments",
    "mcp__issues__post_issue_comment",
    "mcp__issues__update_issue_comment",
    "mcp__issues__delete_issue_comment",
    "mcp__issues__resolve_issue_comment",
    "mcp__issues__finish_issue",
]
ENQUEUE_TOOLS = ["mcp__enqueue__enqueue_spec_pr"]


class IssuesAdapter:
    def __init__(self, store: Store, task: Task, approved_repos: list[str], bot_name: str = "Agent"):
        self.store = store
        self.task = task
        self.approved_repos = approved_repos
        self.bot_name = bot_name

    def _audit(self, tool: str, detail: dict, is_write: bool) -> None:
        self.store.add_event(self.task.task_id, "tool_call", detail, tool_name=f"mcp__issues__{tool}", is_write=is_write)

    async def list_task_feedback(self, args: dict) -> dict:
        limit = min(int(args.get("limit", 50) or 50), 200)
        out = []
        for row in self.store.recent_feedback(limit):
            source = self.store.get_task(row["task_id"])
            out.append(
                {
                    "task_id": row["task_id"],
                    "rating": row["rating"],
                    "comment": row["comment"],
                    "request": source.request_text[:300] if source else None,
                    "result": (source.result_summary or "")[:300] if source else None,
                    "state": source.state if source else None,
                }
            )
        self._audit("list_task_feedback", {"count": len(out)}, False)
        return _text(json.dumps(out, ensure_ascii=False) if out else "no task feedback recorded yet")

    async def list_failed_tasks(self, args: dict) -> dict:
        limit = min(int(args.get("limit", 50) or 50), 200)
        offset = max(int(args.get("offset", 0) or 0), 0)
        query = str(args.get("query", "")).strip() or None
        task_type = str(args.get("task_type", "")).strip() or None
        tasks = self.store.list_tasks(state="failed", limit=limit, offset=offset, query=query, task_type=task_type) + self.store.list_tasks(state="blocked", limit=limit, offset=offset, query=query, task_type=task_type)
        out = [{"task_id": t.task_id, "state": t.state, "task_type": t.task_type, "request": t.request_text[:300], "error": t.error, "blocked_reason": t.blocked_reason} for t in tasks]
        self._audit("list_failed_tasks", {"count": len(out), "offset": offset, "query": query, "task_type": task_type}, False)
        return _text(json.dumps(out, ensure_ascii=False) if out else "no failed or blocked tasks")

    async def list_recent_errors(self, args: dict) -> dict:
        limit = min(int(args.get("limit", 50) or 50), 200)
        offset = max(int(args.get("offset", 0) or 0), 0)
        component = str(args.get("component", "")).strip() or None
        kind = str(args.get("kind", "")).strip() or None
        # traceback_chars stays a distinct "not provided" vs "0" so callers can opt out of the tail entirely
        # (0) without disturbing the default (600, byte-identical to the old hardcoded slice)
        raw_traceback_chars = args.get("traceback_chars")
        traceback_chars = 600 if raw_traceback_chars is None else int(raw_traceback_chars)
        traceback_chars = min(max(traceback_chars, 0), 2000)
        rows = self.store.recent_errors(limit, offset=offset, component=component, kind=kind)
        # counts go first so the recurring-error signal survives even when the row detail is truncated
        counts: dict[tuple[str, str], int] = {}
        for r in rows:
            key = (r["component"], r["kind"])
            counts[key] = counts.get(key, 0) + 1
        summary = [{"component": component_, "kind": kind_, "count": count} for (component_, kind_), count in sorted(counts.items(), key=lambda item: -item[1])]
        # the traceback tail carries the raising frame — enough to locate the fault without flooding context
        out = [
            {
                "component": r["component"],
                "kind": r["kind"],
                "message": (r["message"] or "")[:300],
                "traceback_tail": ((r["traceback"] or "")[-traceback_chars:] or None) if traceback_chars else None,
                "task_id": r["task_id"],
                "ts": r["ts"],
            }
            for r in rows
        ]
        self._audit("list_recent_errors", {"count": len(out), "offset": offset, "component": component, "kind": kind}, False)
        return _text(json.dumps({"counts": summary, "errors": out}, ensure_ascii=False) if out else "no errors recorded")

    async def record_issue(self, args: dict) -> dict:
        repo = str(args.get("repo", "")).strip()
        summary = str(args.get("summary", "")).strip()
        issue_type = str(args.get("issue_type", "")).strip()
        details = str(args.get("details", "")).strip()
        dedupe_key = str(args.get("dedupe_key", "")).strip()
        priority = args.get("priority", 50)
        if not (repo and summary and issue_type and details and dedupe_key):
            return _error("repo, summary, issue_type, details, and dedupe_key are all required")
        if repo not in self.approved_repos:
            return _error(f"repo must be one of {sorted(self.approved_repos)}")
        # cheap backstop: catch a duplicate proposal under a different dedupe_key before it gets recorded.
        # a matching dedupe_key is a refresh (handled by the store's upsert), not a duplicate.
        normalized = summary.strip().lower()
        for existing in self.store.list_issues():
            if existing["repo"] == repo and existing["dedupe_key"] != dedupe_key and existing["summary"].strip().lower() == normalized:
                # the store only upserts dedupe_key matches while the row is still proposed; past that, re-recording is a silent no-op
                if existing["status"] == "proposed":
                    return _error(f"an issue with this summary already exists as #{existing['id']} (proposed); " f"re-record with its dedupe_key '{existing['dedupe_key']}' if you mean to refresh it, or choose a genuinely different issue")
                return _error(f"an issue with this summary already exists as #{existing['id']} ({existing['status']}); " "it can no longer be refreshed by re-recording — choose a genuinely different issue")
        row = self.store.record_issue(dedupe_key, repo, summary, issue_type, details, priority)
        if row["status"] != "proposed":
            # the store's upsert is guarded by WHERE status = 'proposed', so a dedupe_key match on an
            # already-decided row silently wrote nothing — don't report success for a no-op
            return _error(f"an issue with this dedupe_key already exists as #{row['id']} ({row['status']}); it can no longer be refreshed by re-recording — choose a genuinely different dedupe_key")
        self._audit("record_issue", {"id": row["id"], "repo": repo, "dedupe_key": dedupe_key, "type": issue_type, "status": row["status"]}, True)
        return _text(f"recorded issue #{row['id']} ({row['status']}); re-recording the same dedupe_key refreshes it while it stays proposed")

    async def list_accepted_issues(self, args: dict) -> dict:
        # only a hand-typed or retried coordinator gets here without a reservation; gate on ever-reserved so a handed-off batch can't reserve a second one
        rows = self.store.issues_reserved_by(self.task.task_id)
        if not self.store.has_ever_reserved(self.task.task_id):
            rows = self.store.reserve_issues(self.task.task_id)
        out = [{"id": r["id"], "repo": r["repo"], "summary": r["summary"], "issue_type": r["issue_type"], "details": r["details"], "priority": r["priority"]} for r in rows]
        self._audit("list_accepted_issues", {"count": len(out)}, False)
        return _text(json.dumps(out, ensure_ascii=False) if out else "no approved issues are waiting to be implemented")

    async def list_existing_issues(self, args: dict) -> dict:
        limit = min(int(args.get("limit", 200) or 200), 500)
        offset = max(int(args.get("offset", 0) or 0), 0)
        repo = str(args.get("repo", "")).strip() or None
        status = str(args.get("status", "")).strip() or None
        keys_only = bool(args.get("keys_only", False))
        if repo is not None and repo not in self.approved_repos:
            return _error(f"repo must be blank or one of {sorted(self.approved_repos)}")
        if status is not None and status not in ISSUE_STATUSES:
            return _error(f"status must be blank or one of {list(ISSUE_STATUSES)}")
        rows = self.store.list_issues(status=status, limit=limit, offset=offset, repo=repo)
        if keys_only:
            out = [{"id": r["id"], "dedupe_key": r["dedupe_key"], "status": r["status"]} for r in rows]
        else:
            out = [{"id": r["id"], "dedupe_key": r["dedupe_key"], "repo": r["repo"], "summary": r["summary"], "issue_type": r["issue_type"], "status": r["status"], "priority": r["priority"]} for r in rows]
        self._audit("list_existing_issues", {"count": len(out), "offset": offset, "status": status, "keys_only": keys_only}, False)
        return _text(json.dumps(out, ensure_ascii=False) if out else "no issues recorded yet")

    async def get_issue(self, args: dict) -> dict:
        row = self.store.get_issue(int(args.get("id", 0) or 0))
        if row is None:
            return _error("issue not found")
        keep = ("id", "repo", "summary", "issue_type", "details", "priority", "status", "spec", "pr_url")
        self._audit("get_issue", {"id": row["id"], "status": row["status"]}, False)
        return _text(json.dumps({key: row[key] for key in keep}, ensure_ascii=False))

    async def update_issue(self, args: dict) -> dict:
        issue_id = int(args.get("id", 0) or 0)
        summary = str(args.get("summary", "")).strip()
        details = str(args.get("details", "")).strip()
        priority = args.get("priority")
        if not issue_id or not summary or not details or not isinstance(priority, int) or isinstance(priority, bool) or not 1 <= priority <= 100:
            return _error("id, summary, details, and priority (1-100) are required")
        row = self.store.update_issue(issue_id, summary, details, priority)
        if row is None:
            return _error(f"issue #{issue_id} is not editable")
        self._audit("update_issue", {"id": issue_id, "priority": priority}, True)
        return _text(f"updated issue #{issue_id}")

    def _threaded_comments(self, issue_id: int) -> list[dict]:
        attachments: dict[int, list[str]] = {}
        for attachment in self.store.list_issue_attachments(issue_id):
            if attachment["comment_id"] is not None:
                attachments.setdefault(int(attachment["comment_id"]), []).append(attachment["filename"])
        roots: list[dict] = []
        by_id: dict[int, dict] = {}
        for row in self.store.list_issue_comments(issue_id):
            item = {
                "id": row["id"],
                "parent_comment_id": row["parent_comment_id"],
                "author": row["author"],
                "body": row["body"],
                "resolved": bool(row["resolved"]),
                "deleted": row["deleted_at"] is not None,
                "edited_at": row["edited_at"],
                "created_at": row["created_at"],
                "attachments": attachments.get(row["id"], []),
                "replies": [],
            }
            by_id[row["id"]] = item
            if row["parent_comment_id"] is None:
                roots.append(item)
            elif row["parent_comment_id"] in by_id:
                by_id[row["parent_comment_id"]]["replies"].append(item)
        return roots

    async def list_issue_comments(self, args: dict) -> dict:
        issue_id = int(args.get("issue_id", 0) or 0)
        if self.store.get_issue(issue_id) is None:
            return _error("issue not found")
        comments = self._threaded_comments(issue_id)
        self._audit("list_issue_comments", {"issue_id": issue_id, "count": self.store.count_issue_comments(issue_id)}, False)
        return _text(json.dumps(comments, ensure_ascii=False))

    async def post_issue_comment(self, args: dict) -> dict:
        issue_id = int(args.get("issue_id", 0) or 0)
        parent_id = int(args.get("parent_comment_id", 0) or 0) or None
        body = str(args.get("body", "")).strip()
        if not issue_id or not body:
            return _error("issue_id and body are required")
        try:
            row = self.store.add_issue_comment(issue_id, "agent", body, parent_id)
        except (LookupError, ValueError) as exc:
            return _error(str(exc))
        self._audit("post_issue_comment", {"issue_id": issue_id, "comment_id": row["id"], "parent_comment_id": parent_id}, True)
        return _text(f"posted comment #{row['id']} on issue #{issue_id}")

    async def update_issue_comment(self, args: dict) -> dict:
        comment_id = int(args.get("comment_id", 0) or 0)
        body = str(args.get("body", "")).strip()
        if not body:
            return _error("body is required")
        comment = self.store.get_issue_comment(comment_id)
        if comment is None:
            return _error("comment not found")
        if comment["author"] != "agent":
            return _error("only the agent's comments can be edited")
        if comment["deleted_at"] is not None:
            return _error("deleted comments cannot be edited")
        row = self.store.update_issue_comment(comment_id, body)
        if row is None:
            return _error("comment could not be edited")
        self._audit("update_issue_comment", {"comment_id": comment_id}, True)
        return _text(f"updated comment #{comment_id}")

    async def delete_issue_comment(self, args: dict) -> dict:
        comment_id = int(args.get("comment_id", 0) or 0)
        comment = self.store.get_issue_comment(comment_id)
        if comment is None:
            return _error("comment not found")
        if comment["author"] != "agent":
            return _error("only the agent's comments can be deleted")
        if comment["deleted_at"] is not None:
            return _error("comment is already deleted")
        self.store.delete_issue_comment(comment_id)
        self._audit("delete_issue_comment", {"comment_id": comment_id}, True)
        return _text(f"deleted comment #{comment_id}")

    async def resolve_issue_comment(self, args: dict) -> dict:
        comment_id = int(args.get("comment_id", 0) or 0)
        row = self.store.resolve_issue_comment(comment_id, "agent")
        if row is None:
            return _error("comment not found")
        self._audit("resolve_issue_comment", {"comment_id": comment_id}, True)
        return _text(f"resolved comment #{comment_id}")

    async def finish_issue(self, args: dict) -> dict:
        issue_id = int(args.get("id", 0) or 0)
        status = str(args.get("status", "")).strip()
        pr_url = str(args.get("pr_url", "")).strip() or None
        if status not in ("done", "failed"):
            return _error("status must be 'done' or 'failed'")
        # a PR is open but not yet merged — record it as in_review; housekeeping resolves it to done/failed
        stored_status = "in_review" if status == "done" and pr_url else status
        row = self.store.finish_issue(issue_id, stored_status, pr_url)
        if row is None:
            return _error(f"issue #{issue_id} is not in progress, so its outcome cannot be recorded")
        self._audit("finish_issue", {"id": issue_id, "status": stored_status, "pr_url": pr_url}, True)
        if stored_status == "in_review":
            return _text(f"issue #{issue_id} marked in_review — it will move to done once {pr_url} merges, or failed if it's closed unmerged")
        return _text(f"issue #{issue_id} marked {stored_status}")


class EnqueueAdapter:
    """lets the implementation skill hand one PR off to a fresh sub-agent task (the spec2pr skill, on its own model)."""

    def __init__(self, store: Store, task: Task):
        self.store = store
        self.task = task

    async def enqueue_spec_pr(self, args: dict) -> dict:
        issue_id = int(args.get("id", 0) or 0)
        spec = str(args.get("spec", "")).strip()
        if not issue_id or not spec:
            return _error("id (an approved issue id) and spec (the full implementation spec) are both required")
        issue = self.store.get_issue(issue_id)
        if issue is None:
            return _error(f"issue #{issue_id} not found")
        parent = self.task

        # a re-call for an issue this same parent already claimed and enqueued: reply idempotently, no new claim/child
        if issue["status"] == "in_progress" and issue["task_id"]:
            existing_child = self.store.get_task(issue["task_id"])
            if existing_child is not None and existing_child.parent_task_id == parent.task_id:
                return _text(f"a PR task for issue #{issue_id} was already enqueued ({issue['task_id']})")

        reserved_for_this_run = issue["status"] == "implementation_queued" and issue.get("reserved_by") == parent.task_id
        legacy_manual_approval = issue["status"] == "approved"
        if not (reserved_for_this_run or legacy_manual_approval):
            return _error(f"issue #{issue_id} is {issue['status']}, not reserved for this run or approved — only a reserved or approved issue can be enqueued")

        # claim before creating the child so a failed claim never leaves an orphan task behind
        if self.store.start_issue(issue_id, None, spec) is None:
            return _error(f"issue #{issue_id} could not be claimed (its status changed)")

        # inherit the parent's slack context so replies land in the same thread; a per-issue message_ts keeps intake idempotent
        child, created = self.store.create_task(
            slack_team_id=parent.slack_team_id,
            slack_channel_id=parent.slack_channel_id,
            slack_thread_ts=parent.slack_thread_ts,
            slack_message_ts=f"{parent.task_id}:spec:{issue_id}",
            slack_user_id=parent.slack_user_id,
            request_text=f"/spec2pr {issue_id}",
            parent_task_id=parent.task_id,
        )
        if not created:
            return _text(f"a PR task for issue #{issue_id} was already enqueued ({child.task_id})")
        self.store.link_issue_task(issue_id, child.task_id)
        self.store.add_event(parent.task_id, "issue_enqueued", {"issue_id": issue_id, "child_task_id": child.task_id}, tool_name="mcp__enqueue__enqueue_spec_pr", is_write=True)
        return _text(f"enqueued PR task {child.task_id} for issue #{issue_id}; it runs the /spec2pr skill on its own model and reports its own outcome")


def build_issues_server(adapter: IssuesAdapter):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tools = [
        tool("list_task_feedback", "List recent requester feedback on tasks, with each task's request and result, to find where the system fell short.", {"limit": int})(wrap(adapter.list_task_feedback, logger)),
        tool(
            "list_failed_tasks",
            "List recent failed and blocked tasks with their error/blocked reason, to find recurring failure modes. " "Use offset to page past the ~4000-char response cap; task_type/query narrow further.",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer", "description": "skip this many rows (applied per state: failed, blocked) — page through a large backlog"},
                    "task_type": {"type": "string", "description": "keep only rows with this exact task_type"},
                    "query": {"type": "string", "description": "substring match against task_id or request_text"},
                },
                "additionalProperties": False,
            },
        )(wrap(adapter.list_failed_tasks, logger)),
        tool(
            "list_recent_errors",
            "List recent internal errors recorded across the system's components. Use component/kind to narrow " "and offset to page past the ~4000-char response cap; traceback_chars trims or (at 0) omits the traceback tail.",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer", "description": "skip this many rows — page through a large backlog"},
                    "component": {"type": "string", "description": "exact component filter, e.g. 'review_poller'"},
                    "kind": {"type": "string", "description": "exact error kind filter, e.g. 'RuntimeError'"},
                    "traceback_chars": {"type": "integer", "description": "length of traceback_tail per row (default 600); 0 omits it"},
                },
                "additionalProperties": False,
            },
        )(wrap(adapter.list_recent_errors, logger)),
        tool(
            "record_issue",
            "Record one repo-scoped issue. repo must be approved; dedupe_key is stable across discovery runs. issue_type may be feature_request, bug, security, reliability, performance, or another concise category.",
            {"repo": str, "summary": str, "issue_type": str, "details": str, "dedupe_key": str, "priority": int},
        )(wrap(adapter.record_issue, logger)),
        tool("list_accepted_issues", "List the issues reserved for this implementation run, reserving up to 5 approved ones when none are reserved yet.", {})(wrap(adapter.list_accepted_issues, logger)),
        tool(
            "list_existing_issues",
            "List existing issues so discovery can avoid recording duplicates. Check this before record_issue. " "Use status/offset to page through the full table past the ~4000-char response cap; keys_only returns " "compact id/dedupe_key/status rows so more fit per call.",
            {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer", "description": "skip this many rows — page through a large backlog"},
                    "repo": {"type": "string"},
                    "status": {"type": "string", "description": f"one of {list(ISSUE_STATUSES)}"},
                    "keys_only": {"type": "boolean", "description": "return only id/dedupe_key/status per row, to fit more rows under the truncation cap"},
                },
                "additionalProperties": False,
            },
        )(wrap(adapter.list_existing_issues, logger)),
        tool("get_issue", "Get one issue by id, including its stored implementation spec.", {"id": int})(wrap(adapter.get_issue, logger)),
        tool("update_issue", "Update an editable issue's title, markdown description, and priority after reconciling its discussion.", {"id": int, "summary": str, "details": str, "priority": int})(wrap(adapter.update_issue, logger)),
        tool("list_issue_comments", "List an issue's threaded discussion, resolved/deleted state, and attachment filenames.", {"issue_id": int})(wrap(adapter.list_issue_comments, logger)),
        tool(
            "post_issue_comment",
            f"Post a markdown comment or optional one-level reply as {adapter.bot_name}.",
            {
                "type": "object",
                "properties": {"issue_id": {"type": "integer"}, "body": {"type": "string"}, "parent_comment_id": {"type": "integer"}},
                "required": ["issue_id", "body"],
                "additionalProperties": False,
            },
        )(wrap(adapter.post_issue_comment, logger)),
        tool("update_issue_comment", f"Edit one of {adapter.bot_name}'s existing comments.", {"comment_id": int, "body": str})(wrap(adapter.update_issue_comment, logger)),
        tool("delete_issue_comment", f"Soft-delete one of {adapter.bot_name}'s stale comments.", {"comment_id": int})(wrap(adapter.delete_issue_comment, logger)),
        tool("resolve_issue_comment", "Mark an answered discussion comment resolved.", {"comment_id": int})(wrap(adapter.resolve_issue_comment, logger)),
        tool(
            "finish_issue",
            "Record the outcome of an in-progress issue: status 'done' (with the pr_url — stored as in_review until the PR merges) or 'failed'.",
            {"id": int, "status": str, "pr_url": str},
        )(wrap(adapter.finish_issue, logger)),
    ]
    return create_sdk_mcp_server(name="issues", version="1.0.0", tools=tools)


def build_enqueue_server(adapter: EnqueueAdapter):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tools = [
        tool("enqueue_spec_pr", "Hand one approved issue off to a fresh sub-agent that opens a PR. Pass the issue id and the full implementation spec (markdown). Call once per issue.", {"id": int, "spec": str})(wrap(adapter.enqueue_spec_pr, logger)),
    ]
    return create_sdk_mcp_server(name="enqueue", version="1.0.0", tools=tools)
