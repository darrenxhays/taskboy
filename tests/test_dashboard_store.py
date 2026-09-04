from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from taskboy.audit import ship_admin_once, verify_admin_chain
from taskboy.models import FAILED, QUEUED, RECEIVED


def _iso(moment):
    return moment.isoformat(timespec="seconds")


def test_admin_event_chain_verifies_and_detects_tampering(store):
    store.add_admin_event("boss@example.com", "edit", "/etc/taskboy/config.yaml", "success", {"new_hash": "abc"})
    store.add_admin_event("boss@example.com", "task_cancel", "t20260101-aaaaaaaa", "cancelled")
    intact, checked = verify_admin_chain(store)
    assert intact and checked == 2
    store.conn.execute("UPDATE admin_events SET outcome = 'denied' WHERE id = 1")
    store.conn.commit()
    intact, _ = verify_admin_chain(store)
    assert not intact


def test_admin_event_detail_is_redacted(store):
    store.add_admin_event("boss@example.com", "edit", "config", "rejected", {"reason": "ghp_0123456789abcdef0123456789abcdef1234"})
    event = store.admin_events(1)[0]
    assert "ghp_" not in event["detail_json"]


def test_list_tasks_filters_state_and_query(store, make_task):
    fixing = make_task("fix the login bug")
    make_task("write release notes")
    store.transition(fixing.task_id, RECEIVED, QUEUED)
    by_state = store.list_tasks(state=QUEUED)
    by_query = store.list_tasks(query="release")
    assert [task.task_id for task in by_state] == [fixing.task_id]
    assert len(by_query) == 1 and by_query[0].request_text == "write release notes"


def test_children_of_and_task_counts(store, make_task):
    parent = make_task("parent")
    child = make_task("child", parent_task_id=parent.task_id)
    store.transition(parent.task_id, RECEIVED, FAILED, "boom")
    assert [task.task_id for task in store.children_of(parent.task_id)] == [child.task_id]
    counts = store.task_counts()
    assert counts["failed"] == 1 and counts["received"] == 1


def test_event_pagination_and_kinds(store, make_task):
    task = make_task()
    for i in range(5):
        store.add_event(task.task_id, "milestone", {"n": i})
    page = store.events_for(task.task_id, limit=3, offset=0)
    assert len(page) == 3
    assert store.event_count(task.task_id) == 6  # intake + 5 milestones
    milestones = store.events_for_kinds(task.task_id, {"milestone"})
    assert len(milestones) == 5
    assert store.latest_event_id() == 6


def test_usage_windows_and_timeseries(store, make_task):
    task = make_task()
    store.add_usage(task.task_id, "subagent", "claude-fable-5-1", input_tokens=100, output_tokens=50, cost_usd=1.5)
    store.add_usage(task.task_id, "classifier", "haiku", input_tokens=10, output_tokens=5, cost_usd=0.1)
    # age the first row out of the five-hour window
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=8))
    store.conn.execute("UPDATE usage SET ts = ? WHERE model = 'claude-fable-5-1'", (old,))
    store.conn.commit()
    five_hour = store.usage_totals(since_iso=_iso(datetime.now(timezone.utc) - timedelta(hours=5)))
    all_time = store.usage_totals()
    fable_only = store.usage_totals(model="claude-fable-5-1")
    assert five_hour["input_tokens"] == 10
    assert all_time["input_tokens"] == 110
    assert fable_only["output_tokens"] == 50
    by_model = {row["model"]: row for row in store.usage_by_model()}
    assert by_model["claude-fable-5-1"]["cost_usd"] == 1.5
    series = store.usage_timeseries(_iso(datetime.now(timezone.utc) - timedelta(days=1)))
    assert any(row["model"] == "haiku" and row["total_tokens"] == 15 for row in series)


@pytest.mark.asyncio
async def test_ship_admin_once_uses_cursor(store):
    store.add_admin_event("boss@example.com", "edit", "config", "success")
    shipped = []
    with patch("taskboy.audit._put", side_effect=lambda bucket, key, body: shipped.append((bucket, key, body))):
        count = await ship_admin_once(store, "bucket")
        again = await ship_admin_once(store, "bucket")
    assert count == 1 and again == 0
    assert shipped[0][1].startswith("admin-audit/")
