"""live credential validators for the setup wizard and `agent-harness setup --check`.

each check is a plain synchronous function returning (ok, detail). they use urllib so the
wizard works before any optional dependency is installed, and never log or return the
credential itself — only identifiers safe to echo.
"""

import base64
import json
import time
import urllib.error
import urllib.request

USER_AGENT = "agent-harness-setup"


def _get_json(url: str, headers: dict[str, str]) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except (ValueError, OSError):
            return e.code, {}


def _post_json(url: str, headers: dict[str, str], body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except (ValueError, OSError):
            return e.code, {}


def _basic_auth(email: str, api_token: str) -> str:
    return "Basic " + base64.b64encode(f"{email}:{api_token}".encode()).decode()


def check_claude(token: str) -> tuple[bool, str]:
    """one tiny haiku call through the sdk — proves headless auth works before anything else."""
    if not token:
        return False, "no token provided"
    import asyncio
    import os
    import tempfile

    async def probe() -> str:
        from claude_agent_sdk import ClaudeAgentOptions, query

        options = ClaudeAgentOptions(model="haiku", max_turns=3, allowed_tools=[], setting_sources=[], cwd=tempfile.mkdtemp())
        result = ""
        async for message in query(prompt="Reply with exactly: GO", options=options):
            result = str(getattr(message, "result", "") or result)
        return result

    previous = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
    try:
        result = asyncio.run(probe())
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        if previous is None:
            os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        else:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = previous
    if "GO" in result:
        return True, "claude answered"
    return False, f"unexpected reply: {result[:80]!r}"


def check_slack(bot_token: str, team_id: str = "") -> tuple[bool, str]:
    status, body = _post_json("https://slack.com/api/auth.test", {"Authorization": f"Bearer {bot_token}"})
    if status != 200 or not body.get("ok"):
        return False, f"auth.test failed: {body.get('error', status)}"
    if team_id and body.get("team_id") != team_id:
        return False, f"token belongs to team {body.get('team_id')}, not {team_id}"
    return True, f"authed as @{body.get('user')} in team {body.get('team_id')}"


def check_slack_channel(bot_token: str, channel_id: str) -> tuple[bool, str]:
    status, body = _get_json(f"https://slack.com/api/conversations.info?channel={channel_id}", {"Authorization": f"Bearer {bot_token}"})
    if status != 200 or not body.get("ok"):
        return False, f"{channel_id}: {body.get('error', status)}"
    return True, f"{channel_id} = #{(body.get('channel') or {}).get('name', '?')}"


def check_slack_app_token(app_token: str) -> tuple[bool, str]:
    if not app_token.startswith("xapp-"):
        return False, "app-level token must start with xapp-"
    return True, "app token format ok"


def github_app_jwt(app_id: str, private_key_pem: str) -> str:
    import jwt

    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id}, private_key_pem, algorithm="RS256")


def check_github_app(app_id: str, installation_id: str, private_key_pem: str, approved_repos: list[str] | None = None) -> tuple[bool, str]:
    try:
        token_jwt = github_app_jwt(app_id, private_key_pem)
    except Exception as e:
        return False, f"could not sign app jwt: {e}"
    status, app = _get_json("https://api.github.com/app", {"Authorization": f"Bearer {token_jwt}", "Accept": "application/vnd.github+json"})
    if status != 200:
        return False, f"GET /app failed ({status}) — wrong app id or private key"
    status, minted = _post_json(f"https://api.github.com/app/installations/{installation_id}/access_tokens", {"Authorization": f"Bearer {token_jwt}", "Accept": "application/vnd.github+json"})
    if status != 201:
        return False, f"token mint failed ({status}) — wrong installation id?"
    installation_token = str(minted.get("token", ""))
    status, repos = _get_json("https://api.github.com/installation/repositories?per_page=100", {"Authorization": f"Bearer {installation_token}", "Accept": "application/vnd.github+json"})
    if status != 200:
        return False, f"listing installation repositories failed ({status})"
    accessible = {str(repo.get("full_name", "")) for repo in repos.get("repositories") or []}
    missing = [repo for repo in (approved_repos or []) if repo not in accessible]
    if missing:
        return False, f"app '{app.get('slug')}' cannot access: {', '.join(missing)} — install it on those repositories"
    return True, f"app '{app.get('slug')}' ok, {len(accessible)} repositories accessible"


def check_github_pat_repo(token: str, repo: str) -> tuple[bool, str]:
    status, body = _get_json(f"https://api.github.com/repos/{repo}", {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    if status != 200:
        return False, f"GET /repos/{repo} failed ({status})"
    return True, f"{repo} reachable"


def check_jira(site: str, email: str, api_token: str) -> tuple[bool, str]:
    status, body = _get_json(f"{site.rstrip('/')}/rest/api/3/myself", {"Authorization": _basic_auth(email, api_token)})
    if status != 200:
        return False, f"/myself failed ({status})"
    return True, f"authed as {body.get('displayName')} ({body.get('emailAddress')})"


def check_jira_project(site: str, email: str, api_token: str, key: str) -> tuple[bool, str]:
    status, body = _get_json(f"{site.rstrip('/')}/rest/api/3/project/{key}", {"Authorization": _basic_auth(email, api_token)})
    if status != 200:
        return False, f"project {key}: {status}"
    return True, f"project {key} = {body.get('name')}"


def check_confluence(site: str, email: str, api_token: str) -> tuple[bool, str]:
    status, body = _get_json(f"{site.rstrip('/')}/wiki/rest/api/space?limit=1", {"Authorization": _basic_auth(email, api_token)})
    if status != 200:
        return False, f"space list failed ({status})"
    return True, "confluence reachable"


def check_sentry(organization: str, token: str) -> tuple[bool, str]:
    status, body = _get_json(f"https://sentry.io/api/0/organizations/{organization}/", {"Authorization": f"Bearer {token}"})
    if status != 200:
        return False, f"org fetch failed ({status})"
    return True, f"org '{body.get('slug')}' reachable"


def check_aws_role(role_arn: str, external_id: str) -> tuple[bool, str]:
    try:
        import boto3

        sts = boto3.client("sts")
        sts.assume_role(RoleArn=role_arn, RoleSessionName="agent-harness-setup-check", ExternalId=external_id, DurationSeconds=900)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, f"assumed {role_arn.rsplit('/', 1)[-1]}"


def check_aws_secret(secret_name: str, region: str) -> tuple[bool, str]:
    try:
        import boto3

        client = boto3.client("secretsmanager", region_name=region)
        blob = json.loads(client.get_secret_value(SecretId=secret_name)["SecretString"])
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, f"{secret_name}: {len(blob)} keys"
