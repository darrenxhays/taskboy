import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_harness import review_requests
from agent_harness.adapters.github_api import GitHubStatusError
from agent_harness.config import Role
from agent_harness.main import should_start_review_poller
from agent_harness.models import CANCELLED, RECEIVED
from agent_harness.review_requests import DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES, ReviewRequestPoller


def pull(sha="abc", reviewers=None, number=7, repo="org/a", author="human"):
    return {
        "number": number,
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "head": {"sha": sha},
        "user": {"login": author},
        "requested_reviewers": [{"login": login} for login in (reviewers or ["red-app[bot]"])],
    }


def ok(pulls):
    return 200, {}, pulls


def poller(store, config, notifier, repos=None, blue=False):
    config.runner = "claude"
    config.raw = {"github": {"approved_repos": repos or ["org/a"], "review_requests": {"enabled": True, "poll_interval_seconds": 60, "notify_channel": ""}}}
    config.roles["system"] = Role("system", ["github"], ["read_only", "standard"], False, 12.0, None)
    broker = AsyncMock()
    broker.app_slug.return_value = "red-app"
    broker.read_token.return_value = ("token", time.time() + 3600)
    reviewer_broker = None
    if blue:
        config.reviewer.enabled = True
        reviewer_broker = AsyncMock()
        reviewer_broker.app_slug.return_value = "blue-app"
    return ReviewRequestPoller(store, config, broker, notifier, reviewer_broker=reviewer_broker)


@pytest.mark.asyncio
async def test_requested_reviewer_creates_once_per_head_and_new_head_retriggers(store, config, notifier):
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(return_value=ok([pull()]))
    await subject.sweep()
    await subject.sweep()
    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 1
    assert tasks[0].request_text == "/review https://github.com/org/a/pull/7"
    assert tasks[0].slack_user_id == "github"
    assert tasks[0].slack_team_id == "github"
    assert tasks[0].slack_thread_ts == "org/a#7@abc"

    subject._get.return_value = ok([pull(sha="def")])
    await subject.sweep()
    assert len(store.tasks_in_state(RECEIVED)) == 2
    subject.broker.read_token.assert_awaited_once_with(["org/a"], permissions={"pull_requests": "read", "metadata": "read"})


@pytest.mark.asyncio
async def test_main_requested_reviewer_creates_one_blue_task_when_available(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["red-app[bot]", "blue-app[bot]"])]))

    await subject.sweep()

    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 1
    assert tasks[0].persona == "reviewer"
    assert tasks[0].slack_thread_ts == "reviewer:org/a#7@abc"


@pytest.mark.asyncio
async def test_main_requested_reviewer_falls_back_when_reviewer_broker_is_unavailable(store, config, notifier, caplog):
    subject = poller(store, config, notifier, blue=True)
    subject.reviewer_broker = None
    subject._get = AsyncMock(return_value=ok([pull()]))

    await subject.sweep()

    task = store.tasks_in_state(RECEIVED)[0]
    assert task.persona is None
    assert task.slack_thread_ts == "org/a#7@abc"
    assert "falling back to the main agent" in caplog.text


@pytest.mark.asyncio
async def test_other_reviewers_do_not_trigger(store, config, notifier):
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["somebody"])]))
    await subject.sweep()
    assert store.count_tasks(RECEIVED) == 0


@pytest.mark.asyncio
async def test_blue_reviews_red_authored_pr(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["somebody"], author="red-app[bot]")]))
    await subject.sweep()
    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 1
    assert tasks[0].persona == "reviewer"
    assert tasks[0].request_text == "/review https://github.com/org/a/pull/7"
    assert tasks[0].slack_thread_ts == "reviewer:org/a#7@abc"


@pytest.mark.asyncio
async def test_blue_task_skipped_when_red_is_already_working_the_pr(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["red-app[bot]", "blue-app[bot]"])]))
    store.create_task(
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_thread_ts="1",
        slack_message_ts="1",
        slack_user_id="U1",
        request_text="address the comments on https://github.com/org/a/pull/7",
    )

    await subject.sweep()

    assert store.count_tasks(RECEIVED) == 1  # only the pre-existing red task; no blue task created


@pytest.mark.asyncio
async def test_blue_task_still_created_once_the_red_task_referencing_the_pr_is_terminal(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["red-app[bot]", "blue-app[bot]"])]))
    task, _ = store.create_task(
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_thread_ts="1",
        slack_message_ts="1",
        slack_user_id="U1",
        request_text="address the comments on https://github.com/org/a/pull/7",
    )
    store.transition(task.task_id, RECEIVED, CANCELLED, "done")

    await subject.sweep()

    tasks = [t for t in store.tasks_in_state(RECEIVED) if t.persona == "reviewer"]
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_reviewer_requested_reviewer_creates_blue_task(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["blue-app[bot]"])]))
    await subject.sweep()
    task = store.tasks_in_state(RECEIVED)[0]
    assert task.persona == "reviewer"
    assert task.request_text.startswith("/review ")


@pytest.mark.asyncio
async def test_blue_auto_review_can_be_disabled_and_requires_broker(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    config.reviewer.review_agent_prs = False
    subject._get = AsyncMock(return_value=ok([pull(reviewers=["somebody"], author="red-app[bot]")]))
    await subject.sweep()
    assert store.count_tasks(RECEIVED) == 0

    subject.reviewer_broker = None
    subject.reviewer_bot_login = None
    subject._get.return_value = ok([pull(reviewers=["blue-app[bot]"])])
    await subject.sweep()
    assert store.count_tasks(RECEIVED) == 0


@pytest.mark.asyncio
async def test_red_request_and_red_authorship_create_only_one_blue_task(store, config, notifier):
    subject = poller(store, config, notifier, blue=True)
    subject._get = AsyncMock(return_value=ok([pull(author="red-app[bot]")]))
    await subject.sweep()
    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 1
    assert tasks[0].persona == "reviewer"
    assert tasks[0].slack_thread_ts == "reviewer:org/a#7@abc"


@pytest.mark.asyncio
async def test_per_repo_failure_does_not_stop_other_repos(store, config, notifier):
    subject = poller(store, config, notifier, ["org/a", "org/b"])

    async def get(path, token):
        if "org/a" in path:
            raise RuntimeError("github down")
        return ok([pull(repo="org/b")])

    subject._get = get
    await subject.sweep()
    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 1
    assert "org/b/pull/7" in tasks[0].request_text


@pytest.mark.asyncio
async def test_401_clears_cached_token_and_stops_sweep_early(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier, ["org/a", "org/b"])
    subject._get = AsyncMock(side_effect=GitHubStatusError(401, "github api GET x failed: 401 — Bad credentials"))

    await subject.sweep()

    assert subject.token is None
    assert subject.token_expires_at == 0.0
    assert subject._get.await_count == 2  # org/a's first attempt plus the post-remint retry; org/b never fetched
    assert len(store.recent_errors()) == 1
    assert "org/a" not in subject.consecutive_failures
    notifier.debug.system_error.assert_awaited_once()

    # the next sweep re-mints the token instead of reusing the cleared one
    subject.broker.read_token.reset_mock()
    subject._get.side_effect = None
    subject._get.return_value = ok([pull()])
    await subject.sweep()
    subject.broker.read_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_transient_error_is_retried_once_and_succeeds(store, config, notifier, monkeypatch):
    monkeypatch.setattr(review_requests, "TRANSIENT_RETRY_DELAY_SECONDS", 0)
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=[GitHubStatusError(503, "github api GET x failed: 503 — unavailable"), ok([pull()])])

    await subject.sweep()

    assert subject._get.await_count == 2
    assert store.recent_errors() == []
    assert len(store.tasks_in_state(RECEIVED)) == 1
    notifier.debug.system_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_error_persisting_after_retry_does_not_page_before_threshold(store, config, notifier, monkeypatch):
    monkeypatch.setattr(review_requests, "TRANSIENT_RETRY_DELAY_SECONDS", 0)
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=GitHubStatusError(503, "github api GET x failed: 503 — unavailable"))

    await subject.sweep()

    assert subject._get.await_count == 2
    assert len(store.recent_errors()) == 1
    notifier.debug.system_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_error_pages_after_consecutive_failure_threshold(store, config, notifier, monkeypatch):
    monkeypatch.setattr(review_requests, "TRANSIENT_RETRY_DELAY_SECONDS", 0)
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=GitHubStatusError(503, "github api GET x failed: 503 — unavailable"))

    for _ in range(DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES - 1):
        await subject.sweep()
    notifier.debug.system_error.assert_not_awaited()

    await subject.sweep()
    notifier.debug.system_error.assert_awaited_once()

    # a further consecutive failure does not page again immediately
    await subject.sweep()
    notifier.debug.system_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_transient_error_pages_immediately(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=GitHubStatusError(422, "github api GET x failed: 422 — bad request"))

    await subject.sweep()

    assert subject._get.await_count == 1
    notifier.debug.system_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_secondary_rate_limit_with_retry_after_is_treated_as_transient(store, config, notifier, monkeypatch):
    monkeypatch.setattr(review_requests, "TRANSIENT_RETRY_DELAY_SECONDS", 0)
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=[GitHubStatusError(403, "github api GET x failed: 403 — abuse detection", retry_after="30"), ok([pull()])])

    await subject.sweep()

    assert subject._get.await_count == 2
    notifier.debug.system_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_plain_403_without_retry_after_pages_immediately(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=GitHubStatusError(403, "github api GET x failed: 403 — forbidden"))

    await subject.sweep()

    assert subject._get.await_count == 1
    notifier.debug.system_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_consecutive_failure_count_resets_after_success(store, config, notifier, monkeypatch):
    monkeypatch.setattr(review_requests, "TRANSIENT_RETRY_DELAY_SECONDS", 0)
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier)
    subject._get = AsyncMock(side_effect=GitHubStatusError(503, "github api GET x failed: 503 — unavailable"))

    for _ in range(DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES - 1):
        await subject.sweep()
    assert subject.consecutive_failures["org/a"] == DEBUG_PAGE_AFTER_CONSECUTIVE_FAILURES - 1

    subject._get.side_effect = None
    subject._get.return_value = ok([pull()])
    await subject.sweep()
    assert "org/a" not in subject.consecutive_failures

    subject._get.side_effect = GitHubStatusError(503, "github api GET x failed: 503 — unavailable")
    subject._get.return_value = None
    await subject.sweep()
    notifier.debug.system_error.assert_not_awaited()


@pytest.mark.asyncio
async def test_401_remints_once_and_recovers_within_the_same_sweep(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier, ["org/a", "org/b"])
    subject.broker.read_token.side_effect = [("stale-token", time.time() + 3600), ("fresh-token", time.time() + 3600)]

    async def get(path, token):
        if token == "stale-token":
            raise GitHubStatusError(401, "github api GET x failed: 401 — Bad credentials")
        return ok([pull(repo="org/b" if "org/b" in path else "org/a")])

    subject._get = get
    await subject.sweep()

    assert subject.broker.read_token.await_count == 2
    assert store.recent_errors() == []
    notifier.debug.system_error.assert_not_awaited()
    tasks = store.tasks_in_state(RECEIVED)
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_401_retry_also_fails_records_one_error_and_aborts_sweep(store, config, notifier):
    notifier.debug = AsyncMock()
    subject = poller(store, config, notifier, ["org/a", "org/b"])
    subject.broker.read_token.side_effect = [("stale-token", time.time() + 3600), ("still-bad-token", time.time() + 3600)]
    subject._get = AsyncMock(side_effect=GitHubStatusError(401, "github api GET x failed: 401 — Bad credentials"))

    await subject.sweep()

    assert subject.broker.read_token.await_count == 2
    assert len(store.recent_errors()) == 1
    notifier.debug.system_error.assert_awaited_once()
    assert subject._get.await_count == 2  # org/a's first attempt plus the post-remint retry; org/b never fetched
    assert subject.token is None


@pytest.mark.asyncio
async def test_token_remints_when_broker_expiry_is_within_refresh_margin(store, config, notifier):
    subject = poller(store, config, notifier)
    subject.broker.read_token.side_effect = [("token-a", time.time() + 60), ("token-b", time.time() + 3600)]

    assert await subject._token() == "token-a"
    assert await subject._token() == "token-b"
    assert subject.broker.read_token.await_count == 2


@pytest.mark.asyncio
async def test_disabled_sweep_does_nothing(store, config, notifier):
    subject = poller(store, config, notifier)
    subject.enabled = False
    subject._get = AsyncMock()
    await subject.sweep()
    subject._get.assert_not_awaited()
    subject.broker.app_slug.assert_not_awaited()


@pytest.mark.asyncio
async def test_etag_is_cached_and_304_skips_processing(store, config, notifier):
    subject = poller(store, config, notifier)
    path = "/repos/org/a/pulls?state=open&per_page=50"
    subject._get = AsyncMock(side_effect=[(200, {"ETag": '"v1"'}, [pull()]), (304, {}, None)])

    await subject.sweep()
    await subject.sweep()

    assert subject.etags[path] == '"v1"'
    assert len(store.tasks_in_state(RECEIVED)) == 1
    assert store.recent_errors() == []


@pytest.mark.asyncio
async def test_get_sends_if_none_match(monkeypatch, store, config, notifier):
    captured = {}

    class Response:
        status = 200
        headers = {"ETag": '"v2"'}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def json(self):
            return []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, headers):
            captured.update(headers)
            return Response()

    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(ClientSession=Session))
    subject = poller(store, config, notifier)
    path = "/repos/org/a/pulls?state=open&per_page=50"
    subject.etags[path] = '"v1"'

    status, headers, pulls = await subject._get(path, "token")

    assert (status, headers["ETag"], pulls) == (200, '"v2"', [])
    assert captured["If-None-Match"] == '"v1"'


@pytest.mark.asyncio
async def test_get_raises_status_error_with_retry_after_on_failure(monkeypatch, store, config, notifier):
    class Response:
        status = 403
        headers = {"Retry-After": "12"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def text(self):
            return "abuse detection triggered"

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url, headers):
            return Response()

    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(ClientSession=Session))
    subject = poller(store, config, notifier)

    with pytest.raises(GitHubStatusError) as exc_info:
        await subject._get("/repos/org/a/pulls?state=open&per_page=50", "token")

    assert exc_info.value.status == 403
    assert exc_info.value.retry_after == "12"


def test_poller_start_guard(config, caplog):
    broker = object()
    config.runner = "claude"
    config.raw = {"github": {"review_requests": {"enabled": True}}}
    assert should_start_review_poller(config, broker) is False
    assert "no configured role" in caplog.text
    config.roles["system"] = Role("system", ["github"], ["standard"], False, 12.0, None)
    assert should_start_review_poller(config, broker) is True
    assert should_start_review_poller(config, None) is False
    config.raw["github"]["review_requests"]["enabled"] = False
    assert should_start_review_poller(config, broker) is False
