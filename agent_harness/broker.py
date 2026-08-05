"""credential broker: short-lived, down-scoped github app installation tokens per task (GIT-014, TOL-007, EC2-009).

sub-agents never hold a long-lived credential. git asks this broker at use-time through a
credential helper over a unix socket, authenticated by a per-task nonce; the broker mints an
installation token scoped to the task's profile (read-only vs write) and target repositories,
caches it, and re-mints when close to expiry — so github's ~1h ttl never breaks a long task.
task A's nonce is useless for task B, and every minted token is registered with the redactor.
"""

import asyncio
import json
import logging
import os
import secrets as pysecrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import jwt

from agent_harness.adapters.github_api import GitHubStatusError
from agent_harness.models import Task
from agent_harness.redact import redactor

logger = logging.getLogger("agent_harness.broker")

GITHUB_API = "https://api.github.com"
REFRESH_MARGIN_SECONDS = 600  # re-mint when less than 10 minutes remain

# github app permissions requested per routing profile: allowlist, hook, and credential all agree (§8.4)
PROFILE_PERMISSIONS = {
    "read_only": {"contents": "read", "metadata": "read", "pull_requests": "read"},
    "standard": {"contents": "write", "metadata": "read", "pull_requests": "write"},
    "deep": {"contents": "write", "metadata": "read", "pull_requests": "write"},
}


@dataclass
class TaskGrant:
    task_id: str
    nonce: str
    repositories: list[str]  # short repo names (github's api takes names, not org/name)
    permissions: dict
    token: str | None = field(default=None, repr=False)
    expires_at: float = 0.0


class CredentialBroker:
    def __init__(self, app_id: str, installation_id: str, private_key: str, socket_path: str, helper_path: str):
        self.app_id = app_id
        self.installation_id = installation_id
        self.private_key = private_key
        self.socket_path = socket_path
        self.helper_path = helper_path
        self.grants: dict[str, TaskGrant] = {}
        self._nonce_index: dict[str, str] = {}
        self._server: asyncio.AbstractServer | None = None
        self._app_slug_cache: str | None = None
        self.accessible_repos: set[str] | None = None  # short names; None = never discovered (disables the 422 fallback)

    # -- task lifecycle --------------------------------------------------------

    def register_task(self, task: Task, approved_repos: list[str], granted_repos: list[str] | None = None) -> dict[str, str]:
        """returns the env vars for the task session; the future token is scoped to profile + target repos.

        granted_repos are repos an operator approved mid-task; they widen the token scope beyond the task's
        original classification so live git ops against a granted repo authenticate instead of 403ing (§8.4)."""
        permissions = PROFILE_PERMISSIONS.get(task.profile or "", PROFILE_PERMISSIONS["read_only"])
        targets = (json.loads(task.classification_json) if task.classification_json else {}).get("target_repos") or []
        repos = [repo for repo in targets if repo in approved_repos]
        for repo in granted_repos or []:
            if repo in approved_repos and repo not in repos:
                repos.append(repo)
        repos = repos or list(approved_repos)
        nonce = pysecrets.token_urlsafe(32)
        redactor.register(nonce)
        self.grants[task.task_id] = TaskGrant(task.task_id, nonce, [repo.split("/", 1)[-1] for repo in repos], permissions)
        self._nonce_index[nonce] = task.task_id
        return {
            "AGENT_HARNESS_BROKER_SOCKET": self.socket_path,
            "AGENT_HARNESS_TASK_NONCE": nonce,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": self.helper_path,
        }

    def release_task(self, task_id: str) -> None:
        grant = self.grants.pop(task_id, None)
        if grant is None:
            return
        self._nonce_index.pop(grant.nonce, None)
        redactor.unregister(grant.nonce)
        redactor.unregister(grant.token)

    async def verify(self, approved_repos: list[str] | None = None) -> None:
        """startup probe: mint a minimal token so app-id/key/installation problems surface at boot, not mid-task.

        also snapshots which of approved_repos the installation can actually see, so a stale
        config.yaml entry gets a clear warning now instead of a raw 422 mid-task (§8.4)."""
        data = await self._mint_token({"permissions": {"metadata": "read"}})
        token = str(data.get("token"))
        redactor.register(token)
        self.accessible_repos = await self._discover_accessible_repos(token)
        if self.accessible_repos is not None:
            for repo in approved_repos or []:
                if repo.split("/", 1)[-1] not in self.accessible_repos:
                    logger.warning(
                        "github.approved_repos entry %s is not accessible to this GitHub App installation — install the app on it or remove it from approved_repos",
                        repo,
                    )

    async def _discover_accessible_repos(self, token: str) -> set[str] | None:
        """paginated GET /installation/repositories; returns None (disabling the fallback) if listing fails."""
        try:
            accessible: set[str] = set()
            page = 1
            while page <= 10:
                result = await self._get_github(f"/installation/repositories?per_page=100&page={page}", token)
                entries = result.get("repositories") or []
                accessible.update(str(entry["name"]) for entry in entries if entry.get("name"))
                if len(entries) < 100:
                    break
                page += 1
            return accessible
        except Exception as e:
            logger.warning("failed to list installation repositories — approved_repos drift check disabled: %s", e)
            return None

    async def read_token(self, repositories: list[str], permissions: dict | None = None) -> tuple[str, float]:
        """fetch-only installation token for the mirror cache (contents:read, metadata:read).

        returns (token, expires_at_epoch), using github's actual expiry when present."""
        payload: dict = {"permissions": permissions if permissions is not None else {"contents": "read", "metadata": "read"}}
        if repositories:
            payload["repositories"] = [repo.split("/", 1)[-1] for repo in repositories]
        data = await self._mint_token(payload)
        token = str(data["token"])
        redactor.register(token)
        expires_at = _parse_expiry(data.get("expires_at")) or time.time() + 55 * 60
        return token, expires_at

    async def app_slug(self) -> str:
        if self._app_slug_cache is None:
            data = await self._get_github("/app", self._app_jwt())
            slug = str(data.get("slug") or "")
            if not slug:
                raise RuntimeError("github app response did not include a slug")
            self._app_slug_cache = slug
        return self._app_slug_cache

    # -- token minting -----------------------------------------------------------

    async def credentials_for_nonce(self, nonce: str) -> dict:
        task_id = self._nonce_index.get(nonce)
        if task_id is None:
            raise PermissionError("unknown task nonce")
        return {"username": "x-access-token", "password": await self.token_for_task(task_id)}

    async def token_for_task(self, task_id: str) -> str:
        grant = self.grants.get(task_id)
        if grant is None:
            raise PermissionError(f"no credential grant for task {task_id}")
        if grant.token is None or grant.expires_at - time.time() < REFRESH_MARGIN_SECONDS:
            await self._mint(grant)
        assert grant.token is not None
        return grant.token

    async def _mint(self, grant: TaskGrant) -> None:
        payload: dict = {"permissions": grant.permissions}
        if grant.repositories:
            payload["repositories"] = grant.repositories
        data = await self._mint_token(payload)
        redactor.unregister(grant.token)
        grant.token = str(data["token"])
        grant.expires_at = _parse_expiry(data.get("expires_at")) or time.time() + 55 * 60
        redactor.register(grant.token)
        logger.info("minted github token for %s (repos=%s, perms=%s)", grant.task_id, grant.repositories, grant.permissions)

    async def _mint_token(self, payload: dict) -> dict:
        """mint an installation token; on a 422 caused by inaccessible repos, retry with only accessible ones."""
        try:
            return await self._post_github(f"/app/installations/{self.installation_id}/access_tokens", self._app_jwt(), payload)
        except GitHubStatusError as e:
            requested = payload.get("repositories")
            if e.status != 422 or not requested or self.accessible_repos is None or "not accessible" not in str(e):
                raise
            usable = [repo for repo in requested if repo in self.accessible_repos]
            if not usable:
                raise RuntimeError(f"the GitHub App is not installed on {', '.join(requested)} — install it on those repositories or remove them from github.approved_repos") from e
            if usable == list(requested):
                raise  # 422 was not about our repo list after all
            logger.warning("token mint 422ed; retrying without inaccessible repos %s", sorted(set(requested) - set(usable)))
            return await self._post_github(f"/app/installations/{self.installation_id}/access_tokens", self._app_jwt(), {**payload, "repositories": usable})

    def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}, self.private_key, algorithm="RS256")

    async def _post_github(self, path: str, bearer: str, payload: dict) -> dict:
        """the http seam — patched in unit tests."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(GITHUB_API + path, json=payload, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json"}) as response:
                if response.status >= 300:
                    body = redactor.redact(await response.text())[:300]
                    raise GitHubStatusError(response.status, f"github token mint failed: {response.status} — {body}")
                return await response.json()

    async def _get_github(self, path: str, bearer: str) -> dict:
        """the app-metadata http seam — patched in unit tests."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(GITHUB_API + path, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json"}) as response:
                if response.status >= 300:
                    body = redactor.redact(await response.text())[:300]
                    raise RuntimeError(f"github api GET {path} failed: {response.status} — {body}")
                return await response.json()

    # -- unix socket server -------------------------------------------------------

    async def start(self) -> None:
        path = Path(self.socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        os.chmod(self.socket_path, 0o666)  # task slot users must be able to connect; the nonce is the auth

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        Path(self.socket_path).unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            if request.get("op") == "git-credentials":
                response = await self.credentials_for_nonce(str(request.get("nonce", "")))
            else:
                response = {"error": "unknown op"}
        except PermissionError as e:
            response = {"error": str(e)}
        except Exception as e:
            logger.exception("broker request failed")
            response = {"error": redactor.redact(str(e))}
        try:
            writer.write((json.dumps(response) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()


def _parse_expiry(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
