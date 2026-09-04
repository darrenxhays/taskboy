from pathlib import Path

import pytest

from taskboy.config import ConfigError, Role, load_config, role_for

VALID = """
orchestrator:
  max_concurrency: 3
  queue_max: 20
  max_retries: 2
  progress_min_interval_seconds: 60
"""


def test_valid_config_loads(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    config = load_config(str(path))
    assert config.max_concurrency == 3
    assert config.queue_max == 20
    assert config.max_retries == 2
    assert config.reviewer.enabled is False
    assert config.reviewer.name == "Reviewer"
    assert config.reviewer.review_agent_prs is True
    assert config.agent_name == "Agent"


def test_example_config_is_valid():
    example = Path(__file__).parents[1] / "taskboy" / "templates" / "config.example.yaml"
    config = load_config(str(example))
    assert config.max_concurrency >= 1
    assert "models" in config.raw
    assert "routing" in config.raw
    for profile in config.raw["profiles"].values():
        tools = profile["allowed_tools"]
        assert "mcp__github__list_pull_requests" in tools
        assert "mcp__github__list_pr_files" in tools
        assert "mcp__slack__thread_replies" in tools
        assert "mcp__slack__user_info" in tools
        assert "mcp__slack__get_file" in tools
        assert "mcp__jira__search_users" in tools
        assert "ToolSearch" in tools  # deferred-tool schema lookup must be allowed in every profile (#94)
        # the base task prompt (prompts.py) teaches every session to call these regardless of profile (#108)
        assert "mcp__harness__report_progress" in tools
        assert "mcp__harness__report_blocked" in tools
        assert "mcp__harness__request_permission" in tools
        assert "mcp__harness__ask_questions" in tools
    assert "mcp__slack__send_dm" not in config.raw["profiles"]["read_only"]["allowed_tools"]
    assert "mcp__slack__send_dm" in config.raw["profiles"]["standard"]["allowed_tools"]
    assert "mcp__slack__send_dm" in config.raw["profiles"]["deep"]["allowed_tools"]
    assert "mcp__github__resolve_pr_thread" not in config.raw["profiles"]["read_only"]["allowed_tools"]
    assert "mcp__github__resolve_pr_thread" in config.raw["profiles"]["standard"]["allowed_tools"]
    assert "mcp__github__resolve_pr_thread" in config.raw["profiles"]["deep"]["allowed_tools"]
    for tool in ("mcp__github__close_pull_request", "mcp__github__delete_branch", "mcp__github__create_release", "mcp__github__update_pull_request"):
        assert tool not in config.raw["profiles"]["read_only"]["allowed_tools"]
        assert tool in config.raw["profiles"]["standard"]["allowed_tools"]
        assert tool in config.raw["profiles"]["deep"]["allowed_tools"]


def test_missing_file_fails_clearly():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/config.yaml")


def test_missing_orchestrator_section_fails(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("slack: {}\n")
    with pytest.raises(ConfigError, match="orchestrator"):
        load_config(str(path))


def test_wrong_types_fail(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("orchestrator: {max_concurrency: three, queue_max: 20, max_retries: 2, progress_min_interval_seconds: 60}\n")
    with pytest.raises(ConfigError, match="max_concurrency"):
        load_config(str(path))


def test_zero_concurrency_fails(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("orchestrator: {max_concurrency: 0, queue_max: 20, max_retries: 2, progress_min_interval_seconds: 60}\n")
    with pytest.raises(ConfigError, match="max_concurrency"):
        load_config(str(path))


def test_invalid_yaml_fails(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("orchestrator: [unclosed\n")
    with pytest.raises(ConfigError, match="yaml"):
        load_config(str(path))


def test_ack_reaction_defaults_and_rejects_non_bool(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nslack: {}\n")
    assert load_config(str(path)).slack.ack_reaction is True
    path.write_text(VALID + "\nslack: {ack_reaction: yes-please}\n")
    with pytest.raises(ConfigError, match="ack_reaction"):
        load_config(str(path))


@pytest.mark.parametrize(
    "profile,match",
    [
        ({"effort": "extreme"}, "effort"),
        ({"thinking": "adaptive"}, "thinking"),
        ({"thinking": {"type": "mystery"}}, "thinking"),
        ({"thinking": {"type": "enabled"}}, "budget_tokens"),
    ],
)
def test_profile_reasoning_validation(tmp_path, profile, match):
    import yaml

    path = tmp_path / "config.yaml"
    raw = yaml.safe_load(VALID)
    raw["profiles"] = {"read_only": profile}
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match=match):
        load_config(str(path))


def test_roles_hard_cutover_and_validation(tmp_path):
    import yaml

    path = tmp_path / "config.yaml"
    raw = yaml.safe_load(VALID)
    raw["slack"] = {"team_id": "T1", "allowed_users": ["U1"]}
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="migrate.*roles"):
        load_config(str(path))

    raw["slack"] = {"team_id": "T1"}
    raw["profiles"] = {"read_only": {}}
    raw["github"] = {"approved_repos": ["org/a"]}
    raw["roles"] = {
        "one": {"members": ["U1"], "allowed_profiles": ["read_only"]},
        "two": {"members": ["U1"], "allowed_profiles": ["read_only"]},
    }
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="both roles"):
        load_config(str(path))

    raw["roles"]["two"]["members"] = ["*"]
    raw["roles"]["one"]["repos"] = ["org/not-approved"]
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="outside github.approved_repos"):
        load_config(str(path))


def test_github_self_repo_validation(tmp_path):
    import yaml

    path = tmp_path / "config.yaml"
    raw = yaml.safe_load(VALID)

    for self_repo in (None, "", "org/a"):
        raw["github"] = {"approved_repos": ["org/a"]}
        if self_repo is not None:
            raw["github"]["self_repo"] = self_repo
        path.write_text(yaml.safe_dump(raw))
        assert load_config(str(path)).raw["github"].get("self_repo", "") == (self_repo or "")

    raw["github"]["self_repo"] = "org/b"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="github.self_repo must be listed in github.approved_repos"):
        load_config(str(path))

    raw["github"]["self_repo"] = 123
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="github.self_repo must be a string"):
        load_config(str(path))


def test_role_for_prefers_explicit_membership_over_wildcard():
    roles = {
        "wild": Role("wild", ["*"], ["read_only"], False, 2.0, None),
        "admin": Role("admin", ["U1"], ["read_only"], True, None, None),
    }
    assert role_for(roles, "U1").name == "admin"
    assert role_for(roles, "U2").name == "wild"


@pytest.mark.parametrize("skills,match", [({"tier": "mystery", "profile": "standard"}, "skills.tier"), ({"tier": "opus", "profile": "mystery"}, "skills.profile"), ([], "mapping")])
def test_skills_section_validation(tmp_path, skills, match):
    import yaml

    example = Path(__file__).parents[1] / "taskboy" / "templates" / "config.example.yaml"
    raw = yaml.safe_load(example.read_text())
    raw["skills"] = skills
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match=match):
        load_config(str(path))


def test_absent_skills_section_uses_defaults(tmp_path):
    import yaml

    example = Path(__file__).parents[1] / "taskboy" / "templates" / "config.example.yaml"
    raw = yaml.safe_load(example.read_text())
    raw.pop("skills")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert load_config(str(path)).raw.get("skills") is None


def test_conventions_file_resolves_relative_to_config(tmp_path):
    conventions = tmp_path / "conventions" / "x.md"
    conventions.parent.mkdir()
    conventions.write_text("house style")
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nconventions:\n  file: conventions/x.md\n")
    assert load_config(str(path)).conventions_path == str(conventions.resolve())


@pytest.mark.parametrize("section", ["", '\nconventions:\n  file: ""\n'])
def test_conventions_absent_or_empty_disables_feature(tmp_path, section):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + section)
    assert load_config(str(path)).conventions_path is None


@pytest.mark.parametrize(
    "section",
    [
        "conventions: []\n",
        "conventions: {file: 123}\n",
        "conventions: {file: missing.md}\n",
    ],
)
def test_invalid_conventions_config_fails(tmp_path, section):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\n" + section)
    with pytest.raises(ConfigError, match="conventions"):
        load_config(str(path))


def test_agent_name_personality_and_started_messages_paths(tmp_path):
    personality = tmp_path / "voice.md"
    personality.write_text("dry")
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nagent: {name: Scout, personality_file: voice.md}\nslack: {debug_channel: CDEBUG, task_started_messages_file: starts.yaml}\n")
    config = load_config(str(path))
    assert config.agent_name == "Scout"
    assert config.personality_path == str(personality.resolve())
    assert config.slack.debug_channel == "CDEBUG"
    assert config.slack.task_started_messages_path == str((tmp_path / "starts.yaml").resolve())
    path.write_text(VALID + "\nagent: {personality_file: missing.md}\n")
    with pytest.raises(ConfigError, match="agent.personality_file not found"):
        load_config(str(path))


def test_help_file_resolves_relative_to_config(tmp_path):
    help_file = tmp_path / "help.md"
    help_file.write_text("Here's how to work with the agent.")
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nhelp: {file: help.md}\n")
    assert load_config(str(path)).help_path == str(help_file.resolve())


@pytest.mark.parametrize("section", ["", '\nhelp:\n  file: ""\n'])
def test_help_absent_or_empty_disables_curated_file(tmp_path, section):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + section)
    assert load_config(str(path)).help_path is None


@pytest.mark.parametrize(
    "section",
    [
        "help: []\n",
        "help: {file: 123}\n",
        "help: {file: missing.md}\n",
    ],
)
def test_invalid_help_config_fails(tmp_path, section):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\n" + section)
    with pytest.raises(ConfigError, match="help"):
        load_config(str(path))


def test_legacy_personality_and_bot_name_keys_fail_with_pointers(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\npersonality: {file: voice.md}\n")
    with pytest.raises(ConfigError, match="moved to agent.personality_file"):
        load_config(str(path))
    path.write_text(VALID + "\nslack: {bot_name: Red}\n")
    with pytest.raises(ConfigError, match="moved to agent.name"):
        load_config(str(path))


def test_reviewer_config_resolves_personality_and_values(tmp_path):
    personality = tmp_path / "reviewer.md"
    personality.write_text("courteous and exact")
    path = tmp_path / "config.yaml"
    path.write_text(
        VALID
        + """
reviewer:
  enabled: true
  name: Azure
  personality_file: reviewer.md
  slack_icon_url: https://example.test/ignored.png
  slack_icon_emoji: ":ignored:"
  review_agent_prs: false
  commit_name: Azure Reviewer
  commit_email: azure@example.test
"""
    )
    reviewer = load_config(str(path)).reviewer
    assert reviewer.enabled is True
    assert reviewer.name == "Azure"
    assert reviewer.personality_path == str(personality.resolve())
    assert not hasattr(reviewer, "slack_icon_url")
    assert not hasattr(reviewer, "slack_icon_emoji")
    assert reviewer.review_agent_prs is False
    assert (reviewer.commit_name, reviewer.commit_email) == ("Azure Reviewer", "azure@example.test")


def test_reviewer_commit_name_defaults_to_display_name(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nreviewer: {enabled: true, name: Azure, commit_email: azure@example.test}\n")
    reviewer = load_config(str(path)).reviewer
    assert reviewer.commit_name == "Azure"


@pytest.mark.parametrize(
    "section,match",
    [
        ("[]", "reviewer section must be a mapping"),
        ("{enabled: yes-please}", "reviewer.enabled must be a boolean"),
        ("{name: 12}", "reviewer.name must be a non-empty string"),
        ("{personality_file: 12}", "reviewer.personality_file must be a string"),
        ("{personality_file: missing.md}", "reviewer.personality_file not found"),
        ("{review_agent_prs: sometimes}", "reviewer.review_agent_prs must be a boolean"),
        ("{commit_email: 12}", "reviewer.commit_email must be a string"),
        ("{enabled: true}", "reviewer.commit_email is required when the reviewer is enabled"),
    ],
)
def test_invalid_reviewer_config_fails_clearly(tmp_path, section, match):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + f"\nreviewer: {section}\n")
    with pytest.raises(ConfigError, match=match):
        load_config(str(path))


def test_unknown_top_level_sections_are_ignored(tmp_path):
    # warmup is no longer a config section (it is a seeded scheduler default); leaving one in place must not fail load
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nwarmup: {enabled: true}\n")
    assert load_config(str(path)).max_concurrency >= 1


def test_dashboard_public_url_normalizes_and_validates(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "dashboard:\n  public_url: https://agent.example.com/\n")
    assert load_config(str(path)).dashboard.public_url == "https://agent.example.com"
    path.write_text(VALID + "dashboard: {}\n")
    assert load_config(str(path)).dashboard.public_url == ""
    path.write_text(VALID + "dashboard:\n  public_url: agent.example.com\n")
    with pytest.raises(ConfigError, match="public_url"):
        load_config(str(path))


def test_cli_update_defaults_off(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    config = load_config(str(path))
    assert config.cli_update.enabled is False
    assert config.cli_update.at_time == "02:00"
    assert config.cli_update.tzname == "America/Los_Angeles"


def test_cli_update_can_be_enabled_with_custom_window(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "cli_update:\n  enabled: true\n  at_time: '03:30'\n  tzname: America/New_York\n")
    config = load_config(str(path))
    assert config.cli_update.enabled is True
    assert config.cli_update.at_time == "03:30"
    assert config.cli_update.tzname == "America/New_York"


def test_cli_update_rejects_bad_at_time(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "cli_update:\n  enabled: true\n  at_time: '25:99'\n")
    with pytest.raises(ConfigError, match="at_time"):
        load_config(str(path))


def test_cli_update_rejects_unknown_timezone(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "cli_update:\n  enabled: true\n  tzname: Nowhere/Fake\n")
    with pytest.raises(ConfigError, match="tzname"):
        load_config(str(path))


def test_cli_update_rejects_non_boolean_enabled(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "cli_update:\n  enabled: yes-please\n")
    with pytest.raises(ConfigError, match="cli_update.enabled"):
        load_config(str(path))


def test_dashboard_expected_alb_arn_configurable_and_validated(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "dashboard:\n  expected_alb_arn: arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/red-dashboard/abc123\n")
    config = load_config(str(path))
    assert config.dashboard.expected_alb_arn == "arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/red-dashboard/abc123"
    path.write_text(VALID + "dashboard: {}\n")
    assert load_config(str(path)).dashboard.expected_alb_arn == ""
    path.write_text(VALID + "dashboard:\n  expected_alb_arn: 12\n")
    with pytest.raises(ConfigError, match="dashboard.expected_alb_arn must be a string"):
        load_config(str(path))


def test_shipped_config_template_uses_model_aliases():
    # the sdk resolves bare aliases to the newest release of each class; a pinned id silently freezes a tier
    example = Path(__file__).parents[1] / "taskboy" / "templates" / "config.example.yaml"
    config = load_config(str(example))

    for alias, model in config.raw["models"].items():
        assert model["id"] == alias


def test_dashboard_auto_commit_committer_identity_is_configurable(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "dashboard: {}\n")
    default = load_config(str(path)).dashboard
    assert default.committer_name == "Agent Dashboard"  # derived from agent.name
    assert default.committer_email == ""

    path.write_text(VALID + "agent: {name: Scout}\ndashboard: {}\n")
    named = load_config(str(path)).dashboard
    assert named.committer_name == "Scout Dashboard"

    path.write_text(VALID + "dashboard:\n  auto_commit:\n    committer_name: Custom Committer\n    committer_email: committer@example.test\n")
    custom = load_config(str(path)).dashboard
    assert custom.committer_name == "Custom Committer"
    assert custom.committer_email == "committer@example.test"


@pytest.mark.parametrize(
    "section,match",
    [
        ("{committer_name: 12}", "committer_name must be a non-empty string"),
        ("{committer_email: 12}", "committer_email must be a string"),
        ("{repo: org/x}", "committer_email is required when auto_commit.repo is set"),
    ],
)
def test_invalid_dashboard_auto_commit_committer_fields_fail_clearly(tmp_path, section, match):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + f"dashboard:\n  auto_commit: {section}\n")
    with pytest.raises(ConfigError, match=match):
        load_config(str(path))


def test_dashboard_email_domain_required_when_enabled(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "dashboard: {enabled: true}\n")
    with pytest.raises(ConfigError, match="allowed_email_domain is required when the dashboard is enabled"):
        load_config(str(path))
    path.write_text(VALID + "dashboard: {enabled: true, allowed_email_domain: example.com}\n")
    assert load_config(str(path)).dashboard.allowed_email_domain == "example.com"


# -- per-service config files (services/<name>.yaml) -------------------------


def _write_services(tmp_path, **files):
    services = tmp_path / "services"
    services.mkdir(exist_ok=True)
    for name, content in files.items():
        (services / f"{name}.yaml").write_text(content)


def test_service_files_merge_and_enable(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nroles:\n  admin: {members: [U1], allowed_profiles: []}\n")
    _write_services(
        tmp_path,
        slack="enabled: true\nteam_id: T1\nallowed_channels: [C1]\n",
        jira="enabled: false\nsite: https://org.atlassian.net\n",
    )
    config = load_config(str(path))
    assert config.services["slack"] is True
    assert config.slack.enabled is True and config.slack.team_id == "T1"
    assert config.services["jira"] is False  # fully configured but explicitly off
    assert config.raw["jira"]["site"] == "https://org.atlassian.net"  # settings still visible when disabled
    assert config.service_enabled("github") is True  # no file and no inline section: legacy default, secrets still gate it at runtime
    assert config.enabled_integrations() == ["github"]


def test_service_file_conflicts_with_inline_section(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nsentry: {organization: org}\n")
    _write_services(tmp_path, sentry="enabled: true\norganization: org\n")
    with pytest.raises(ConfigError, match="both config.yaml and services/sentry.yaml"):
        load_config(str(path))


def test_service_file_requires_explicit_enabled(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    _write_services(tmp_path, sentry="organization: org\n")
    with pytest.raises(ConfigError, match="services/sentry.yaml must set enabled"):
        load_config(str(path))


def test_unknown_service_file_fails(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    _write_services(tmp_path, mystery="enabled: true\n")
    with pytest.raises(ConfigError, match="services/mystery.yaml is not a known service"):
        load_config(str(path))


def test_enabled_service_requires_its_key(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID)
    _write_services(tmp_path, jira="enabled: true\n")
    with pytest.raises(ConfigError, match="jira.site is required when jira is enabled"):
        load_config(str(path))


def test_inline_sections_keep_legacy_enable_inference(tmp_path):
    # configs from before the services/ split: presence of the key signals "on"; github rides on secrets alone
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nslack: {team_id: T1}\nroles:\n  admin: {members: [U1], allowed_profiles: []}\njira: {site: https://org.atlassian.net}\n")
    config = load_config(str(path))
    assert config.services == {"slack": True, "github": True, "jira": True, "confluence": False, "sentry": False, "aws": False}


def test_inline_enabled_false_turns_off_a_configured_service(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nslack: {enabled: false, team_id: T1}\ngithub: {enabled: false}\n")
    config = load_config(str(path))
    assert config.slack.enabled is False
    assert config.service_enabled("github") is False
    assert config.enabled_integrations() == []


def test_non_boolean_enabled_fails(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(VALID + "\nsentry: {enabled: definitely}\n")
    with pytest.raises(ConfigError, match="sentry.enabled must be a boolean"):
        load_config(str(path))
