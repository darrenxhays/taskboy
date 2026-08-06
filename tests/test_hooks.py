import pytest

from taskboy.hooks import TaskHooks, bash_denial, classify_tool, profile_permits_writes, repo_grantable, tool_grantable

PROTECTED = ["main", "develop"]

# the real read_only tier allowlist: all read tools plus Bash. Notably includes mcp__slack__user_info,
# which is a read tool — it must not flip the tier to write-capable via the unknown-tool default.
READ_ONLY_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Bash",
    "WebFetch",
    "mcp__harness__report_progress",
    "mcp__github__get_pull_request",
    "mcp__slack__channel_history",
    "mcp__slack__user_info",
    "mcp__confluence__get_page",
]
WRITE_TOOLS = READ_ONLY_TOOLS + ["Write", "Edit", "mcp__jira__add_comment"]


def test_profile_permits_writes_ignores_read_and_unknown_tools():
    # a read-only tier is not write-capable even though it carries Bash and (formerly unclassified) slack tools
    assert profile_permits_writes(READ_ONLY_TOOLS) is False
    # an unrecognized tool in the allowlist must not be treated as a write tool that opens the gate
    assert profile_permits_writes(["Read", "Bash", "mcp__brandnew__unknown_tool"]) is False
    # a genuine write tool other than Bash does make the tier write-capable
    assert profile_permits_writes(WRITE_TOOLS) is True


def test_tool_grantable_gates_writes_by_tier():
    # read-only tier: read tools grantable, write tools never
    assert tool_grantable("mcp__jira__get_issue", READ_ONLY_TOOLS) is True
    assert tool_grantable("mcp__slack__get_file", READ_ONLY_TOOLS) is True
    assert tool_grantable("Write", READ_ONLY_TOOLS) is False
    assert tool_grantable("Edit", READ_ONLY_TOOLS) is False
    assert tool_grantable("mcp__jira__add_comment", READ_ONLY_TOOLS) is False
    assert tool_grantable("mcp__github__resolve_pr_thread", READ_ONLY_TOOLS) is False
    # unrecognized targets are never grantable regardless of tier
    assert tool_grantable("mcp__brandnew__unknown_tool", WRITE_TOOLS) is False
    # write-capable tier: write tools grantable
    assert tool_grantable("mcp__jira__add_comment", WRITE_TOOLS) is True
    assert tool_grantable("mcp__github__resolve_pr_thread", WRITE_TOOLS) is True


def test_repo_grantable_allows_same_org_repos_outside_approved_list():
    # the evidence case (issue #39): example-org/portal wasn't in approved_repos, but example-org/taskboy
    # was — same org, so it should be grantable, whether or not the installation's repo list is known
    approved = ["example-org/taskboy"]
    assert repo_grantable("example-org/portal", approved, None) is True
    assert repo_grantable("example-org/portal", approved, {"taskboy", "portal"}) is True


def test_repo_grantable_rejects_other_orgs_and_uninstalled_repos():
    approved = ["example-org/taskboy"]
    # a different org entirely is never grantable, regardless of installation knowledge
    assert repo_grantable("othercorp/secret", approved, None) is False
    # same org, but the installation's known repo list doesn't include it
    assert repo_grantable("example-org/portal", approved, {"taskboy"}) is False


def test_repo_grantable_rejects_malformed_targets():
    approved = ["example-org/taskboy"]
    assert repo_grantable("notaslug", approved, None) is False
    assert repo_grantable("example-org/", approved, None) is False
    assert repo_grantable("/portal", approved, None) is False


def test_bash_denials():
    assert "metadata" in bash_denial("curl http://169.254.169.254/latest/meta-data/", PROTECTED)
    assert "environment" in bash_denial("printenv | grep TOKEN", PROTECTED)
    assert "environment" in bash_denial("env", PROTECTED)
    assert "environment" in bash_denial("cat /proc/self/environ", PROTECTED)
    assert "environment" in bash_denial("cat /proc/123/environ", PROTECTED)
    assert "environment" in bash_denial("python -c 'import os; print(os.environ)'", PROTECTED)
    assert "force" in bash_denial("git push --force origin feature", PROTECTED)
    assert "force" in bash_denial("git push -f origin feature", PROTECTED)
    assert "protected branch 'main'" in bash_denial("git push origin main", PROTECTED)
    assert "protected branch 'develop'" in bash_denial("git push origin HEAD:develop", PROTECTED)


def test_bash_allows_normal_work():
    for command in [
        "git clone https://github.com/org/repo.git",
        "git push origin agent/t123-fix",
        "pytest tests/ -q",
        "ls -la && cat README.md",
        "grep -r 'environment' src/",  # mentions the word, doesn't dump it
        "python -m venv .venv",
    ]:
        assert bash_denial(command, PROTECTED) is None, command


def test_tool_classification_defaults_to_write():
    assert classify_tool("Read") == "read"
    assert classify_tool("Bash") == "write"
    assert classify_tool("mcp__harness__report_progress") == "read"
    assert classify_tool("mcp__slack__channel_history") == "read"
    assert classify_tool("mcp__slack__thread_replies") == "read"
    assert classify_tool("mcp__slack__get_file") == "read"
    assert classify_tool("mcp__github__list_pull_requests") == "read"
    assert classify_tool("mcp__github__list_pr_files") == "read"
    assert classify_tool("mcp__github__resolve_pr_thread") == "write"
    assert classify_tool("mcp__github__close_pull_request") == "write"
    assert classify_tool("mcp__github__delete_branch") == "write"
    assert classify_tool("mcp__github__create_release") == "write"
    assert classify_tool("mcp__jira__search_users") == "read"
    assert classify_tool("mcp__jira__list_boards") == "read"
    assert classify_tool("mcp__jira__list_sprints") == "read"
    assert classify_tool("mcp__jira__assign_issue") == "write"
    assert classify_tool("mcp__jira__move_to_sprint") == "write"
    assert classify_tool("mcp__jira__set_epic") == "write"
    assert classify_tool("mcp__jira__set_story_points") == "write"
    assert classify_tool("mcp__confluence__search_pages") == "read"
    assert classify_tool("mcp__confluence__get_page") == "read"
    assert classify_tool("SomeUnknownTool") == "write"


@pytest.mark.asyncio
async def test_pre_tool_use_hook_denies_and_audits(store, make_task):
    # the PreToolUse hook is the real gate: allowed_tools entries shadow can_use_tool
    task = make_task()
    hooks = TaskHooks(store, task, PROTECTED)
    denied = await hooks.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "protected branch" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert await hooks.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "git status"}}) == {}
    assert await hooks.pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}) == {}
    events = store.events_for(task.task_id)
    kinds = [(event["kind"], event["tool_name"], event["is_write"]) for event in events if event["kind"] in ("tool_call", "security_denial")]
    assert ("security_denial", "Bash", 1) in kinds
    assert ("tool_call", "Bash", 1) in kinds
    assert ("tool_call", "Read", 0) in kinds


@pytest.mark.asyncio
async def test_tools_outside_the_profile_allowlist_are_denied(store, make_task):
    task = make_task()
    hooks = TaskHooks(store, task, PROTECTED, allowed_tools=["Read", "Bash", "mcp__harness__report_progress"])
    denied = await hooks.pre_tool_use({"tool_name": "Task", "tool_input": {"prompt": "count the lambdas"}})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "not in this task's profile" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    backstop = await hooks.can_use_tool("WebSearch", {"query": "x"})
    assert backstop.behavior == "deny"
    assert await hooks.pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": "/tmp/x"}}) == {}
    denials = [e for e in store.events_for(task.task_id) if e["kind"] == "security_denial"]
    assert len(denials) == 2


@pytest.mark.asyncio
async def test_can_use_tool_backstop_matches_hook_policy(store, make_task):
    task = make_task()
    hooks = TaskHooks(store, task, PROTECTED)
    denied = await hooks.can_use_tool("Bash", {"command": "git push origin main"})
    assert denied.behavior == "deny"
    allowed = await hooks.can_use_tool("Read", {"file_path": "/tmp/x"})
    assert allowed.behavior == "allow"
