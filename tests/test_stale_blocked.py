"""tests for the generic stale-BLOCKED sweep: expires tasks blocked on ask_questions or an undecided permission request (#104)."""

from datetime import datetime, timedelta, timezone

import pytest

from taskboy.models import BLOCKED, CANCELLED, FAILED, QUEUED, RECEIVED, RUNNING
from taskboy.orchestrator import expire_stale_blocked_tasks

RETENTION = {"blocked_task_max_days": 10, "blocked_task_reminder_days": 5}


def _block(store, task, reason="need an answer"):
    store.transition(task.task_id, RECEIVED, QUEUED)
    store.transition(task.task_id, QUEUED, RUNNING)
    return store.transition(task.task_id, RUNNING, BLOCKED, "waiting", blocked_reason=reason)


def _age(store, task_id, days):
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (ts, task_id))
    store.conn.commit()


def _age_event(store, task_id, kind, days):
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE task_events SET ts = ? WHERE task_id = ? AND kind = ?", (ts, task_id, kind))
    store.conn.commit()


@pytest.mark.asyncio
async def test_fresh_blocked_task_is_left_alone(store, make_task, notifier):
    task = make_task()
    _block(store, task)
    _age(store, task.task_id, 1)

    counts = await expire_stale_blocked_tasks(store, notifier, RETENTION)

    assert counts == {"reminded": 0, "expired": 0}
    assert store.get_task(task.task_id).state == BLOCKED
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_sends_one_reminder_partway_through_the_window_and_does_not_repeat(store, make_task, notifier):
    task = make_task()
    _block(store, task)
    _age(store, task.task_id, 6)  # past the 5-day reminder mark, short of the 10-day expiry

    first = await expire_stale_blocked_tasks(store, notifier, RETENTION)
    second = await expire_stale_blocked_tasks(store, notifier, RETENTION)

    assert first == {"reminded": 1, "expired": 0}
    assert second == {"reminded": 0, "expired": 0}  # already reminded — no duplicate nagging
    assert store.get_task(task.task_id).state == BLOCKED
    progress_calls = [c for c in notifier.calls if c[0] == "progress"]
    assert len(progress_calls) == 1
    assert "expires" in progress_calls[0][2]


@pytest.mark.asyncio
async def test_sends_a_fresh_reminder_after_the_task_re_blocks(store, make_task, notifier):
    # a task that got answered and blocked again must get its own reminder, not be silently carried past the
    # window by the first round's blocked_reminder event (#104)
    task = make_task()
    _block(store, task)
    _age(store, task.task_id, 6)
    await expire_stale_blocked_tasks(store, notifier, RETENTION)  # sends the first round's reminder
    _age_event(store, task.task_id, "blocked_reminder", 20)  # that reminder predates the re-block below

    store.transition(task.task_id, BLOCKED, QUEUED, "resumed: requester answered questions")
    store.transition(task.task_id, QUEUED, RUNNING)
    store.transition(task.task_id, RUNNING, BLOCKED, "waiting again", blocked_reason="need another answer")
    _age(store, task.task_id, 6)

    counts = await expire_stale_blocked_tasks(store, notifier, RETENTION)

    assert counts == {"reminded": 1, "expired": 0}
    assert store.get_task(task.task_id).state == BLOCKED


@pytest.mark.asyncio
async def test_expires_a_non_issue_backed_task_past_the_window(store, make_task, notifier):
    task = make_task()
    _block(store, task, reason="unanswered questions")
    store.ask_questions(task.task_id, "1. Which repo?")
    _age(store, task.task_id, 11)

    counts = await expire_stale_blocked_tasks(store, notifier, RETENTION)

    assert counts == {"reminded": 0, "expired": 1}
    failed = store.get_task(task.task_id)
    assert failed.state == FAILED
    assert "unanswered questions" in failed.error
    failed_calls = [c for c in notifier.calls if c[0] == "failed" and c[1] == task.task_id]
    assert len(failed_calls) == 1 and "unanswered questions" in failed_calls[0][2]


@pytest.mark.asyncio
async def test_expires_an_issue_backed_task_by_reopening_its_issue(store, make_task, notifier):
    task = make_task()
    issue = store.record_issue("x", "redzone-co/taskboy", "s", "organization", "d", 50)
    store.decide_issue(issue["id"], "approved", "boss")
    store.start_issue(issue["id"], task.task_id, "the spec")
    _block(store, task, reason="waiting for the requester to answer follow-up questions")
    _age(store, task.task_id, 11)

    counts = await expire_stale_blocked_tasks(store, notifier, RETENTION)

    assert counts == {"reminded": 0, "expired": 1}
    assert store.get_task(task.task_id).state == CANCELLED
    reopened = store.get_issue(issue["id"])
    assert reopened["status"] == "proposed"
    assert ("issue_blocked", task.task_id, issue["id"]) in notifier.calls


@pytest.mark.asyncio
async def test_does_not_expire_a_task_that_resumed_in_the_same_tick(store, make_task, notifier):
    # answers/grants resume a BLOCKED task via store.transition's from-state guard; a task that already moved on
    # must not be re-failed out from under whoever resumed it (#104)
    task = make_task()
    _block(store, task)
    _age(store, task.task_id, 11)
    store.transition(task.task_id, BLOCKED, QUEUED, "resumed: requester answered questions")

    counts = await expire_stale_blocked_tasks(store, notifier, RETENTION)

    assert counts == {"reminded": 0, "expired": 0}
    assert store.get_task(task.task_id).state == QUEUED


@pytest.mark.asyncio
async def test_pending_permission_request_expires_too(store, make_task, notifier):
    task = make_task()
    _block(store, task, reason="an undecided permission request")
    store.request_permission(task.task_id, "tool", "mcp__jira__add_comment", "need it")
    _age(store, task.task_id, 11)

    counts = await expire_stale_blocked_tasks(store, notifier, RETENTION)

    assert counts == {"reminded": 0, "expired": 1}
    failed = store.get_task(task.task_id)
    assert failed.state == FAILED
    assert "undecided permission request" in failed.error
