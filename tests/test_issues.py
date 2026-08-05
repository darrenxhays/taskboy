import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_harness.adapters.issues import EnqueueAdapter, IssuesAdapter
from agent_harness.issue_runs import fail_stalled_implementation_run
from agent_harness.models import BLOCKED, FAILED, RECEIVED, REFUSED


def rec(store, key="a", summary="s", type_="organization", details="d", priority=50):
    return store.record_issue(key, "example-org/agent-harness", summary, type_, details, priority)


def test_record_is_idempotent_while_proposed_and_frozen_after_decision(store):
    first = rec(store, key="trim-prompt", summary="trim it", priority=40)
    again = store.record_issue("trim-prompt", "example-org/agent-harness", "trim it better", "token_efficiency", "more detail", 70)
    assert again["id"] == first["id"]
    assert again["summary"] == "trim it better" and again["priority"] == 70 and again["issue_type"] == "token_efficiency"

    store.decide_issue(first["id"], "approved", "boss@example.com")
    # re-recording an approved proposal must not clobber the operator's decision or its content
    after = store.record_issue("trim-prompt", "example-org/agent-harness", "sneaky rewrite", "organization", "x", 1)
    assert after["status"] == "approved" and after["summary"] == "trim it better" and after["priority"] == 70


def test_priority_clamped(store):
    assert rec(store, key="hi", priority=999)["priority"] == 100
    assert rec(store, key="lo", priority=-5)["priority"] == 1


def test_list_ordered_by_priority(store):
    rec(store, key="low", priority=10)
    rec(store, key="high", priority=90)
    rec(store, key="mid", priority=50)
    order = [row["dedupe_key"] for row in store.list_issues()]
    assert order == ["high", "mid", "low"]
    assert store.list_issues(status="proposed") == store.list_issues()


def test_decide_guards(store):
    row = rec(store, key="x")
    assert store.decide_issue(row["id"], "denied", "boss")["status"] == "denied"
    assert store.decide_issue(row["id"], "approved", "boss")["status"] == "approved"
    with pytest.raises(ValueError):
        store.decide_issue(row["id"], "garbage", "boss")
    assert store.decide_issue(9999, "approved", "boss") is None


def test_update_allowed_while_proposed_or_approved_then_locked(store):
    row = rec(store, key="x", summary="orig summary", details="orig details")
    updated = store.update_issue(row["id"], "new summary", "new details")
    assert updated["summary"] == "new summary" and updated["details"] == "new details"

    store.decide_issue(row["id"], "approved", "boss")
    still_editable = store.update_issue(row["id"], "approved-stage summary", "approved-stage details")
    assert still_editable["summary"] == "approved-stage summary"

    store.decide_issue(row["id"], "denied", "boss")
    assert store.update_issue(row["id"], "nope", "nope") is None
    assert store.get_issue(row["id"])["summary"] == "approved-stage summary"

    assert store.update_issue(9999, "nope", "nope") is None


def test_start_and_finish_transitions(store, make_task):
    row = rec(store, key="x")
    task = make_task()
    # cannot start until approved
    assert store.start_issue(row["id"], task.task_id, "spec") is None
    store.decide_issue(row["id"], "approved", "boss")
    started = store.start_issue(row["id"], task.task_id, "the spec")
    assert started["status"] == "in_progress" and started["task_id"] == task.task_id and started["spec"] == "the spec"
    # finishing only works from in_progress
    done = store.finish_issue(row["id"], "done", "https://github.com/example-org/agent-harness/pull/9")
    assert done["status"] == "done" and done["pr_url"].endswith("/pull/9")
    assert store.finish_issue(row["id"], "done", None) is None


@pytest.mark.asyncio
async def test_record_issue_tool_requires_all_fields(store, make_task):
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])
    bad = await adapter.record_issue({"repo": "example-org/agent-harness", "summary": "s", "issue_type": "", "details": "d", "dedupe_key": "k"})
    assert bad.get("isError")
    ok = await adapter.record_issue({"repo": "example-org/agent-harness", "summary": "s", "issue_type": "organization", "details": "d", "dedupe_key": "k", "priority": 60})
    assert not ok.get("isError")
    assert store.list_issues()[0]["dedupe_key"] == "k"


@pytest.mark.asyncio
async def test_list_existing_issues_includes_every_status_without_details(store, make_task):
    proposed = rec(store, key="proposed-one", summary="proposed one", details="secret detail")
    approved = rec(store, key="approved-one", summary="approved one")
    store.decide_issue(approved["id"], "approved", "boss")
    denied = rec(store, key="denied-one", summary="denied one")
    store.decide_issue(denied["id"], "denied", "boss")

    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])
    result = await adapter.list_existing_issues({})
    payload = json.loads(result["content"][0]["text"])

    statuses = {row["dedupe_key"]: row["status"] for row in payload}
    assert statuses == {"proposed-one": "proposed", "approved-one": "approved", "denied-one": "denied"}
    for row in payload:
        assert set(row.keys()) == {"id", "dedupe_key", "repo", "summary", "issue_type", "status", "priority"}
    assert proposed["id"] in {row["id"] for row in payload}


@pytest.mark.asyncio
async def test_list_existing_issues_pages_with_offset_status_and_keys_only(store, make_task):
    for i in range(3):
        rec(store, key=f"k{i}", summary=f"issue {i}", priority=10 + i)
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])

    page1 = json.loads((await adapter.list_existing_issues({"limit": 2})).get("content")[0]["text"])
    page2 = json.loads((await adapter.list_existing_issues({"limit": 2, "offset": 2})).get("content")[0]["text"])
    assert [row["dedupe_key"] for row in page1] == ["k2", "k1"]
    assert [row["dedupe_key"] for row in page2] == ["k0"]

    only_proposed = json.loads((await adapter.list_existing_issues({"status": "proposed"})).get("content")[0]["text"])
    assert len(only_proposed) == 3
    bad_status = await adapter.list_existing_issues({"status": "bogus"})
    assert bad_status.get("isError")

    compact = json.loads((await adapter.list_existing_issues({"keys_only": True})).get("content")[0]["text"])
    for row in compact:
        assert set(row.keys()) == {"id", "dedupe_key", "status"}


@pytest.mark.asyncio
async def test_list_existing_issues_filters_repo_at_db_level_so_paging_does_not_skip_matches(store, make_task):
    # a higher-priority row in another repo must not push a lower-priority target-repo row off page 1 —
    # repo has to be a DB-level filter (like status/task_type), not applied after limit/offset (issue #57 follow-up)
    store.record_issue("b-high", "other/repo", "high priority other repo issue", "bug", "d", 90)
    store.record_issue("a-low", "example-org/agent-harness", "low priority target repo issue", "bug", "d", 10)
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness", "other/repo"])

    page = json.loads((await adapter.list_existing_issues({"repo": "example-org/agent-harness", "limit": 1})).get("content")[0]["text"])
    assert [row["dedupe_key"] for row in page] == ["a-low"]


@pytest.mark.asyncio
async def test_record_issue_rejects_duplicate_summary_under_different_key(store, make_task):
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])
    first = await adapter.record_issue({"repo": "example-org/agent-harness", "summary": "Trim the classifier prompt", "issue_type": "token_efficiency", "details": "d", "dedupe_key": "trim-prompt"})
    assert not first.get("isError")

    # same summary (case/whitespace-insensitive), different dedupe_key -> rejected, nothing inserted
    dup = await adapter.record_issue({"repo": "example-org/agent-harness", "summary": "  trim the classifier prompt  ", "issue_type": "token_efficiency", "details": "d2", "dedupe_key": "shrink-prompt"})
    assert dup.get("isError")
    assert "#1" in dup["content"][0]["text"] or "trim-prompt" in dup["content"][0]["text"]
    assert len(store.list_issues()) == 1

    # same dedupe_key with the same summary is a refresh, not a duplicate -> still allowed
    refresh = await adapter.record_issue({"repo": "example-org/agent-harness", "summary": "Trim the classifier prompt", "issue_type": "token_efficiency", "details": "d3", "dedupe_key": "trim-prompt", "priority": 80})
    assert not refresh.get("isError")
    assert len(store.list_issues()) == 1
    assert store.list_issues()[0]["details"] == "d3"


@pytest.mark.asyncio
async def test_record_issue_duplicate_of_non_proposed_row_does_not_suggest_refresh(store, make_task):
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])
    first = await adapter.record_issue({"repo": "example-org/agent-harness", "summary": "Trim the classifier prompt", "issue_type": "token_efficiency", "details": "d", "dedupe_key": "trim-prompt"})
    assert not first.get("isError")
    store.decide_issue(1, "approved", "boss")

    # a duplicate of an already-decided row can't be refreshed by re-recording (the store only upserts proposed rows)
    dup = await adapter.record_issue({"repo": "example-org/agent-harness", "summary": "trim the classifier prompt", "issue_type": "token_efficiency", "details": "d2", "dedupe_key": "shrink-prompt"})
    assert dup.get("isError")
    text = dup["content"][0]["text"]
    assert "approved" in text and "dedupe_key" not in text
    assert len(store.list_issues()) == 1


@pytest.mark.asyncio
async def test_record_issue_same_dedupe_key_as_decided_row_errors_instead_of_silent_noop(store, make_task):
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])
    first = await adapter.record_issue({"repo": "example-org/agent-harness", "summary": "Trim the classifier prompt", "issue_type": "token_efficiency", "details": "d", "dedupe_key": "trim-prompt"})
    assert not first.get("isError")
    store.decide_issue(1, "approved", "boss")

    # the store's upsert only fires while the row is still 'proposed', so re-recording under the SAME
    # dedupe_key as an already-decided row silently writes nothing -> must not be reported as success
    again = await adapter.record_issue({"repo": "example-org/agent-harness", "summary": "Trim the classifier prompt (updated)", "issue_type": "token_efficiency", "details": "d2", "dedupe_key": "trim-prompt"})
    assert again.get("isError")
    assert "approved" in again["content"][0]["text"]
    row = store.get_issue(1)
    assert row["status"] == "approved" and row["details"] == "d"


@pytest.mark.asyncio
async def test_list_recent_errors_includes_traceback_tail(store, make_task):
    store.add_error("housekeeping", "RuntimeError", "sweep failed", traceback="Traceback (most recent call last):\n" + "x" * 700 + "\nRuntimeError: sweep failed")
    store.add_error("classifier", "ValueError", "bad json")  # no traceback recorded
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])
    store.add_error("housekeeping", "RuntimeError", "sweep failed again", traceback="Traceback:\nRuntimeError: sweep failed again")
    result = await adapter.list_recent_errors({})
    payload = json.loads(result["content"][0]["text"])
    assert payload["counts"][0] == {"component": "housekeeping", "kind": "RuntimeError", "count": 2}
    newest, classifier, oldest = payload["errors"]
    assert newest["traceback_tail"].endswith("RuntimeError: sweep failed again")
    assert classifier["traceback_tail"] is None
    assert len(oldest["traceback_tail"]) == 600 and oldest["traceback_tail"].endswith("RuntimeError: sweep failed")


@pytest.mark.asyncio
async def test_list_recent_errors_filters_by_component_kind_offset_and_traceback_chars(store, make_task):
    store.add_error("review_poller", "RuntimeError", "first", traceback="Traceback:\nRuntimeError: first")
    store.add_error("classifier", "ValueError", "unrelated")
    store.add_error("review_poller", "RuntimeError", "second", traceback="Traceback:\nRuntimeError: second")
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])

    filtered = json.loads((await adapter.list_recent_errors({"component": "review_poller", "kind": "RuntimeError"})).get("content")[0]["text"])
    assert [row["message"] for row in filtered["errors"]] == ["second", "first"]

    paged = json.loads((await adapter.list_recent_errors({"component": "review_poller", "kind": "RuntimeError", "offset": 1})).get("content")[0]["text"])
    assert [row["message"] for row in paged["errors"]] == ["first"]

    trimmed = json.loads((await adapter.list_recent_errors({"traceback_chars": 5})).get("content")[0]["text"])
    assert all(row["traceback_tail"] is None or len(row["traceback_tail"]) <= 5 for row in trimmed["errors"])

    omitted = json.loads((await adapter.list_recent_errors({"traceback_chars": 0})).get("content")[0]["text"])
    assert all(row["traceback_tail"] is None for row in omitted["errors"])


@pytest.mark.asyncio
async def test_list_failed_tasks_excludes_refused(store, make_task):
    # issue #16: refused (unsupported-request) tasks have their own terminal state so they
    # stop drowning real failures in list_failed_tasks and its failure-rate metrics.
    real_failure = make_task("do a real thing")
    store.transition(real_failure.task_id, RECEIVED, FAILED, "crashed", error="boom")
    refusal = make_task("hi")
    store.transition(refusal.task_id, RECEIVED, REFUSED, "unsupported request", error="unsupported request")
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])
    result = await adapter.list_failed_tasks({})
    payload = json.loads(result["content"][0]["text"])
    task_ids = [row["task_id"] for row in payload]
    assert real_failure.task_id in task_ids
    assert refusal.task_id not in task_ids


@pytest.mark.asyncio
async def test_list_failed_tasks_filters_by_task_type_query_and_offset(store, make_task):
    a = make_task("thing a")
    store.transition(a.task_id, RECEIVED, FAILED, "crashed", error="boom a")
    store.set_fields(a.task_id, task_type="spec2pr")
    b = make_task("thing b")
    store.transition(b.task_id, RECEIVED, FAILED, "crashed", error="boom b")
    store.set_fields(b.task_id, task_type="discoverissues")
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])

    only_spec2pr = json.loads((await adapter.list_failed_tasks({"task_type": "spec2pr"})).get("content")[0]["text"])
    assert [row["task_id"] for row in only_spec2pr] == [a.task_id]

    by_query = json.loads((await adapter.list_failed_tasks({"query": "thing b"})).get("content")[0]["text"])
    assert [row["task_id"] for row in by_query] == [b.task_id]

    # most-recent-first ordering: offset 1 skips b, leaving a
    paged = json.loads((await adapter.list_failed_tasks({"limit": 1, "offset": 1})).get("content")[0]["text"])
    assert [row["task_id"] for row in paged] == [a.task_id]


@pytest.mark.asyncio
async def test_enqueue_spec_pr_creates_linked_child_task(store, make_task):
    parent = make_task(text="/implementapprovedissues")
    row = rec(store, key="x")
    enqueue = EnqueueAdapter(store, parent)

    # not approved or reserved yet -> refused
    refused = await enqueue.enqueue_spec_pr({"id": row["id"], "spec": "spec"})
    assert refused.get("isError")

    store.decide_issue(row["id"], "approved", "boss")
    result = await enqueue.enqueue_spec_pr({"id": row["id"], "spec": "the full spec"})
    assert not result.get("isError")

    children = store.children_of(parent.task_id)
    assert len(children) == 1
    assert children[0].request_text == f"/spec2pr {row['id']}"
    updated = store.get_issue(row["id"])
    assert updated["status"] == "in_progress" and updated["task_id"] == children[0].task_id and updated["spec"] == "the full spec"

    # re-enqueue for the same parent is idempotent — it replies with the existing child, never spawns a duplicate task
    again = await enqueue.enqueue_spec_pr({"id": row["id"], "spec": "x"})
    assert not again.get("isError")
    assert children[0].task_id in again["content"][0]["text"]
    assert len(store.children_of(parent.task_id)) == 1


@pytest.mark.asyncio
async def test_enqueue_spec_pr_accepts_a_row_reserved_for_this_run(store, make_task):
    parent = make_task(text="/implementapprovedissues")
    row = rec(store, key="reserved-one")
    store.decide_issue(row["id"], "approved", "boss")
    reserved = store.reserve_issues(parent.task_id, 5)
    assert [r["id"] for r in reserved] == [row["id"]]
    assert store.get_issue(row["id"])["status"] == "implementation_queued"

    enqueue = EnqueueAdapter(store, parent)
    result = await enqueue.enqueue_spec_pr({"id": row["id"], "spec": "spec a"})
    assert not result.get("isError")
    assert len(store.children_of(parent.task_id)) == 1
    assert store.get_issue(row["id"])["status"] == "in_progress"


@pytest.mark.asyncio
async def test_enqueue_spec_pr_rejects_row_reserved_by_another_task_without_creating_child(store, make_task):
    parent = make_task(text="/implementapprovedissues")
    other_parent = make_task(text="/implementapprovedissues")
    row = rec(store, key="x")
    store.decide_issue(row["id"], "approved", "boss")
    store.reserve_issues(other_parent.task_id, 5)
    assert store.get_issue(row["id"])["reserved_by"] == other_parent.task_id

    enqueue = EnqueueAdapter(store, parent)
    refused = await enqueue.enqueue_spec_pr({"id": row["id"], "spec": "spec"})
    assert refused.get("isError")
    assert store.children_of(parent.task_id) == []
    assert store.get_issue(row["id"])["status"] == "implementation_queued"


@pytest.mark.asyncio
async def test_finish_issue_tool_only_from_in_progress(store, make_task):
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])
    row = rec(store, key="x")
    result = await adapter.finish_issue({"id": row["id"], "status": "done", "pr_url": "u"})
    assert result.get("isError")  # still proposed, not in progress


@pytest.mark.asyncio
async def test_finish_issue_tool_stores_in_review_when_pr_opened(store, make_task):
    task = make_task()
    adapter = IssuesAdapter(store, task, ["example-org/agent-harness"])
    row = rec(store, key="x")
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "spec")

    result = await adapter.finish_issue({"id": row["id"], "status": "done", "pr_url": "https://github.com/example-org/agent-harness/pull/9"})
    assert not result.get("isError")
    assert "in_review" in result["content"][0]["text"]
    assert store.get_issue(row["id"])["status"] == "in_review"


@pytest.mark.asyncio
async def test_finish_issue_tool_done_without_pr_url_stays_done(store, make_task):
    task = make_task()
    adapter = IssuesAdapter(store, task, ["example-org/agent-harness"])
    row = rec(store, key="x")
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "spec")

    result = await adapter.finish_issue({"id": row["id"], "status": "done"})
    assert not result.get("isError")
    assert store.get_issue(row["id"])["status"] == "done"


@pytest.mark.asyncio
async def test_finish_issue_tool_failed_stays_failed(store, make_task):
    task = make_task()
    adapter = IssuesAdapter(store, task, ["example-org/agent-harness"])
    row = rec(store, key="x")
    store.decide_issue(row["id"], "approved", "boss")
    store.start_issue(row["id"], task.task_id, "spec")

    result = await adapter.finish_issue({"id": row["id"], "status": "failed"})
    assert not result.get("isError")
    assert store.get_issue(row["id"])["status"] == "failed"


def test_reserve_issues_takes_only_the_top_batch(store):
    ids = []
    for i in range(6):
        row = rec(store, key=f"k{i}", priority=10 + i)  # k5 is highest priority
        store.decide_issue(row["id"], "approved", "boss")
        ids.append(row["id"])
    reserved = store.reserve_issues("coordinator-1", 5)
    reserved_ids = [r["id"] for r in reserved]
    # the 5 highest-priority approved rows are reserved, in priority order; the 6th (lowest) is untouched
    assert reserved_ids == list(reversed(ids[1:]))
    for r in reserved:
        assert r["status"] == "implementation_queued" and r["reserved_by"] == "coordinator-1"
    untouched = store.get_issue(ids[0])
    assert untouched["status"] == "approved" and untouched["reserved_by"] is None


def test_reserve_issues_is_a_noop_when_nothing_approved(store):
    assert store.reserve_issues("coordinator-1", 5) == []


def test_active_implementation_run_tracks_non_terminal_coordinator(store, make_task):
    assert store.active_implementation_run() is None
    task = make_task(text="/implementapprovedissues")
    assert store.active_implementation_run() == task.task_id
    store.transition(task.task_id, task.state, "cancelled")
    assert store.active_implementation_run() is None


def test_fail_stalled_implementation_run_fails_blocked_coordinator_and_releases_reservations(store, make_task):
    row = rec(store, key="stalled")
    store.decide_issue(row["id"], "approved", "boss")
    task = make_task(text="/implementapprovedissues")
    store.reserve_issues(task.task_id, 1)
    store.transition(task.task_id, RECEIVED, BLOCKED, "needs permission")
    stale_at = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    store.conn.execute("UPDATE tasks SET updated_at = ? WHERE task_id = ?", (stale_at, task.task_id))
    store.conn.commit()

    assert fail_stalled_implementation_run(store) == task.task_id
    assert store.get_task(task.task_id).state == FAILED
    assert store.get_issue(row["id"])["status"] == "approved"
    assert store.active_implementation_run() is None
    assert "recovery" in [event["kind"] for event in store.events_for(task.task_id)]


def test_fail_stalled_implementation_run_leaves_fresh_blocked_coordinator_alone(store, make_task):
    task = make_task(text="/implementapprovedissues")
    store.transition(task.task_id, RECEIVED, BLOCKED, "needs permission")

    assert fail_stalled_implementation_run(store) is None
    assert store.get_task(task.task_id).state == BLOCKED


def test_fail_stalled_implementation_run_without_coordinator_is_noop(store):
    assert fail_stalled_implementation_run(store) is None


def test_fail_stalled_implementation_run_leaves_non_blocked_coordinator_alone(store, make_task):
    task = make_task(text="/implementapprovedissues")

    assert fail_stalled_implementation_run(store) is None
    assert store.get_task(task.task_id).state == RECEIVED


def test_release_reserved_issues_restores_only_the_given_reservation(store):
    a = rec(store, key="a")
    b = rec(store, key="b")
    store.decide_issue(a["id"], "approved", "boss")
    store.decide_issue(b["id"], "approved", "boss")
    store.reserve_issues("coordinator-1", 1)  # reserves the higher-priority of the two (equal here -> lower id, "a")
    released = store.release_reserved_issues("coordinator-1")
    assert released == 1
    assert store.get_issue(a["id"])["status"] == "approved"
    assert store.get_issue(a["id"])["reserved_by"] is None
    # a reservation held by someone else, or none at all, is untouched
    assert store.release_reserved_issues("coordinator-1") == 0


def test_release_stale_reservations_restores_orphaned_and_terminal_coordinators(store, make_task):
    pending = rec(store, key="pending")
    dead = rec(store, key="dead-coordinator")
    refused = rec(store, key="refused-coordinator")
    live = rec(store, key="live-coordinator")
    for row in (pending, dead, refused, live):
        store.decide_issue(row["id"], "approved", "boss")

    store.reserve_issues("pending:abc123", 1)  # never became a real task (dashboard click died before creation)
    dead_task = make_task(text="/implementapprovedissues")
    store.transition(dead_task.task_id, dead_task.state, "failed", finished_at="2026-01-01T00:00:00+00:00")
    store.reserve_issues(dead_task.task_id, 1)
    refused_task = make_task(text="/implementapprovedissues")
    store.transition(refused_task.task_id, refused_task.state, REFUSED, finished_at="2026-01-01T00:00:00+00:00")
    store.reserve_issues(refused_task.task_id, 1)
    live_task = make_task(text="/implementapprovedissues")
    store.reserve_issues(live_task.task_id, 1)

    released = store.release_stale_reservations()
    assert released == 3
    assert store.get_issue(pending["id"])["status"] == "approved"
    assert store.get_issue(dead["id"])["status"] == "approved"
    assert store.get_issue(refused["id"])["status"] == "approved"
    # the live coordinator's reservation survives
    assert store.get_issue(live["id"])["status"] == "implementation_queued"


def test_assign_reservation_moves_pending_marker_onto_the_real_task(store, make_task):
    row = rec(store, key="x")
    store.decide_issue(row["id"], "approved", "boss")
    store.reserve_issues("pending:abc", 1)
    task = make_task(text="/implementapprovedissues")
    moved = store.assign_reservation("pending:abc", task.task_id)
    assert moved == 1
    assert store.get_issue(row["id"])["reserved_by"] == task.task_id


@pytest.mark.asyncio
async def test_list_accepted_issues_returns_only_this_runs_reservation_and_reserves_when_empty(store, make_task):
    coordinator = make_task(text="/implementapprovedissues")
    other_coordinator = make_task(text="/implementapprovedissues")
    others = rec(store, key="others")  # created first, so it is the one reserve_issues grabs first (tied priority, lowest id)
    mine = rec(store, key="mine")
    store.decide_issue(others["id"], "approved", "boss")
    store.decide_issue(mine["id"], "approved", "boss")
    store.reserve_issues(other_coordinator.task_id, 1)  # claims "others" first, leaving only "mine" approved

    adapter = IssuesAdapter(store, coordinator, ["example-org/agent-harness"])
    result = await adapter.list_accepted_issues({})
    payload = json.loads(result["content"][0]["text"])
    # nothing reserved for `coordinator` yet -> it reserves the (only) remaining approved row itself
    assert [row["id"] for row in payload] == [mine["id"]]
    assert store.get_issue(mine["id"])["reserved_by"] == coordinator.task_id

    # a second call returns the same reserved batch rather than reserving again
    again = await adapter.list_accepted_issues({})
    assert json.loads(again["content"][0]["text"]) == payload


@pytest.mark.asyncio
async def test_record_issue_tool_rejects_unapproved_repo(store, make_task):
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])
    result = await adapter.record_issue({"repo": "other/repo", "summary": "s", "issue_type": "bug", "details": "d", "dedupe_key": "k", "priority": 50})
    assert result.get("isError")
    assert "example-org/agent-harness" in result["content"][0]["text"]
    assert store.list_issues() == []


def test_issue_comments_are_one_level_soft_deleted_and_resolvable(store):
    issue = rec(store)
    root = store.add_issue_comment(issue["id"], "person@example.com", "root")
    reply = store.add_issue_comment(issue["id"], "agent", "reply", root["id"])
    with pytest.raises(ValueError, match="top-level"):
        store.add_issue_comment(issue["id"], "agent", "too deep", reply["id"])

    edited = store.update_issue_comment(root["id"], "updated")
    assert edited["body"] == "updated" and edited["edited_at"]
    resolved = store.resolve_issue_comment(root["id"], "agent")
    assert resolved["resolved"] == 1 and resolved["resolved_by"] == "agent"
    deleted = store.delete_issue_comment(root["id"])
    assert deleted["body"] == "" and deleted["deleted_at"]
    assert store.delete_issue_comment(root["id"]) is None
    assert [row["id"] for row in store.list_issue_comments(issue["id"])] == [root["id"], reply["id"]]


@pytest.mark.asyncio
async def test_issue_comment_tools_only_edit_red_comments(store, make_task):
    issue = rec(store)
    human = store.add_issue_comment(issue["id"], "person@example.com", "human")
    adapter = IssuesAdapter(store, make_task(), ["example-org/agent-harness"])
    refused = await adapter.update_issue_comment({"comment_id": human["id"], "body": "changed"})
    assert refused.get("isError")

    posted = await adapter.post_issue_comment({"issue_id": issue["id"], "body": "question"})
    assert not posted.get("isError")
    red_comment = store.list_issue_comments(issue["id"])[-1]
    assert (await adapter.update_issue_comment({"comment_id": red_comment["id"], "body": "updated"})).get("isError") is None
    assert (await adapter.resolve_issue_comment({"comment_id": human["id"]})).get("isError") is None
    threaded = json.loads((await adapter.list_issue_comments({"issue_id": issue["id"]}))["content"][0]["text"])
    assert threaded[0]["resolved"] is True


def test_delete_issue_removes_comments_and_attachments_but_is_blocked_while_active(store):
    issue = rec(store, key="del")
    comment = store.add_issue_comment(issue["id"], "person@example.com", "body")
    store.add_issue_attachment(issue["id"], comment["id"], "notes.txt", "text/plain", 4, "issues/1/key/notes.txt", "person@example.com")

    store.decide_issue(issue["id"], "approved", "boss")
    [reserved] = store.reserve_issues("coordinator", 1)
    store.start_issue(reserved["id"], None, "spec")
    assert store.get_issue(issue["id"])["status"] == "in_progress"
    assert store.delete_issue(issue["id"]) is None
    assert store.get_issue(issue["id"]) is not None

    store.finish_issue(issue["id"], "in_review", "https://example.test/pr/1")
    assert store.get_issue(issue["id"])["status"] == "in_review"
    assert store.delete_issue(issue["id"]) is None

    store.finish_issue(issue["id"], "done", "https://example.test/pr/1")
    deleted = store.delete_issue(issue["id"])
    assert deleted is not None and deleted["id"] == issue["id"]
    assert store.get_issue(issue["id"]) is None
    assert store.list_issue_comments(issue["id"]) == []
    assert store.list_issue_attachments(issue["id"]) == []

    assert store.delete_issue(9999) is None


def test_issue_attachments_and_refine_lookup(store, make_task):
    issue = rec(store)
    comment = store.add_issue_comment(issue["id"], "person@example.com", "body")
    attachment = store.add_issue_attachment(issue["id"], comment["id"], "notes.txt", "text/plain", 4, "issues/1/key/notes.txt", "person@example.com")
    assert store.get_issue_attachment(attachment["id"])["filename"] == "notes.txt"
    assert store.list_issue_attachments(issue["id"])[0]["comment_id"] == comment["id"]

    seven = make_task(text=f"/refineissue {issue['id']}")
    make_task(text=f"/refineissue {issue['id']}0")
    assert store.active_refine_task(issue["id"]) == seven.task_id
    assert store.active_refine_tasks_by_issue()[issue["id"]] == seven.task_id
    assert store.count_issue_comments_by_issue() == {issue["id"]: 1}
