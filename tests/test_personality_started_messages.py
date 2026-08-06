import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from taskboy.personality import load
from taskboy.started_messages import pick, validate_content


def test_personality_loads_fresh_and_hashes_file_content(tmp_path):
    path = tmp_path / "personality_agent.md"
    path.write_text("dry and exact\n")
    assert load(str(path)) == ("dry and exact", hashlib.sha256(b"dry and exact\n").hexdigest())
    path.write_text("new voice")
    assert load(str(path))[0] == "new voice"
    path.write_text("  \n")
    assert load(str(path)) is None
    assert load(str(tmp_path / "missing")) is None


def test_started_messages_loads_shipped_pools_and_falls_back(tmp_path):
    shipped = Path(__file__).parents[1] / "taskboy" / "templates" / "task_started_messages.yaml"
    with patch("taskboy.started_messages.random.choice", side_effect=lambda values: values[0]):
        assert pick(str(shipped), "agent") == "On it. Starting now."
        assert pick(str(shipped), "reviewer") == "{reviewer_name} is starting a PR review."
    bad = tmp_path / "bad.yaml"
    bad.write_text("agent: nope")
    assert pick(str(bad), "agent") is None
    assert pick(str(shipped), "unknown") is None
    assert pick(str(tmp_path / "missing"), "agent") is None


def test_started_messages_validation_requires_agent_and_reviewer_pools():
    assert validate_content('agent: ["Ready."]\nreviewer: ["Reviewing."]\n') == {"agent": ["Ready."], "reviewer": ["Reviewing."]}
    with pytest.raises(ValueError, match="must be a yaml mapping with 'agent' and 'reviewer' lists"):
        validate_content('agent: ["Ready."]\n')
    with pytest.raises(ValueError, match="for 'reviewer' must be a non-empty list"):
        validate_content('agent: ["Ready."]\nreviewer: nope\n')
