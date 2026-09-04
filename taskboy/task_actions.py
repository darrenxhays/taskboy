"""privileged task controls shared by the dashboard and command-line operator."""

import time

from taskboy.config import Config
from taskboy.models import BLOCKED, CANCELLED, FAILED, QUEUED, TERMINAL_STATES, Task, utcnow
from taskboy.orchestrator import accept_task, reopen_issue_and_cancel
from taskboy.store import Store, TransitionRaced


def cancel_task(store: Store, task_id: str, actor: str) -> tuple[Task | None, str]:
    task = store.get_task(task_id)
    if task is None:
        return None, "not found"
    if task.state in TERMINAL_STATES:
        store.add_event(task_id, "operator_action", {"actor": actor, "action": "cancel", "outcome": "noop", "state": task.state})
        return task, f"already {task.state}"
    try:
        cancelled = store.transition(task_id, task.state, CANCELLED, f"cancelled via dashboard by {actor}", finished_at=utcnow())
    except TransitionRaced:
        current = store.get_task(task_id)
        if current is not None:
            store.add_event(task_id, "operator_action", {"actor": actor, "action": "cancel", "outcome": "raced", "state": current.state})
        return current, f"already {current.state}" if current else "not found"
    store.add_event(task_id, "operator_action", {"actor": actor, "action": "cancel", "outcome": "cancelled", "previous_state": task.state})
    return cancelled, "cancelled"


async def retry_task(store: Store, config: Config, notifier, source_task_id: str, actor: str) -> tuple[Task | None, str]:
    source = store.get_task(source_task_id)
    if source is None:
        return None, "not found"
    if source.state not in (FAILED, CANCELLED):
        store.add_event(source_task_id, "operator_action", {"actor": actor, "action": "retry", "outcome": "rejected", "state": source.state})
        return source, f"cannot retry {source.state}"
    if (source.request_text or "").startswith("/spec2pr "):
        # an issue-backed task's issue is already back to `proposed` with no spec by the time it's terminal (#76)
        store.add_event(source_task_id, "operator_action", {"actor": actor, "action": "retry", "outcome": "rejected", "state": source.state, "reason": "issue_backed"})
        return source, "cannot retry an issue-backed task — re-approve its issue instead"
    retried, status = await accept_task(
        store,
        config,
        notifier,
        team_id=source.slack_team_id,
        channel_id=source.slack_channel_id,
        thread_ts=source.slack_thread_ts,
        message_ts=str(time.time_ns()),
        user_id=actor,
        text=source.request_text,
        parent_task_id=source.task_id,
        model_override=source.model_override,
        effort_override=source.effort_override,
        thread_context=source.thread_context,
        debug_thread_ts=source.debug_thread_ts,
        debug_permalink=source.debug_permalink,
    )
    store.add_event(source_task_id, "operator_action", {"actor": actor, "action": "retry", "outcome": status, "new_task_id": retried.task_id if retried else None})
    return retried, status


def resume_task(store: Store, task_id: str, actor: str) -> tuple[Task | None, str]:
    """requeue a BLOCKED task on the same session after an operator fixed its blocker out of band (an IAM change,
    a config value, a service outage). tasks waiting on a permission decision or requester answers have their own
    resume paths, so this is for the report_blocked case that used to be a dead end."""
    task = store.get_task(task_id)
    if task is None:
        return None, "not found"
    if task.state != BLOCKED:
        store.add_event(task_id, "operator_action", {"actor": actor, "action": "resume", "outcome": "noop", "state": task.state})
        return task, f"not blocked ({task.state})"
    if store.has_pending_permission_request(task_id):
        store.add_event(task_id, "operator_action", {"actor": actor, "action": "resume", "outcome": "noop", "waiting": "permission"})
        return task, "waiting on a permission decision — grant or deny it instead"
    if store.pending_questions_for(task_id) is not None:
        store.add_event(task_id, "operator_action", {"actor": actor, "action": "resume", "outcome": "noop", "waiting": "questions"})
        return task, "waiting on requester answers — reply in the Slack thread instead"
    try:
        resumed = store.transition(task_id, BLOCKED, QUEUED, f"operator resume by {actor}", resume_session_id=task.session_id)
    except TransitionRaced:
        current = store.get_task(task_id)
        if current is not None:
            store.add_event(task_id, "operator_action", {"actor": actor, "action": "resume", "outcome": "raced", "state": current.state})
        return current, f"already {current.state}" if current else "not found"
    store.add_event(task_id, "operator_action", {"actor": actor, "action": "resume", "outcome": "resumed", "session_id": task.session_id})
    return resumed, "resumed"


async def decide_permission(store: Store, notifier, task_id: str, kind: str, target: str, decision: str, actor: str) -> tuple[Task | None, str]:
    """grant or deny a sub-agent's permission request: granting a blocked task resumes it, denying reopens and cancels its issue-backed task instead of stranding it (#76)."""
    if decision not in ("granted", "denied"):
        return None, "decision must be granted or denied"
    task = store.get_task(task_id)
    if task is None:
        return None, "not found"
    row = store.decide_permission_request(task_id, kind, target, decision, actor)
    if row is None:
        store.add_event(task_id, "operator_action", {"actor": actor, "action": f"permission_{decision}", "outcome": "no pending request", "kind": kind, "target": target})
        return task, "no pending request"
    store.add_event(task_id, "permission_decision", {"actor": actor, "decision": decision, "kind": kind, "target": target})
    # re-read: the task may have settled into BLOCKED since we first fetched it. if it is still RUNNING, the
    # orchestrator will pick up this grant when the session ends (run-start grant snapshot diff), so we leave it be here.
    task = store.get_task(task_id) or task
    if task.state == BLOCKED:
        if decision == "granted":
            try:
                task = store.transition(task_id, BLOCKED, QUEUED, f"resumed after permission granted by {actor}", resume_session_id=task.session_id)
            except TransitionRaced:
                pass
        elif not store.has_pending_permission_request(task_id):
            # no other request is still pending on this task; an issue-backed task must not strand its issue in_progress forever (#76)
            if await reopen_issue_and_cancel(store, notifier, task):
                task = store.get_task(task_id) or task
    return task, decision
