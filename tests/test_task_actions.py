import pytest

from agent_harness.models import BLOCKED, CANCELLED, COMPLETED, FAILED, QUEUED, RECEIVED, RUNNING
from agent_harness.task_actions import cancel_task, decide_permission, retry_task
from tests.conftest import make_config


def _blocked_task_with_request(store, make_task, kind="tool", target="mcp__jira__add_comment"):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED)
    store.transition(task.task_id, QUEUED, RUNNING, session_id="sess-1")
    store.request_permission(task.task_id, kind, target, "need it")
    store.transition(task.task_id, RUNNING, BLOCKED, "needs permission")
    return store.get_task(task.task_id)


def test_grant_permission_resumes_blocked_task(store, make_task):
    task = _blocked_task_with_request(store, make_task)
    updated, status = decide_permission(store, task.task_id, "tool", "mcp__jira__add_comment", "granted", "boss@example.com")
    assert status == "granted"
    assert updated.state == QUEUED  # resumed so the runner picks it up again
    assert updated.resume_session_id == "sess-1"
    assert store.granted_permissions_for(task.task_id)["tools"] == ["mcp__jira__add_comment"]
    assert "permission_decision" in [event["kind"] for event in store.events_for(task.task_id)]


def test_deny_permission_leaves_task_blocked(store, make_task):
    task = _blocked_task_with_request(store, make_task)
    updated, status = decide_permission(store, task.task_id, "tool", "mcp__jira__add_comment", "denied", "boss@example.com")
    assert status == "denied"
    assert updated.state == BLOCKED
    assert store.granted_permissions_for(task.task_id)["tools"] == []


def test_grant_permission_without_pending_request_is_noop(store, make_task):
    task = _blocked_task_with_request(store, make_task)
    decide_permission(store, task.task_id, "tool", "mcp__jira__add_comment", "granted", "a")
    again, status = decide_permission(store, task.task_id, "tool", "mcp__jira__add_comment", "granted", "b")
    assert status == "no pending request"


def test_decide_permission_rejects_bad_decision(store, make_task):
    task = _blocked_task_with_request(store, make_task)
    _, status = decide_permission(store, task.task_id, "tool", "mcp__jira__add_comment", "maybe", "a")
    assert status == "decision must be granted or denied"


def test_decide_permission_missing_task(store):
    task, status = decide_permission(store, "t20260101-deadbeef", "tool", "x", "granted", "a")
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
    row = store.record_issue("cancelled-coordinator", "example-org/agent-harness", "summary", "organization", "details", 50)
    store.decide_issue(row["id"], "approved", "boss")
    task = make_task(text="/implementapprovedissues")
    store.reserve_issues(task.task_id, 1)

    cancelled, status = cancel_task(store, task.task_id, "boss@example.com")

    assert status == "cancelled"
    assert cancelled.state == CANCELLED
    assert store.get_issue(row["id"])["status"] == "approved"
    assert store.get_issue(row["id"])["reserved_by"] is None


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
async def test_retry_rejects_non_terminal_task(store, make_task, notifier):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED)
    same, status = await retry_task(store, make_config(), notifier, task.task_id, "boss@example.com")
    assert status == "cannot retry queued"
    assert same.task_id == task.task_id
