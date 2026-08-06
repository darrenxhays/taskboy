"""tool policy + audit for sub-agent sessions (TOL-004/005, §8.4).

pure functions decide; the TaskHooks callbacks apply them inside a session. platform
enforcement (github rulesets, IAM, jira permissions) is always the real control — this
layer is defense-in-depth and the audit feed. every denial is recorded (§10).
"""

import re

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from taskboy.models import Task
from taskboy.store import Store

# read/write classification per tool; unknown tools are conservatively write (TOL-005)
TOOL_CLASSIFICATION = {
    "Read": "read",
    "Grep": "read",
    "Glob": "read",
    "WebFetch": "read",
    "WebSearch": "read",
    "Bash": "write",  # conservative: bash can do anything
    "Write": "write",
    "Edit": "write",
    "NotebookEdit": "write",
    "mcp__harness__report_progress": "read",
    "mcp__harness__report_blocked": "read",
    "mcp__harness__request_permission": "read",
    "mcp__issues__list_task_feedback": "read",
    "mcp__issues__list_failed_tasks": "read",
    "mcp__issues__list_recent_errors": "read",
    "mcp__issues__record_issue": "write",
    "mcp__issues__list_accepted_issues": "read",
    "mcp__issues__list_existing_issues": "read",
    "mcp__issues__get_issue": "read",
    "mcp__issues__update_issue": "write",
    "mcp__issues__list_issue_comments": "read",
    "mcp__issues__post_issue_comment": "write",
    "mcp__issues__update_issue_comment": "write",
    "mcp__issues__delete_issue_comment": "write",
    "mcp__issues__resolve_issue_comment": "write",
    "mcp__issues__finish_issue": "write",
    "mcp__enqueue__enqueue_spec_pr": "write",
    "mcp__github__get_pull_request": "read",
    "mcp__github__list_pull_requests": "read",
    "mcp__github__list_pr_files": "read",
    "mcp__github__list_pr_comments": "read",
    "mcp__github__create_pull_request": "write",
    "mcp__github__comment_on_pull_request": "write",
    "mcp__github__create_pr_review": "write",
    "mcp__github__reply_to_pr_comment": "write",
    "mcp__github__resolve_pr_thread": "write",
    "mcp__github__close_pull_request": "write",
    "mcp__github__delete_branch": "write",
    "mcp__github__create_release": "write",
    "mcp__jira__search_issues": "read",
    "mcp__jira__search_users": "read",
    "mcp__jira__get_issue": "read",
    "mcp__jira__list_boards": "read",
    "mcp__jira__list_sprints": "read",
    "mcp__jira__create_issue": "write",
    "mcp__jira__add_comment": "write",
    "mcp__jira__assign_issue": "write",
    "mcp__jira__transition_issue": "write",
    "mcp__jira__move_to_sprint": "write",
    "mcp__jira__set_epic": "write",
    "mcp__jira__set_story_points": "write",
    "mcp__jira__link_pr": "write",
    "mcp__sentry__list_issues": "read",
    "mcp__sentry__get_issue": "read",
    "mcp__sentry__get_latest_event": "read",
    "mcp__aws__aws_read": "read",
    "mcp__slack__channel_history": "read",
    "mcp__slack__thread_replies": "read",
    "mcp__slack__user_info": "read",
    "mcp__slack__get_file": "read",
    "mcp__slack__send_dm": "write",
    "mcp__confluence__search_pages": "read",
    "mcp__confluence__get_page": "read",
}


def classify_tool(tool_name: str) -> str:
    return TOOL_CLASSIFICATION.get(tool_name, "write")


def profile_permits_writes(allowed_tools: list[str]) -> bool:
    """a profile tier is write-capable if its base allowlist holds a *recognized* write tool other than Bash.
    Bash is present in every tier for investigation and is separately policed (hooks + read-only git token),
    so its presence does not make an otherwise read-only profile eligible to gain Write/Edit/write-MCP tools.
    Unrecognized tools are deliberately NOT treated as write here: an unclassified read tool in a read-only
    profile (e.g. mcp__slack__user_info) must not flip the tier to write-capable and reopen the gate (§8.4)."""
    return any(TOOL_CLASSIFICATION.get(tool_name) == "write" and tool_name != "Bash" for tool_name in allowed_tools)


def tool_grantable(tool_name: str, profile_allowed_tools: list[str]) -> bool:
    """whether a mid-task grant may admit this tool: it must be recognized, and a read-only profile
    tier can only ever gain read tools — never a write tool it was deliberately denied (§8.4)."""
    if tool_name not in TOOL_CLASSIFICATION:
        return False
    if classify_tool(tool_name) == "read":
        return True
    return profile_permits_writes(profile_allowed_tools)


def repo_grantable(target: str, approved_repos: list[str], accessible_repos: set[str] | None) -> bool:
    """whether an out-of-role-scope repo may be escalated for operator review (issue #39)."""
    if "/" not in target:
        return False
    owner, _, name = target.partition("/")
    if not owner or not name:
        return False
    allowed_orgs = {repo.split("/", 1)[0] for repo in approved_repos if "/" in repo}
    if owner not in allowed_orgs:
        return False
    if accessible_repos is not None and name not in accessible_repos:
        return False
    return True


def bash_denial(command: str, protected_branches: list[str]) -> str | None:
    """returns a denial reason, or None when the command is allowed."""
    if "169.254.169.254" in command:
        return "access to the instance metadata service is blocked"
    # deliberately non-exhaustive defense-in-depth; host controls remain authoritative.
    if re.search(r"/proc/(?:self|\d+)/environ\b", command) or ("os.environ" in command and re.search(r"\bpython(?:3(?:\.\d+)?)?\b[^;&|]*\s-c\b", command)):
        return "dumping the process environment is blocked"
    if re.search(r"\bprintenv\b", command) or re.search(r"(^|[;&|]\s*)env\s*($|[;&|>])", command.strip()):
        return "dumping the process environment is blocked"
    if re.search(r"\bgit\s+push\b[^;&|]*(\s--force\b|\s-f\b)", command):
        return "force pushes are blocked"
    for branch in protected_branches:
        if re.search(rf"\bgit\s+push\b[^;&|]*\b{re.escape(branch)}\b", command):
            return f"pushing to protected branch '{branch}' is blocked"
    return None


class TaskHooks:
    """per-session policy + audit, wired as a PreToolUse hook.

    NOT can_use_tool: allowed_tools entries auto-approve whole tools before that callback
    is consulted (CanUseToolShadowedWarning), which would silently skip audit and denials.
    a PreToolUse hook fires for every call regardless of allow rules.
    """

    def __init__(self, store: Store, task: Task, protected_branches: list[str], allowed_tools: list[str] | None = None):
        self.store = store
        self.task = task
        self.protected_branches = protected_branches
        self.allowed_tools = allowed_tools  # None = no profile restriction; otherwise deny anything off-list

    def _decide(self, tool_name: str, tool_input: dict) -> str | None:
        """audit the call and return a denial reason, or None to allow."""
        if self.allowed_tools is not None and tool_name not in self.allowed_tools:
            # the profile allowlist is the contract; without this, non-allowlisted tools
            # (e.g. Task, which spawns sub-agents) fall through to the callback and slip past
            reason = f"tool {tool_name} is not in this task's profile"
            self.store.add_event(self.task.task_id, "security_denial", {"reason": reason, "input": _digest(tool_input)}, tool_name=tool_name, is_write=True)
            return reason
        if tool_name == "Bash":
            bash_reason = bash_denial(str(tool_input.get("command", "")), self.protected_branches)
            if bash_reason:
                self.store.add_event(self.task.task_id, "security_denial", {"reason": bash_reason, "input": _digest(tool_input)}, tool_name=tool_name, is_write=True)
                return bash_reason
        self.store.add_event(self.task.task_id, "tool_call", {"input": _digest(tool_input)}, tool_name=tool_name, is_write=classify_tool(tool_name) == "write")
        return None

    async def pre_tool_use(self, input_data: dict, tool_use_id=None, context=None) -> dict:
        reason = self._decide(str(input_data.get("tool_name", "")), input_data.get("tool_input") or {})
        if reason:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
        return {}

    async def can_use_tool(self, tool_name: str, tool_input: dict, context=None) -> PermissionResultAllow | PermissionResultDeny:
        # backstop for calls that reach the permission callback (non-allowlisted tools)
        reason = self._decide(tool_name, tool_input)
        if reason:
            return PermissionResultDeny(message=reason)
        return PermissionResultAllow()


def _digest(tool_input: dict) -> str:
    # bounded before it enters the audit trail (TOL-006); store redacts on write
    return str(tool_input)[:300]
