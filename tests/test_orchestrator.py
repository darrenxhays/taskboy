import asyncio
import sqlite3

import pytest

from agent_harness.classifier import stub_classify
from agent_harness.config import Config, Role, SlackConfig
from agent_harness.models import BLOCKED, CANCELLED, COMPLETED, FAILED, QUEUED, RECEIVED, REFUSED, RUNNING, Outcome
from agent_harness.orchestrator import Orchestrator, accept_task
from agent_harness.router import RoleRefusal


async def _accept(store, config, notifier, text, n):
    return await accept_task(store, config, notifier, team_id="T1", channel_id="C1", thread_ts=str(n), message_ts=str(n), user_id="U1", text=text)


@pytest.mark.asyncio
async def test_concurrency_cap_respected_and_all_tasks_complete(store, config, notifier, make_task, wait_until):
    peak = 0
    current = 0

    async def run(task):
        nonlocal peak, current
        current += 1
        peak = max(peak, current)
        await asyncio.sleep(0.05)
        current -= 1
        return Outcome(state=COMPLETED, result_summary="done")

    for _ in range(6):
        make_task()
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(COMPLETED) == 6)
    await orchestrator.shutdown()
    await loop_task
    assert peak <= config.max_concurrency
    assert notifier.kinds().count("completed") == 6


@pytest.mark.asyncio
async def test_one_crashing_runner_does_not_affect_siblings(store, config, notifier, make_task, wait_until):
    async def run(task):
        if task.request_text == "boom":
            raise RuntimeError("runner exploded")
        await asyncio.sleep(0.02)
        return Outcome(state=COMPLETED, result_summary="fine")

    make_task("boom")
    for _ in range(3):
        make_task("fine")
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(COMPLETED) == 3 and store.count_tasks(FAILED) == 1)
    await orchestrator.shutdown()
    await loop_task
    failed = store.tasks_in_state(FAILED)[0]
    assert "runner exploded" in failed.error


@pytest.mark.asyncio
async def test_blocked_outcome_transitions_and_notifies(store, config, notifier, make_task, wait_until):
    async def run(task):
        return Outcome(state=BLOCKED, blocked_reason="need repo access", session_id="s-block")

    make_task()
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(BLOCKED) == 1)
    await orchestrator.shutdown()
    await loop_task
    blocked = store.tasks_in_state(BLOCKED)[0]
    assert blocked.blocked_reason == "need repo access"
    assert blocked.session_id == "s-block"
    assert "blocked" in notifier.kinds()


@pytest.mark.asyncio
async def test_grant_during_running_resumes_instead_of_stranding_blocked(store, config, notifier, make_task, wait_until):
    # an operator grants while the session is still RUNNING (decide_permission can't resume a non-BLOCKED task);
    # once the session settles BLOCKED the orchestrator must pick the grant up rather than leave it stranded (§8.4).
    calls = {"n": 0}

    async def run(task):
        calls["n"] += 1
        if calls["n"] == 1:
            store.request_permission(task.task_id, "tool", "mcp__jira__add_comment", "need it")
            store.decide_permission_request(task.task_id, "tool", "mcp__jira__add_comment", "granted", "boss")
            return Outcome(state=BLOCKED, blocked_reason="waiting on grant", session_id="s-1")
        return Outcome(state=COMPLETED, result_summary="done")

    task = make_task()
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.get_task(task.task_id).state == COMPLETED)
    await orchestrator.shutdown()
    await loop_task
    assert calls["n"] == 2  # resumed and ran again with the grant
    assert "blocked" not in notifier.kinds()  # never surfaced to the operator as blocked
    assert store.get_task(task.task_id).resume_session_id == "s-1"


@pytest.mark.asyncio
async def test_shutdown_requeues_running_tasks_for_resume(store, config, notifier, make_task, wait_until):
    async def run(task):
        await asyncio.sleep(30)
        return Outcome(state=COMPLETED)

    task = make_task()
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(RUNNING) == 1)
    await orchestrator.shutdown()
    await loop_task
    requeued = store.get_task(task.task_id)
    assert requeued.state == QUEUED
    assert requeued.attempt == 1


@pytest.mark.asyncio
async def test_reconcile_requeues_orphaned_running_tasks(store, config, notifier, make_task):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED, "classified")
    store.transition(task.task_id, QUEUED, RUNNING, "dispatched", session_id="s-123")
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=None, notifier=notifier)
    await orchestrator.reconcile()
    recovered = store.get_task(task.task_id)
    assert recovered.state == QUEUED
    assert recovered.resume_session_id == "s-123"
    assert recovered.attempt == 1
    assert "recovered" in notifier.kinds()


@pytest.mark.asyncio
async def test_reconcile_fails_task_when_retries_exhausted(store, config, notifier, make_task):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED, "classified")
    store.transition(task.task_id, QUEUED, RUNNING, "dispatched", attempt=config.max_retries)
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=None, notifier=notifier)
    await orchestrator.reconcile()
    failed = store.get_task(task.task_id)
    assert failed.state == FAILED
    assert "failed" in notifier.kinds()


@pytest.mark.asyncio
async def test_retryable_session_limit_outcome_requeues_gated_by_not_before(store, config, notifier, make_task, wait_until):
    # a transient session/rate-limit death gets a second chance with the same session, gated until reset (§10)
    async def run(task):
        return Outcome(state=FAILED, error="You've hit your session limit · resets 6:50pm (UTC)", session_id="s-limit", retryable=True, retry_not_before="9999-01-01T00:00:00+00:00")

    task = make_task()
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.get_task(task.task_id).state == QUEUED and store.get_task(task.task_id).attempt == 1)
    await orchestrator.shutdown()
    await loop_task
    requeued = store.get_task(task.task_id)
    assert requeued.state == QUEUED
    assert requeued.attempt == 1
    assert requeued.resume_session_id == "s-limit"
    assert requeued.not_before == "9999-01-01T00:00:00+00:00"
    assert store.count_tasks(FAILED) == 0
    assert "recovered" in notifier.kinds()


@pytest.mark.asyncio
async def test_retryable_outcome_fails_once_retries_are_exhausted(store, config, notifier, make_task, wait_until):
    async def run(task):
        return Outcome(state=FAILED, error="session limit hit", retryable=True, retry_not_before="9999-01-01T00:00:00+00:00")

    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED, "classified", attempt=config.max_retries)
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(FAILED) == 1)
    await orchestrator.shutdown()
    await loop_task
    failed = store.get_task(task.task_id)
    assert failed.state == FAILED
    assert "failed" in notifier.kinds()


@pytest.mark.asyncio
async def test_non_retryable_failure_is_not_requeued(store, config, notifier, make_task, wait_until):
    async def run(task):
        return Outcome(state=FAILED, error="boom", retryable=False)

    task = make_task()
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(FAILED) == 1)
    await orchestrator.shutdown()
    await loop_task
    failed = store.get_task(task.task_id)
    assert failed.state == FAILED
    assert failed.attempt == 0


@pytest.mark.asyncio
async def test_unsupported_classification_refuses_without_executing(store, config, notifier, make_task, wait_until):
    async def classify(task):
        return {"task_type": "unsupported", "complexity": "trivial", "routing_rationale": "rule: none"}

    ran = False

    async def run(task):
        nonlocal ran
        ran = True
        return Outcome(state=COMPLETED)

    make_task("hey what's up")
    orchestrator = Orchestrator(store, config, classify=classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(REFUSED) == 1)
    await orchestrator.shutdown()
    await loop_task
    assert ran is False  # SLK-010: must not execute
    assert "refused" in notifier.kinds()
    # its own terminal state, not FAILED — refusals shouldn't pollute failure listings/metrics (issue #16)
    assert store.count_tasks(FAILED) == 0
    refused = store.tasks_in_state(REFUSED)[0]
    assert refused.task_type == "unsupported"


@pytest.mark.asyncio
async def test_refusal_transition_db_error_is_loud_and_falls_back_to_failed(store, config, notifier, make_task, wait_until):
    """issue #58: a stale/partial deployment whose tasks.state CHECK constraint predates REFUSED must not
    silently strand the row in received — the breakage should be recorded and the task still reach a
    terminal state."""

    async def classify(task):
        return {"task_type": "unsupported", "complexity": "trivial", "routing_rationale": "rule: none"}

    real_transition = store.transition

    def flaky_transition(task_id, from_state, to_state, reason="", **fields):
        if to_state == REFUSED:
            raise sqlite3.IntegrityError("CHECK constraint failed: tasks")
        return real_transition(task_id, from_state, to_state, reason, **fields)

    store.transition = flaky_transition

    task = make_task("hey what's up")
    orchestrator = Orchestrator(store, config, classify=classify, run=None, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(FAILED) == 1)
    await orchestrator.shutdown()
    await loop_task
    # never left stuck in received, and never miscounted as a clean REFUSED
    assert store.count_tasks(REFUSED) == 0
    assert store.count_tasks(RECEIVED) == 0
    failed = store.tasks_in_state(FAILED)[0]
    assert "refusal transition failed" in failed.error
    assert "failed" in notifier.kinds()
    errors = store.errors_for(task.task_id)
    assert any(e["component"] == "orchestrator" and e["kind"] == "IntegrityError" for e in errors)


@pytest.mark.asyncio
async def test_classification_failure_fails_the_task(store, config, notifier, make_task, wait_until):
    async def bad_classify(task):
        raise RuntimeError("classifier down")

    make_task()
    orchestrator = Orchestrator(store, config, classify=bad_classify, run=None, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(FAILED) == 1)
    await orchestrator.shutdown()
    await loop_task
    failed = store.tasks_in_state(FAILED)[0]
    assert "classifier down" in failed.error


@pytest.mark.asyncio
async def test_cancel_of_running_task_stops_it_without_requeue(store, config, notifier, make_task, wait_until):
    async def run(task):
        await asyncio.sleep(30)
        return Outcome(state=COMPLETED)

    task = make_task()
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(RUNNING) == 1)
    store.transition(task.task_id, RUNNING, CANCELLED, "cancelled via cli")
    orchestrator.wake.set()
    await wait_until(lambda: len(orchestrator.running) == 0)
    await orchestrator.shutdown()
    await loop_task
    assert store.get_task(task.task_id).state == CANCELLED


@pytest.mark.asyncio
async def test_reconcile_releases_stale_issue_reservations(store, config, notifier):
    row = store.record_issue("x", "example-org/agent-harness", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    store.reserve_issues("pending:abc123", 5)  # a dashboard click that died before its coordinator was created
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=None, notifier=notifier)
    await orchestrator.reconcile()
    assert store.get_issue(row["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_failed_coordinator_releases_its_reserved_issues(store, config, notifier, make_task, wait_until):
    row = store.record_issue("x", "example-org/agent-harness", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    coordinator = make_task(text="/implementapprovedissues")
    store.reserve_issues(coordinator.task_id, 5)

    async def run(task):
        return Outcome(state=FAILED, error="boom", retryable=False)

    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(FAILED) == 1)
    await orchestrator.shutdown()
    await loop_task
    assert store.get_issue(row["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_crashed_coordinator_releases_its_reserved_issues(store, config, notifier, make_task, wait_until):
    row = store.record_issue("x", "example-org/agent-harness", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    coordinator = make_task(text="/implementapprovedissues")
    store.reserve_issues(coordinator.task_id, 5)

    async def run(task):
        raise RuntimeError("runner exploded")

    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(FAILED) == 1)
    await orchestrator.shutdown()
    await loop_task
    assert store.get_issue(row["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_cancelled_coordinator_releases_its_reserved_issues(store, config, notifier, make_task, wait_until):
    row = store.record_issue("x", "example-org/agent-harness", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    coordinator = make_task(text="/implementapprovedissues")
    store.reserve_issues(coordinator.task_id, 5)

    async def run(task):
        await asyncio.sleep(30)
        return Outcome(state=COMPLETED)

    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(RUNNING) == 1)
    store.transition(coordinator.task_id, RUNNING, CANCELLED, "cancelled via cli")
    orchestrator.wake.set()
    await wait_until(lambda: len(orchestrator.running) == 0)
    await orchestrator.shutdown()
    await loop_task
    assert store.get_issue(row["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_completed_coordinator_releases_any_leftover_reservation(store, config, notifier, make_task, wait_until):
    # a coordinator that finishes without enqueuing everything it reserved (e.g. it decided some no longer applied)
    # must not leave those rows stuck in implementation_queued forever
    row = store.record_issue("x", "example-org/agent-harness", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", "boss")
    coordinator = make_task(text="/implementapprovedissues")
    store.reserve_issues(coordinator.task_id, 5)

    async def run(task):
        return Outcome(state=COMPLETED, result_summary="done")

    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(COMPLETED) == 1)
    await orchestrator.shutdown()
    await loop_task
    assert store.get_issue(row["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_accept_task_dedup_and_queue_full(store, notifier):
    config = Config(max_concurrency=1, queue_max=1, max_retries=0, progress_min_interval_seconds=0, runner="echo", slack=SlackConfig(team_id="T1", allowed_channels=[]), roles={"admin": Role("admin", ["U1"], ["read_only"], True, None, None)}, raw={})
    task1, status1 = await _accept(store, config, notifier, "first", 1)
    dup, status_dup = await _accept(store, config, notifier, "first again", 1)
    task2, status2 = await _accept(store, config, notifier, "second", 2)
    assert status1 == "created"
    assert status_dup == "duplicate"
    assert dup.task_id == task1.task_id
    assert status2 == "queue_full"
    assert store.get_task(task2.task_id).state == FAILED
    assert "refused" in notifier.kinds()


@pytest.mark.asyncio
async def test_paused_intake_refuses_new_tasks(store, config, notifier):
    store.meta_set("intake_paused", "1")
    task, status = await _accept(store, config, notifier, "anything", 1)
    assert task is None
    assert status == "paused"
    store.meta_set("intake_paused", "0")
    task, status = await _accept(store, config, notifier, "anything", 2)
    assert status == "created"


@pytest.mark.asyncio
async def test_role_refusal_fails_and_uses_refused_notifier(store, config, notifier, make_task, wait_until):
    async def classify(task):
        raise RoleRefusal("your readonly role does not allow the deep execution profile")

    make_task()
    orchestrator = Orchestrator(store, config, classify=classify, run=None, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(FAILED) == 1)
    await orchestrator.shutdown()
    await loop_task
    failed = store.tasks_in_state(FAILED)[0]
    assert "readonly role" in failed.error
    assert "refused" in notifier.kinds()


@pytest.mark.asyncio
async def test_reply_timing_and_crash_errors_are_persisted(store, config, notifier, make_task, wait_until):
    completed_task = make_task("complete")

    async def run(task):
        return Outcome(state=COMPLETED, result_summary="## Final Report\nDone", reply="Done. Tests pass.")

    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(COMPLETED) == 1)
    await orchestrator.shutdown()
    await loop_task
    done = store.get_task(completed_task.task_id)
    assert done.reply == "Done. Tests pass."
    stages = {__import__("json").loads(event["detail_json"])["stage"] for event in store.events_for(done.task_id) if event["kind"] == "timing"}
    assert {"classification", "queue_wait", "run", "total"} <= stages
    classification_timings = [event for event in store.events_for(done.task_id) if event["kind"] == "timing" and __import__("json").loads(event["detail_json"])["stage"] == "classification"]
    assert len(classification_timings) == 1

    crashing = make_task("crash")

    async def crash(task):
        raise RuntimeError("runner exploded")

    orchestrator = Orchestrator(store, config, classify=stub_classify, run=crash, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.get_task(crashing.task_id).state == FAILED)
    await orchestrator.shutdown()
    await loop_task
    assert store.recent_errors(1)[0]["component"] == "orchestrator"


@pytest.mark.asyncio
async def test_blocked_on_questions_posts_the_questions_not_generic_blocked(store, config, notifier, make_task, wait_until):
    async def run(task):
        store.ask_questions(task.task_id, "1. Which env?\n2. Postgres or Dynamo?")
        return Outcome(state=BLOCKED, blocked_reason="waiting for the requester to answer follow-up questions", session_id="s-q")

    task = make_task()
    orchestrator = Orchestrator(store, config, classify=stub_classify, run=run, notifier=notifier)
    loop_task = asyncio.create_task(orchestrator.dispatcher_loop())
    await wait_until(lambda: store.count_tasks(BLOCKED) == 1)
    await orchestrator.shutdown()
    await loop_task
    assert ("questions", task.task_id, "1. Which env?\n2. Postgres or Dynamo?") in notifier.calls
    assert "blocked" not in notifier.kinds()
