import json
import sys
from types import SimpleNamespace

from agent_harness.redact import redactor
from agent_harness.secrets import Secrets, load_secrets


def test_reviewer_github_secrets_load_from_environment_and_private_key_is_redacted(monkeypatch):
    monkeypatch.setattr("agent_harness.settings.ENVIRONMENT", "local")
    monkeypatch.setenv("REVIEWER_GITHUB_APP_ID", "123")
    monkeypatch.setenv("REVIEWER_GITHUB_INSTALLATION_ID", "456")
    monkeypatch.setenv("REVIEWER_GITHUB_APP_PRIVATE_KEY", "unique-blue-private-key-material")

    secrets = load_secrets()

    assert secrets.reviewer_github_enabled is True
    assert secrets.reviewer_github_app_id == "123"
    assert secrets.reviewer_github_installation_id == "456"
    assert redactor.redact("key=unique-blue-private-key-material") == "key=[redacted]"


def test_reviewer_github_enabled_requires_all_three_values():
    assert Secrets(reviewer_github_app_id="1", reviewer_github_installation_id="2").reviewer_github_enabled is False


def test_reviewer_github_secrets_load_from_bundle(monkeypatch):
    monkeypatch.setattr("agent_harness.settings.ENVIRONMENT", "staging")
    for name in ("SLACK_BOT_TOKEN", "GITHUB_APP_ID", "REVIEWER_GITHUB_APP_ID"):
        monkeypatch.delenv(name, raising=False)
    blob = {
        "reviewer_github_app_id": "blue-app",
        "reviewer_github_installation_id": "blue-installation",
        "reviewer_github_app_private_key": "bundle-blue-private-key",
    }
    client = SimpleNamespace(get_secret_value=lambda **kwargs: {"SecretString": json.dumps(blob)})
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda *args, **kwargs: client))

    secrets = load_secrets()

    assert secrets.reviewer_github_enabled is True
    assert secrets.reviewer_github_app_private_key == "bundle-blue-private-key"
