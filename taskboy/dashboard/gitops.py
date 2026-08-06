"""auto-commit dashboard edits back to the repo via the github contents api.

edits apply to the live host files immediately; this pushes the same change to git so
the repo stays the source of truth and the next deploy re-ships what is already live.
dashboard commits go straight to main; red's app tokens are for task sessions, which
are blocked from pushing to protected branches, so the dashboard uses its own contents-only pat.
"""

import base64
import json
import logging

logger = logging.getLogger("taskboy.dashboard")

API_BASE = "https://api.github.com"


class GitOpsError(Exception):
    pass


async def _request(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    """the network seam — patched in unit tests."""
    import aiohttp

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async with session.request(method, url, headers=headers, data=json.dumps(payload) if payload is not None else None) as response:
            body = await response.text()
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}
            return response.status, data


async def commit_file(token: str, repo: str, branch: str, repo_path: str, content: str, message: str, actor: str, committer_name: str = "Dashboard", committer_email: str = "") -> dict:
    """create or update one file on the branch; returns {'commit_sha', 'html_url'}."""
    url = f"{API_BASE}/repos/{repo}/contents/{repo_path}"
    status, existing = await _request("GET", f"{url}?ref={branch}", token)
    if status == 200 and existing.get("content") is not None:
        current = base64.b64decode(existing["content"]).decode()
        if current == content:
            return {"commit_sha": "", "html_url": "", "unchanged": True}
    if status not in (200, 404):
        raise GitOpsError(f"github read of {repo_path} failed with status {status}")
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
        "committer": {"name": committer_name, "email": committer_email},
    }
    if status == 200 and existing.get("sha"):
        payload["sha"] = existing["sha"]
    status, data = await _request("PUT", url, token, payload)
    if status not in (200, 201):
        raise GitOpsError(f"github commit of {repo_path} failed with status {status}: {str(data.get('message') or '')[:200]}")
    commit = data.get("commit") or {}
    logger.info("committed %s to %s@%s (%s, by %s)", repo_path, repo, branch, commit.get("sha", ""), actor)
    return {"commit_sha": commit.get("sha", ""), "html_url": commit.get("html_url", ""), "unchanged": False}
