import pytest

from taskboy.models import BLOCKED, CANCELLED, COMPLETED, FAILED, QUEUED, RECEIVED, RUNNING
from taskboy.task_actions import cancel_task, decide_permission, retry_task
from tests.conftest import make_config


def _blocked_task_with_request(store, make_task, kind="tool", target="mcp__jira__add_comment"):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED)
    store.transition(task.task_id, QUEUED, RUNNING, session_id="sess-1")
    store.request_permission(task.task_id, kind, target, "need it")
    store.transition(task.task_id, RUNNING, BLOCKED, "needs permission")
    return store.get_task(task.task_id)


@pytest.mark.asyncio
async def test_grant_permission_resumes_blocked_task(store, make_task, notifier):
    task = _blocked_task_with_request(store, make_task)
    updated, status = await decide_permission(store, notifier, task.task_id, "tool", "mcp__jira__add_comment", "granted", "boss@example.com")
    assert status == "granted"
    assert updated.state == QUEUED  # resumed so the runner picks it up again
    assert updated.resume_session_id == "sess-1"
    assert store.granted_permissions_for(task.task_id)["tools"] == ["mcp__jira__add_comment"]
    assert "permission_decision" in [event["kind"] for event in store.events_for(task.task_id)]


@pytest.mark.asyncio
async def test_deny_permission_leaves_task_blocked(store, make_task, notifier):
    task = _blocked_task_with_request(store, make_task)
    updated, status = await decide_permission(store, notifier, task.task_id, "tool", "mcp__jira__add_comment", "denied", "boss@example.com")
    assert status == "denied"
    assert updated.state == BLOCKED
    assert store.granted_permissions_for(task.task_id)["tools"] == []


@pytest.mark.asyncio
async def test_deny_permission_on_issue_backed_task_reopens_issue_and_cancels(store, make_task, notifier):
    # a denied permission request must not leave the issue stranded in_progress forever (#76 review)
    task = _blocked_task_with_request(store, make_task)
    issue = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    store.decide_issue(issue["id"], "approved", "boss")
    store.start_issue(issue["id"], task.task_id, "the spec")

    updated, status = await decide_permission(store, notifier, task.task_id, "tool", "mcp__jira__add_comment", "denied", "boss@example.com")

    assert status == "denied"
    assert updated.state == CANCELLED
    reopened = store.get_issue(issue["id"])
    assert reopened["status"] == "proposed"
    assert reopened["task_id"] is None
    comments = store.list_issue_comments(issue["id"])
    assert len(comments) == 1 and task.task_id in comments[0]["body"]
    assert ("issue_blocked", task.task_id, issue["id"]) in notifier.calls


@pytest.mark.asyncio
async def test_deny_permission_with_another_pending_request_stays_blocked(store, make_task, notifier):
    # a second still-pending request means decide_permission's resume path may still need this exact task_id (#76 review)
    task = _blocked_task_with_request(store, make_task)
    store.request_permission(task.task_id, "tool", "mcp__github__close_pull_request", "also need it")
    issue = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    store.decide_issue(issue["id"], "approved", "boss")
    store.start_issue(issue["id"], task.task_id, "the spec")

    updated, status = await decide_permission(store, notifier, task.task_id, "tool", "mcp__jira__add_comment", "denied", "boss@example.com")

    assert status == "denied"
    assert updated.state == BLOCKED
    assert store.get_issue(issue["id"])["status"] == "in_progress"


@pytest.mark.asyncio
async def test_grant_permission_without_pending_request_is_noop(store, make_task, notifier):
    task = _blocked_task_with_request(store, make_task)
    await decide_permission(store, notifier, task.task_id, "tool", "mcp__jira__add_comment", "granted", "a")
    again, status = await decide_permission(store, notifier, task.task_id, "tool", "mcp__jira__add_comment", "granted", "b")
    assert status == "no pending request"


@pytest.mark.asyncio
async def test_decide_permission_rejects_bad_decision(store, make_task, notifier):
    task = _blocked_task_with_request(store, make_task)
    _, status = await decide_permission(store, notifier, task.task_id, "tool", "mcp__jira__add_comment", "maybe", "a")
    assert status == "decision must be granted or denied"


@pytest.mark.asyncio
async def test_decide_permission_missing_task(store, notifier):
    task, status = await decide_permission(store, notifier, "t20260101-deadbeef", "tool", "x", "granted", "a")
    assert task is None
    assert status == "not found"


def test_cancel_running_task(store, make_task):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED)
    store.transition(task.task_id, QUEUED, RUNNING)
    cancelled, status = cancel_task(store, task.task_id, "boss@example.com")
    kinds = [event["kind"] for event in store.events_for(task.task_id)]
    assert status == "cancelled"
    assert cancelled.state == CANCELLED
    assert cancelled.finished_at is not None
    assert "operator_action" in kinds


def test_cancel_coordinator_releases_reserved_issues(store, make_task):
    row = store.record_issue("cancelled-coordinator", "example-org/taskboy", "summary", "organization", "details", 50)
    store.decide_issue(row["id"], "approved", "boss")
    task = make_task(text="/implementapprovedissues")
    store.reserve_issues(task.task_id, 1)

    cancelled, status = cancel_task(store, task.task_id, "boss@example.com")

    assert status == "cancelled"
    assert cancelled.state == CANCELLED
    assert store.get_issue(row["id"])["status"] == "approved"
    assert store.get_issue(row["id"])["reserved_by"] is None


def test_cancel_issue_backed_task_reopens_its_issue(store, make_task):
    # an operator cancel is a terminal transition too — it must not strand the issue in_progress either (#76 review)
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED)
    store.transition(task.task_id, QUEUED, RUNNING)
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "the spec")

    cancelled, status = cancel_task(store, task.task_id, "boss@redzone.co")

    assert status == "cancelled"
    assert cancelled.state == CANCELLED
    reopened = store.get_issue(row["id"])
    assert reopened["status"] == "proposed"
    assert reopened["task_id"] is None


def test_cancel_terminal_task_is_noop(store, make_task):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED)
    store.transition(task.task_id, QUEUED, RUNNING)
    store.transition(task.task_id, RUNNING, COMPLETED)
    unchanged, status = cancel_task(store, task.task_id, "boss@example.com")
    assert status == "already completed"
    assert unchanged.state == COMPLETED


def test_cancel_missing_task(store):
    task, status = cancel_task(store, "t20260101-deadbeef", "boss@example.com")
    assert task is None
    assert status == "not found"


@pytest.mark.asyncio
async def test_retry_failed_task_creates_child(store, make_task, notifier):
    task = make_task("fix the flaky test")
    store.transition(task.task_id, RECEIVED, FAILED, "boom", error="boom")
    retried, status = await retry_task(store, make_config(), notifier, task.task_id, "boss@example.com")
    assert status == "created"
    assert retried.parent_task_id == task.task_id
    assert retried.request_text == "fix the flaky test"
    assert retried.slack_channel_id == task.slack_channel_id
    assert "ack" in notifier.kinds()


@pytest.mark.asyncio
async def test_retry_rejects_an_issue_backed_task(store, make_task, notifier):
    # retrying would re-run /spec2pr against the issue store.transition already handed back to `proposed`
    # with no spec, which just re-blocks it immediately — re-approving the issue is the correct path (#76)
    task = make_task("/spec2pr 42")
    store.transition(task.task_id, RECEIVED, FAILED, "boom", error="boom")
    same, status = await retry_task(store, make_config(), notifier, task.task_id, "boss@redzone.co")
    assert status == "cannot retry an issue-backed task — re-approve its issue instead"
    assert same.task_id == task.task_id
    assert "ack" not in notifier.kinds()


@pytest.mark.asyncio
async def test_retry_rejects_non_terminal_task(store, make_task, notifier):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED)
    same, status = await retry_task(store, make_config(), notifier, task.task_id, "boss@example.com")
    assert status == "cannot retry queued"
    assert same.task_id == task.task_id
