"""credential loading: env vars for local dev, one aws secrets manager json bundle on the host (§8.2).

rotation = update the secret + restart the service; nothing here touches disk. every loaded
credential is registered with the redactor so it cannot survive any output sink (AC-17).
"""

import json
import os
from dataclasses import dataclass

from taskboy import settings
from taskboy.redact import redactor


@dataclass
class Secrets:
    slack_bot_token: str = ""
    slack_app_token: str = ""
    github_app_id: str = ""
    github_installation_id: str = ""
    github_app_private_key: str = ""
    reviewer_github_app_id: str = ""
    reviewer_github_installation_id: str = ""
    reviewer_github_app_private_key: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    sentry_token: str = ""
    claude_oauth_token: str = ""  # populated from the bundle on the host; local dev exports it directly
    dashboard_github_token: str = ""  # contents-only pat for dashboard commits straight to protected main

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_app_id and self.github_installation_id and self.github_app_private_key)

    @property
    def reviewer_github_enabled(self) -> bool:
        return bool(self.reviewer_github_app_id and self.reviewer_github_installation_id and self.reviewer_github_app_private_key)

    @property
    def jira_enabled(self) -> bool:
        return bool(self.jira_email and self.jira_api_token)


def load_secrets() -> Secrets:
    # local dev never touches secrets manager: env vars only, missing values stay empty
    if settings.ENVIRONMENT == "local" or os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("GITHUB_APP_ID") or os.environ.get("REVIEWER_GITHUB_APP_ID"):
        secrets = Secrets(
            slack_bot_token=os.environ.get("SLACK_BOT_TOKEN", ""),
            slack_app_token=os.environ.get("SLACK_APP_TOKEN", ""),
            github_app_id=os.environ.get("GITHUB_APP_ID", ""),
            github_installation_id=os.environ.get("GITHUB_INSTALLATION_ID", ""),
            github_app_private_key=os.environ.get("GITHUB_APP_PRIVATE_KEY", ""),
            reviewer_github_app_id=os.environ.get("REVIEWER_GITHUB_APP_ID", ""),
            reviewer_github_installation_id=os.environ.get("REVIEWER_GITHUB_INSTALLATION_ID", ""),
            reviewer_github_app_private_key=os.environ.get("REVIEWER_GITHUB_APP_PRIVATE_KEY", ""),
            jira_email=os.environ.get("JIRA_EMAIL", ""),
            jira_api_token=os.environ.get("JIRA_API_TOKEN", ""),
            sentry_token=os.environ.get("SENTRY_TOKEN", ""),
            dashboard_github_token=os.environ.get("DASHBOARD_GITHUB_TOKEN", ""),
        )
    else:
        import boto3  # lazy: only needed on the ec2 host

        client = boto3.client("secretsmanager", region_name=settings.REGION)
        blob = json.loads(client.get_secret_value(SecretId=settings.SECRETS_NAME)["SecretString"])
        secrets = Secrets(
            slack_bot_token=blob.get("slack_bot_token", ""),
            slack_app_token=blob.get("slack_app_token", ""),
            github_app_id=blob.get("github_app_id", ""),
            github_installation_id=blob.get("github_installation_id", ""),
            github_app_private_key=blob.get("github_app_private_key", ""),
            reviewer_github_app_id=blob.get("reviewer_github_app_id", ""),
            reviewer_github_installation_id=blob.get("reviewer_github_installation_id", ""),
            reviewer_github_app_private_key=blob.get("reviewer_github_app_private_key", ""),
            jira_email=blob.get("jira_email", ""),
            jira_api_token=blob.get("jira_api_token", ""),
            sentry_token=blob.get("sentry_token", ""),
            claude_oauth_token=blob.get("claude_oauth_token", ""),
            dashboard_github_token=blob.get("dashboard_github_token", ""),
        )
    for value in (secrets.slack_bot_token, secrets.slack_app_token, secrets.github_app_private_key, secrets.reviewer_github_app_private_key, secrets.jira_api_token, secrets.sentry_token, secrets.claude_oauth_token, secrets.dashboard_github_token):
        redactor.register(value)
    return secrets
