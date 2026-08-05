"""privileged task controls shared by the dashboard and command-line operator."""

import time

from agent_harness.config import Config
from agent_harness.models import BLOCKED, CANCELLED, FAILED, QUEUED, TERMINAL_STATES, Task, utcnow
from agent_harness.orchestrator import accept_task
from agent_harness.store import Store, TransitionRaced


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
    # a cancelled coordinator must not strand its reserved issues until restart
    store.release_reserved_issues(task_id)
    store.add_event(task_id, "operator_action", {"actor": actor, "action": "cancel", "outcome": "cancelled", "previous_state": task.state})
    return cancelled, "cancelled"


async def retry_task(store: Store, config: Config, notifier, source_task_id: str, actor: str) -> tuple[Task | None, str]:
    source = store.get_task(source_task_id)
    if source is None:
        return None, "not found"
    if source.state not in (FAILED, CANCELLED):
        store.add_event(source_task_id, "operator_action", {"actor": actor, "action": "retry", "outcome": "rejected", "state": source.state})
        return source, f"cannot retry {source.state}"
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


def decide_permission(store: Store, task_id: str, kind: str, target: str, decision: str, actor: str) -> tuple[Task | None, str]:
    """grant or deny a sub-agent's permission request. granting a blocked task also resumes it so it runs again with the access."""
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
    if decision == "granted":
        # re-read: the task may have settled into BLOCKED since we first fetched it. if it is still RUNNING, the
        # orchestrator will pick up this grant when the session ends (run-start grant snapshot diff), so we leave it be here.
        task = store.get_task(task_id) or task
        if task.state == BLOCKED:
            try:
                task = store.transition(task_id, BLOCKED, QUEUED, f"resumed after permission granted by {actor}", resume_session_id=task.session_id)
            except TransitionRaced:
                pass
    return task, decision
