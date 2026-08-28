from pathlib import Path
from unittest.mock import MagicMock, patch

from taskboy import setup_checks, setup_wizard
from taskboy.config import load_config

EXAMPLE = Path(__file__).parents[1] / "taskboy" / "templates" / "config.example.yaml"


def _wizard_paths(monkeypatch, tmp_path):
    config_path = tmp_path / "config" / "config.yaml"
    config_path.parent.mkdir()
    example = config_path.parent / "config.example.yaml"
    example.write_text(EXAMPLE.read_text())
    monkeypatch.setattr(setup_wizard, "CONFIG_PATH", config_path)
    monkeypatch.setattr(setup_wizard, "EXAMPLE_PATH", example)
    monkeypatch.setattr(setup_wizard, "ENV_PATH", tmp_path / ".env")
    return config_path


def test_config_round_trip_preserves_comments_and_stays_loadable(tmp_path, monkeypatch):
    config_path = _wizard_paths(monkeypatch, tmp_path)
    data = setup_wizard.load_config_data()
    data["agent"]["name"] = "Scout"
    data["slack"]["team_id"] = "T123"
    setup_wizard.save_config_data(data)
    text = config_path.read_text()
    assert "# max sub-agents running at once (ORC-005)" in text  # comments survive the edit
    assert "name: Scout" in text
    config = load_config(str(config_path))
    assert config.agent_name == "Scout" and config.slack.team_id == "T123"


def test_save_rejects_and_restores_on_invalid_config(tmp_path, monkeypatch):
    import pytest

    from taskboy.config import ConfigError

    _wizard_paths(monkeypatch, tmp_path)
    data = setup_wizard.load_config_data()
    setup_wizard.save_config_data(data)
    before = setup_wizard.CONFIG_PATH.read_text()
    data["orchestrator"]["runner"] = "nonsense"
    with pytest.raises(ConfigError):
        setup_wizard.save_config_data(data)
    assert setup_wizard.CONFIG_PATH.read_text() == before  # bad write rolled back


def test_env_round_trip_handles_multiline_pem_and_quotes(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "ENV_PATH", tmp_path / ".env")
    values = {
        "SLACK_BOT_TOKEN": "xoxb-abc123",
        "GITHUB_APP_PRIVATE_KEY": "-----BEGIN RSA PRIVATE KEY-----\nline'with'quotes\n-----END RSA PRIVATE KEY-----\n",
    }
    setup_wizard.write_env(values)
    assert setup_wizard.read_env() == values
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600


def test_local_mode_creates_config_and_prints_next_steps(tmp_path, monkeypatch, capsys):
    config_path = _wizard_paths(monkeypatch, tmp_path)
    args = MagicMock(check=False, local=True, step=None, no_validate=False)
    assert setup_wizard.run(args) == 0
    assert config_path.exists()
    out = capsys.readouterr().out
    assert "taskboy inject" in out
    load_config(str(config_path))  # the copied example must be immediately runnable


def test_check_mode_fails_with_exit_64_on_bad_config(tmp_path, monkeypatch, capsys):
    config_path = _wizard_paths(monkeypatch, tmp_path)
    config_path.write_text("orchestrator: {runner: nonsense}\n")
    args = MagicMock(check=True, local=False, step=None, no_validate=False)
    assert setup_wizard.run(args) == 64


def test_check_mode_passes_on_example_config_without_credentials(tmp_path, monkeypatch, capsys):
    _wizard_paths(monkeypatch, tmp_path)
    setup_wizard.save_config_data(setup_wizard.load_config_data())
    for key in setup_wizard.SECRET_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    args = MagicMock(check=True, local=False, step=None, no_validate=False)
    assert setup_wizard.run(args) == 0  # nothing configured -> nothing to fail


def test_template_variables_derive_from_config():
    data = {
        "agent": {"name": "Scout"},
        "reviewer": {"name": "Critic"},
        "github": {"approved_repos": ["example-org/svc-a", "example-org/svc-b"], "self_repo": "example-org/taskboy"},
        "jira": {"site": "https://example.atlassian.net", "projects": ["ENG"]},
        "conventions": {"file": "conventions.md"},
    }
    variables = setup_wizard.template_variables(data)
    assert variables["agent_name"] == "Scout"
    assert variables["reviewer_name"] == "Critic"
    assert variables["github_org"] == "example-org"
    assert variables["repo_list"] == "`svc-a`, `svc-b`"
    assert variables["self_repo"] == "example-org/taskboy"
    assert variables["jira_project"] == "ENG"
    assert variables["jira_site"] == "example.atlassian.net"
    assert variables["conventions_file"] == "conventions.md"


def test_check_slack_parses_auth_test(monkeypatch):
    monkeypatch.setattr(setup_checks, "_post_json", lambda url, headers, body=None: (200, {"ok": True, "user": "scout", "team_id": "T1"}))
    ok, detail = setup_checks.check_slack("xoxb-x", "T1")
    assert ok and "scout" in detail
    ok, detail = setup_checks.check_slack("xoxb-x", "T2")
    assert not ok and "T2" in detail
    monkeypatch.setattr(setup_checks, "_post_json", lambda url, headers, body=None: (200, {"ok": False, "error": "invalid_auth"}))
    ok, detail = setup_checks.check_slack("xoxb-x")
    assert not ok and "invalid_auth" in detail


def test_check_github_app_cross_checks_approved_repos(monkeypatch):
    monkeypatch.setattr(setup_checks, "github_app_jwt", lambda app_id, pem: "jwt")
    responses = {
        "https://api.github.com/app": (200, {"slug": "my-agent"}),
        "https://api.github.com/installation/repositories?per_page=100": (200, {"repositories": [{"full_name": "org/a"}]}),
    }
    monkeypatch.setattr(setup_checks, "_get_json", lambda url, headers: responses[url])
    monkeypatch.setattr(setup_checks, "_post_json", lambda url, headers, body=None: (201, {"token": "ghs_x"}))
    ok, detail = setup_checks.check_github_app("1", "2", "pem", ["org/a"])
    assert ok and "my-agent" in detail
    ok, detail = setup_checks.check_github_app("1", "2", "pem", ["org/a", "org/missing"])
    assert not ok and "org/missing" in detail


def test_check_jira_and_sentry(monkeypatch):
    monkeypatch.setattr(setup_checks, "_get_json", lambda url, headers: (200, {"displayName": "Bot", "emailAddress": "bot@example.com", "slug": "example"}))
    ok, detail = setup_checks.check_jira("https://example.atlassian.net", "bot@example.com", "token")
    assert ok and "Bot" in detail
    ok, detail = setup_checks.check_sentry("example", "token")
    assert ok and "example" in detail
    monkeypatch.setattr(setup_checks, "_get_json", lambda url, headers: (401, {}))
    assert setup_checks.check_jira("https://example.atlassian.net", "e", "t")[0] is False
    assert setup_checks.check_sentry("example", "t")[0] is False


def test_skills_step_instantiates_selected_templates(tmp_path, monkeypatch):
    _wizard_paths(monkeypatch, tmp_path)
    templates_root = tmp_path / "templates"
    (templates_root / "skills" / "greet").mkdir(parents=True)
    (templates_root / "skills" / "greet" / "SKILL.md").write_text("---\nname: greet\ndescription: say hi as @{{agent_name}}\n---\nGreet users of {{github_org}}.\n")
    monkeypatch.setattr(setup_wizard, "TEMPLATES_ROOT", templates_root)
    skills_root = tmp_path / "skills"
    monkeypatch.setattr(setup_wizard.settings, "SKILLS_ROOT", str(skills_root))
    data = {"agent": {"name": "Scout"}, "github": {"approved_repos": ["example-org/svc-a"], "self_repo": ""}, "jira": {}}
    with patch("builtins.input", side_effect=["y", "all"]):
        with patch.object(setup_wizard, "ask_yes", return_value=True):
            with patch.object(setup_wizard, "ask", return_value="all"):
                setup_wizard.step_skills(data, {})
    installed = (skills_root / "greet" / "SKILL.md").read_text()
    assert "@Scout" in installed and "example-org" in installed and "{{" not in installed


def test_service_sections_split_into_service_files(tmp_path, monkeypatch):
    import yaml

    config_path = _wizard_paths(monkeypatch, tmp_path)
    data = setup_wizard.load_config_data()  # seeds config.yaml + services/*.yaml and returns the merged view
    data["slack"]["enabled"] = True
    data["slack"]["team_id"] = "T123"
    data["roles"]["admin"]["members"] = ["U1"]
    setup_wizard.save_config_data(data)
    core = yaml.safe_load(config_path.read_text())
    assert "slack" not in core and "github" not in core  # service sections live in their own files
    slack_file = config_path.parent / "services" / "slack.yaml"
    slack = yaml.safe_load(slack_file.read_text())
    assert slack["enabled"] is True and slack["team_id"] == "T123"
    assert "socket-mode intake" in slack_file.read_text()  # template comments survive the round trip
    merged = setup_wizard.load_config_data()
    assert merged["slack"]["team_id"] == "T123"  # later steps still see the merged view
    config = load_config(str(config_path))
    assert config.slack.enabled is True and config.services["github"] is False
