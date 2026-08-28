import dataclasses
from unittest.mock import patch

import pytest

from taskboy.models import BLOCKED, CANCELLED, COMPLETED, FAILED, QUEUED, RECEIVED, RUNNING
from taskboy.store import IllegalTransition, Store, TransitionRaced


def test_create_task_is_idempotent_per_slack_message(store):
    task1, created1 = store.create_task(slack_team_id="T1", slack_channel_id="C1", slack_thread_ts="100.1", slack_message_ts="100.1", slack_user_id="U1", request_text="fix the bug")
    task2, created2 = store.create_task(slack_team_id="T1", slack_channel_id="C1", slack_thread_ts="100.1", slack_message_ts="100.1", slack_user_id="U1", request_text="fix the bug")
    assert created1 is True
    assert created2 is False
    assert task1.task_id == task2.task_id
    assert task1.state == RECEIVED
    assert store.count_tasks(RECEIVED) == 1


def test_thread_context_is_persisted_and_redacted(store):
    task, _ = store.create_task(
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_thread_ts="100.1",
        slack_message_ts="100.2",
        slack_user_id="U1",
        request_text="follow up",
        thread_context="earlier token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    )
    assert task.thread_context == "earlier token [redacted]"


def test_task_persona_persists(store):
    task, created = store.create_task(
        slack_team_id="github",
        slack_channel_id="",
        slack_thread_ts="reviewer:org/a#1@abc",
        slack_message_ts="reviewer:org/a#1@abc",
        slack_user_id="github",
        request_text="/review url",
        persona="reviewer",
    )
    assert created is True
    assert task.persona == "reviewer"
    assert store.get_task(task.task_id).persona == "reviewer"


def test_task_schedule_name_persists(store):
    task, created = store.create_task(
        slack_team_id="github",
        slack_channel_id="C-DEBUG",
        slack_thread_ts="schedule:1@abc",
        slack_message_ts="schedule:1@abc",
        slack_user_id="cli",
        request_text="/discoverissues example-org/taskboy",
        schedule_name="Discover issues (daily)",
    )
    assert created is True
    assert task.schedule_name == "Discover issues (daily)"
    assert store.get_task(task.task_id).schedule_name == "Discover issues (daily)"


def test_has_active_main_task_referencing(store, make_task):
    assert store.has_active_main_task_referencing("github.com/org/a/pull/7") is False

    red_task = make_task("address review comments on https://github.com/org/a/pull/7")
    assert store.has_active_main_task_referencing("github.com/org/a/pull/7") is True
    assert store.has_active_main_task_referencing("github.com/org/a/pull/9") is False

    # a blue-persona task referencing the same PR does not count as an active red task
    store.transition(red_task.task_id, RECEIVED, CANCELLED, "test cleanup")
    make_task("/review https://github.com/org/a/pull/7", persona="reviewer")
    assert store.has_active_main_task_referencing("github.com/org/a/pull/7") is False


def test_has_active_main_task_referencing_matches_pr_as_bounded_token(store, make_task):
    # a task on PR #70 must not be treated as referencing PR #7 (substring over-suppression)
    make_task("address review comments on https://github.com/org/a/pull/70")
    assert store.has_active_main_task_referencing("https://github.com/org/a/pull/70") is True
    assert store.has_active_main_task_referencing("https://github.com/org/a/pull/7") is False


def test_has_active_main_task_referencing_excludes_blocked(store, make_task):
    task = make_task("address review comments on https://github.com/org/a/pull/7")
    store.transition(task.task_id, RECEIVED, BLOCKED, "waiting on requester")
    assert store.has_active_main_task_referencing("github.com/org/a/pull/7") is False


def test_task_by_intake_key(store, make_task):
    task = make_task("do the thing")
    found = store.task_by_intake_key(task.slack_team_id, task.slack_channel_id, task.slack_message_ts)
    assert found is not None and found.task_id == task.task_id
    assert store.task_by_intake_key("T1", "C1", "nope") is None


def test_has_active_main_task_referencing_include_reviewer(store, make_task):
    assert store.has_active_main_task_referencing("github.com/org/a/pull/7", include_reviewer=True) is False

    reviewer_task = make_task("/review https://github.com/org/a/pull/7", persona="reviewer")
    assert store.has_active_main_task_referencing("github.com/org/a/pull/7") is False  # blue is invisible without the flag
    assert store.has_active_main_task_referencing("github.com/org/a/pull/7", include_reviewer=True) is True
    assert store.has_active_main_task_referencing("github.com/org/a/pull/70", include_reviewer=True) is False  # still a bounded-token match

    store.transition(reviewer_task.task_id, RECEIVED, CANCELLED, "test cleanup")
    assert store.has_active_main_task_referencing("github.com/org/a/pull/7", include_reviewer=True) is False


def test_slack_event_dedup(store):
    assert store.slack_event_seen("Ev123") is False
    assert store.slack_event_seen("Ev123") is True
    assert store.slack_event_seen("Ev456") is False


def test_transition_happy_path_records_events(store, make_task):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED, "classified", task_type="investigation", persona="reviewer")
    store.transition(task.task_id, QUEUED, RUNNING, "dispatched")
    final = store.transition(task.task_id, RUNNING, COMPLETED, "runner finished", result_summary="done", cost_usd=0.5, num_turns=3)
    assert final.state == COMPLETED
    assert final.task_type == "investigation"
    assert final.persona == "reviewer"
    assert final.result_summary == "done"
    assert final.cost_usd == 0.5
    kinds = [event["kind"] for event in store.events_for(task.task_id)]
    assert kinds == ["intake", "state_change", "state_change", "state_change"]


def test_illegal_transition_rejected(store, make_task):
    task = make_task()
    with pytest.raises(IllegalTransition):
        store.transition(task.task_id, RECEIVED, COMPLETED, "skipping the lifecycle")
    with pytest.raises(IllegalTransition):
        store.transition(task.task_id, COMPLETED, RUNNING, "resurrecting")


def test_raced_transition_rejected(store, make_task):
    task = make_task()
    # row is in received, but the caller believes it's running: legal edge, wrong reality
    with pytest.raises(TransitionRaced):
        store.transition(task.task_id, RUNNING, COMPLETED, "stale caller")
    current = store.get_task(task.task_id)
    assert current.state == RECEIVED


def test_transition_rejects_unknown_fields(store, make_task):
    task = make_task()
    with pytest.raises(ValueError):
        store.transition(task.task_id, RECEIVED, QUEUED, "bad", state="completed")


def test_cancel_from_any_non_terminal_state(store, make_task):
    task = make_task()
    cancelled = store.transition(task.task_id, RECEIVED, CANCELLED, "cancelled via cli")
    assert cancelled.state == CANCELLED


def test_artifact_uniqueness_survives_retries(store, make_task):
    task = make_task()
    assert store.add_artifact(task.task_id, "pull_request", "org/repo#12", "https://github.com/org/repo/pull/12") is True
    assert store.add_artifact(task.task_id, "pull_request", "org/repo#12", "https://github.com/org/repo/pull/12") is False
    assert len(store.artifacts_for(task.task_id)) == 1


def test_usage_and_meta(store, make_task):
    task = make_task()
    store.add_usage(task.task_id, "classifier", "claude-haiku-4-5", input_tokens=100, output_tokens=20, cost_usd=0.001)
    store.meta_set("intake_paused", "1")
    assert store.meta_get("intake_paused") == "1"
    store.meta_set("intake_paused", "0")
    assert store.meta_get("intake_paused") == "0"
    assert store.meta_get("missing") is None


def test_rate_limit_windows_insert_and_upsert(store):
    with patch("taskboy.store.utcnow", side_effect=["2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00", "2026-01-01T00:02:00+00:00"]):
        store.record_rate_limit("five_hour", "allowed", None, None)
        store.record_rate_limit("seven_day", "allowed_warning", 0.4, 200)
        store.record_rate_limit("five_hour", "rejected", 1.0, 100)

    assert store.rate_limit_windows() == [
        {"rate_limit_type": "five_hour", "status": "rejected", "utilization": 1.0, "resets_at": 100, "observed_at": "2026-01-01T00:02:00+00:00"},
        {"rate_limit_type": "seven_day", "status": "allowed_warning", "utilization": 0.4, "resets_at": 200, "observed_at": "2026-01-01T00:01:00+00:00"},
    ]


def test_next_queued_is_oldest_first(store, make_task):
    first = make_task("first")
    second = make_task("second")
    store.transition(first.task_id, RECEIVED, QUEUED, "classified")
    store.transition(second.task_id, RECEIVED, QUEUED, "classified")
    queued = store.next_queued()
    assert queued.task_id == first.task_id


def test_next_queued_skips_task_gated_by_not_before(store, make_task):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED, "classified")
    store.set_fields(task.task_id, not_before="2026-01-01T01:00:00+00:00")
    with patch("taskboy.store.utcnow", return_value="2026-01-01T00:00:00+00:00"):
        assert store.next_queued() is None
    with patch("taskboy.store.utcnow", return_value="2026-01-01T02:00:00+00:00"):
        queued = store.next_queued()
    assert queued is not None and queued.task_id == task.task_id


def test_next_queued_unaffected_when_not_before_is_null(store, make_task):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED, "classified")
    queued = store.next_queued()
    assert queued is not None and queued.task_id == task.task_id


def test_reopen_preserves_state(tmp_path, make_task, store):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED, "classified")
    path = str(tmp_path / "test.db")
    reopened = Store(path)
    try:
        persisted = reopened.get_task(task.task_id)
        assert persisted is not None
        assert persisted.state == QUEUED
    finally:
        reopened.close()


def test_failed_transition_records_error(store, make_task):
    task = make_task()
    failed = store.transition(task.task_id, RECEIVED, FAILED, "queue full", error="queue_full")
    assert failed.state == FAILED
    assert failed.error == "queue_full"


def test_latest_task_in_thread(store, make_task):
    first, _ = store.create_task(slack_team_id="T1", slack_channel_id="C1", slack_thread_ts="9.9", slack_message_ts="9.9", slack_user_id="U1", request_text="root")
    second, _ = store.create_task(slack_team_id="T1", slack_channel_id="C1", slack_thread_ts="9.9", slack_message_ts="10.1", slack_user_id="U1", request_text="follow-up")
    latest = store.latest_task_in_thread("C1", "9.9")
    assert latest.task_id == second.task_id
    assert store.latest_task_in_thread("C1", "no-such-thread") is None


def test_base_schema_creates_every_table_and_column(store):
    from taskboy.store import MIGRATIONS

    assert store.meta_get("schema_version") == str(len(MIGRATIONS))
    store.record_intake_denial("T1", "C1", "U1", "not allowed")
    for column in ("thread_context", "reply", "persona", "effort", "effort_override", "schedule_name", "not_before"):
        assert any(row["name"] == column for row in store.conn.execute("PRAGMA table_info(tasks)")), column
    assert any(row["name"] == "effort" for row in store.conn.execute("PRAGMA table_info(schedules)"))
    tables = {row["name"] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"tasks", "slack_events", "task_events", "artifacts", "usage", "intake_denials", "slack_users", "errors", "admin_events", "permission_requests", "rate_limits", "task_questions", "task_feedback", "schedules", "issues", "issue_comments", "issue_attachments"} <= tables
    index_names = {row["name"] for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'tasks'")}
    assert {"idx_tasks_state", "idx_tasks_thread"} <= index_names


def test_appended_migrations_apply_to_existing_databases(tmp_path, monkeypatch):
    import taskboy.store as store_module

    path = str(tmp_path / "x.db")
    Store(path).close()  # base schema applied, version = len(MIGRATIONS)
    monkeypatch.setattr(store_module, "MIGRATIONS", store_module.MIGRATIONS + ["CREATE TABLE future_feature (id INTEGER PRIMARY KEY);"])
    upgraded = Store(path)
    try:
        assert upgraded.meta_get("schema_version") == str(len(store_module.MIGRATIONS))
        assert upgraded.conn.execute("SELECT name FROM sqlite_master WHERE name = 'future_feature'").fetchone()
    finally:
        upgraded.close()


def test_issues_table_accepts_implementation_queued_and_rejects_garbage_status(store):
    import sqlite3

    now = "2026-01-01T00:00:00+00:00"
    store.conn.execute(
        "INSERT INTO issues (dedupe_key, repo, summary, issue_type, details, priority, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'implementation_queued', ?, ?)",
        ("q", "example-org/taskboy", "s", "t", "d", 50, now, now),
    )
    store.conn.commit()
    assert store.conn.execute("SELECT status FROM issues WHERE dedupe_key = 'q'").fetchone()["status"] == "implementation_queued"
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO issues (dedupe_key, repo, summary, issue_type, details, priority, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'bogus', ?, ?)",
            ("bad", "example-org/taskboy", "s", "t", "d", 50, now, now),
        )


def test_finish_issue_accepts_in_review_and_resolves_it(store, make_task):
    task = make_task()
    row = store.record_issue("x", "example-org/taskboy", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "spec")

    in_review = store.finish_issue(row["id"], "in_review", "https://github.com/example-org/taskboy/pull/9")
    assert in_review["status"] == "in_review" and in_review["pr_url"].endswith("/pull/9")

    # the sync process resolves an in_review row the same way spec2pr resolves an in_progress one
    resolved = store.finish_issue(row["id"], "done", "https://github.com/example-org/taskboy/pull/9")
    assert resolved["status"] == "done"

    with pytest.raises(ValueError):
        store.finish_issue(row["id"], "garbage")


def test_reopen_linked_issue_clears_the_claim_and_returns_to_proposed(store, make_task):
    task = make_task()
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "the spec")

    reopened = store.reopen_linked_issue(task)
    assert reopened["status"] == "proposed"
    assert reopened["task_id"] is None
    assert reopened["spec"] is None
    assert reopened["reserved_by"] is None
    assert reopened["decided_by"] is None
    assert reopened["decided_at"] is None


def test_reopen_linked_issue_refuses_terminal_or_not_yet_claimed_statuses(store, make_task):
    task = make_task()
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    # proposed: never claimed, nothing to reopen
    assert store.reopen_linked_issue(task) is None

    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "spec")
    store.finish_issue(row["id"], "failed")
    # failed keeps its one remaining meaning (sync_in_review saw a PR closed unmerged) — not reopenable
    assert store.reopen_linked_issue(task) is None
    assert store.get_issue(row["id"])["status"] == "failed"


def test_reopen_linked_issue_prefers_the_real_error_over_a_stale_blocked_reason(store, make_task):
    # blocked_reason is never cleared on resume, so a task that blocked then later failed for a different
    # reason must not have its stale block reason quoted instead of the real error (#76 review)
    task = make_task()
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "the spec")
    stale = dataclasses.replace(task, blocked_reason="needs permission for bash", error="runner crashed: boom")

    store.reopen_linked_issue(stale)

    comments = store.list_issue_comments(row["id"])
    assert len(comments) == 1
    assert "runner crashed: boom" in comments[0]["body"]
    assert "needs permission" not in comments[0]["body"]


def test_reopen_stranded_issues_catches_one_left_behind_a_task_that_finished_before_the_hook_ran(store, make_task):
    # a startup safety net for rows that predate transition()'s TERMINAL_STATES hook, or a crash between a
    # task landing terminal and this reconcile pass getting a chance to run (#76)
    task = make_task()
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "the spec")
    # move the task straight to failed without going through transition(), simulating a pre-hook stranding
    store.conn.execute("UPDATE tasks SET state = 'failed' WHERE task_id = ?", (task.task_id,))
    store.conn.commit()

    assert store.reopen_stranded_issues() == 1
    assert store.get_issue(row["id"])["status"] == "proposed"


def test_reopen_stranded_issues_leaves_a_still_in_progress_task_alone(store, make_task):
    task = make_task()
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "the spec")

    assert store.reopen_stranded_issues() == 0
    assert store.get_issue(row["id"])["status"] == "in_progress"


def test_transition_to_completed_reopens_an_issue_left_in_progress(store, make_task):
    # finish_issue is supposed to close the issue before a task completes; if it never ran, transition()'s
    # hook must still catch it — reopen_linked_issue is a no-op once finish_issue already moved it on (#76 review)
    task = make_task()
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "the spec")

    store.transition(task.task_id, RECEIVED, QUEUED, "classified")
    store.transition(task.task_id, QUEUED, RUNNING, "dispatched")
    store.transition(task.task_id, RUNNING, COMPLETED, "runner finished", result_summary="done")

    assert store.get_issue(row["id"])["status"] == "proposed"


def test_reopen_stranded_issues_catches_one_behind_a_completed_task_that_never_closed_it(store, make_task):
    # a startup safety net for a completed-without-finish_issue row that predates transition()'s hook (#76 review)
    task = make_task()
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "the spec")
    # move the task straight to completed without going through transition(), simulating a pre-hook stranding
    store.conn.execute("UPDATE tasks SET state = 'completed' WHERE task_id = ?", (task.task_id,))
    store.conn.commit()

    assert store.reopen_stranded_issues() == 1
    assert store.get_issue(row["id"])["status"] == "proposed"


def test_reopen_stranded_issues_catches_one_with_no_linked_task(store):
    # a crash between start_issue and link_issue_task can leave task_id NULL while status is still in_progress —
    # the plain inner join used to miss this row entirely (#76 review)
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], None, "the spec")

    assert store.reopen_stranded_issues() == 1
    reopened = store.get_issue(row["id"])
    assert reopened["status"] == "proposed"
    comments = store.list_issue_comments(row["id"])
    assert len(comments) == 1 and "no linked task" in comments[0]["body"]


def test_issue_for_task_looks_up_by_linked_task_id(store, make_task):
    task = make_task()
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    assert store.issue_for_task(task.task_id) is None
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "spec")
    assert store.issue_for_task(task.task_id)["id"] == row["id"]


def test_transition_to_any_non_completed_terminal_state_reopens_a_linked_issue(store, make_task):
    # centralized in transition()'s TERMINAL_STATES hook so no call site (reconcile, classification, an operator
    # cancel) can forget to hand a linked issue back (#76 review)
    task = make_task()
    row = store.record_issue("x", "redzone-co/agent-red", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "the spec")

    store.transition(task.task_id, RECEIVED, FAILED, "recovery: retry attempts exhausted", error="task was interrupted too many times")

    reopened = store.get_issue(row["id"])
    assert reopened["status"] == "proposed"
    assert reopened["task_id"] is None
    comments = store.list_issue_comments(row["id"])
    assert len(comments) == 1 and "task was interrupted too many times" in comments[0]["body"]


def test_quick_answer_is_born_terminal_and_idempotent(store):
    kwargs = dict(
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_thread_ts="1.1",
        slack_message_ts="1.1",
        slack_user_id="U1",
        request_text="what is 429?",
        answer_text="Too Many Requests",
        model_alias="haiku",
        model_id="claude-haiku",
        latency_s=0.1,
    )
    first = store.record_quick_answer(**kwargs)
    second = store.record_quick_answer(**kwargs)
    assert first.task_id == second.task_id
    assert first.state == COMPLETED
    assert first.routing_rationale == "quick-answer"
    assert store.count_tasks(RECEIVED) == 0
    assert store.tasks_in_state(RECEIVED) == []
    assert store.next_queued() is None
    assert [event["kind"] for event in store.events_for(first.task_id)] == ["intake", "quick_answer"]


def test_analytics_tables_and_debug_fields_round_trip(store):
    task, _ = store.create_task(
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_thread_ts="1",
        slack_message_ts="1",
        slack_user_id="U1",
        request_text="fix it",
        debug_thread_ts="9.9",
        debug_permalink="https://slack.test/debug",
    )
    store.set_fields(task.task_id, reply="Handled it.")
    updated = store.get_task(task.task_id)
    assert (updated.reply, updated.debug_thread_ts, updated.debug_permalink) == ("Handled it.", "9.9", "https://slack.test/debug")
    profile = store.upsert_slack_user("U1", team_id="T1", real_name="Ada", email="ada@example.test", is_bot=0)
    assert profile["real_name"] == "Ada"
    assert store.get_slack_user("U1")["email"] == "ada@example.test"
    store.add_error("runner", "timeout", "token ghp_abcdefghijklmnopqrstuvwxyz0123456789", task_id=task.task_id, context={"attempt": 1})
    error = store.recent_errors(1)[0]
    assert error["task_id"] == task.task_id
    assert "ghp_" not in error["message"]


def test_permission_request_lifecycle(store, make_task):
    task = make_task()
    row = store.request_permission(task.task_id, "tool", "mcp__jira__add_comment", "need to post findings")
    assert row["status"] == "pending"
    assert store.granted_permissions_for(task.task_id) == {"tools": [], "repos": []}
    decided = store.decide_permission_request(task.task_id, "tool", "mcp__jira__add_comment", "granted", "boss@example.com")
    assert decided["status"] == "granted"
    assert decided["decided_by"] == "boss@example.com"
    assert store.granted_permissions_for(task.task_id) == {"tools": ["mcp__jira__add_comment"], "repos": []}


def test_permission_request_is_retry_safe_and_reopens(store, make_task):
    task = make_task()
    store.request_permission(task.task_id, "repo", "example-org/core", "first reason")
    store.decide_permission_request(task.task_id, "repo", "example-org/core", "denied", "boss@example.com")
    reopened = store.request_permission(task.task_id, "repo", "example-org/core", "second reason")
    assert reopened["status"] == "pending"  # a fresh request re-opens a decided one
    assert len(store.permission_requests_for(task.task_id)) == 1  # unique per (task, kind, target)


def test_decide_permission_request_only_acts_on_pending(store, make_task):
    task = make_task()
    store.request_permission(task.task_id, "tool", "mcp__jira__add_comment", "why")
    store.decide_permission_request(task.task_id, "tool", "mcp__jira__add_comment", "granted", "a")
    again = store.decide_permission_request(task.task_id, "tool", "mcp__jira__add_comment", "denied", "b")
    assert again is None  # already decided
    assert store.decide_permission_request("t20260101-deadbeef", "tool", "x", "granted", "a") is None


def test_permission_request_reason_is_redacted(store, make_task):
    task = make_task()
    store.request_permission(task.task_id, "tool", "mcp__jira__add_comment", "token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    assert "ghp_" not in store.permission_requests_for(task.task_id)[0]["reason"]


def test_preclassification_round_trips_at_creation(store):
    classification = {"task_type": "question", "complexity": "trivial"}
    task, created = store.create_task(
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_thread_ts="1",
        slack_message_ts="1",
        slack_user_id="U1",
        request_text="question",
        pre_classification=classification,
    )
    assert created is True
    assert __import__("json").loads(task.classification_json) == classification


def test_last_event_ts_filters_in_sql_and_returns_newest(store, make_task):
    task = make_task()
    store.add_event(task.task_id, "state_change", {"to": "queued"})
    first = store.events_for(task.task_id)[-1]["ts"]
    store.add_event(task.task_id, "state_change", {"to": "running"})
    store.add_event(task.task_id, "state_change", {"to": "queued"})
    latest = store.events_for(task.task_id)[-1]["ts"]

    assert store.last_event_ts(task.task_id, "state_change", "to", "queued") == latest
    assert store.last_event_ts(task.task_id, "state_change", "to", "missing") is None
    assert first <= latest


def test_questions_round_trip_pending_semantics_and_redaction(store, make_task):
    task = make_task()
    assert store.pending_questions_for(task.task_id) is None
    assert store.answer_questions(task.task_id, "1. staging", "U1") is None  # nothing pending
    store.ask_questions(task.task_id, "1. Which env?\n2. token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    pending = store.pending_questions_for(task.task_id)
    assert pending is not None
    assert "ghp_" not in pending["questions"]
    answered = store.answer_questions(task.task_id, "1. staging, key ghp_abcdefghijklmnopqrstuvwxyz0123456789", "U1")
    assert answered is not None
    assert answered["answered_by"] == "U1"
    assert "ghp_" not in answered["answer_text"]
    assert store.pending_questions_for(task.task_id) is None
    assert [row["id"] for row in store.answered_questions_for(task.task_id)] == [answered["id"]]
    # a second round opens independently and questions_for sees both
    store.ask_questions(task.task_id, "1. Anything else?")
    assert store.pending_questions_for(task.task_id) is not None
    assert len(store.questions_for(task.task_id)) == 2
    assert len(store.answered_questions_for(task.task_id)) == 1


def test_feedback_upsert_listing_and_redaction(store, make_task):
    task = make_task()
    store.add_feedback(task.task_id, "person@example.com", 2, "missed the point")
    store.add_feedback(task.task_id, "other@example.com", 5, "great, but token ghp_abcdefghijklmnopqrstuvwxyz0123456789 leaked")
    revised = store.add_feedback(task.task_id, "person@example.com", 4, "better after retry")
    rows = store.feedback_for(task.task_id)
    assert len(rows) == 2
    assert revised["rating"] == 4
    assert revised["comment"] == "better after retry"
    other = next(row for row in rows if row["submitted_by"] == "other@example.com")
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in (other["comment"] or "")
    assert any(event["kind"] == "feedback" for event in store.events_for(task.task_id))
    assert {row["submitted_by"] for row in store.recent_feedback()} == {"person@example.com", "other@example.com"}
