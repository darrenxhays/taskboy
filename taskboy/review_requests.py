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


def _should_page_debug(count: int, transient: bool) -> bool:
    """non-transient failures page at count 1, then both kinds join the N, N**2, N**3, ... progression."""
    if not transient and count == 1:
        return True
    power = DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES
    while power < count:
        power *= DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES
    return power == count


def _failure_signature(error: Exception) -> str:
    """identifies an error's *shape*, so a shape change gets its own error row without resetting the streak count."""
    if isinstance(error, GitHubStatusError):
        return f"http:{error.status}"
    return type(error).__name__


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
        self.auto_address_agent_prs = bool(review_requests.get("auto_address_agent_prs", False))
        if self.auto_address_agent_prs and (reviewer_broker is None or not config.reviewer.enabled or not config.reviewer.review_agent_prs):
            # the loop needs an enabled reviewer reviewing the main agent's pushes; anything less leaves the flag silently doing nothing
            logger.warning("github.review_requests.auto_address_agent_prs requires an enabled reviewer with reviewer.review_agent_prs — forcing it off")
            self.auto_address_agent_prs = False
        self.round_cap = max(int(review_requests.get("round_cap", 10)), 1)
        self.approved_repos = list(github.get("approved_repos") or [])
        self.bot_login: str | None = None
        self.reviewer_bot_login: str | None = None
        self.token: str | None = None
        self.token_expires_at = 0.0
        self.etags: dict[str, str] = {}
        self.consecutive_failures: dict[str, int] = {}
        self.failure_signatures: dict[str, str] = {}

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
                self.failure_signatures.pop(repo, None)
            except Exception as e:
                abort_sweep = False
                signature = _failure_signature(e)
                first_failure_this_streak = self.failure_signatures.get(repo) != signature
                if isinstance(e, GitHubStatusError) and (e.status == 401 or (e.status == 403 and e.retry_after is None)):
                    abort_sweep = e.status == 401
                    # clear so a healed permission is picked up next sweep, but never discard a token this sweep already minted for another repo
                    if abort_sweep or not reminted:
                        self.token, self.token_expires_at = None, 0.0
                    if first_failure_this_streak and not reminted:
                        reminted = True
                        try:
                            token = await self._token()  # re-mints immediately
                            await self._sweep_repo(repo, token)
                            self.consecutive_failures.pop(repo, None)
                            self.failure_signatures.pop(repo, None)
                            logger.warning("github rejected the cached review-poller token (%s) for %s — re-minted and recovered in-sweep", e.status, repo)
                            continue
                        except Exception as retry_error:  # mint failed or the repo failed again post-mint
                            e = retry_error
                            signature = _failure_signature(e)
                            first_failure_this_streak = self.failure_signatures.get(repo) != signature
                            if abort_sweep:
                                self.token, self.token_expires_at = None, 0.0
                self.failure_signatures[repo] = signature
                count = self.consecutive_failures.get(repo, 0) + 1
                self.consecutive_failures[repo] = count
                if first_failure_this_streak:
                    # count keeps climbing across a shape change so a flapping error can't re-arm the page-on-first rule
                    self.store.add_error("review_poller", type(e).__name__, str(e), traceback="".join(traceback.format_exception(e)), context={"repository": repo})
                if _should_page_debug(count, _is_transient(e)):
                    debug = getattr(self.notifier, "debug", None)
                    if debug is not None:
                        await debug.system_error("review_poller", f"{repo}: {e}")
                # exc_info=e (not logger.exception, which reads sys.exc_info()) so a re-mint retry that reassigned
                # `e` above logs the same exception that was just recorded to sqlite, not the pre-retry one
                logger.error("github review-request poll failed for %s", repo, exc_info=e)
                if abort_sweep:
                    break

    async def _sweep_repo(self, repo: str, token: str) -> None:
        pulls = await self._list_open_pulls(repo, token)
        if pulls is None:
            return  # a real 304 on page 1 — nothing changed since the last sweep
        # _list_open_pulls always walks every page, so `pulls` is the complete open-pulls set —
        # a missing number here is genuinely closed/merged, never just pushed off page 1 (issue #88)
        self._cleanup_followup_meta_for_closed_prs(repo, {int(pr["number"]) for pr in pulls})
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
                reviewer_key = self._reviewer_key(repo, number, head_sha)
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
            if self.auto_address_agent_prs and reviewer_available and author_login == self.bot_login:
                await self._maybe_follow_up_agent(repo, pr, number, head_sha, token)

    async def _list_open_pulls(self, repo: str, token: str) -> list[dict] | None:
        """page through open PRs at 100/page until a short page ends the walk (mirrors `_get_reviews`)."""
        page1_path = f"/repos/{repo}/pulls?state=open&per_page=100&page=1"
        status, headers, page_pulls = await self._get_with_retry(page1_path, token)
        if status == 304:
            return None
        page_pulls = page_pulls or []
        etag = headers.get("ETag") or headers.get("etag")
        # don't cache while auto-follow-up is on: a 304 would skip the per-PR stall check
        # don't cache a full page 1 either: a 304 would exit before page 2 (issue #88)
        if etag and not self.auto_address_agent_prs and len(page_pulls) < 100:
            self.etags[page1_path] = str(etag)
        pulls = list(page_pulls)
        page = 1
        while len(page_pulls) == 100:
            page += 1
            path = f"/repos/{repo}/pulls?state=open&per_page=100&page={page}"
            _, _, page_pulls = await self._get_with_retry(path, token)
            page_pulls = page_pulls or []
            pulls.extend(page_pulls)
        return pulls

    async def _maybe_follow_up_agent(self, repo: str, pr: dict, number: int, head_sha: str, token: str) -> None:
        """when the reviewer has reviewed the current push of an agent-authored PR and hasn't approved, spawn the main
        agent to address it — up to `round_cap` rounds, then stop and ping `notify_channel` once instead of pinging every sweep."""
        html_url = pr["html_url"]
        if self.store.has_active_main_task_referencing(html_url, include_reviewer=True):
            return  # a main or reviewer task is already in flight for this pr — the reviewer's review task may still be running
        round_key = f"review_followup_round:{repo}#{number}"
        capped_key = f"review_followup_capped:{repo}#{number}"
        stalled_key = f"review_followup_stalled:{repo}#{number}@{head_sha}"

        capped_at_sha = self.store.meta_get(capped_key)
        if capped_at_sha is not None:
            if capped_at_sha in (head_sha, "1"):
                # "1" is the legacy sentinel from before this row stored a head_sha (pre-existing capped rows
                # at deploy time) — treat it as still capped rather than re-arming with no push having landed
                return  # still capped for this exact push — stop paying a paged reviews GET per sweep
            # a new push landed after the cap fired: a human intervened (pushed a fix, re-requested review),
            # so give the loop a fresh round budget for it instead of leaving it dead for the pr's whole life
            self.store.meta_delete(capped_key)
            self.store.meta_delete(round_key)

        reviews = await self._get_reviews(repo, number, token)
        reviewer_reviews = [review for review in reviews if (review.get("user") or {}).get("login") == self.reviewer_bot_login and review.get("state") in {"APPROVED", "CHANGES_REQUESTED"}]
        latest = reviewer_reviews[-1] if reviewer_reviews else None  # github returns reviews in submission order
        if latest is not None and latest.get("state") == "APPROVED":
            return  # the reviewer is satisfied — the loop is done (issue #75)
        if latest is None or latest.get("commit_id") != head_sha:
            # no reviewer review of the current push: normally one is just in flight, but a reviewer task that
            # died or parked without posting would leave this branch returning forever with no escalation.
            # the guard at the top already returned for an active task, so any task found here is stalled.
            reviewer_task = self.store.task_by_intake_key("github", self.notify_channel, self._reviewer_key(repo, number, head_sha))
            if reviewer_task is not None:
                await self._escalate_once(
                    stalled_key,
                    "review follow-up loop stalled on %s — the reviewer's review task %s is %s without posting a review" % (html_url, reviewer_task.task_id, reviewer_task.state),
                    f"{self._loop_name} review loop on {html_url} stalled — {self.config.reviewer.name}'s review task is {reviewer_task.state} without posting a review, so the loop can't advance — needs a human look.",
                )
            return
        # count rounds this poller actually created, not every reviewer CHANGES_REQUESTED ever (pre-flag or /review runs)
        round_number = int(self.store.meta_get(round_key) or "0") + 1
        if round_number > self.round_cap:
            await self._escalate_once(
                capped_key,
                f"review follow-up loop hit its {self.round_cap}-round cap on {html_url} — stopping and escalating",
                f"{self._loop_name} review loop on {html_url} hit its {self.round_cap}-round cap without {self.config.reviewer.name} approving — needs a human look.",
                value=head_sha,
            )
            return
        key = f"{repo}#{number}@{head_sha}:review:{latest.get('id')}"
        branch = str((pr.get("head") or {}).get("ref") or "")
        task, status = await accept_task(
            self.store,
            self.config,
            self.notifier,
            team_id="github",
            channel_id=self.notify_channel,
            thread_ts=key,
            message_ts=key,
            user_id="github",
            text=f"address review comments on {html_url} — push the fix to the existing branch `{branch}`, not a new one",
        )
        if status == "created":
            self.store.meta_set(round_key, str(round_number))
            self.store.meta_delete(stalled_key)  # forward progress made — clear any stale stall marker for this head
            logger.info("created follow-up task %s for %s (round %d/%d)", task.task_id if task else "", key, round_number, self.round_cap)
        elif status == "queue_full":
            logger.warning("follow-up task refused because the queue is full: %s", key)
        elif status == "duplicate" and task is not None:
            # terminal task on an unchanged key would dedup forever — escalate instead
            await self._escalate_once(
                stalled_key,
                "review follow-up loop stalled on %s — the follow-up task %s finished (%s) without a new push" % (html_url, task.task_id, task.state),
                f"{self._loop_name} review loop on {html_url} stalled — {self.config.agent_name}'s last follow-up task finished ({task.state}) without pushing a fix, so {self.config.reviewer.name} never re-reviewed — needs a human look.",
            )

    @property
    def _loop_name(self) -> str:
        return f"{self.config.agent_name}↔{self.config.reviewer.name}"

    def _reviewer_key(self, repo: str, number: int, head_sha: str) -> str:
        # also the poller's intake message_ts, so _maybe_follow_up_agent can look the reviewer task back up
        return f"reviewer:{repo}#{number}@{head_sha}"

    async def _escalate_once(self, escalation_key: str, log_message: str, notify_message: str, value: str = "1") -> None:
        """fire a warning + a single human-facing notification per `escalation_key`, falling back to the debug
        notifier when no `notify_channel` is configured — otherwise the escalation is a log line no human sees."""
        if self.store.meta_get(escalation_key) is not None:
            return
        logger.warning(log_message)
        if self.notify_channel:
            await self.notifier.answer(self.notify_channel, None, notify_message)
        else:
            debug = getattr(self.notifier, "debug", None)
            if debug is not None:
                posted = await debug.system_error("review_poller", notify_message)
                if not posted:
                    return  # suppressed by the cooldown — nothing landed
        # marked sent only after the notification lands — a failed send must retry on the next sweep
        self.store.meta_set(escalation_key, value)

    def _cleanup_followup_meta_for_closed_prs(self, repo: str, open_numbers: set[int]) -> None:
        """sweep meta for any pr this repo still has follow-up rows for but that is no longer in the open list."""
        for key in self.store.meta_keys_with_prefix("review_followup_"):
            # key shapes: review_followup_{round,capped}:{repo}#{number}, review_followup_stalled:{repo}#{number}@{sha}
            _, _, rest = key.partition(":")
            pr_repo, _, suffix = rest.partition("#")
            if pr_repo != repo:
                continue
            number_part = suffix.split("@", 1)[0]
            if number_part.isdigit() and int(number_part) not in open_numbers:
                self.store.meta_delete(key)

    async def _get_reviews(self, repo: str, number: int, token: str) -> list[dict]:
        reviews: list[dict] = []
        page = 1
        while True:
            path = f"/repos/{repo}/pulls/{number}/reviews?per_page=100&page={page}"
            _, _, page_reviews = await self._get_with_retry(path, token)
            page_reviews = page_reviews or []
            reviews.extend(page_reviews)
            if len(page_reviews) < 100:
                return reviews
            page += 1

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
