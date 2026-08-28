import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from taskboy.config import Role
from taskboy.models import BLOCKED, COMPLETED, FAILED, QUEUED, RECEIVED, RUNNING, Outcome
from taskboy.runner import ClaudeRunner, ModelUnavailable, extract_final_report, extract_reply, looks_model_unavailable, looks_session_limit, record_rate_limit_event, retry_not_before, session_option_kwargs

RAW = {
    "models": {
        "haiku": {"id": "claude-haiku-4-5", "fallbacks": ["sonnet"]},
        "sonnet": {"id": "claude-sonnet-4-6", "fallbacks": ["opus"]},
        "opus": {"id": "claude-opus-4-6", "fallbacks": []},
    },
    "profiles": {
        "read_only": {"allowed_tools": ["Read", "Bash"], "max_budget_usd": 2.0, "max_turns": 60, "max_runtime_minutes": 30},
        "standard": {"allowed_tools": ["Read", "Bash", "Write", "Edit"], "max_budget_usd": 12.0, "max_turns": 400, "max_runtime_minutes": 240},
    },
    "github": {"protected_branch_patterns": ["main"]},
}


def make_runner(store, config, tmp_path):
    config.raw = RAW
    return ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock())


def routed_task(store, make_task):
    task = make_task("investigate")
    store.transition(task.task_id, RECEIVED, QUEUED, "classified", model_alias="sonnet", model_id="claude-sonnet-4-6", profile="read_only", max_turns=60, max_runtime_minutes=30)
    return store.transition(task.task_id, QUEUED, RUNNING, "dispatched", max_budget_usd=2.0)


@pytest.mark.asyncio
async def test_run_creates_workspace_and_returns_outcome(store, config, make_task, tmp_path):
    runner = make_runner(store, config, tmp_path)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done", session_id="s1"))
    task = routed_task(store, make_task)
    outcome = await runner.run(task)
    assert outcome.state == COMPLETED
    workspace_path = store.get_task(task.task_id).workspace_path
    assert workspace_path is not None
    for sub in ("repo", "notes", "home"):
        assert (Path(workspace_path) / sub).is_dir()
    # ran on the routed model, no fallback events
    assert runner._run_session.call_args.args[1] == "claude-sonnet-4-6"
    assert not [e for e in store.events_for(task.task_id) if e["kind"] == "model_fallback"]


@pytest.mark.asyncio
async def test_run_loads_personality_records_hash_and_posts_prompt(store, config, make_task, tmp_path):
    personality = tmp_path / "personality_red.md"
    personality.write_text("Dry and exact.")
    config.personality_path = str(personality)
    debug = AsyncMock()
    runner = make_runner(store, config, tmp_path)
    runner.debug = debug
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done", reply="Done."))
    task = routed_task(store, make_task)
    await runner.run(task)
    prompt = runner._run_session.call_args.args[3]
    assert "## Personality" in prompt
    assert "Dry and exact." in prompt
    assert any(event["kind"] == "personality" for event in store.events_for(task.task_id))
    debug.prompt_file.assert_awaited_once_with(task, prompt)


@pytest.mark.asyncio
async def test_model_unavailable_walks_fallback_chain(store, config, make_task, tmp_path):
    runner = make_runner(store, config, tmp_path)
    runner._run_session = AsyncMock(side_effect=[ModelUnavailable("sonnet down"), Outcome(state=COMPLETED, result_summary="done on opus")])
    task = routed_task(store, make_task)
    outcome = await runner.run(task)
    assert outcome.state == COMPLETED
    attempted = [call.args[1] for call in runner._run_session.call_args_list]
    assert attempted == ["claude-sonnet-4-6", "claude-opus-4-6"]
    fallbacks = [e for e in store.events_for(task.task_id) if e["kind"] == "model_fallback"]
    assert len(fallbacks) == 1  # MOD-009: every hop audited


@pytest.mark.asyncio
async def test_exhausted_fallback_chain_blocks_task(store, config, make_task, tmp_path):
    runner = make_runner(store, config, tmp_path)
    runner._run_session = AsyncMock(side_effect=ModelUnavailable("everything down"))
    task = routed_task(store, make_task)
    outcome = await runner.run(task)
    assert outcome.state == BLOCKED
    assert "no configured model" in outcome.blocked_reason
    assert runner._run_session.call_count == 2  # sonnet, opus — never anything unconfigured


def test_extract_final_report_finds_section():
    text = "lots of working notes\n\n## Final Report\nResult: fixed it\nActions: patched foo.py"
    assert extract_final_report(text).startswith("## Final Report")
    assert "fixed it" in extract_final_report(text)
    # no section: fall back to the tail
    assert extract_final_report("just an answer") == "just an answer"


def test_extract_reply_finds_only_reply_body():
    text = "notes\n## Reply\nFixed it. Tests pass.\n\n## Final Report\nResult: fixed"
    assert extract_reply(text) == "Fixed it. Tests pass."
    assert extract_reply("## Final Report\nResult: old session") == ""


def test_git_identity_env():
    from taskboy.runner import git_identity_env

    env = git_identity_env({"commit_name": "Red", "commit_email": "red@example.com"})
    assert env["GIT_AUTHOR_NAME"] == "Red"
    assert env["GIT_COMMITTER_EMAIL"] == "red@example.com"
    blue_env = git_identity_env({}, name="Blue", email="blue@example.com")
    assert blue_env["GIT_AUTHOR_NAME"] == "Blue"
    assert blue_env["GIT_COMMITTER_EMAIL"] == "blue@example.com"
    assert git_identity_env({}) == {}  # no email configured -> leave git identity alone


def test_looks_model_unavailable():
    assert looks_model_unavailable(RuntimeError("model claude-x not found"))
    assert looks_model_unavailable(RuntimeError("The model is overloaded (529)"))
    assert not looks_model_unavailable(RuntimeError("connection reset by peer"))
    assert not looks_model_unavailable(RuntimeError("model output was weird"))


def test_looks_session_limit():
    assert looks_session_limit("You've hit your session limit · resets 6:50pm (UTC)")
    assert looks_session_limit("Rate limit exceeded, try again later")
    assert looks_session_limit("USAGE LIMIT reached for this account")
    assert not looks_session_limit("connection reset by peer")
    assert not looks_session_limit("model claude-x not found")


def test_retry_not_before_uses_resets_at_plus_buffer():
    now = datetime.now(timezone.utc)
    resets_at = int(now.timestamp()) + 120
    computed = datetime.fromisoformat(retry_not_before(resets_at))
    assert abs((computed - now).total_seconds() - 180) < 5  # resets_at + 60s buffer


def test_retry_not_before_defaults_without_resets_at():
    now = datetime.now(timezone.utc)
    computed = datetime.fromisoformat(retry_not_before(None))
    assert abs((computed - now).total_seconds() - 900) < 5  # ~15 minutes


def test_retry_not_before_caps_at_six_hours():
    now = datetime.now(timezone.utc)
    far_future = int(now.timestamp()) + 24 * 3600  # a day out, well past the reset+buffer
    computed = datetime.fromisoformat(retry_not_before(far_future))
    assert abs((computed - now).total_seconds() - 6 * 3600) < 5


def test_session_option_kwargs_reasoning_matrix(store, make_task, tmp_path):
    task = routed_task(store, make_task)
    ws = tmp_path / "workspace"
    profile = {"allowed_tools": ["Read"], "effort": "high", "thinking": {"type": "adaptive"}}
    kwargs = session_option_kwargs(task, "claude-fable-5", ws, profile)
    assert kwargs == {
        "cwd": str(ws / "repo"),
        "model": "claude-fable-5",
        "allowed_tools": ["Read"],
        "permission_mode": "acceptEdits",
        "max_turns": 60,
        "max_budget_usd": 2.0,
        "setting_sources": [],
        "resume": None,
        "settings": json.dumps({"includeCoAuthoredBy": False}),
        "effort": "high",
        "thinking": {"type": "adaptive"},
    }
    kwargs = session_option_kwargs(task, "sonnet", ws, {"allowed_tools": []})
    assert "effort" not in kwargs
    assert "thinking" not in kwargs


def test_session_option_kwargs_disables_claude_code_attribution(store, make_task, tmp_path):
    """issue #47: commits/PRs must be attributed to the bot persona alone, never Claude Code."""
    task = routed_task(store, make_task)
    ws = tmp_path / "workspace"
    kwargs = session_option_kwargs(task, "sonnet", ws, {"allowed_tools": []})
    assert json.loads(kwargs["settings"]) == {"includeCoAuthoredBy": False}


def test_session_option_kwargs_effort_override_wins_over_profile(store, make_task, tmp_path):
    task = make_task("investigate", effort_override="xhigh")
    store.transition(task.task_id, RECEIVED, QUEUED, "classified", model_alias="sonnet", model_id="claude-sonnet-4-6", profile="read_only", max_turns=60, max_runtime_minutes=30)
    task = store.transition(task.task_id, QUEUED, RUNNING, "dispatched", max_budget_usd=2.0)
    ws = tmp_path / "workspace"
    profile = {"allowed_tools": ["Read"], "effort": "high"}
    kwargs = session_option_kwargs(task, "claude-sonnet-4-6", ws, profile)
    assert kwargs["effort"] == "xhigh"


def test_session_option_kwargs_effort_override_applies_without_profile_effort(store, make_task, tmp_path):
    task = make_task("investigate", effort_override="low")
    store.transition(task.task_id, RECEIVED, QUEUED, "classified", model_alias="sonnet", model_id="claude-sonnet-4-6", profile="read_only", max_turns=60, max_runtime_minutes=30)
    task = store.transition(task.task_id, QUEUED, RUNNING, "dispatched", max_budget_usd=2.0)
    ws = tmp_path / "workspace"
    kwargs = session_option_kwargs(task, "claude-sonnet-4-6", ws, {"allowed_tools": []})
    assert kwargs["effort"] == "low"


def test_session_option_kwargs_classifier_effort_wins_over_profile(store, make_task, tmp_path):
    task = make_task("investigate")
    store.transition(task.task_id, RECEIVED, QUEUED, "classified", model_alias="sonnet", model_id="claude-sonnet-4-6", profile="read_only", max_turns=60, max_runtime_minutes=30, effort="xhigh")
    task = store.transition(task.task_id, QUEUED, RUNNING, "dispatched", max_budget_usd=2.0)
    ws = tmp_path / "workspace"
    kwargs = session_option_kwargs(task, "claude-sonnet-4-6", ws, {"allowed_tools": [], "effort": "high"})
    assert kwargs["effort"] == "xhigh"


def test_session_option_kwargs_effort_override_wins_over_classifier_effort(store, make_task, tmp_path):
    task = make_task("investigate", effort_override="max")
    store.transition(task.task_id, RECEIVED, QUEUED, "classified", model_alias="sonnet", model_id="claude-sonnet-4-6", profile="read_only", max_turns=60, max_runtime_minutes=30, effort="low")
    task = store.transition(task.task_id, QUEUED, RUNNING, "dispatched", max_budget_usd=2.0)
    ws = tmp_path / "workspace"
    kwargs = session_option_kwargs(task, "claude-sonnet-4-6", ws, {"allowed_tools": []})
    assert kwargs["effort"] == "max"


def test_session_option_kwargs_falls_back_to_profile_when_no_effort_chosen(store, make_task, tmp_path):
    task = routed_task(store, make_task)
    ws = tmp_path / "workspace"
    kwargs = session_option_kwargs(task, "sonnet", ws, {"allowed_tools": [], "effort": "medium"})
    assert kwargs["effort"] == "medium"


@pytest.mark.asyncio
async def test_runner_scopes_broker_repositories_to_role(store, config, make_task, tmp_path):
    raw = {**RAW, "github": {"approved_repos": ["org/a", "org/b"], "protected_branch_patterns": ["main"]}}
    config.raw = raw
    config.roles["admin"] = Role("admin", ["U1"], ["read_only"], True, None, ["org/a"])
    broker = MagicMock()
    broker.register_task.return_value = {}
    runner = ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock(), broker=broker)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    await runner.run(task)
    assert broker.register_task.call_args.args[1] == ["org/a"]


@pytest.mark.asyncio
async def test_runner_resolves_hooks_path_for_relative_workspaces_root(store, config, make_task, tmp_path, monkeypatch):
    # TASKBOY_WORKSPACES_ROOT defaults to a relative path ("local/workspaces"); core.hooksPath is
    # resolved by git against its own cwd, so a relative hooks_path would silently miss the hook (#75)
    config.raw = RAW
    monkeypatch.chdir(tmp_path)
    broker = MagicMock()
    broker.register_task.return_value = {}
    runner = ClaudeRunner(store, config, "workspaces", str(tmp_path / "memory"), progress=AsyncMock(), broker=broker)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    await runner.run(task)
    hooks_path = broker.register_task.call_args.kwargs["hooks_path"]
    assert Path(hooks_path).is_absolute()
    assert hooks_path == str((tmp_path / "workspaces" / task.task_id / "githooks").resolve())


@pytest.mark.asyncio
async def test_blue_runner_uses_reviewer_broker_personality_and_name(store, config, make_task, tmp_path):
    config.raw = RAW
    red_personality = tmp_path / "red.md"
    red_personality.write_text("Red voice")
    reviewer_personality = tmp_path / "blue.md"
    reviewer_personality.write_text("Blue voice")
    config.personality_path = str(red_personality)
    config.reviewer.enabled = True
    config.reviewer.name = "Blue"
    config.reviewer.personality_path = str(reviewer_personality)
    config.reviewer.commit_name = "Blue"
    config.reviewer.commit_email = "blue@example.com"
    red_broker = MagicMock()
    red_broker.register_task.return_value = {"broker": "agent"}
    reviewer_broker = MagicMock()
    reviewer_broker.register_task.return_value = {"broker": "reviewer"}
    runner = ClaudeRunner(
        store,
        config,
        str(tmp_path / "workspaces"),
        str(tmp_path / "memory"),
        progress=AsyncMock(),
        broker=red_broker,
        reviewer_broker=reviewer_broker,
    )
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    task.persona = "reviewer"

    await runner.run(task)

    reviewer_broker.register_task.assert_called_once_with(task, [], granted_repos=[], hooks_path=ANY)
    red_broker.register_task.assert_not_called()
    assert runner._run_session.call_args.args[6] is reviewer_broker
    prompt = runner._run_session.call_args.args[3]
    assert "You are Blue, an autonomous engineering agent" in prompt
    assert "Blue voice" in prompt
    assert "Red voice" not in prompt
    reviewer_broker.release_task.assert_called_once_with(task.task_id)
    red_broker.release_task.assert_not_called()


@pytest.mark.asyncio
async def test_blue_only_broker_clones_target_repositories(store, config, make_task, tmp_path, monkeypatch):
    config.raw = {**RAW, "github": {"approved_repos": ["org/a"], "protected_branch_patterns": ["main"]}}
    config.reviewer.enabled = True
    reviewer_broker = MagicMock()
    reviewer_broker.register_task.return_value = {}
    refresh = AsyncMock(return_value=True)
    clone = AsyncMock(return_value=True)
    monkeypatch.setattr("taskboy.runner.repocache.refresh_one", refresh)
    monkeypatch.setattr("taskboy.runner.repocache.clone_from_mirror", clone)
    runner = ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock(), broker=None, reviewer_broker=reviewer_broker)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    task.persona = "reviewer"
    task.classification_json = json.dumps({"target_repos": ["org/a"]})

    await runner.run(task)

    refresh.assert_awaited_once()
    assert refresh.call_args.args[0] is reviewer_broker
    clone.assert_awaited_once()


@pytest.mark.asyncio
async def test_blue_session_uses_reviewer_github_adapter_and_git_identity(store, config, make_task, tmp_path, monkeypatch):
    config.raw = {**RAW, "github": {"commit_name": "Red", "commit_email": "red@example.com", "protected_branch_patterns": ["main"]}}
    config.reviewer.enabled = True
    config.reviewer.commit_name = "Blue"
    config.reviewer.commit_email = "blue@example.com"
    task = routed_task(store, make_task)
    task.persona = "reviewer"
    red_broker = MagicMock()
    red_broker.app_slug = AsyncMock(return_value="red-app")
    reviewer_broker = MagicMock()
    reviewer_broker.app_slug = AsyncMock(return_value="blue-app")
    captured = {}

    class FakeOptions:
        def __init__(self, **kwargs):
            captured["options"] = kwargs

    class FakeClient:
        def __init__(self, options):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield SimpleNamespace(total_cost_usd=0, num_turns=1, usage={}, result="## Final Report\ndone", is_error=False, session_id="s1")

    def github_adapter(broker, *args, **kwargs):
        captured["github_broker"] = broker
        captured["github_kwargs"] = kwargs
        return object()

    def slack_adapter(*args, **kwargs):
        captured["slack_kwargs"] = kwargs
        return object()

    monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("claude_agent_sdk.HookMatcher", lambda **kwargs: object())
    monkeypatch.setattr("taskboy.runner.build_progress_server", lambda *args: object())
    monkeypatch.setattr("taskboy.adapters.github_api.GitHubAdapter", github_adapter)
    monkeypatch.setattr("taskboy.adapters.github_api.build_github_server", lambda adapter: adapter)
    monkeypatch.setattr("taskboy.adapters.slack_history.SlackHistoryAdapter", slack_adapter)
    monkeypatch.setattr("taskboy.adapters.slack_history.build_slack_server", lambda adapter: adapter)
    runner = ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock(), broker=red_broker, reviewer_broker=reviewer_broker, slack_client=object())

    outcome = await runner._run_session(task, "claude-sonnet-4-6", tmp_path, "prompt", {"TASKBOY_BROKER_SOCKET": "blue.sock"}, [], reviewer_broker)

    assert outcome.state == COMPLETED
    assert captured["github_broker"] is reviewer_broker
    assert captured["github_kwargs"]["main_broker"] is red_broker  # APPROVE is gated to main-agent-authored prs, not just the reviewer persona
    assert captured["github_kwargs"]["reviewer_broker"] is reviewer_broker  # so resolve_pr_thread resolves both logins lazily (issue #72)
    assert captured["github_kwargs"]["can_approve"] is True  # the reviewer persona may APPROVE
    assert captured["options"]["env"]["GIT_AUTHOR_NAME"] == "Blue"
    assert captured["options"]["env"]["GIT_COMMITTER_EMAIL"] == "blue@example.com"
    assert captured["slack_kwargs"] == {"allowed_channels": config.slack.allowed_channels, "files_dir": tmp_path / "repo" / "slack_files"}


@pytest.mark.asyncio
async def test_runner_injects_conventions_for_scoped_target_repo(store, config, make_task, tmp_path):
    conventions = tmp_path / "source-conventions.md"
    conventions.write_text("house style")
    config.conventions_path = str(conventions)
    runner = make_runner(store, config, tmp_path)
    config.raw = {**RAW, "github": {"approved_repos": ["org/a"], "protected_branch_patterns": ["main"]}}
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"target_repos": ["org/a"]}')
    await runner.run(store.get_task(task.task_id))
    workspace = Path(store.get_task(task.task_id).workspace_path)
    assert (workspace / "repo" / "CONVENTIONS.md").read_text() == "house style"
    assert "## Engineering conventions" in runner._run_session.call_args.args[3]


@pytest.mark.asyncio
async def test_runner_skips_conventions_without_target_repo(store, config, make_task, tmp_path):
    conventions = tmp_path / "source-conventions.md"
    conventions.write_text("house style")
    config.conventions_path = str(conventions)
    runner = make_runner(store, config, tmp_path)
    config.raw = {**RAW, "github": {"approved_repos": ["org/a"], "protected_branch_patterns": ["main"]}}
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    await runner.run(task)
    workspace = Path(store.get_task(task.task_id).workspace_path)
    assert not (workspace / "repo" / "CONVENTIONS.md").exists()
    assert "## Engineering conventions" not in runner._run_session.call_args.args[3]


@pytest.mark.asyncio
async def test_runner_skips_conventions_when_disabled(store, config, make_task, tmp_path):
    config.conventions_path = None
    runner = make_runner(store, config, tmp_path)
    config.raw = {**RAW, "github": {"approved_repos": ["org/a"], "protected_branch_patterns": ["main"]}}
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"target_repos": ["org/a"]}')
    await runner.run(store.get_task(task.task_id))
    workspace = Path(store.get_task(task.task_id).workspace_path)
    assert not (workspace / "repo" / "CONVENTIONS.md").exists()
    assert "## Engineering conventions" not in runner._run_session.call_args.args[3]


@pytest.mark.asyncio
async def test_skill_task_renders_instructions_into_prompt(store, config, make_task, tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    skill_path = skills_root / "review"
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text("---\nname: review\ndescription: review\n---\n\nfollow the simplicity lens\n")
    monkeypatch.setattr("taskboy.settings.SKILLS_ROOT", str(skills_root))
    runner = make_runner(store, config, tmp_path)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"skill": "review", "skill_args": "https://example.test/pr/1"}')
    task = store.get_task(task.task_id)
    outcome = await runner.run(task)
    assert outcome.state == COMPLETED
    prompt = runner._run_session.call_args.args[3]
    assert "### Skill: /review" in prompt
    assert "follow the simplicity lens" in prompt
    assert "Arguments: https://example.test/pr/1" in prompt


@pytest.mark.asyncio
async def test_missing_skill_returns_failed_outcome(store, config, make_task, tmp_path, monkeypatch):
    # a non-builtin name: built-ins like /review resolve from the packaged templates even with no skills dir
    monkeypatch.setattr("taskboy.settings.SKILLS_ROOT", str(tmp_path / "missing"))
    runner = make_runner(store, config, tmp_path)
    runner._run_session = AsyncMock()
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"skill": "slack2pr", "skill_args": ""}')
    outcome = await runner.run(store.get_task(task.task_id))
    assert outcome.state == FAILED
    assert outcome.error == "skill /slack2pr is not installed"
    runner._run_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_builtin_skill_runs_without_an_installed_copy(store, config, make_task, tmp_path, monkeypatch):
    monkeypatch.setattr("taskboy.settings.SKILLS_ROOT", str(tmp_path / "missing"))
    runner = make_runner(store, config, tmp_path)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"skill": "review", "skill_args": "https://example.test/pr/1"}')
    outcome = await runner.run(store.get_task(task.task_id))
    assert outcome.state == COMPLETED
    prompt = runner._run_session.call_args.args[3]
    assert "### Skill: /review" in prompt
    assert "{{" not in prompt  # runtime variables substituted from config


@pytest.mark.asyncio
async def test_invalid_skill_returns_specific_error(store, config, make_task, tmp_path, monkeypatch):
    skill_path = tmp_path / "skills" / "review"
    skill_path.mkdir(parents=True)
    (skill_path / "SKILL.md").write_text("invalid")
    monkeypatch.setattr("taskboy.settings.SKILLS_ROOT", str(tmp_path / "skills"))
    runner = make_runner(store, config, tmp_path)
    runner._run_session = AsyncMock()
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"skill": "review", "skill_args": ""}')
    outcome = await runner.run(store.get_task(task.task_id))
    assert outcome.state == FAILED
    assert outcome.error == "skill /review has invalid frontmatter"
    runner._run_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_permission_requester_records_and_blocks(store, config, make_task, tmp_path):
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)
    blocked: dict = {}
    progress = AsyncMock()
    request = runner._permission_requester(task, blocked, ["Read"], ["Read", "Write"], ["example-org/core"], ["example-org/core"], None, progress)
    message = await request("tool", "mcp__jira__add_comment", "need to post findings")
    assert "recorded" in message
    assert blocked["reason"].startswith("needs permission for tool 'mcp__jira__add_comment'")
    requests = store.permission_requests_for(task.task_id)
    assert [(r["kind"], r["target"], r["status"]) for r in requests] == [("tool", "mcp__jira__add_comment", "pending")]
    progress.assert_awaited_once()


@pytest.mark.asyncio
async def test_permission_requester_rejects_unknown_and_already_held(store, config, make_task, tmp_path):
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)
    request = runner._permission_requester(task, {}, ["Read", "mcp__jira__add_comment"], ["Read", "Write"], ["example-org/core"], ["example-org/core"], None, AsyncMock())
    assert "not a recognized tool" in await request("tool", "made_up_tool", "why")
    assert "already have" in await request("tool", "mcp__jira__add_comment", "why")
    assert "not an approved repository" in await request("repo", "othercorp/secret", "why")  # a different org entirely
    assert "owner/name" in await request("repo", "notaslug", "why")
    assert "'tool' or 'repo'" in await request("bogus", "x", "why")
    assert store.permission_requests_for(task.task_id) == []  # nothing invalid was recorded


@pytest.mark.asyncio
async def test_permission_requester_records_same_org_repo_outside_role_scope(store, config, make_task, tmp_path):
    # issue #39: a repo outside this task's role-scoped list is recorded for operator review, not
    # auto-rejected, as long as it shares an org with an already-approved repo (the evidence case: a
    # request for example-org/portal died blocked because example-org/taskboy was the only scoped repo)
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)
    progress = AsyncMock()
    request = runner._permission_requester(task, {}, ["Read"], ["Read", "Write"], ["example-org/taskboy"], ["example-org/taskboy"], None, progress)
    message = await request("repo", "example-org/portal", "need to review a PR")
    assert "recorded" in message
    requests = store.permission_requests_for(task.task_id)
    assert [(r["kind"], r["target"], r["status"]) for r in requests] == [("repo", "example-org/portal", "pending")]


@pytest.mark.asyncio
async def test_permission_requester_rejects_same_org_repo_not_installed(store, config, make_task, tmp_path):
    # when the installation's repo list is known, a same-org repo the app isn't installed on is still rejected
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)
    request = runner._permission_requester(task, {}, ["Read"], ["Read", "Write"], ["example-org/taskboy"], ["example-org/taskboy"], {"taskboy"}, AsyncMock())
    assert "not an approved repository" in await request("repo", "example-org/portal", "why")
    assert store.permission_requests_for(task.task_id) == []


@pytest.mark.asyncio
async def test_run_merges_granted_tool_into_session_and_prompt(store, config, make_task, tmp_path):
    runner = make_runner(store, config, tmp_path)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, profile="standard")  # a write-capable tier may be granted a write tool
    store.request_permission(task.task_id, "tool", "mcp__jira__add_comment", "post findings")
    store.decide_permission_request(task.task_id, "tool", "mcp__jira__add_comment", "granted", "boss")
    await runner.run(store.get_task(task.task_id))
    granted_tools = runner._run_session.call_args.args[7]
    prompt = runner._run_session.call_args.args[3]
    assert granted_tools == ["mcp__jira__add_comment"]
    assert "## Granted permissions" in prompt
    assert "mcp__jira__add_comment" in prompt
    assert any(e["kind"] == "permissions_applied" for e in store.events_for(task.task_id))


@pytest.mark.asyncio
async def test_read_only_tier_cannot_gain_write_tool(store, config, make_task, tmp_path):
    runner = make_runner(store, config, tmp_path)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)  # read_only tier
    # the requester refuses to even record a write-tool request from a read-only tier
    request = runner._permission_requester(task, {}, ["Read", "Bash"], ["Read", "Bash"], [], [], None, AsyncMock())
    assert "cannot be granted" in await request("tool", "Write", "want to edit")
    assert store.permission_requests_for(task.task_id) == []
    # and a write grant that somehow lands is filtered out before it widens the session allowlist
    store.request_permission(task.task_id, "tool", "mcp__jira__add_comment", "post")
    store.decide_permission_request(task.task_id, "tool", "mcp__jira__add_comment", "granted", "boss")
    await runner.run(store.get_task(task.task_id))
    assert runner._run_session.call_args.args[7] == []


@pytest.mark.asyncio
async def test_run_clones_granted_repo(store, config, make_task, tmp_path):
    config.raw = {**RAW, "github": {"approved_repos": ["org/a", "org/b"], "protected_branch_patterns": ["main"]}}
    broker = MagicMock()
    broker.register_task.return_value = {}
    runner = ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock(), broker=broker)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    seen: list[str] = []

    async def fake_refresh(_broker, _root, repo, timeout=60):
        seen.append(repo)
        return True

    async def fake_clone(_root, _repo, _dest):
        return True

    import taskboy.repocache as repocache

    runner_repocache = repocache
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"target_repos": ["org/a"]}')
    store.request_permission(task.task_id, "repo", "org/b", "need to check it")
    store.decide_permission_request(task.task_id, "repo", "org/b", "granted", "boss")
    import unittest.mock as mock

    with mock.patch.object(runner_repocache, "refresh_one", fake_refresh), mock.patch.object(runner_repocache, "clone_from_mirror", fake_clone):
        await runner.run(store.get_task(task.task_id))
    assert "org/b" in seen  # the granted repo was cloned alongside the originally-scoped one
    assert "org/b" in broker.register_task.call_args.args[1]


@pytest.mark.asyncio
async def test_run_clones_granted_repo_outside_approved_list_same_org(store, config, make_task, tmp_path):
    # issue #39: a granted repo doesn't need to already be in github.approved_repos — sharing an org
    # with an approved repo (and, when known, being installed) is enough for the grant to take effect
    config.raw = {**RAW, "github": {"approved_repos": ["example-org/taskboy"], "protected_branch_patterns": ["main"]}}
    broker = MagicMock()
    broker.register_task.return_value = {}
    broker.accessible_repos = None  # installation membership unknown — org match alone is enough
    runner = ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock(), broker=broker)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    seen: list[str] = []

    async def fake_refresh(_broker, _root, repo, timeout=60):
        seen.append(repo)
        return True

    async def fake_clone(_root, _repo, _dest):
        return True

    import taskboy.repocache as repocache

    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"target_repos": ["example-org/taskboy"]}')
    store.request_permission(task.task_id, "repo", "example-org/portal", "need to check it")
    store.decide_permission_request(task.task_id, "repo", "example-org/portal", "granted", "boss")
    import unittest.mock as mock

    with mock.patch.object(repocache, "refresh_one", fake_refresh), mock.patch.object(repocache, "clone_from_mirror", fake_clone):
        await runner.run(store.get_task(task.task_id))
    assert "example-org/portal" in seen  # cloned even though it was never in github.approved_repos
    assert "example-org/portal" in broker.register_task.call_args.args[1]


@pytest.mark.asyncio
async def test_run_records_repo_seed_failure_when_refresh_fails(store, config, make_task, tmp_path, monkeypatch):
    config.raw = {**RAW, "github": {"approved_repos": ["org/a"], "protected_branch_patterns": ["main"]}}
    broker = MagicMock()
    broker.register_task.return_value = {}
    runner = ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock(), broker=broker)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    monkeypatch.setattr("taskboy.runner.repocache.refresh_one", AsyncMock(return_value=False))
    clone = AsyncMock(return_value=True)
    monkeypatch.setattr("taskboy.runner.repocache.clone_from_mirror", clone)
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"target_repos": ["org/a"]}')

    await runner.run(store.get_task(task.task_id))

    clone.assert_not_awaited()  # short-circuits on refresh failure, same as before
    errors = store.errors_for(task.task_id)
    assert any(e["kind"] == "repo_seed_failed" and e["context_json"] and "org/a" in e["context_json"] for e in errors)
    events = [e for e in store.events_for(task.task_id) if e["kind"] == "repo_seed_failed"]
    assert len(events) == 1
    detail = json.loads(events[0]["detail_json"])
    assert detail["repository"] == "org/a"
    assert detail["stage"] == "refresh"
    prompt = runner._run_session.call_args.args[3]
    assert "org/a" in prompt
    assert "could not be pre-cloned" in prompt


@pytest.mark.asyncio
async def test_run_records_repo_seed_failure_when_clone_from_mirror_fails(store, config, make_task, tmp_path, monkeypatch):
    config.raw = {**RAW, "github": {"approved_repos": ["org/a"], "protected_branch_patterns": ["main"]}}
    broker = MagicMock()
    broker.register_task.return_value = {}
    runner = ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock(), broker=broker)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    monkeypatch.setattr("taskboy.runner.repocache.refresh_one", AsyncMock(return_value=True))
    monkeypatch.setattr("taskboy.runner.repocache.clone_from_mirror", AsyncMock(return_value=False))
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"target_repos": ["org/a"]}')

    await runner.run(store.get_task(task.task_id))

    events = [e for e in store.events_for(task.task_id) if e["kind"] == "repo_seed_failed"]
    assert len(events) == 1
    assert json.loads(events[0]["detail_json"])["stage"] == "clone"


@pytest.mark.asyncio
async def test_run_skips_reclone_when_workspace_already_has_repo(store, config, make_task, tmp_path, monkeypatch):
    config.raw = {**RAW, "github": {"approved_repos": ["org/a"], "protected_branch_patterns": ["main"]}}
    broker = MagicMock()
    broker.register_task.return_value = {}
    runner = ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock(), broker=broker)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    refresh = AsyncMock(return_value=True)
    clone = AsyncMock(return_value=True)
    monkeypatch.setattr("taskboy.runner.repocache.refresh_one", refresh)
    monkeypatch.setattr("taskboy.runner.repocache.clone_from_mirror", clone)
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"target_repos": ["org/a"]}')
    dest = tmp_path / "workspaces" / task.task_id / "repo" / "a"
    (dest / ".git").mkdir(parents=True)
    (dest / "marker.txt").write_text("prior attempt's working tree")

    await runner.run(store.get_task(task.task_id))

    refresh.assert_not_awaited()
    clone.assert_not_awaited()
    assert (dest / "marker.txt").exists()  # prior working tree survives, not rmtree'd by clone_from_mirror
    assert store.errors_for(task.task_id) == []
    prompt = runner._run_session.call_args.args[3]
    assert "could not be pre-cloned" not in prompt
    assert "./a" in prompt


@pytest.mark.asyncio
async def test_run_reclones_when_workspace_has_leftover_non_git_dir(store, config, make_task, tmp_path, monkeypatch):
    config.raw = {**RAW, "github": {"approved_repos": ["org/a"], "protected_branch_patterns": ["main"]}}
    broker = MagicMock()
    broker.register_task.return_value = {}
    runner = ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock(), broker=broker)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    refresh = AsyncMock(return_value=True)
    clone = AsyncMock(return_value=True)
    monkeypatch.setattr("taskboy.runner.repocache.refresh_one", refresh)
    monkeypatch.setattr("taskboy.runner.repocache.clone_from_mirror", clone)
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"target_repos": ["org/a"]}')
    dest = tmp_path / "workspaces" / task.task_id / "repo" / "a"
    dest.mkdir(parents=True)  # empty/half-cloned leftover, no .git — must not be mistaken for a real clone

    await runner.run(store.get_task(task.task_id))

    refresh.assert_awaited_once()
    clone.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_repo_seed_success_path_records_no_new_events(store, config, make_task, tmp_path, monkeypatch):
    config.raw = {**RAW, "github": {"approved_repos": ["org/a"], "protected_branch_patterns": ["main"]}}
    broker = MagicMock()
    broker.register_task.return_value = {}
    runner = ClaudeRunner(store, config, str(tmp_path / "workspaces"), str(tmp_path / "memory"), progress=AsyncMock(), broker=broker)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    monkeypatch.setattr("taskboy.runner.repocache.refresh_one", AsyncMock(return_value=True))
    monkeypatch.setattr("taskboy.runner.repocache.clone_from_mirror", AsyncMock(return_value=True))
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, classification_json='{"target_repos": ["org/a"]}')

    await runner.run(store.get_task(task.task_id))

    assert store.errors_for(task.task_id) == []
    assert not [e for e in store.events_for(task.task_id) if e["kind"] == "repo_seed_failed"]
    prompt = runner._run_session.call_args.args[3]
    assert "could not be pre-cloned" not in prompt


def test_build_progress_server_exposes_request_permission():
    from taskboy.runner import build_progress_server

    server = build_progress_server(AsyncMock(), {}, AsyncMock(return_value="ok"))
    assert server is not None


def test_record_rate_limit_event_writes_window_and_structured_audit(store, make_task):
    task = make_task()
    message = SimpleNamespace(rate_limit_info=SimpleNamespace(status="allowed_warning", resets_at=2000000000, rate_limit_type="five_hour", utilization=0.83))

    resets_at = record_rate_limit_event(store, task, message)

    assert resets_at == 2000000000
    assert store.rate_limit_windows()[0] | {"observed_at": "ignored"} == {
        "rate_limit_type": "five_hour",
        "status": "allowed_warning",
        "utilization": 0.83,
        "resets_at": 2000000000,
        "observed_at": "ignored",
    }
    event = [event for event in store.events_for(task.task_id) if event["kind"] == "rate_limit"][0]
    assert json.loads(event["detail_json"]) == {"type": "five_hour", "status": "allowed_warning", "utilization": 0.83, "resets_at": 2000000000}


def test_record_rate_limit_event_malformed_is_audited_without_raising(store, make_task):
    task = make_task()

    resets_at = record_rate_limit_event(store, task, SimpleNamespace())

    assert resets_at is None
    assert store.rate_limit_windows() == []
    assert any(event["kind"] == "rate_limit" for event in store.events_for(task.task_id))
    assert store.errors_for(task.task_id)[0]["kind"] == "rate_limit_event"


def test_record_rate_limit_event_partial_info_keeps_resets_at_without_recording_window(store, make_task):
    """type/status missing shouldn't discard resets_at (§10 needs it to schedule a requeue), but a defaulted "unknown" must not be persisted as a window row."""
    task = make_task()
    message = SimpleNamespace(rate_limit_info=SimpleNamespace(status=None, resets_at=2000000000, rate_limit_type=None, utilization=None))

    resets_at = record_rate_limit_event(store, task, message)

    assert resets_at == 2000000000
    # a defaulted "unknown" would clobber a real window via the ON CONFLICT upsert, so no window row is written
    assert store.rate_limit_windows() == []
    event = [event for event in store.events_for(task.task_id) if event["kind"] == "rate_limit"][0]
    assert json.loads(event["detail_json"]) == {"type": "unknown", "status": "unknown", "utilization": None, "resets_at": 2000000000}
    assert store.errors_for(task.task_id) == []


def test_record_rate_limit_event_partial_info_does_not_clobber_existing_window(store, make_task):
    """a later partial event must leave a previously recorded real window untouched."""
    task = make_task()
    good = SimpleNamespace(rate_limit_info=SimpleNamespace(status="allowed_warning", resets_at=2000000000, rate_limit_type="five_hour", utilization=0.83))
    record_rate_limit_event(store, task, good)

    partial = SimpleNamespace(rate_limit_info=SimpleNamespace(status=None, resets_at=2000000001, rate_limit_type="five_hour", utilization=None))
    resets_at = record_rate_limit_event(store, task, partial)

    assert resets_at == 2000000001
    assert store.rate_limit_windows()[0] | {"observed_at": "ignored"} == {
        "rate_limit_type": "five_hour",
        "status": "allowed_warning",
        "utilization": 0.83,
        "resets_at": 2000000000,
        "observed_at": "ignored",
    }


@pytest.mark.asyncio
async def test_run_session_marks_session_limit_error_retryable(store, config, make_task, tmp_path, monkeypatch):
    config.raw = RAW
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)

    class RateLimitEvent:
        def __init__(self, rate_limit_info):
            self.rate_limit_info = rate_limit_info

    class FakeOptions:
        def __init__(self, **kwargs):
            pass

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield RateLimitEvent(SimpleNamespace(rate_limit_type="five_hour", status="rejected", utilization=1.0, resets_at=1780000000))
            yield SimpleNamespace(total_cost_usd=0.5, num_turns=3, usage={"input_tokens": 40, "output_tokens": 20, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, result="You've hit your session limit · resets 6:50pm (UTC)", is_error=True, subtype="error_max_turns", session_id="s1")

    monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("claude_agent_sdk.HookMatcher", lambda **kwargs: object())
    monkeypatch.setattr("taskboy.runner.build_progress_server", lambda *args: object())

    outcome = await runner._run_session(task, "claude-sonnet-4-6", tmp_path, "prompt", {}, [])

    assert outcome.state == FAILED
    assert outcome.retryable is True
    assert outcome.retry_not_before is not None
    assert store.rate_limit_windows()[0]["resets_at"] == 1780000000
    # a clean session with a result message must record usage exactly once, not once per recording site
    assert len(store.usage_for(task.task_id)) == 1


@pytest.mark.asyncio
async def test_run_session_limit_shaped_text_without_rejected_event_is_not_retryable(store, config, make_task, tmp_path, monkeypatch):
    """a task whose own output merely mentions a rate/session/usage limit must not be requeued —
    only an actually-observed rejecting RateLimitEvent earns a retry (§10)."""
    config.raw = RAW
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)

    class FakeOptions:
        def __init__(self, **kwargs):
            pass

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield SimpleNamespace(total_cost_usd=0.2, num_turns=2, usage={}, result="I reviewed the PR that adds rate limit handling — looks good.", is_error=True, subtype="error", session_id="s1")

    monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("claude_agent_sdk.HookMatcher", lambda **kwargs: object())
    monkeypatch.setattr("taskboy.runner.build_progress_server", lambda *args: object())

    outcome = await runner._run_session(task, "claude-sonnet-4-6", tmp_path, "prompt", {}, [])

    assert outcome.state == FAILED
    assert outcome.retryable is False
    assert outcome.retry_not_before is None
    assert store.rate_limit_windows() == []


@pytest.mark.asyncio
async def test_run_session_non_rejected_rate_limit_event_is_not_retryable(store, config, make_task, tmp_path, monkeypatch):
    """an 'allowed_warning' RateLimitEvent is just a heads-up, not the SDK telling us a request was throttled."""
    config.raw = RAW
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)

    class RateLimitEvent:
        def __init__(self, rate_limit_info):
            self.rate_limit_info = rate_limit_info

    class FakeOptions:
        def __init__(self, **kwargs):
            pass

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield RateLimitEvent(SimpleNamespace(rate_limit_type="five_hour", status="allowed_warning", utilization=0.9, resets_at=1780000000))
            yield SimpleNamespace(total_cost_usd=0.3, num_turns=2, usage={}, result="You've hit your session limit · resets 6:50pm (UTC)", is_error=True, subtype="error", session_id="s1")

    monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("claude_agent_sdk.HookMatcher", lambda **kwargs: object())
    monkeypatch.setattr("taskboy.runner.build_progress_server", lambda *args: object())

    outcome = await runner._run_session(task, "claude-sonnet-4-6", tmp_path, "prompt", {}, [])

    assert outcome.state == FAILED
    assert outcome.retryable is False
    assert outcome.retry_not_before is None
    assert store.rate_limit_windows()[0]["status"] == "allowed_warning"  # observed, but not a rejection


@pytest.mark.asyncio
async def test_run_session_non_limit_error_is_not_retryable(store, config, make_task, tmp_path, monkeypatch):
    config.raw = RAW
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)

    class FakeOptions:
        def __init__(self, **kwargs):
            pass

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield SimpleNamespace(total_cost_usd=0.1, num_turns=1, usage={}, result="something went wrong", is_error=True, subtype="error", session_id="s1")

    monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("claude_agent_sdk.HookMatcher", lambda **kwargs: object())
    monkeypatch.setattr("taskboy.runner.build_progress_server", lambda *args: object())

    outcome = await runner._run_session(task, "claude-sonnet-4-6", tmp_path, "prompt", {}, [])

    assert outcome.state == FAILED
    assert outcome.retryable is False
    assert outcome.retry_not_before is None


@pytest.mark.asyncio
async def test_run_session_without_result_message_fails_instead_of_completing(store, config, make_task, tmp_path, monkeypatch):
    """if the message stream ends without a ResultMessage (CLI dies quietly, stream closes early), the
    session must be reported FAILED — not COMPLETED with an empty '*Done*' summary (issue #91)."""
    config.raw = RAW
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)

    class FakeOptions:
        def __init__(self, **kwargs):
            pass

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield SimpleNamespace(usage={"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}, session_id="s1")
            # stream closes without ever yielding a ResultMessage

    monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("claude_agent_sdk.HookMatcher", lambda **kwargs: object())
    monkeypatch.setattr("taskboy.runner.build_progress_server", lambda *args: object())

    outcome = await runner._run_session(task, "claude-sonnet-4-6", tmp_path, "prompt", {}, [])

    assert outcome.state == FAILED
    rows = store.usage_for(task.task_id)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 100
    assert rows[0]["output_tokens"] == 50
    assert rows[0]["cost_usd"] is None
    assert outcome.error == "session ended without a result message"
    assert outcome.retryable is False
    assert outcome.result_summary == ""
    assert store.errors_for(task.task_id)[0]["kind"] == "missing_result"


@pytest.mark.asyncio
async def test_run_session_records_partial_usage_on_timeout(store, config, make_task, tmp_path, monkeypatch):
    config.raw = RAW
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)

    class FakeOptions:
        def __init__(self, **kwargs):
            pass

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield SimpleNamespace(usage={"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}, session_id="s1")
            yield SimpleNamespace(usage={"input_tokens": 200, "output_tokens": 80, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 20}, session_id="s1")
            # totals deliberately don't equal the sum of the two frames above, to prove this frame's cumulative usage is used verbatim
            yield SimpleNamespace(total_cost_usd=1.23, usage={"input_tokens": 500, "output_tokens": 222, "cache_read_input_tokens": 33, "cache_creation_input_tokens": 44}, num_turns=3, session_id="s1")
            raise TimeoutError()

    monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("claude_agent_sdk.HookMatcher", lambda **kwargs: object())
    monkeypatch.setattr("taskboy.runner.build_progress_server", lambda *args: object())

    outcome = await runner._run_session(task, "claude-sonnet-4-6", tmp_path, "prompt", {}, [])

    assert outcome.state == FAILED
    assert outcome.session_id == "s1"
    rows = store.usage_for(task.task_id)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 500
    assert rows[0]["output_tokens"] == 222
    assert rows[0]["cache_read_tokens"] == 33
    assert rows[0]["cache_write_tokens"] == 44
    assert rows[0]["cost_usd"] == 1.23


@pytest.mark.asyncio
async def test_run_session_records_partial_usage_on_cancellation(store, config, make_task, tmp_path, monkeypatch):
    config.raw = RAW
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)

    class FakeOptions:
        def __init__(self, **kwargs):
            pass

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            # no result frame ever arrives, so the finally block must fall back to the running
            # per-turn sum of these two frames rather than a cumulative total
            yield SimpleNamespace(usage={"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5}, session_id="s1")
            yield SimpleNamespace(usage={"input_tokens": 200, "output_tokens": 80, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 20}, session_id="s1")
            raise asyncio.CancelledError()

    monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("claude_agent_sdk.HookMatcher", lambda **kwargs: object())
    monkeypatch.setattr("taskboy.runner.build_progress_server", lambda *args: object())

    with pytest.raises(asyncio.CancelledError):
        await runner._run_session(task, "claude-sonnet-4-6", tmp_path, "prompt", {}, [])

    rows = store.usage_for(task.task_id)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 300
    assert rows[0]["output_tokens"] == 130
    assert rows[0]["cache_read_tokens"] == 10
    assert rows[0]["cache_write_tokens"] == 25
    assert rows[0]["cost_usd"] is None


@pytest.mark.asyncio
async def test_run_session_timeout_without_usage_records_nothing(store, config, make_task, tmp_path, monkeypatch):
    config.raw = RAW
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)
    store.set_fields(task.task_id, max_runtime_minutes=None)
    task = store.get_task(task.task_id)

    class FakeOptions:
        def __init__(self, **kwargs):
            pass

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            raise TimeoutError()
            yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr("claude_agent_sdk.ClaudeAgentOptions", FakeOptions)
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    monkeypatch.setattr("claude_agent_sdk.HookMatcher", lambda **kwargs: object())
    monkeypatch.setattr("taskboy.runner.build_progress_server", lambda *args: object())

    outcome = await runner._run_session(task, "claude-sonnet-4-6", tmp_path, "prompt", {}, [])

    assert outcome.state == FAILED
    assert outcome.error == "exceeded the 60 minute runtime limit"
    assert store.errors_for(task.task_id)[0]["message"] == "exceeded the 60 minute runtime limit"
    assert store.usage_for(task.task_id) == []


@pytest.mark.asyncio
async def test_artifact_milestone_is_audited_and_debugged_without_requester_progress(store, config, make_task, tmp_path):
    runner = make_runner(store, config, tmp_path)
    runner.debug = AsyncMock()
    task = routed_task(store, make_task)

    await runner._on_artifact_milestone(task)("Opened pull request https://example.test/pr/1")

    runner.progress.assert_not_awaited()
    runner.debug.progress.assert_awaited_once_with(task, "Opened pull request https://example.test/pr/1")
    milestones = [event for event in store.events_for(task.task_id) if event["kind"] == "milestone"]
    assert len(milestones) == 1


@pytest.mark.asyncio
async def test_question_asker_records_and_blocks(store, config, make_task, tmp_path):
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)
    blocked: dict = {}
    ask = runner._question_asker(task, blocked)
    assert "no questions recorded" in await ask("   ")
    assert blocked == {}
    message = await ask("1. Which environment?\n2. Postgres or Dynamo?")
    assert "stop working now" in message
    assert blocked["reason"] == "waiting for the requester to answer follow-up questions"
    pending = store.pending_questions_for(task.task_id)
    assert pending["questions"] == "1. Which environment?\n2. Postgres or Dynamo?"
    assert any(e["kind"] == "questions_asked" for e in store.events_for(task.task_id))


@pytest.mark.asyncio
async def test_question_asker_tells_an_issue_backed_task_it_reopens_instead_of_slack(store, config, make_task, tmp_path):
    # an issue-backed task's questions land on the issue and reopen it, not a Slack thread reply (#76)
    runner = make_runner(store, config, tmp_path)
    task = routed_task(store, make_task)
    issue = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    store.decide_issue(issue["id"], "approved", "boss")
    store.start_issue(issue["id"], task.task_id, "the spec")
    blocked: dict = {}
    ask = runner._question_asker(task, blocked)

    message = await ask("1. Which environment?")

    assert "tracked as an issue" in message and "reopened as 'proposed'" in message
    assert "resumes automatically with their answers" not in message


@pytest.mark.asyncio
async def test_run_replays_answered_questions_into_prompt(store, config, make_task, tmp_path):
    runner = make_runner(store, config, tmp_path)
    runner._run_session = AsyncMock(return_value=Outcome(state=COMPLETED, result_summary="done"))
    task = routed_task(store, make_task)
    store.ask_questions(task.task_id, "1. Which env?")
    store.answer_questions(task.task_id, "1. staging", "U1")
    await runner.run(store.get_task(task.task_id))
    prompt = runner._run_session.call_args.args[3]
    assert "## Answers from the requester" in prompt
    assert "1. Which env?" in prompt
    assert "1. staging" in prompt


def test_build_progress_server_exposes_ask_questions():
    from taskboy.runner import build_progress_server

    server = build_progress_server(AsyncMock(), {}, AsyncMock(return_value="ok"), AsyncMock(return_value="ok"))
    assert server is not None


# -- disabled services drop their mcp tools from session allowlists ----------


def test_strip_disabled_service_tools_filters_only_disabled_service_prefixes():
    from taskboy.runner import strip_disabled_service_tools
    from tests.conftest import make_config

    config = make_config(services={"slack": False, "github": True, "jira": False, "confluence": False, "sentry": False, "aws": False})
    tools = ["Read", "Bash", "mcp__harness__report_progress", "mcp__github__create_pull_request", "mcp__jira__get_issue", "mcp__slack__send_dm", "mcp__sentry__list_issues", "mcp__issues__update_issue"]
    assert strip_disabled_service_tools(tools, config) == ["Read", "Bash", "mcp__harness__report_progress", "mcp__github__create_pull_request", "mcp__issues__update_issue"]
