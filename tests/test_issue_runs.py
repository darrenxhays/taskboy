from unittest.mock import AsyncMock

import pytest

from taskboy import issue_runs
from taskboy.adapters.github_api import GitHubStatusError


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
