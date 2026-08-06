"""poll github review requests and turn app-reviewer requests into /review tasks."""

import asyncio
import logging
import time
import traceback

from taskboy.adapters.github_api import GitHubStatusError
from taskboy.orchestrator import accept_task
from taskboy.redact import redactor

logger = logging.getLogger("taskboy.review_requests")

GITHUB_API = "https://api.github.com"
TOKEN_REFRESH_MARGIN_SECONDS = 600
TRANSIENT_RETRY_DELAY_SECONDS = 2
DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES = 3
SECONDARY_RATE_LIMIT_STATUSES = {403, 429}  # github's abuse/secondary-rate-limit responses always carry Retry-After


def _is_transient(error: Exception) -> bool:
    """github blips the next sweep will heal: connection problems, timeouts, 5xx, and secondary rate limits."""
    import aiohttp

    if isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError)):
        return True
    if isinstance(error, GitHubStatusError):
        if error.status >= 500:
            return True
        # a plain 403 can also mean "permission denied" (not transient) — Retry-After is what tells them apart
        if error.status in SECONDARY_RATE_LIMIT_STATUSES and error.retry_after is not None:
            return True
    return False


class ReviewRequestPoller:
    def __init__(self, store, config, broker, notifier, reviewer_broker=None):
        self.store = store
        self.config = config
        self.broker = broker
        self.reviewer_broker = reviewer_broker
        self.notifier = notifier
        github = config.raw.get("github") or {}
        review_requests = github.get("review_requests") or {}
        self.enabled = bool(review_requests.get("enabled", False))
        self.poll_interval_seconds = max(int(review_requests.get("poll_interval_seconds", 60)), 1)
        self.notify_channel = str(review_requests.get("notify_channel") or "")
        self.approved_repos = list(github.get("approved_repos") or [])
        self.bot_login: str | None = None
        self.reviewer_bot_login: str | None = None
        self.token: str | None = None
        self.token_expires_at = 0.0
        self.etags: dict[str, str] = {}
        self.consecutive_failures: dict[str, int] = {}

    async def run(self) -> None:
        while True:
            try:
                if self.bot_login is None:
                    self.bot_login = f"{await self.broker.app_slug()}[bot]"
                if self.reviewer_broker is not None and self.reviewer_bot_login is None:
                    self.reviewer_bot_login = f"{await self.reviewer_broker.app_slug()}[bot]"
                await self.sweep()
            except Exception as e:
                self.store.add_error("review_poller", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"operation": "sweep"})
                debug = getattr(self.notifier, "debug", None)
                if debug is not None:
                    await debug.system_error("review_poller", str(e))
                logger.exception("github review-request sweep failed")
            await asyncio.sleep(self.poll_interval_seconds)

    async def sweep(self) -> None:
        if not self.enabled:
            return
        if self.bot_login is None:
            self.bot_login = f"{await self.broker.app_slug()}[bot]"
        if self.reviewer_broker is not None and self.reviewer_bot_login is None:
            self.reviewer_bot_login = f"{await self.reviewer_broker.app_slug()}[bot]"
        token = await self._token()
        reminted = False  # at most one inline re-mint per sweep
        for repo in self.approved_repos:
            try:
                await self._sweep_repo(repo, token)
                self.consecutive_failures.pop(repo, None)
            except Exception as e:
                if isinstance(e, GitHubStatusError) and e.status == 401:
                    # the token is shared across repos: every remaining repo would fail identically.
                    self.token, self.token_expires_at = None, 0.0
                    if not reminted:
                        reminted = True
                        try:
                            token = await self._token()  # re-mints immediately
                            await self._sweep_repo(repo, token)
                            self.consecutive_failures.pop(repo, None)
                            logger.warning("github rejected the cached review-poller token (401) for %s — re-minted and recovered in-sweep", repo)
                            continue
                        except Exception as retry_error:  # mint failed or the repo failed again post-mint
                            e = retry_error
                            self.token, self.token_expires_at = None, 0.0
                    self.store.add_error("review_poller", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"repository": repo})
                    debug = getattr(self.notifier, "debug", None)
                    if debug is not None:
                        await debug.system_error("review_poller", f"{repo}: {e} — cached token cleared, ending sweep early")
                    logger.warning("review-poller token still failing after re-mint for %s — cleared cache, ending sweep early", repo)
                    break
                self.store.add_error("review_poller", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"repository": repo})
                count = self.consecutive_failures.get(repo, 0) + 1
                self.consecutive_failures[repo] = count
                if not _is_transient(e) or count == DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES:
                    debug = getattr(self.notifier, "debug", None)
                    if debug is not None:
                        await debug.system_error("review_poller", f"{repo}: {e}")
                logger.exception("github review-request poll failed for %s", repo)

    async def _sweep_repo(self, repo: str, token: str) -> None:
        path = f"/repos/{repo}/pulls?state=open&per_page=50"
        status, headers, pulls = await self._get_with_retry(path, token)
        if status == 304:
            return
        etag = headers.get("ETag") or headers.get("etag")
        if etag:
            self.etags[path] = str(etag)
        for pr in pulls:
            reviewers = [(reviewer or {}).get("login") for reviewer in pr.get("requested_reviewers") or []]
            number = int(pr["number"])
            head_sha = str((pr.get("head") or {})["sha"])
            main_requested = self.bot_login in reviewers
            author_login = str((pr.get("user") or {}).get("login") or "")
            reviewer_requested = self.reviewer_bot_login is not None and self.reviewer_bot_login in reviewers
            review_agent_pr = self.config.reviewer.review_agent_prs and author_login == self.bot_login
            reviewer_available = self.reviewer_broker is not None and self.config.reviewer.enabled
            if reviewer_available and (main_requested or reviewer_requested or review_agent_pr):
                if self.store.has_active_main_task_referencing(pr["html_url"]):
                    logger.info("skipping reviewer task for %s#%d — an active main-agent task already references this PR", repo, number)
                    continue
                reviewer_key = f"reviewer:{repo}#{number}@{head_sha}"
                task, status = await accept_task(
                    self.store,
                    self.config,
                    self.notifier,
                    team_id="github",
                    channel_id=self.notify_channel,
                    thread_ts=reviewer_key,
                    message_ts=reviewer_key,
                    user_id="github",
                    text=f"/review {pr['html_url']}",
                    persona="reviewer",
                )
                if status == "created":
                    logger.info("created reviewer github review task %s for %s", task.task_id if task else "", reviewer_key)
                elif status == "queue_full":
                    logger.warning("reviewer github review task refused because the queue is full: %s", reviewer_key)
            elif main_requested:
                key = f"{repo}#{number}@{head_sha}"
                task, status = await accept_task(
                    self.store,
                    self.config,
                    self.notifier,
                    team_id="github",
                    channel_id=self.notify_channel,
                    thread_ts=key,
                    message_ts=key,
                    user_id="github",
                    text=f"/review {pr['html_url']}",
                )
                if status == "created":
                    if self.config.reviewer.enabled:
                        logger.warning("reviewer is unavailable; falling back to the main agent with GitHub review task %s for %s", task.task_id if task else "", key)
                    else:
                        logger.info("created github review task %s for %s", task.task_id if task else "", key)
                elif status == "queue_full":
                    logger.warning("github review task refused because the queue is full: %s", key)

    async def _token(self) -> str:
        if self.token is None or self.token_expires_at - time.time() < TOKEN_REFRESH_MARGIN_SECONDS:
            self.token, self.token_expires_at = await self.broker.read_token(self.approved_repos, permissions={"pull_requests": "read", "metadata": "read"})
        assert self.token is not None
        return self.token

    async def _get_with_retry(self, path: str, token: str):
        try:
            return await self._get(path, token)
        except Exception as e:
            if not _is_transient(e):
                raise
            logger.warning("transient github error for %s, retrying once: %s", path, e)
            await asyncio.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
            return await self._get(path, token)

    async def _get(self, path: str, token: str):
        """the github http seam — patched in unit tests."""
        import aiohttp

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        if path in self.etags:
            headers["If-None-Match"] = self.etags[path]
        async with aiohttp.ClientSession() as session:
            async with session.get(GITHUB_API + path, headers=headers) as response:
                response_headers = dict(response.headers)
                if response.status == 304:
                    return response.status, response_headers, None
                if response.status >= 300:
                    body = redactor.redact(await response.text())[:300]
                    retry_after = response_headers.get("Retry-After") or response_headers.get("retry-after")
                    raise GitHubStatusError(response.status, f"github api GET {path} failed: {response.status} — {body}", retry_after=retry_after)
                return response.status, response_headers, await response.json()
