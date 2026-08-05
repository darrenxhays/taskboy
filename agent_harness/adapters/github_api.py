"""github pull-request operations as permission-aware in-process mcp tools (TOL-001/004/005/006/007).

credentials never reach the model or the task env: every call fetches a fresh broker token.
writes are idempotent (check the artifacts table, then github, before creating — ORC-012),
recorded as artifacts, and bounded before entering model context. git itself (clone/commit/
push) stays in Bash via the credential helper; these tools cover the api surface.
"""

import json
import logging
import re

from agent_harness.adapters._util import _error, _text, wrap
from agent_harness.models import Task
from agent_harness.redact import redactor
from agent_harness.store import Store

logger = logging.getLogger("agent_harness.github")

GITHUB_API = "https://api.github.com"
ALLOWED_REVIEW_EVENTS = {"COMMENT", "REQUEST_CHANGES"}  # approving is a human act in v1 (GIT-011 spirit)
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
BRANCH_ALREADY_GONE = {404, 422}  # github's "ref does not exist" statuses for a missing branch


class GitHubStatusError(RuntimeError):
    """carries the http status (and optional Retry-After) so callers can special-case specific failures
    (e.g. a missing ref, or a secondary rate limit) without reparsing the message string."""

    def __init__(self, status: int, message: str, retry_after: str | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class GitHubAdapter:
    def __init__(self, broker, store: Store, task: Task, approved_repos: list[str], on_milestone=None, bot_logins: list[str] | None = None, bot_name: str = "Agent", other_bot_name: str = "Reviewer"):
        self.broker = broker
        self.store = store
        self.task = task
        self.approved_repos = approved_repos
        self.on_milestone = on_milestone  # async (message) -> None; artifact auto-milestones (SLK-006)
        self.bot_logins = bot_logins or []
        self.bot_name = bot_name
        self.other_bot_name = other_bot_name

    # -- tools -------------------------------------------------------------------

    async def create_pull_request(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        head = str(args.get("head", "")).strip()
        base = str(args.get("base", "")).strip() or "main"
        if not head:
            return _error("head branch is required")

        # idempotency layer 1: we already made a pr for this head (ORC-012)
        for artifact in self.store.artifacts_for(self.task.task_id):
            if artifact["kind"] == "pull_request" and artifact["external_id"].startswith(f"{repo}#"):
                return _text(f"a pull request already exists for this task: {artifact['url']} — update it instead of creating another")
        # idempotency layer 2: check github for an open pr with this head (crash-after-create window)
        owner = repo.split("/", 1)[0]
        existing = await self._request("GET", f"/repos/{repo}/pulls?head={owner}:{head}&state=open")
        if existing:
            pr = existing[0]
            self._record_pr(repo, pr)
            return _text(f"an open pull request already exists for branch {head}: {pr['html_url']} — update it instead of creating another")

        body = str(args.get("body", "")).rstrip() + f"\n\n---\nRequested via Slack — agent-harness task `{self.task.task_id}`."
        pr = await self._request("POST", f"/repos/{repo}/pulls", {"title": str(args.get("title", "")), "head": head, "base": base, "body": body})
        self._record_pr(repo, pr)
        self.store.add_artifact(self.task.task_id, "branch", f"{repo}:{head}")
        if self.on_milestone:
            await self.on_milestone(f"Opened pull request {pr['html_url']}")
        return _text(f"created pull request #{pr['number']}: {pr['html_url']}")

    async def get_pull_request(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        pr = await self._request("GET", f"/repos/{repo}/pulls/{int(args['number'])}")
        trimmed = {key: pr.get(key) for key in ("number", "title", "state", "html_url", "user", "body", "head", "base", "mergeable_state", "changed_files", "additions", "deletions", "draft", "created_at", "updated_at")}
        trimmed["user"] = (pr.get("user") or {}).get("login")
        trimmed["body"] = str(pr.get("body") or "")[:2000]
        trimmed["head"] = (pr.get("head") or {}).get("ref")
        trimmed["head_sha"] = (pr.get("head") or {}).get("sha")
        trimmed["base"] = (pr.get("base") or {}).get("ref")
        trimmed["requested_reviewers"] = [(reviewer or {}).get("login") for reviewer in pr.get("requested_reviewers") or []]
        return _text(json.dumps(trimmed))

    async def list_pull_requests(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        state = str(args.get("state") or "open").lower()
        if state not in {"open", "closed", "all"}:
            return _error("state must be open, closed, or all")
        pulls = await self._request("GET", f"/repos/{repo}/pulls?state={state}&per_page=50&sort=created&direction=desc")
        lines = []
        for pr in pulls:
            status = str(pr.get("state") or "") + (", draft" if pr.get("draft") else "")
            lines.append(f"#{pr.get('number')} [{status}] {(pr.get('user') or {}).get('login')}: {pr.get('title')} (head {str((pr.get('head') or {}).get('sha') or '')[:7]}, updated {pr.get('updated_at')})")
        return _text("\n".join(lines) or "no pull requests")

    async def list_pr_files(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        number = int(args["number"])
        page = max(int(args.get("page") or 1), 1)
        files = await self._request("GET", f"/repos/{repo}/pulls/{number}/files?per_page=10&page={page}")
        headers = []
        patches = []
        for item in files:
            header = f"--- {item.get('filename')} ({item.get('status')}, +{item.get('additions', 0)}/-{item.get('deletions', 0)})"
            headers.append(header)
            patch = str(item.get("patch") or "")
            if len(patch) > 1500:
                marker = "\n…(patch truncated — fetch the full diff with git)"
                patch = patch[: 1500 - len(marker)] + marker
            if patch:
                patches.append(f"patch for {item.get('filename')}:\n{patch}")
        sections = headers + (["", "patches:", *patches] if patches else [])
        return _text("\n".join(sections) or "no files")

    async def list_pr_comments(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        number = int(args["number"])
        issue_comments = await self._request("GET", f"/repos/{repo}/issues/{number}/comments")
        review_comments = await self._request("GET", f"/repos/{repo}/pulls/{number}/comments")
        resolution_by_comment: dict[int, bool] = {}
        try:
            for thread in await self._review_threads(repo, number):
                for comment in (thread.get("comments") or {}).get("nodes") or []:
                    comment_id = comment.get("databaseId")
                    if comment_id is not None:
                        resolution_by_comment[int(comment_id)] = bool(thread.get("isResolved"))
        except Exception:
            logger.debug("could not annotate review thread status for %s#%s", repo, number, exc_info=True)
        lines = []
        for kind, comments in (("issue", issue_comments), ("review", review_comments)):
            for comment in comments:
                status = ""
                if kind == "review" and comment.get("id") in resolution_by_comment:
                    status = " (resolved)" if resolution_by_comment[comment["id"]] else " (unresolved)"
                lines.append(f"[{comment.get('id')}] [{comment.get('created_at')}] {(comment.get('user') or {}).get('login')} ({kind}): {str(comment.get('body', ''))[:400]}{status}")
        return _text("\n".join(lines) or "no comments")

    async def comment_on_pull_request(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        number = int(args["number"])
        comment = await self._request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": str(args.get("body", ""))})
        self.store.add_artifact(self.task.task_id, "pr_comment", f"{repo}#{number}/comment/{comment['id']}", comment.get("html_url"))
        return _text(f"commented: {comment.get('html_url')}")

    async def create_pr_review(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        event = str(args.get("event", "COMMENT")).upper()
        if event not in ALLOWED_REVIEW_EVENTS:
            return _error(f"review event {event!r} is not permitted; use COMMENT or REQUEST_CHANGES")
        number = int(args["number"])
        payload: dict = {"body": str(args.get("body", "")), "event": event}
        comments_json = str(args.get("comments_json") or "").strip()
        if comments_json:
            try:
                comments = json.loads(comments_json)
            except json.JSONDecodeError:
                return _error("comments_json must be valid json")
            if not isinstance(comments, list):
                return _error("comments_json must be a list of path, line, and body objects")
            normalized = []
            for comment in comments:
                if not isinstance(comment, dict) or not isinstance(comment.get("path"), str) or not isinstance(comment.get("line"), int) or not isinstance(comment.get("body"), str):
                    return _error("comments_json must be a list of path, line, and body objects")
                normalized.append({"path": comment["path"], "line": comment["line"], "body": comment["body"], "side": str(comment.get("side") or "RIGHT")})
            payload["comments"] = normalized
        review = await self._request("POST", f"/repos/{repo}/pulls/{number}/reviews", payload)
        self.store.add_artifact(self.task.task_id, "pr_comment", f"{repo}#{number}/review/{review['id']}", review.get("html_url"))
        return _text(f"posted {event} review on {repo}#{number}")

    async def reply_to_pr_comment(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        number = int(args["number"])
        reply = await self._request("POST", f"/repos/{repo}/pulls/{number}/comments/{int(args['comment_id'])}/replies", {"body": str(args.get("body", ""))})
        self.store.add_artifact(self.task.task_id, "pr_comment", f"{repo}#{number}/comment/{reply['id']}", reply.get("html_url"))
        return _text(f"replied: {reply.get('html_url')}")

    async def close_pull_request(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        number = int(args["number"])
        pr = await self._request("GET", f"/repos/{repo}/pulls/{number}")
        if pr.get("state") == "closed":
            return _text(f"pull request #{number} is already closed: {pr.get('html_url')}")
        pr = await self._request("PATCH", f"/repos/{repo}/pulls/{number}", {"state": "closed"})
        if self.on_milestone:
            await self.on_milestone(f"Closed pull request {pr['html_url']}")
        return _text(f"closed pull request #{number}: {pr['html_url']}")

    async def delete_branch(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        branch = str(args.get("branch", "")).strip()
        if branch.startswith("refs/heads/"):
            branch = branch[len("refs/heads/") :]
        if not branch:
            return _error("branch is required")
        if not branch.startswith("agent/"):
            return _error("only agent/-prefixed branches can be deleted")
        try:
            await self._request("DELETE", f"/repos/{repo}/git/refs/heads/{branch}")
        except GitHubStatusError as status_error:
            if status_error.status not in BRANCH_ALREADY_GONE:
                raise
            return _text(f"branch {branch} in {repo} is already deleted")
        return _text(f"deleted branch {branch} from {repo}")

    async def create_release(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        tag_name = str(args.get("tag_name", "")).strip()
        if not TAG_RE.match(tag_name):
            return _error("tag_name must look like vX.Y.Z (e.g. v1.4.0)")
        body = str(args.get("body", "")).strip()
        if not body:
            return _error("body (release notes) is required")
        # idempotency layer 1: we already published this tag's release (ORC-012)
        for artifact in self.store.artifacts_for(self.task.task_id):
            if artifact["kind"] == "release" and artifact["external_id"] == f"{repo}@{tag_name}":
                return _text(f"a release for {tag_name} already exists: {artifact['url']} — nothing to do")
        payload = {"tag_name": tag_name, "name": str(args.get("name", "")).strip() or tag_name, "body": body}
        target_commitish = str(args.get("target_commitish", "")).strip()
        if target_commitish:
            payload["target_commitish"] = target_commitish
        # idempotency layer 2: github rejects an already-cut tag with 422; treat that as done rather than surfacing a raw api error (crash-after-create window)
        try:
            release = await self._request("POST", f"/repos/{repo}/releases", payload)
        except GitHubStatusError as exc:
            if exc.status == 422:
                return _text(f"a release for {tag_name} already exists on {repo} — nothing to do")
            raise
        self.store.add_artifact(self.task.task_id, "release", f"{repo}@{tag_name}", release.get("html_url"))
        if self.on_milestone:
            await self.on_milestone(f"Published release {tag_name}: {release['html_url']}")
        return _text(f"created release {tag_name}: {release['html_url']}")

    async def resolve_pr_thread(self, args: dict) -> dict:
        repo, error = self._check_repo(args)
        if error:
            return error
        if not self.bot_logins:
            return _error("cannot verify thread authorship — resolution unavailable for this task")
        number = int(args["number"])
        comment_id = int(args["comment_id"])
        thread = next(
            (thread for thread in await self._review_threads(repo, number) if any(comment.get("databaseId") == comment_id for comment in ((thread.get("comments") or {}).get("nodes") or []))),
            None,
        )
        if thread is None:
            return _error("comment id was not found in this pull request's review threads; pass a review-comment id from list_pr_comments")
        comments = (thread.get("comments") or {}).get("nodes") or []
        root_login = str((((comments[0] if comments else {}).get("author") or {}).get("login")) or "")
        if root_login.lower() not in {login.lower() for login in self.bot_logins}:
            return _error(f"only review threads started by {self.bot_name} or {self.other_bot_name} can be resolved")
        if thread.get("isResolved"):
            return _text(f"review thread for comment {comment_id} on {repo}#{number} is already resolved")
        data = await self._graphql(
            """mutation ResolveReviewThread($id: ID!) {
  resolveReviewThread(input: {threadId: $id}) { thread { isResolved } }
}""",
            {"id": thread["id"]},
        )
        if not (((data.get("resolveReviewThread") or {}).get("thread") or {}).get("isResolved")):
            raise RuntimeError("github did not confirm that the review thread was resolved")
        return _text(f"resolved review thread for comment {comment_id} on {repo}#{number}")

    # -- plumbing ------------------------------------------------------------------

    def _check_repo(self, args: dict) -> tuple[str, dict | None]:
        repo = str(args.get("repo", "")).strip()
        if repo not in self.approved_repos:
            return repo, _error(f"repository {repo!r} is not on the approved list {self.approved_repos}")
        return repo, None

    def _record_pr(self, repo: str, pr: dict) -> None:
        self.store.add_artifact(self.task.task_id, "pull_request", f"{repo}#{pr['number']}", pr.get("html_url"))

    async def _request(self, method: str, path: str, payload: dict | None = None):
        """the http seam — patched in unit tests. token fetched per call, never stored (TOL-007)."""
        import aiohttp

        token = await self.broker.token_for_task(self.task.task_id)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        async with aiohttp.ClientSession() as session:
            async with session.request(method, GITHUB_API + path, json=payload, headers=headers) as response:
                if response.status >= 300:
                    raise GitHubStatusError(response.status, redactor.redact(f"github api {method} {path} failed: {response.status}"))
                if response.status == 204:
                    return None
                return await response.json()

    async def _graphql(self, query: str, variables: dict) -> dict:
        """the graphql http seam — patched separately in unit tests."""
        import aiohttp

        token = await self.broker.token_for_task(self.task.task_id)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(GITHUB_API + "/graphql", json={"query": query, "variables": variables}, headers=headers) as response:
                if response.status >= 300:
                    raise RuntimeError(redactor.redact(f"github graphql failed: {response.status}"))
                payload = await response.json()
                if payload.get("errors"):
                    raise RuntimeError(redactor.redact(f"github graphql failed: {json.dumps(payload['errors'])}"))
                return payload.get("data") or {}

    async def _review_threads(self, repo: str, number: int) -> list[dict]:
        owner, name = repo.split("/", 1)
        data = await self._graphql(
            """query ReviewThreads($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes {
          id
          isResolved
          comments(first: 100) { nodes { databaseId author { login } } }
        }
      }
    }
  }
}""",
            {"owner": owner, "name": name, "number": number},
        )
        return ((((data.get("repository") or {}).get("pullRequest") or {}).get("reviewThreads") or {}).get("nodes")) or []


def build_github_server(adapter: GitHubAdapter):
    """expose the adapter as mcp tools; names become mcp__github__<name> in allowed_tools."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    tools = [
        tool("create_pull_request", "Create a pull request. Include a Summary, Testing performed, and Known limitations in the body.", {"repo": str, "title": str, "head": str, "base": str, "body": str})(wrap(adapter.create_pull_request, logger)),
        tool("get_pull_request", "Get one pull request's metadata.", {"repo": str, "number": int})(wrap(adapter.get_pull_request, logger)),
        tool(
            "list_pull_requests",
            "List pull requests in a repository. State defaults to open and may be open, closed, or all.",
            {"type": "object", "properties": {"repo": {"type": "string"}, "state": {"type": "string"}}, "required": ["repo"]},
        )(wrap(adapter.list_pull_requests, logger)),
        tool(
            "list_pr_files",
            "List changed files and bounded patches for a pull request, 10 per page. Fetch full diffs with git.",
            {"type": "object", "properties": {"repo": {"type": "string"}, "number": {"type": "integer"}, "page": {"type": "integer"}}, "required": ["repo", "number"]},
        )(wrap(adapter.list_pr_files, logger)),
        tool("list_pr_comments", "List comments and review comments on a pull request.", {"repo": str, "number": int})(wrap(adapter.list_pr_comments, logger)),
        tool("comment_on_pull_request", "Post a comment on a pull request.", {"repo": str, "number": int, "body": str})(wrap(adapter.comment_on_pull_request, logger)),
        tool(
            "create_pr_review",
            "Post a review (COMMENT or REQUEST_CHANGES) on a pull request, optionally with inline comments_json.",
            {
                "type": "object",
                "properties": {"repo": {"type": "string"}, "number": {"type": "integer"}, "body": {"type": "string"}, "event": {"type": "string"}, "comments_json": {"type": "string"}},
                "required": ["repo", "number", "body", "event"],
            },
        )(wrap(adapter.create_pr_review, logger)),
        tool("reply_to_pr_comment", "Reply to a specific pull request review comment.", {"repo": str, "number": int, "comment_id": int, "body": str})(wrap(adapter.reply_to_pr_comment, logger)),
        tool(
            "resolve_pr_thread",
            f"Resolve a pull request review thread, identified by any review comment id in it. Only threads started by {adapter.bot_name} or {adapter.other_bot_name} can be resolved; use it once the thread's finding is verifiably fixed.",
            {"repo": str, "number": int, "comment_id": int},
        )(wrap(adapter.resolve_pr_thread, logger)),
        tool("close_pull_request", "Close a pull request without merging it. Idempotent — already-closed pull requests are reported, not re-closed.", {"repo": str, "number": int})(wrap(adapter.close_pull_request, logger)),
        tool("delete_branch", "Delete a branch. Only agent/-prefixed branches can be deleted; protected and default branches are refused.", {"repo": str, "branch": str})(wrap(adapter.delete_branch, logger)),
        tool(
            "create_release",
            "Create a GitHub release with a new tag (vX.Y.Z) and release notes. The tag is created from target_commitish (default branch when omitted); name defaults to the tag.",
            {
                "type": "object",
                "properties": {"repo": {"type": "string"}, "tag_name": {"type": "string"}, "body": {"type": "string"}, "name": {"type": "string"}, "target_commitish": {"type": "string"}},
                "required": ["repo", "tag_name", "body"],
            },
        )(wrap(adapter.create_release, logger)),
    ]
    return create_sdk_mcp_server(name="github", version="1.0.0", tools=tools)
