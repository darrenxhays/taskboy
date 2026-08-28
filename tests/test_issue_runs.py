import asyncio
from unittest.mock import AsyncMock

import pytest

from taskboy import issue_runs
from taskboy.adapters.github_api import GitHubStatusError
from tests.conftest import RecordingNotifier, make_config


class FakeBroker:
    def __init__(self):
        self.read_token = AsyncMock(return_value=("tok", 9999999999.0))


def rec_in_review(store, make_task, key="x", pr_url="https://github.com/example-org/taskboy/pull/9"):
    row = store.record_issue(key, "example-org/taskboy", "s", "organization", "d", 50)
    task = make_task()
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "spec")
    return store.finish_issue(row["id"], "in_review", pr_url)


@pytest.mark.asyncio
async def test_start_implementation_run_releases_the_batch_when_accept_task_raises(store, monkeypatch):
    # a stranded pending: marker makes has_pending_implementation_reservation() refuse every later run until a restart
    row = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    monkeypatch.setattr(issue_runs, "accept_task", AsyncMock(side_effect=RuntimeError("slack ack exploded")))

    with pytest.raises(RuntimeError):
        await issue_runs.start_implementation_run(store, make_config(), RecordingNotifier(), thread_key="k")

    assert store.get_issue(row["id"])["status"] == "approved"
    assert store.has_pending_implementation_reservation() is False


@pytest.mark.asyncio
async def test_start_implementation_run_releases_the_batch_when_the_call_is_cancelled(store, monkeypatch):
    row = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    monkeypatch.setattr(issue_runs, "accept_task", AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await issue_runs.start_implementation_run(store, make_config(), RecordingNotifier(), thread_key="k")

    assert store.get_issue(row["id"])["status"] == "approved"
    assert store.has_pending_implementation_reservation() is False


@pytest.mark.asyncio
async def test_start_implementation_run_for_issues_approves_and_reserves_exactly_the_given_ids(store):
    row = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    other = store.record_issue("y", "example-org/taskboy", "s2", "organization", "d", 90)
    store.decide_issue(other["id"], "approved", "boss")

    task, status, active_task_id = await issue_runs.start_implementation_run(store, make_config(), RecordingNotifier(), issue_ids=[row["id"]], actor="boss", thread_key="k")

    assert status == "created" and task is not None and active_task_id is None
    # the still-proposed row was approved by the call, not just reserved
    reserved_row = store.get_issue(row["id"])
    assert reserved_row["status"] == "implementation_queued" and reserved_row["reserved_by"] == task.task_id
    # the higher-priority other row was left untouched: this is a scoped run, not the top-priority batch
    assert store.get_issue(other["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_start_implementation_run_for_issues_refuses_while_a_batch_coordinator_is_active(store, make_task):
    row = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    coordinator = make_task(text="/implementapprovedissues")

    task, status, active_task_id = await issue_runs.start_implementation_run(store, make_config(), RecordingNotifier(), issue_ids=[row["id"]], actor="boss", thread_key="k")

    assert (task, status, active_task_id) == (None, "already_running", coordinator.task_id)
    assert store.get_issue(row["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_start_implementation_run_for_issues_no_approved_issues_when_ids_are_not_eligible(store):
    row = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "denied", "boss")

    task, status, active_task_id = await issue_runs.start_implementation_run(store, make_config(), RecordingNotifier(), issue_ids=[row["id"]], actor="boss", thread_key="k")

    assert (task, status, active_task_id) == (None, "no_approved_issues", None)


@pytest.mark.asyncio
async def test_start_implementation_run_for_issues_releases_the_reservation_when_accept_task_raises(store, monkeypatch):
    row = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    monkeypatch.setattr(issue_runs, "accept_task", AsyncMock(side_effect=RuntimeError("slack ack exploded")))

    with pytest.raises(RuntimeError):
        await issue_runs.start_implementation_run(store, make_config(), RecordingNotifier(), issue_ids=[row["id"]], actor="boss", thread_key="k")

    assert store.get_issue(row["id"])["status"] == "approved"
    assert store.has_pending_implementation_reservation() is False


@pytest.mark.asyncio
async def test_start_implementation_run_for_issues_reverts_auto_approval_when_accept_task_raises(store, monkeypatch):
    # row was still `proposed`, so the call auto-approves it before reserving; if accept_task then blows up,
    # it must land back on `proposed` rather than `approved` — otherwise the next scheduled batch run would
    # implement it even though the operator's rocket click failed
    row = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    already_approved = store.record_issue("y", "example-org/taskboy", "s2", "organization", "d", 90)
    store.decide_issue(already_approved["id"], "approved", "boss")
    monkeypatch.setattr(issue_runs, "accept_task", AsyncMock(side_effect=RuntimeError("slack ack exploded")))

    with pytest.raises(RuntimeError):
        await issue_runs.start_implementation_run(store, make_config(), RecordingNotifier(), issue_ids=[row["id"], already_approved["id"]], actor="boss", thread_key="k")

    assert store.get_issue(row["id"])["status"] == "proposed"
    # this one was already approved by an operator before the rocket click, not by us — leave it approved
    assert store.get_issue(already_approved["id"])["status"] == "approved"
    assert store.has_pending_implementation_reservation() is False


@pytest.mark.asyncio
async def test_sync_in_review_merged_pr_marks_done(store, make_task, monkeypatch):
    row = rec_in_review(store, make_task)
    broker = FakeBroker()
    monkeypatch.setattr(issue_runs, "_get_pr", AsyncMock(return_value={"merged": True, "state": "closed"}))

    updated = await issue_runs.sync_in_review(store, broker)

    assert updated == 1
    assert store.get_issue(row["id"])["status"] == "done"
    broker.read_token.assert_awaited_once_with(["example-org/taskboy"], permissions={"pull_requests": "read", "metadata": "read"})


@pytest.mark.asyncio
async def test_sync_in_review_closed_unmerged_marks_failed(store, make_task, monkeypatch):
    row = rec_in_review(store, make_task)
    broker = FakeBroker()
    monkeypatch.setattr(issue_runs, "_get_pr", AsyncMock(return_value={"merged": False, "state": "closed"}))

    updated = await issue_runs.sync_in_review(store, broker)

    assert updated == 1
    assert store.get_issue(row["id"])["status"] == "failed"


@pytest.mark.asyncio
async def test_sync_in_review_still_open_is_untouched(store, make_task, monkeypatch):
    row = rec_in_review(store, make_task)
    broker = FakeBroker()
    monkeypatch.setattr(issue_runs, "_get_pr", AsyncMock(return_value={"merged": False, "state": "open"}))

    updated = await issue_runs.sync_in_review(store, broker)

    assert updated == 0
    assert store.get_issue(row["id"])["status"] == "in_review"


@pytest.mark.asyncio
async def test_sync_in_review_skips_unparseable_pr_url_without_raising(store, make_task, monkeypatch):
    row = rec_in_review(store, make_task, pr_url="not-a-github-url")
    broker = FakeBroker()
    get_pr = AsyncMock()
    monkeypatch.setattr(issue_runs, "_get_pr", get_pr)

    updated = await issue_runs.sync_in_review(store, broker)

    assert updated == 0
    assert store.get_issue(row["id"])["status"] == "in_review"
    get_pr.assert_not_awaited()
    errors = store.recent_errors(10)
    assert any(e["component"] == "issue_runs" and e["kind"] == "BadPrUrl" for e in errors)


@pytest.mark.asyncio
async def test_sync_in_review_passes_unpacked_token_string_to_get_pr(store, make_task, monkeypatch):
    row = rec_in_review(store, make_task)
    broker = FakeBroker()
    get_pr = AsyncMock(return_value={"merged": True, "state": "closed"})
    monkeypatch.setattr(issue_runs, "_get_pr", get_pr)

    await issue_runs.sync_in_review(store, broker)

    assert store.get_issue(row["id"])["status"] == "done"
    get_pr.assert_awaited_once_with("example-org/taskboy", 9, "tok")


@pytest.mark.asyncio
async def test_sync_in_review_reuses_token_for_same_repo(store, make_task, monkeypatch):
    a = rec_in_review(store, make_task, key="a", pr_url="https://github.com/example-org/taskboy/pull/1")
    b = rec_in_review(store, make_task, key="b", pr_url="https://github.com/example-org/taskboy/pull/2")
    broker = FakeBroker()
    monkeypatch.setattr(issue_runs, "_get_pr", AsyncMock(return_value={"merged": True, "state": "closed"}))

    updated = await issue_runs.sync_in_review(store, broker)

    assert updated == 2
    assert store.get_issue(a["id"])["status"] == "done"
    assert store.get_issue(b["id"])["status"] == "done"
    broker.read_token.assert_awaited_once_with(["example-org/taskboy"], permissions={"pull_requests": "read", "metadata": "read"})


@pytest.mark.asyncio
async def test_sync_in_review_one_row_error_does_not_abort_the_rest(store, make_task, monkeypatch):
    broken = rec_in_review(store, make_task, key="broken", pr_url="https://github.com/example-org/taskboy/pull/1")
    healthy = rec_in_review(store, make_task, key="healthy", pr_url="https://github.com/example-org/taskboy/pull/2")
    broker = FakeBroker()

    async def fake_get_pr(repo, number, token):
        if number == 1:
            raise GitHubStatusError(500, "boom")
        return {"merged": True, "state": "closed"}

    monkeypatch.setattr(issue_runs, "_get_pr", fake_get_pr)

    updated = await issue_runs.sync_in_review(store, broker)

    assert updated == 1
    assert store.get_issue(broken["id"])["status"] == "in_review"
    assert store.get_issue(healthy["id"])["status"] == "done"
    errors = store.recent_errors(10)
    assert any(e["component"] == "issue_runs" and e["kind"] == "GitHubStatusError" for e in errors)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example-org/taskboy/pull/9",
        "https://github.com/example-org/taskboy/pull/9/",
        "https://github.com/example-org/taskboy/pull/9/files",
        "https://github.com/example-org/taskboy/pull/9?w=1",
        "https://github.com/example-org/taskboy/pull/9#discussion_r123",
        "https://github.com/example-org/taskboy/pull/9.",
        "https://github.com/example-org/taskboy/pull/9)",
    ],
)
def test_pr_url_re_tolerates_trailing_suffixes(url):
    # a /files suffix, a ?query, a #fragment, or trailing punctuation used to make PR_URL_RE fail to match at
    # all, leaving the row logging one BadPrUrl per sweep forever (#87)
    match = issue_runs.PR_URL_RE.match(url)
    assert match is not None, url
    assert match.group(1) == "example-org/taskboy"
    assert match.group(2) == "9"


def test_pr_url_re_still_rejects_non_github_urls():
    assert issue_runs.PR_URL_RE.match("not-a-github-url") is None
    assert issue_runs.PR_URL_RE.match("https://gitlab.com/example-org/taskboy/pull/9") is None
