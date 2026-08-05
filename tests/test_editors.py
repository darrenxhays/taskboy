import pytest

from agent_harness.config import ReviewerConfig
from agent_harness.dashboard.editors import EDITABLE_KINDS, EditorError, contains_secret_submission, target_for, validate
from agent_harness.dashboard.render import redact_value
from tests.conftest import make_config

CONFIG_WITH_TOKEN_LIMITS = """orchestrator: {max_concurrency: 1, queue_max: 5, max_retries: 1, progress_min_interval_seconds: 0, runner: echo}
usage_limits:
  five_hour_tokens: 88000000
  weekly_tokens: 550000000
  fable_weekly_tokens: 220000000
"""


# -- item 4: token *counts* must not read as secrets -----------------------


def test_usage_limit_token_counts_do_not_block_config_editing():
    # the fix: only string values under a secret-named key are secrets; integer *_tokens limits are not
    assert contains_secret_submission("config", CONFIG_WITH_TOKEN_LIMITS) is False


def test_real_secret_string_still_blocks():
    assert contains_secret_submission("config", "dashboard:\n  api_key: sk-ant-abcdefghijklmnop\n") is True


def test_redact_value_keeps_numeric_token_counts_but_masks_string_secrets():
    view = redact_value({"usage_limits": {"five_hour_tokens": 88000000}, "some_token": "ghp_secretsecretsecret"})
    assert view == {"usage_limits": {"five_hour_tokens": 88000000}, "some_token": "••••••••"}


# -- item 5: blue personality is editable ----------------------------------


def test_reviewer_personality_is_editable(tmp_path):
    blue_file = tmp_path / "personality_blue.md"
    blue_file.write_text("Blue is exhaustively polite.")
    config = make_config(reviewer=ReviewerConfig(enabled=True, personality_path=str(blue_file)))

    assert "reviewer_personality" in EDITABLE_KINDS
    target, repo_path, title = target_for(config, "reviewer_personality", None)
    assert target == blue_file and repo_path == "config/personality_blue.md" and title == "Reviewer personality"

    validate("reviewer_personality", None, "Blue stays courteous.", target)  # non-empty passes
    with pytest.raises(ValueError):
        validate("reviewer_personality", None, "   \n  ", target)  # whitespace-only rejected


def test_reviewer_personality_missing_path_is_not_a_target():
    config = make_config(reviewer=ReviewerConfig(enabled=True, personality_path=None))
    with pytest.raises(EditorError):
        target_for(config, "reviewer_personality", None)


# -- conventions doc is editable --------------------------------------------


def test_conventions_is_editable_when_configured(tmp_path):
    conventions_file = tmp_path / "conventions.md"
    conventions_file.write_text("# house rules")
    config = make_config(conventions_path=str(conventions_file))

    assert "conventions" in EDITABLE_KINDS
    target, repo_path, title = target_for(config, "conventions", None)
    assert target == conventions_file and repo_path == "config/conventions.md" and title == "Engineering conventions"

    validate("conventions", None, "# updated rules", target)  # non-empty passes
    with pytest.raises(ValueError):
        validate("conventions", None, "   \n  ", target)  # whitespace-only rejected


def test_conventions_missing_path_is_not_a_target():
    config = make_config()
    with pytest.raises(EditorError):
        target_for(config, "conventions", None)
