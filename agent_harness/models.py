"""task states, allowed transitions, and the dataclasses passed between modules."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

RECEIVED = "received"
QUEUED = "queued"
RUNNING = "running"
BLOCKED = "blocked"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
REFUSED = "refused"

STATES = [RECEIVED, QUEUED, RUNNING, BLOCKED, COMPLETED, FAILED, CANCELLED, REFUSED]
TERMINAL_STATES = [COMPLETED, FAILED, CANCELLED, REFUSED]

# the sdk's reasoning-effort levels (issue #67); "auto" is a classifier-only value meaning "no opinion" and is
# never persisted or passed to the sdk — see agent_harness.classifier and agent_harness.runner.session_option_kwargs
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# every transition in the system; store.transition rejects anything else (ORC-007)
ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    # REFUSED only happens at classification time, so it's only reachable from RECEIVED (issue #16)
    RECEIVED: [QUEUED, BLOCKED, FAILED, CANCELLED, REFUSED],
    QUEUED: [RUNNING, FAILED, CANCELLED],
    RUNNING: [COMPLETED, FAILED, BLOCKED, QUEUED, CANCELLED],
    BLOCKED: [QUEUED, FAILED, CANCELLED],
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_task_id() -> str:
    # date prefix keeps ids sortable and slack-readable
    return "t" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8]


@dataclass
class Task:
    task_id: str
    idempotency_key: str
    state: str
    attempt: int
    resume_session_id: str | None
    not_before: str | None
    slack_team_id: str
    slack_channel_id: str
    slack_thread_ts: str
    slack_message_ts: str
    slack_user_id: str
    request_text: str
    thread_context: str | None
    persona: str | None
    schedule_name: str | None
    parent_task_id: str | None
    classification_json: str | None
    task_type: str | None
    complexity: str | None
    risk: str | None
    model_alias: str | None
    model_id: str | None
    profile: str | None
    routing_rationale: str | None
    model_override: str | None
    effort_override: str | None
    effort: str | None  # resolved by routing: effort_override > classifier's pick > profile default (issue #67)
    session_id: str | None
    workspace_path: str | None
    max_budget_usd: float | None
    max_turns: int | None
    max_runtime_minutes: int | None
    cost_usd: float
    num_turns: int
    blocked_reason: str | None
    error: str | None
    result_summary: str | None
    reply: str | None
    debug_thread_ts: str | None
    debug_permalink: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str


@dataclass
class Outcome:
    """what a runner reports back for a finished session."""

    state: str  # completed | failed | blocked
    result_summary: str = ""
    reply: str = ""
    error: str = ""
    blocked_reason: str = ""
    session_id: str | None = None
    cost_usd: float = 0.0
    num_turns: int = 0
    retryable: bool = False
    retry_not_before: str | None = None
