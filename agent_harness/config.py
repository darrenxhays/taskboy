"""config.yaml loading and validation. invalid config fails fast (exit 64) so systemd stops instead of crash-looping."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from agent_harness.models import EFFORT_LEVELS


class ConfigError(Exception):
    pass


@dataclass
class SlackConfig:
    team_id: str
    allowed_channels: list[str]  # empty = any channel the bot is invited to (the invite is the gate)
    ack_reaction: bool = True
    debug_channel: str = ""
    task_started_messages_path: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.team_id)


@dataclass
class ReviewerConfig:
    # optional second github-only persona that reviews the main agent's prs (needs its own github app)
    enabled: bool = False
    name: str = "Reviewer"
    personality_path: str | None = None
    review_agent_prs: bool = True
    commit_name: str = "Reviewer"
    commit_email: str = ""


@dataclass
class DashboardConfig:
    # mission control web ui. served behind an alb that authenticates auth0 sso; the app
    # trusts the alb's signed identity header, so bind stays private (alb -> instance only).
    enabled: bool = False
    bind: str = "127.0.0.1"  # 0.0.0.0 on the host (security group limits ingress to the alb)
    port: int = 8787
    allowed_email_domain: str = ""  # any member of this email domain can view; required when enabled
    admin_emails: list[str] = field(default_factory=list)  # only these emails can edit/cancel/retry
    dev_user_email: str | None = None  # local-only identity when no alb header is present
    public_url: str = ""  # e.g. https://agent.example.com; empty disables dashboard links in slack posts
    commit_repo: str = ""  # e.g. example-org/agent-harness; empty disables auto-commit of edits
    commit_branch: str = "main"
    committer_name: str = ""  # git committer identity for dashboard auto-commits; defaults to "<agent name> Dashboard"
    committer_email: str = ""  # required when auto_commit.repo is set

    @property
    def auto_commit_enabled(self) -> bool:
        return bool(self.commit_repo)


@dataclass
class CliUpdateConfig:
    # off-peak, in-process upgrade of the claude-agent-sdk package (which bundles the claude cli) in the
    # service venv, plus a restart so the new version takes effect — ungated by CI (issue #67)
    enabled: bool = False
    at_time: str = "02:00"
    tzname: str = "America/Los_Angeles"


@dataclass
class Role:
    name: str
    members: list[str]
    allowed_profiles: list[str]
    model_override: bool
    max_budget_usd: float | None
    repos: list[str] | None


@dataclass
class Config:
    max_concurrency: int
    queue_max: int
    max_retries: int
    progress_min_interval_seconds: int
    runner: str  # echo (dev/lifecycle testing) | claude (real sub-agent sessions)
    slack: SlackConfig
    roles: dict[str, Role]
    raw: dict  # full parsed yaml; later phases read their own sections from here
    agent_name: str = "Agent"  # the main agent's display name, used in all user-facing text
    conventions_path: str | None = None
    personality_path: str | None = None
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    reviewer: ReviewerConfig = field(default_factory=ReviewerConfig)
    cli_update: CliUpdateConfig = field(default_factory=CliUpdateConfig)


def load_config(path: str) -> Config:
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (copy config/config.example.yaml to get started)")
    except yaml.YAMLError as e:
        raise ConfigError(f"config is not valid yaml: {e}")
    if not isinstance(raw, dict):
        raise ConfigError("config must be a yaml mapping")
    orchestrator = raw.get("orchestrator")
    if not isinstance(orchestrator, dict):
        raise ConfigError("config is missing the 'orchestrator' section")
    runner = orchestrator.get("runner", "echo")
    if runner not in ("echo", "claude"):
        raise ConfigError(f"orchestrator.runner must be 'echo' or 'claude', got {runner!r}")
    slack = _slack_config(raw.get("slack") or {}, path)
    github = raw.get("github") or {}
    if not isinstance(github, dict):
        raise ConfigError("github section must be a mapping")
    self_repo = github.get("self_repo", "")
    if not isinstance(self_repo, str):
        raise ConfigError(f"github.self_repo must be a string, got {self_repo!r}")
    if self_repo and self_repo not in (github.get("approved_repos") or []):
        raise ConfigError(f"github.self_repo must be listed in github.approved_repos, got {self_repo!r}")
    _validate_profiles(raw)
    roles = _roles_config(raw, slack)
    _validate_skills(raw)
    agent_name, personality_path = _agent_config(raw, path)
    conventions_path = _conventions_path(raw, path)
    dashboard = _dashboard_config(raw.get("dashboard"), agent_name)
    reviewer = _reviewer_config(raw.get("reviewer"), path)
    cli_update = _cli_update_config(raw.get("cli_update"))
    return Config(
        max_concurrency=_int_at_least(orchestrator, "max_concurrency", 1),
        queue_max=_int_at_least(orchestrator, "queue_max", 1),
        max_retries=_int_at_least(orchestrator, "max_retries", 0),
        progress_min_interval_seconds=_int_at_least(orchestrator, "progress_min_interval_seconds", 0),
        runner=runner,
        slack=slack,
        roles=roles,
        raw=raw,
        agent_name=agent_name,
        conventions_path=conventions_path,
        personality_path=personality_path,
        dashboard=dashboard,
        reviewer=reviewer,
        cli_update=cli_update,
    )


def _cli_update_config(section: object) -> CliUpdateConfig:
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ConfigError("cli_update section must be a mapping")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"cli_update.enabled must be a boolean, got {enabled!r}")
    at_time = section.get("at_time", "02:00")
    if not isinstance(at_time, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at_time) is None:
        raise ConfigError(f"cli_update.at_time must be HH:MM in 24-hour time, got {at_time!r}")
    tzname = section.get("tzname", "America/Los_Angeles")
    if not isinstance(tzname, str) or not tzname:
        raise ConfigError(f"cli_update.tzname must be a non-empty string, got {tzname!r}")
    try:
        ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"cli_update.tzname {tzname!r} is not a known IANA timezone")
    return CliUpdateConfig(enabled=enabled, at_time=at_time, tzname=tzname)


def _reviewer_config(section: object, config_path: str) -> ReviewerConfig:
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ConfigError("reviewer section must be a mapping")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"reviewer.enabled must be a boolean, got {enabled!r}")
    name = section.get("name", "Reviewer")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"reviewer.name must be a non-empty string, got {name!r}")
    personality_file = section.get("personality_file", "")
    if not isinstance(personality_file, str):
        raise ConfigError(f"reviewer.personality_file must be a string, got {personality_file!r}")
    personality_path = None
    if personality_file:
        resolved = (Path(config_path).resolve().parent / personality_file).resolve()
        if not resolved.is_file():
            raise ConfigError(f"reviewer.personality_file not found: {resolved}")
        personality_path = str(resolved)
    review_agent_prs = section.get("review_agent_prs", True)
    if not isinstance(review_agent_prs, bool):
        raise ConfigError(f"reviewer.review_agent_prs must be a boolean, got {review_agent_prs!r}")
    commit_name = section.get("commit_name") or name  # git author defaults to the display name
    if not isinstance(commit_name, str) or not commit_name:
        raise ConfigError(f"reviewer.commit_name must be a non-empty string, got {commit_name!r}")
    commit_email = section.get("commit_email", "")
    if not isinstance(commit_email, str):
        raise ConfigError(f"reviewer.commit_email must be a string, got {commit_email!r}")
    if enabled and not commit_email:
        raise ConfigError("reviewer.commit_email is required when the reviewer is enabled")
    return ReviewerConfig(
        enabled=enabled,
        name=name,
        personality_path=personality_path,
        review_agent_prs=review_agent_prs,
        commit_name=commit_name,
        commit_email=commit_email,
    )


def _dashboard_config(section: object, agent_name: str) -> DashboardConfig:
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ConfigError("dashboard section must be a mapping")
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"dashboard.enabled must be a boolean, got {enabled!r}")
    bind = section.get("bind", "127.0.0.1")
    if bind not in ("127.0.0.1", "0.0.0.0"):
        raise ConfigError(f"dashboard.bind must be '127.0.0.1' or '0.0.0.0', got {bind!r}")
    port = section.get("port", 8787)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError(f"dashboard.port must be an integer between 1 and 65535, got {port!r}")
    domain = section.get("allowed_email_domain", "")
    if not isinstance(domain, str) or "@" in domain:
        raise ConfigError(f"dashboard.allowed_email_domain must be a bare domain like 'example.com', got {domain!r}")
    if enabled and not domain:
        raise ConfigError("dashboard.allowed_email_domain is required when the dashboard is enabled")
    admin_emails = section.get("admin_emails") or []
    if not isinstance(admin_emails, list) or not all(isinstance(item, str) and "@" in item for item in admin_emails):
        raise ConfigError(f"dashboard.admin_emails must be a list of email addresses, got {admin_emails!r}")
    dev_user_email = section.get("dev_user_email")
    if dev_user_email is not None and not isinstance(dev_user_email, str):
        raise ConfigError(f"dashboard.dev_user_email must be a string or null, got {dev_user_email!r}")
    public_url = section.get("public_url", "")
    if not isinstance(public_url, str) or (public_url and not public_url.startswith(("http://", "https://"))):
        raise ConfigError(f"dashboard.public_url must be an http(s) url or empty, got {public_url!r}")
    commit = section.get("auto_commit") or {}
    if not isinstance(commit, dict):
        raise ConfigError("dashboard.auto_commit must be a mapping")
    commit_repo = commit.get("repo", "")
    if not isinstance(commit_repo, str) or (commit_repo and "/" not in commit_repo):
        raise ConfigError(f"dashboard.auto_commit.repo must be 'owner/name', got {commit_repo!r}")
    commit_branch = commit.get("branch", "main")
    if not isinstance(commit_branch, str) or not commit_branch:
        raise ConfigError(f"dashboard.auto_commit.branch must be a non-empty string, got {commit_branch!r}")
    committer_name = commit.get("committer_name") or f"{agent_name} Dashboard"
    if not isinstance(committer_name, str) or not committer_name:
        raise ConfigError(f"dashboard.auto_commit.committer_name must be a non-empty string, got {committer_name!r}")
    committer_email = commit.get("committer_email", "")
    if not isinstance(committer_email, str):
        raise ConfigError(f"dashboard.auto_commit.committer_email must be a string, got {committer_email!r}")
    if commit_repo and not committer_email:
        raise ConfigError("dashboard.auto_commit.committer_email is required when auto_commit.repo is set")
    return DashboardConfig(
        enabled=enabled,
        bind=bind,
        port=port,
        allowed_email_domain=domain.lower(),
        admin_emails=[item.lower() for item in admin_emails],
        dev_user_email=dev_user_email or None,
        public_url=public_url.rstrip("/"),
        commit_repo=commit_repo,
        commit_branch=commit_branch,
        committer_name=committer_name,
        committer_email=committer_email,
    )


def _slack_config(section: dict, config_path: str) -> SlackConfig:
    if not isinstance(section, dict):
        raise ConfigError("slack section must be a mapping")
    legacy = [key for key in ("allowed_users", "admins") if key in section]
    if legacy:
        raise ConfigError(f"slack.{legacy[0]} is no longer supported; migrate Slack users and model-override permissions to the top-level roles section")
    if "bot_name" in section:
        raise ConfigError("slack.bot_name moved to agent.name")
    team_id = section.get("team_id") or ""
    if not isinstance(team_id, str):
        raise ConfigError(f"slack.team_id must be a string, got {team_id!r}")
    ack_reaction = section.get("ack_reaction", True)
    if not isinstance(ack_reaction, bool):
        raise ConfigError(f"slack.ack_reaction must be a boolean, got {ack_reaction!r}")
    debug_channel = section.get("debug_channel", "")
    if not isinstance(debug_channel, str):
        raise ConfigError(f"slack.debug_channel must be a string, got {debug_channel!r}")
    messages_file = section.get("task_started_messages_file", "task_started_messages.yaml")
    if not isinstance(messages_file, str):
        raise ConfigError(f"slack.task_started_messages_file must be a string, got {messages_file!r}")
    messages_path = str((Path(config_path).resolve().parent / messages_file).resolve()) if messages_file else None
    return SlackConfig(
        team_id=team_id,
        allowed_channels=_str_list(section, "allowed_channels"),
        ack_reaction=ack_reaction,
        debug_channel=debug_channel,
        task_started_messages_path=messages_path,
    )


def _str_list(section: dict, key: str) -> list[str]:
    value = section.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"slack.{key} must be a list of strings, got {value!r}")
    return value


def role_for(roles: dict[str, Role], user_id: str) -> Role | None:
    for role in roles.values():
        if user_id in role.members:
            return role
    for role in roles.values():
        if "*" in role.members:
            return role
    return None


def _roles_config(raw: dict, slack: SlackConfig) -> dict[str, Role]:
    section = raw.get("roles") or {}
    if not isinstance(section, dict):
        raise ConfigError("roles section must be a mapping")
    if slack.team_id and not section:
        raise ConfigError("roles must contain at least one role when Slack intake is enabled")
    profiles = raw.get("profiles") or {}
    approved_repos = set((raw.get("github") or {}).get("approved_repos") or [])
    roles: dict[str, Role] = {}
    owners: dict[str, str] = {}
    wildcard_roles = 0
    for name, value in section.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise ConfigError(f"roles.{name} must be a mapping")
        members = _role_str_list(value, "members", name)
        allowed_profiles = _role_str_list(value, "allowed_profiles", name)
        unknown_profiles = set(allowed_profiles) - set(profiles)
        if unknown_profiles:
            raise ConfigError(f"roles.{name}.allowed_profiles contains unknown profiles: {sorted(unknown_profiles)}")
        model_override = value.get("model_override", False)
        if not isinstance(model_override, bool):
            raise ConfigError(f"roles.{name}.model_override must be a boolean, got {model_override!r}")
        max_budget = value.get("max_budget_usd")
        if max_budget is not None and (not isinstance(max_budget, (int, float)) or isinstance(max_budget, bool) or max_budget < 0):
            raise ConfigError(f"roles.{name}.max_budget_usd must be a non-negative number or null, got {max_budget!r}")
        repos_value = value.get("repos")
        repos = None if repos_value is None else _role_str_list(value, "repos", name)
        unknown_repos = set(repos or []) - approved_repos
        if unknown_repos:
            raise ConfigError(f"roles.{name}.repos contains repositories outside github.approved_repos: {sorted(unknown_repos)}")
        for member in members:
            if member == "*":
                wildcard_roles += 1
                continue
            if member in owners:
                raise ConfigError(f"user {member!r} belongs to both roles {owners[member]!r} and {name!r}")
            owners[member] = name
        roles[name] = Role(name=name, members=members, allowed_profiles=allowed_profiles, model_override=model_override, max_budget_usd=float(max_budget) if max_budget is not None else None, repos=repos)
    if wildcard_roles > 1:
        raise ConfigError("at most one role may contain the wildcard member '*'")
    return roles


def _role_str_list(section: dict, key: str, role_name: str) -> list[str]:
    value = section.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"roles.{role_name}.{key} must be a list of strings, got {value!r}")
    return value


def _validate_profiles(raw: dict) -> None:
    profiles = raw.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise ConfigError("profiles section must be a mapping")
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ConfigError(f"profiles.{name} must be a mapping")
        effort = profile.get("effort")
        if effort is not None and effort not in EFFORT_LEVELS:
            raise ConfigError(f"profiles.{name}.effort must be one of {', '.join(EFFORT_LEVELS)}, got {effort!r}")
        thinking = profile.get("thinking")
        if thinking is None:
            continue
        if not isinstance(thinking, dict) or thinking.get("type") not in {"adaptive", "enabled", "disabled"}:
            raise ConfigError(f"profiles.{name}.thinking must be a mapping with type adaptive, enabled, or disabled")
        if thinking["type"] == "enabled":
            budget = thinking.get("budget_tokens")
            if not isinstance(budget, int) or isinstance(budget, bool):
                raise ConfigError(f"profiles.{name}.thinking enabled requires an integer budget_tokens")


def _validate_skills(raw: dict) -> None:
    section = raw.get("skills")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ConfigError("skills section must be a mapping")
    tier = section.get("tier", "opus")
    profile = section.get("profile", "standard")
    models = raw.get("models")
    profiles = raw.get("profiles")
    if not isinstance(tier, str) or (isinstance(models, dict) and tier not in models):
        raise ConfigError(f"skills.tier {tier!r} is not in the model catalog")
    if not isinstance(profile, str) or (isinstance(profiles, dict) and profile not in profiles):
        raise ConfigError(f"skills.profile {profile!r} is not configured")


def _conventions_path(raw: dict, config_path: str) -> str | None:
    if "conventions" not in raw:
        return None
    section = raw["conventions"]
    if not isinstance(section, dict):
        raise ConfigError("conventions section must be a mapping")
    file_value = section.get("file")
    if not isinstance(file_value, str):
        raise ConfigError(f"conventions.file must be a string, got {file_value!r}")
    if not file_value:
        return None
    resolved = (Path(config_path).resolve().parent / file_value).resolve()
    if not resolved.is_file():
        raise ConfigError(f"conventions.file not found: {resolved}")
    return str(resolved)


def _agent_config(raw: dict, config_path: str) -> tuple[str, str | None]:
    if "personality" in raw:
        raise ConfigError("the top-level personality section moved to agent.personality_file")
    section = raw.get("agent")
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ConfigError("agent section must be a mapping")
    name = section.get("name", "Agent")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"agent.name must be a non-empty string, got {name!r}")
    file_value = section.get("personality_file", "")
    if not isinstance(file_value, str):
        raise ConfigError(f"agent.personality_file must be a string, got {file_value!r}")
    if not file_value:
        return name, None
    resolved = (Path(config_path).resolve().parent / file_value).resolve()
    if not resolved.is_file():
        raise ConfigError(f"agent.personality_file not found: {resolved}")
    return name, str(resolved)


def _int_at_least(section: dict, key: str, minimum: int) -> int:
    value = section.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"orchestrator.{key} must be an integer >= {minimum}, got {value!r}")
    return value
