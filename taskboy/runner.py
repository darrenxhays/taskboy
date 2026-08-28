"""sub-agent runners: the echo runner (dev/lifecycle tests) and the real claude-agent-sdk runner.

one session per task, isolated cwd + HOME, budgets from the routing profile, policy + audit
via TaskHooks, milestones via an in-process mcp server. model-unavailable errors walk the
configured fallback chain and never pick an unconfigured model (MOD-009).
"""

import asyncio
import json
import logging
import re
import shutil
from datetime import datetime, timedelta, timezone

from taskboy import memory, repocache, settings, skills, workspace
from taskboy.config import KNOWN_SERVICES, Config, role_for
from taskboy.hooks import TOOL_CLASSIFICATION, TaskHooks, repo_grantable, tool_grantable
from taskboy.models import BLOCKED, COMPLETED, FAILED, Outcome, Task
from taskboy.personality import load as load_personality
from taskboy.prompts import CONVENTIONS_FILENAME, task_prompt
from taskboy.router import fallback_chain
from taskboy.store import Store

logger = logging.getLogger("taskboy.runner")


class EchoRunner:
    """fake runner for development and lifecycle tests: sleeps, then echoes the request back."""

    def __init__(self, seconds: float = 1.0):
        self.seconds = seconds

    async def run(self, task: Task) -> Outcome:
        await asyncio.sleep(self.seconds)
        return Outcome(state=COMPLETED, result_summary=f"echo: {task.request_text}")


class ModelUnavailable(Exception):
    """the session could not start (or continue) because the model was refused/overloaded."""


def record_rate_limit_event(store: Store, task: Task, message) -> int | None:
    """persist a RateLimitEvent and return its resets_at; partial events default type/status to "unknown" rather than raising, so resets_at survives for the requeue gate (§10)."""
    detail: dict[str, object] = {"type": None, "status": None, "utilization": None, "resets_at": None}
    try:
        info = getattr(message, "rate_limit_info", None)
        if info is None:
            raise ValueError("RateLimitEvent has no rate_limit_info")
        rate_limit_type = getattr(info, "rate_limit_type", None) or "unknown"
        status = getattr(info, "status", None) or "unknown"
        partial = rate_limit_type == "unknown" or status == "unknown"
        if partial:
            logger.warning("rate-limit event missing type or status for %s — recording partial detail", task.task_id)
        utilization_value = getattr(info, "utilization", None)
        resets_at_value = getattr(info, "resets_at", None)
        detail = {
            "type": str(rate_limit_type),
            "status": str(status),
            "utilization": float(utilization_value) if utilization_value is not None else None,
            "resets_at": int(resets_at_value) if resets_at_value is not None else None,
        }
    except Exception as exc:
        try:
            store.add_event(task.task_id, "rate_limit", detail)
            store.add_error("runner", "rate_limit_event", str(exc), task_id=task.task_id)
        except Exception:
            logger.exception("failed to record malformed rate-limit event for %s", task.task_id)
        return None
    try:
        store.add_event(task.task_id, "rate_limit", detail)
        # a defaulted "unknown" type/status would clobber a real window's row via record_rate_limit's ON CONFLICT upsert, so only record a fully-resolved event; the requeue path only needs the returned resets_at
        if not partial:
            store.record_rate_limit(str(rate_limit_type), str(status), float(utilization_value) if utilization_value is not None else None, int(resets_at_value) if resets_at_value is not None else None)
    except Exception as exc:
        try:
            store.add_error("runner", "rate_limit_event", str(exc), task_id=task.task_id)
        except Exception:
            logger.exception("failed to record rate-limit event for %s", task.task_id)
        return None
    return detail["resets_at"]  # type: ignore[return-value]


def strip_disabled_service_tools(tools: list[str], config: Config) -> list[str]:
    """drop mcp tools belonging to services the operator turned off, so allowlists and prompts never advertise dead tools."""
    disabled = [name for name in KNOWN_SERVICES if not config.service_enabled(name)]
    return [tool for tool in tools if not any(tool.startswith(f"mcp__{name}__") for name in disabled)]


def session_option_kwargs(task: Task, model_id: str, ws, profile: dict) -> dict:
    """scalar sdk session options derived only from routed task state and profile config."""
    kwargs = {
        "cwd": str(ws / "repo"),
        "model": model_id,
        "allowed_tools": list(profile.get("allowed_tools") or []),
        "permission_mode": "acceptEdits",
        "max_turns": task.max_turns,
        "max_budget_usd": task.max_budget_usd,
        "setting_sources": [],
        "resume": task.resume_session_id or None,
        # commits and PRs must be attributed to the bot persona alone, not the CLI's default attribution
        "settings": json.dumps({"includeCoAuthoredBy": False}),
    }
    # precedence: an explicit Slack override wins, then the classifier's own pick (persisted as task.effort,
    # issue #67), then the routed profile's configured default
    effort = task.effort_override or task.effort or profile.get("effort")
    if effort is not None:
        kwargs["effort"] = effort
    if "thinking" in profile:
        kwargs["thinking"] = profile["thinking"]
    return kwargs


class ClaudeRunner:
    def __init__(self, store: Store, config: Config, workspaces_root: str, memory_root: str, progress, broker=None, secrets=None, slack_client=None, debug=None, reviewer_broker=None):
        self.store = store
        self.config = config
        self.workspaces_root = workspaces_root
        self.memory_root = memory_root
        self.progress = progress  # async (task, message) -> None; feeds report_progress to slack
        self.broker = broker  # CredentialBroker; when set, sessions get git push auth via the credential helper
        self.reviewer_broker = reviewer_broker
        self.secrets = secrets  # jira/sentry credentials for the adapters; never enter the session env
        self.slack_client = slack_client
        self.debug = debug

    async def run(self, task: Task) -> Outcome:
        ws = workspace.create(self.workspaces_root, task.task_id)
        self.store.set_fields(task.task_id, workspace_path=str(ws))
        classification = json.loads(task.classification_json) if task.classification_json else {}
        skill_prompt = None
        skill_internal_tools: list[str] = []
        if classification.get("skill"):
            name = str(classification["skill"])
            try:
                variables = skills.runtime_variables(self.config)
                skill = skills.resolve(settings.SKILLS_ROOT, name, variables)
                if skill is None:
                    raise skills.SkillError(f"skill /{name} is not installed")
                instructions = skills.render(settings.SKILLS_ROOT, name, variables)
            except (OSError, ValueError) as e:
                self.store.add_error("runner", type(e).__name__, str(e), task_id=task.task_id, context={"skill": name})
                return Outcome(state=FAILED, error=str(e))
            skill_internal_tools = skill.internal_tools  # loaded fresh from disk: the gate for the issues/enqueue servers
            skill_prompt = {"name": name, "args": str(classification.get("skill_args") or ""), "instructions": instructions}
        models = self.config.raw.get("models") or {}
        github = self.config.raw.get("github") or {}
        approved_repos = github.get("approved_repos") or []
        self_repo = str(github.get("self_repo") or "")
        role = role_for(self.config.roles, task.slack_user_id)
        scoped_repos = [repo for repo in approved_repos if role is None or role.repos is None or repo in role.repos]
        scoped_targets = [repo for repo in (classification.get("target_repos") or []) if repo in scoped_repos]
        is_reviewer = task.persona == "reviewer" and self.config.reviewer.enabled
        task_broker = self.reviewer_broker if is_reviewer and self.reviewer_broker is not None else self.broker
        if is_reviewer and self.reviewer_broker is None:
            logger.warning("reviewer task %s has no reviewer credential broker — falling back to the main broker", task.task_id)
        # operator-granted permissions widen this run's scope: extra tools go to the allowlist, extra repos get cloned.
        # grants are gated exactly like the original request — recognized tools within the profile tier; a granted repo
        # either was already role-scoped, or is an org/installation-grantable escalation (repo_grantable, issue #39)
        profile_tools = strip_disabled_service_tools(list(((self.config.raw.get("profiles") or {}).get(task.profile or "", {})).get("allowed_tools") or []), self.config)
        granted = self.store.granted_permissions_for(task.task_id)
        granted_tools = [tool_name for tool_name in granted["tools"] if tool_grantable(tool_name, profile_tools)]
        accessible_repos = task_broker.accessible_repos if task_broker is not None else None
        granted_repos = [repo for repo in granted["repos"] if repo in scoped_repos or repo_grantable(repo, approved_repos, accessible_repos)]
        # widen scoped_repos itself so a granted escalation actually takes effect downstream: register_task's token
        # scope, the github adapter's approved list, and any further in-session permission requests all key off it
        scoped_repos = scoped_repos + [repo for repo in granted_repos if repo not in scoped_repos]
        for repo in granted_repos:
            if repo not in scoped_targets:
                scoped_targets.append(repo)
        applied_permissions = {"tools": granted_tools, "repos": granted_repos} if (granted_tools or granted_repos) else None
        if applied_permissions:
            self.store.add_event(task.task_id, "permissions_applied", applied_permissions)
        broker_env: dict[str, str] = {}
        cloned_repos: list[str] = []
        failed_repo_clones: list[str] = []
        if task_broker is not None:
            for repo in scoped_targets:
                dest = ws / "repo" / repo.split("/", 1)[-1]
                if (dest / ".git").is_dir():
                    # clone_from_mirror rmtrees dest on failure — don't re-clone over a prior attempt's tree
                    cloned_repos.append(repo)
                    continue
                refreshed = await repocache.refresh_one(task_broker, settings.REPOS_ROOT, repo, timeout=60)
                cloned = refreshed and await repocache.clone_from_mirror(settings.REPOS_ROOT, repo, dest)
                if cloned:
                    cloned_repos.append(repo)
                else:
                    failed_repo_clones.append(repo)
                    stage = "refresh" if not refreshed else "clone"
                    detail = {"repository": repo, "stage": stage}
                    self.store.add_error("runner", "repo_seed_failed", f"failed to pre-clone {repo} into workspace ({stage} step failed)", task_id=task.task_id, context=detail)
                    self.store.add_event(task.task_id, "repo_seed_failed", detail)
        if task_broker is not None:
            # granted_repos are passed through so the live credential token is scoped to them too,
            # not just the task's original classification — otherwise mid-session git ops 403 (§8.4)
            # git resolves core.hooksPath against its own cwd, so the path must be absolute (#75)
            broker_env = task_broker.register_task(task, scoped_repos, granted_repos=granted_repos, hooks_path=str(workspace.hooks_dir(ws).resolve()))
        conventions_path = self.config.conventions_path
        inject_conventions = bool(scoped_targets) and conventions_path is not None
        if inject_conventions:
            try:
                assert conventions_path is not None
                shutil.copyfile(conventions_path, ws / "repo" / CONVENTIONS_FILENAME)
            except OSError as e:
                logger.warning("could not inject engineering conventions for %s: %s", task.task_id, e)
                inject_conventions = False
        personality_path = self.config.reviewer.personality_path if is_reviewer else self.config.personality_path
        personality = load_personality(personality_path)
        if personality:
            self.store.add_event(task.task_id, "personality", {"hash": personality[1], "path": personality_path})
        jira_available = bool(self.config.service_enabled("jira") and self.secrets is not None and self.secrets.jira_enabled and (self.config.raw.get("jira") or {}).get("site"))
        prompt = task_prompt(
            task,
            memory.parent_context(self.store, self.memory_root, task),
            self.store.artifacts_for(task.task_id),
            github=task_broker is not None,
            jira=jira_available,
            bot_name=self.config.reviewer.name if is_reviewer else self.config.agent_name,
            other_bot_name=self.config.agent_name if is_reviewer else self.config.reviewer.name,
            is_reviewer=is_reviewer,
            thread_context=task.thread_context,
            cloned_repos=cloned_repos,
            failed_repo_clones=failed_repo_clones,
            skill=skill_prompt,
            conventions=inject_conventions,
            personality=personality[0] if personality else None,
            self_repo=self_repo if self_repo in scoped_targets else None,
            granted_permissions=applied_permissions,
            answered_questions=self.store.answered_questions_for(task.task_id),
        )
        if self.debug is not None:
            await self.debug.prompt_file(task, prompt)
        try:
            last_error: Exception | None = None
            for alias in fallback_chain(task.model_alias or "sonnet", models):
                if alias != task.model_alias:
                    self.store.add_event(task.task_id, "model_fallback", {"from": task.model_alias, "to": alias})
                try:
                    return await self._run_session(task, str(models[alias]["id"]), ws, prompt, broker_env, scoped_repos, task_broker, granted_tools, skill_internal_tools)
                except ModelUnavailable as e:
                    logger.warning("model %s unavailable for %s: %s", alias, task.task_id, e)
                    self.store.add_error("runner", type(e).__name__, str(e), task_id=task.task_id, context={"model_alias": alias})
                    last_error = e
            # chain exhausted: block visibly, never silently substitute (MOD-009)
            return Outcome(state=BLOCKED, blocked_reason=f"no configured model is currently available ({last_error})")
        finally:
            if task_broker is not None:
                task_broker.release_task(task.task_id)

    async def _run_session(self, task: Task, model_id: str, ws, prompt: str, broker_env: dict[str, str], scoped_repos: list[str], task_broker=None, granted_tools: list[str] | None = None, skill_internal_tools: list[str] | None = None) -> Outcome:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher

        from taskboy.adapters.issues import ENQUEUE_TOOLS, ISSUES_TOOLS, EnqueueAdapter, IssuesAdapter, build_enqueue_server, build_issues_server

        github = self.config.raw.get("github") or {}
        blocked: dict = {}
        profile = (self.config.raw.get("profiles") or {}).get(task.profile or "", {})
        allowed_tools = list(profile.get("allowed_tools") or [])
        profile_tools = list(allowed_tools)  # the base tier allowlist, before any mid-task grant widens it
        for tool_name in granted_tools or []:
            if tool_name not in allowed_tools:
                allowed_tools.append(tool_name)
        # skills that opt into a capability server get its tools admitted to the allowlist for this session
        internal = set(skill_internal_tools or [])
        if "issues" in internal:
            allowed_tools += [name for name in ISSUES_TOOLS if name not in allowed_tools]
        if "enqueue" in internal:
            allowed_tools += [name for name in ENQUEUE_TOOLS if name not in allowed_tools]
        allowed_tools = strip_disabled_service_tools(allowed_tools, self.config)
        profile = {**profile, "allowed_tools": allowed_tools}
        hooks = TaskHooks(self.store, task, github.get("protected_branch_patterns") or [], allowed_tools=allowed_tools or None)
        # metadata-disabled is the sdk-level courtesy layer; the real imds block is the per-uid iptables rule on the host (§8.3)
        is_reviewer = task.persona == "reviewer" and self.config.reviewer.enabled
        bot_name = self.config.reviewer.name if is_reviewer else self.config.agent_name
        other_bot_name = self.config.agent_name if is_reviewer else self.config.reviewer.name
        identity_env = git_identity_env(github, name=self.config.reviewer.commit_name, email=self.config.reviewer.commit_email) if is_reviewer else git_identity_env(github, name=github.get("commit_name") or self.config.agent_name)
        env = {"HOME": str(ws / "home"), "AWS_EC2_METADATA_DISABLED": "true", **identity_env, **broker_env}
        progress = self._on_progress(task)
        approved_repos = github.get("approved_repos") or []
        accessible_repos = task_broker.accessible_repos if task_broker is not None else None
        mcp_servers = {"harness": build_progress_server(progress, blocked, self._permission_requester(task, blocked, allowed_tools, profile_tools, scoped_repos, approved_repos, accessible_repos, progress), self._question_asker(task, blocked))}
        if "issues" in internal:
            mcp_servers["issues"] = build_issues_server(IssuesAdapter(self.store, task, approved_repos, bot_name=bot_name))
        if "enqueue" in internal:
            mcp_servers["enqueue"] = build_enqueue_server(EnqueueAdapter(self.store, task))
        if task_broker is not None:
            from taskboy.adapters.github_api import GitHubAdapter, build_github_server

            mcp_servers["github"] = build_github_server(
                GitHubAdapter(task_broker, self.store, task, scoped_repos, on_milestone=self._on_artifact_milestone(task), main_broker=self.broker, reviewer_broker=self.reviewer_broker, bot_name=bot_name, other_bot_name=other_bot_name, can_approve=is_reviewer)
            )
        jira_config = self.config.raw.get("jira") or {}
        if self.config.service_enabled("jira") and self.secrets is not None and self.secrets.jira_enabled and jira_config.get("site"):
            from taskboy.adapters.jira import JiraAdapter, build_jira_server

            mcp_servers["jira"] = build_jira_server(
                JiraAdapter(
                    self.store,
                    task,
                    jira_config["site"],
                    self.secrets.jira_email,
                    self.secrets.jira_api_token,
                    jira_config.get("projects") or [],
                    jira_config.get("issue_types") or [],
                    story_points_field=jira_config.get("story_points_field") or "",
                    on_milestone=self._on_artifact_milestone(task),
                    bot_name=bot_name,
                )
            )
        confluence_config = self.config.raw.get("confluence") or {}
        if self.config.service_enabled("confluence") and self.secrets is not None and self.secrets.jira_enabled and confluence_config.get("site"):
            from taskboy.adapters.confluence import ConfluenceAdapter, build_confluence_server

            mcp_servers["confluence"] = build_confluence_server(ConfluenceAdapter(self.store, task, confluence_config["site"], self.secrets.jira_email, self.secrets.jira_api_token, confluence_config.get("spaces") or []))
        sentry_config = self.config.raw.get("sentry") or {}
        if self.config.service_enabled("sentry") and self.secrets is not None and self.secrets.sentry_token and sentry_config.get("organization"):
            from taskboy.adapters.sentry import SentryAdapter, build_sentry_server

            mcp_servers["sentry"] = build_sentry_server(SentryAdapter(self.store, task, sentry_config["organization"], self.secrets.sentry_token, sentry_config.get("projects") or []))
        aws_config = self.config.raw.get("aws") or {}
        if self.config.service_enabled("aws") and aws_config.get("allowed_services"):
            from taskboy.adapters.aws_read import AwsReadAdapter, build_aws_server

            mcp_servers["aws"] = build_aws_server(AwsReadAdapter(self.store, task, aws_config["allowed_services"], aws_config.get("allowed_regions") or [], role_arns=aws_config.get("diagnostics_role_arns") or {}))
        if self.slack_client is not None:
            from taskboy.adapters.slack_history import SlackHistoryAdapter, build_slack_server

            mcp_servers["slack"] = build_slack_server(SlackHistoryAdapter(self.store, task, self.slack_client, allowed_channels=self.config.slack.allowed_channels, files_dir=ws / "repo" / "slack_files"))
        options = ClaudeAgentOptions(
            **session_option_kwargs(task, model_id, ws, profile),
            # PreToolUse fires for every call, even auto-approved allowlisted tools; can_use_tool alone would be shadowed
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[hooks.pre_tool_use])]},  # type: ignore[list-item]
            can_use_tool=hooks.can_use_tool,
            mcp_servers=mcp_servers,
            env=env,
        )
        session_id = task.resume_session_id
        final = None
        limit_resets_at: int | None = None
        saw_rejected_rate_limit = False
        limit_minutes = task.max_runtime_minutes or 60
        running_usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        try:
            async with asyncio.timeout(limit_minutes * 60):
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(prompt)
                    async for message in client.receive_response():
                        sid = session_id_of(message)
                        if sid and sid != session_id:
                            session_id = sid
                            self.store.set_fields(task.task_id, session_id=sid)  # crash-resume depends on this being durable early
                        if type(message).__name__ == "RateLimitEvent":
                            resets_at = record_rate_limit_event(self.store, task, message)  # §10
                            if resets_at is not None:
                                limit_resets_at = resets_at
                            # "rejected" is the SDK's own signal that a request was actually throttled — anything
                            # weaker (e.g. "allowed_warning") is just a heads-up, not a reason to requeue (§10)
                            if getattr(getattr(message, "rate_limit_info", None), "status", None) == "rejected":
                                saw_rejected_rate_limit = True
                        if is_result(message):
                            final = message
                            continue
                        # assistant frames carry per-turn usage; the result frame's is cumulative
                        message_usage = getattr(message, "usage", None) or {}
                        for key in running_usage:
                            running_usage[key] += message_usage.get(key) or 0
        except TimeoutError:
            error = f"exceeded the {limit_minutes} minute runtime limit"
            self.store.add_error("runner", "timeout", error, task_id=task.task_id)
            return Outcome(state=FAILED, error=error, session_id=session_id)
        except Exception as e:
            if looks_model_unavailable(e):
                raise ModelUnavailable(str(e)) from e
            raise
        finally:
            # the result frame's cumulative usage/cost are exact, unlike the running per-turn sum; record
            # whichever we have, so this is the single site that persists usage either way (issue #101, #91)
            record_usage = getattr(final, "usage", None) or running_usage
            record_cost = getattr(final, "total_cost_usd", None)
            if record_cost or any(record_usage.get(key) or 0 for key in running_usage):
                self.store.add_usage(
                    task.task_id,
                    "subagent",
                    model_id,
                    input_tokens=record_usage.get("input_tokens"),
                    output_tokens=record_usage.get("output_tokens"),
                    cache_read_tokens=record_usage.get("cache_read_input_tokens"),
                    cache_write_tokens=record_usage.get("cache_creation_input_tokens"),
                    cost_usd=record_cost,
                )

        cost = float(getattr(final, "total_cost_usd", 0) or 0)
        turns = int(getattr(final, "num_turns", 0) or 0)
        if blocked:
            return Outcome(state=BLOCKED, blocked_reason=blocked.get("reason", "blocked"), session_id=session_id, cost_usd=cost, num_turns=turns)
        if final is None:
            error = "session ended without a result message"
            self.store.add_error("runner", "missing_result", error, task_id=task.task_id)
            return Outcome(state=FAILED, error=error, session_id=session_id, cost_usd=cost, num_turns=turns)
        text = str(getattr(final, "result", "") or "")
        if getattr(final, "is_error", False):
            error = text or f"session ended with {getattr(final, 'subtype', 'an error')}"
            self.store.add_error("runner", "session_error", error, task_id=task.task_id)
            # text alone is not trustworthy (a task's own output can happen to mention "rate limit"); only
            # requeue when the SDK actually told us this run got throttled (§10)
            if saw_rejected_rate_limit and looks_session_limit(error):
                return Outcome(state=FAILED, error=error, session_id=session_id, cost_usd=cost, num_turns=turns, retryable=True, retry_not_before=retry_not_before(limit_resets_at))
            return Outcome(state=FAILED, error=error, session_id=session_id, cost_usd=cost, num_turns=turns)
        return Outcome(state=COMPLETED, result_summary=extract_final_report(text), reply=extract_reply(text), session_id=session_id, cost_usd=cost, num_turns=turns)

    def _on_progress(self, task: Task):
        async def post(message: str) -> None:
            self.store.add_event(task.task_id, "milestone", {"message": message[:500]})
            await self.progress(task, message)

        return post

    def _on_artifact_milestone(self, task: Task):
        async def audit(message: str) -> None:
            self.store.add_event(task.task_id, "milestone", {"message": message[:500]})
            if self.debug is not None:
                await self.debug.progress(task, message)

        return audit

    def _permission_requester(self, task: Task, blocked: dict, allowed_tools: list[str], profile_tools: list[str], scoped_repos: list[str], approved_repos: list[str], accessible_repos: set[str] | None, progress):
        """validate a session's request for one extra tool/repo, record it for operator review, and stop the task (§8.4).

        allowed_tools is the effective allowlist (base + any prior grant); profile_tools is the base tier used to
        gate escalation; scoped_repos is the role-scoped repo set (not the org-wide approved list) — a repo request
        outside it is still recorded for operator review as long as it's a well-formed 'owner/name' whose org
        already appears in approved_repos and, when accessible_repos is known, the github app is installed on it;
        auto-rejection is reserved for malformed targets or repos outside the org/installation (issue #39)."""

        async def request(kind: str, target: str, reason: str) -> str:
            kind = (kind or "").strip().lower()
            target = (target or "").strip()
            reason = (reason or "").strip()
            if kind not in ("tool", "repo"):
                return "kind must be 'tool' or 'repo'. no request recorded."
            if not target:
                return f"a target {kind} is required. no request recorded."
            if kind == "tool":
                if target not in TOOL_CLASSIFICATION:
                    return f"'{target}' is not a recognized tool, so it cannot be granted. no request recorded."
                if target in allowed_tools:
                    return f"you already have {target} for this task — no permission needed."
                if not tool_grantable(target, profile_tools):
                    return f"'{target}' is a write tool and this task runs on a read-only profile, so it cannot be granted. no request recorded."
            else:
                if "/" not in target:
                    return "a repo target must be 'owner/name'. no request recorded."
                if target not in scoped_repos and not repo_grantable(target, approved_repos, accessible_repos):
                    return f"{target} is not an approved repository for this task, so it cannot be granted. no request recorded."
            self.store.request_permission(task.task_id, kind, target, reason)
            self.store.add_event(task.task_id, "permission_request", {"kind": kind, "target": target, "reason": reason[:500]})
            blocked["reason"] = f"needs permission for {kind} '{target}': {reason[:300]}" if reason else f"needs permission for {kind} '{target}'"
            await progress(f"Requested permission for {kind} `{target}` to continue — awaiting operator approval.")
            return f"recorded a permission request for {kind} '{target}'. an operator will review it; if granted, this task resumes automatically with the access. stop working now."

        return request

    def _question_asker(self, task: Task, blocked: dict):
        """record the session's follow-up questions for the requester and stop the task until they answer."""

        async def ask(questions: str) -> str:
            questions = (questions or "").strip()
            if not questions:
                return "questions text is required. no questions recorded."
            self.store.ask_questions(task.task_id, questions)
            self.store.add_event(task.task_id, "questions_asked", {"questions": questions[:1000]})
            blocked["reason"] = "waiting for the requester to answer follow-up questions"
            if self.store.issue_for_task(task.task_id) is not None:
                return "recorded; this task is tracked as an issue, so it will be reopened as 'proposed' with these questions posted there instead of asked in the Slack thread. stop working now."
            return "recorded; the requester will be asked in the Slack thread and this task resumes automatically with their answers. stop working now."

        return ask


def build_progress_server(on_progress, blocked: dict, on_permission_request=None, on_questions=None):
    """in-process mcp server exposing report_progress / report_blocked / request_permission / ask_questions to the session (SLK-006, ORC-010, §8.4)."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool("report_progress", "Post a short milestone update to the requester. Use only at meaningful milestones.", {"message": str})
    async def report_progress(args: dict) -> dict:
        await on_progress(str(args.get("message", "")))
        return {"content": [{"type": "text", "text": "posted"}]}

    @tool("report_blocked", "Report that you cannot proceed, and what is needed to continue. Then stop working.", {"reason": str, "needed": str})
    async def report_blocked(args: dict) -> dict:
        blocked["reason"] = f"{args.get('reason', '')} — needs: {args.get('needed', '')}"
        return {"content": [{"type": "text", "text": "recorded; stop working now"}]}

    @tool(
        "request_permission",
        "Request one additional permission this task did not start with: kind='tool' with target a tool name you were denied (e.g. mcp__jira__add_comment), or kind='repo' with target an approved 'owner/name' repository you need to access. reason explains why it is needed. An operator reviews it; if granted the task resumes automatically with the access. After calling this, stop working.",
        {"kind": str, "target": str, "reason": str},
    )
    async def request_permission(args: dict) -> dict:
        if on_permission_request is None:
            return {"content": [{"type": "text", "text": "permission requests are not available for this task"}]}
        message = await on_permission_request(str(args.get("kind", "")), str(args.get("target", "")), str(args.get("reason", "")))
        return {"content": [{"type": "text", "text": message}]}

    @tool(
        "ask_questions",
        "Ask the requester follow-up questions when requirements or a design decision are genuinely ambiguous. Put every question you have in this ONE call, as a numbered list with one question per line. The requester is asked in the Slack thread; when they answer, this task resumes automatically with their answers. After calling this, stop working.",
        {"questions": str},
    )
    async def ask_questions(args: dict) -> dict:
        if on_questions is None:
            return {"content": [{"type": "text", "text": "follow-up questions are not available for this task"}]}
        message = await on_questions(str(args.get("questions", "")))
        return {"content": [{"type": "text", "text": message}]}

    return create_sdk_mcp_server(name="harness", version="1.0.0", tools=[report_progress, report_blocked, request_permission, ask_questions])


def git_identity_env(github_config: dict, *, name: str | None = None, email: str | None = None) -> dict[str, str]:
    """commit author/committer identity for task sessions — per-task HOME has no .gitconfig."""
    name = str(name if name is not None else github_config.get("commit_name") or "Agent")
    email = str(email if email is not None else github_config.get("commit_email") or "")
    if not email:
        return {}
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def session_id_of(message) -> str | None:
    sid = getattr(message, "session_id", None)
    if sid:
        return str(sid)
    data = getattr(message, "data", None)
    if isinstance(data, dict) and data.get("session_id"):
        return str(data["session_id"])
    return None


def is_result(message) -> bool:
    return type(message).__name__ == "ResultMessage" or hasattr(message, "total_cost_usd")


def looks_model_unavailable(error: Exception) -> bool:
    """error-shape heuristic, refined by phase 0 spike findings."""
    text = str(error).lower()
    return "model" in text and any(marker in text for marker in ("not found", "unavailable", "invalid", "overloaded", "not_found_error", "529", "404"))


def looks_session_limit(text: str) -> bool:
    """error-shape heuristic for transient claude usage/session limits — text alone is not a retry gate,
    it only labels the error once an actual rejecting RateLimitEvent has been observed (§10)."""
    lowered = text.lower()
    return any(marker in lowered for marker in ("session limit", "rate limit", "usage limit"))


def retry_not_before(limit_resets_at: int | None) -> str:
    """when to requeue a task that died on a session/rate limit: shortly after the recorded reset, a default
    wait when no reset time was observed, and never later than a bounded cap either way."""
    now = datetime.now(timezone.utc)
    if limit_resets_at is not None:
        candidate = datetime.fromtimestamp(limit_resets_at + 60, tz=timezone.utc)
    else:
        candidate = now + timedelta(minutes=15)
    cap = now + timedelta(hours=6)
    if candidate > cap:
        candidate = cap
    return candidate.isoformat(timespec="seconds")


def extract_final_report(text: str) -> str:
    """the prompt requires a `## Final Report` section; fall back to the tail of the message."""
    match = re.search(r"^##\s*Final Report\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
    if match:
        return text[match.start() :].strip()
    return text.strip()[-4000:]


def extract_reply(text: str) -> str:
    """return the requester-facing Reply body, or empty when an older session omitted it."""
    match = re.search(r"^##\s*Reply\s*$", text, flags=re.MULTILINE | re.IGNORECASE)
    if not match:
        return ""
    final = re.search(r"^##\s*Final Report\s*$", text[match.end() :], flags=re.MULTILINE | re.IGNORECASE)
    end = match.end() + final.start() if final else len(text)
    return text[match.end() : end].strip()
