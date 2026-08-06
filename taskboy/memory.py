"""per-task markdown summaries (MEM-007) and thread-scoped recall for follow-ups (MEM-009/010).

the structured record is the store itself; these files are the human-readable layer.
scoping is structural: a follow-up only ever loads its immediate parent's summary — never
cross-thread or cross-repo context.
"""

from pathlib import Path

from taskboy.models import Task
from taskboy.redact import redactor
from taskboy.store import Store

RESULT_MAX_CHARS = 4000
RESULT_TRUNCATION_MARKER = "\n…(result truncated at 4000 chars)"


def write_summary(memory_root: str, task: Task, artifacts: list[dict]) -> Path:
    """written on every terminal transition; the compact durable record of what happened (MEM-006)."""
    lines = [
        f"# task {task.task_id}",
        "",
        f"- requested by: {task.slack_user_id} in {task.slack_channel_id} (thread {task.slack_thread_ts})",
        f"- state: {task.state}",
        f"- model: {task.model_alias or 'n/a'} ({task.routing_rationale or 'unrouted'})",
        f"- created: {task.created_at}  finished: {task.finished_at or 'n/a'}",
        f"- cost: ${task.cost_usd:.4f} over {task.num_turns} turns",
        "",
    ]
    if artifacts:
        lines.append("## artifacts")
        for a in artifacts:
            lines.append(f"- {a['kind']}: {a['external_id']}" + (f" ({a['url']})" if a.get("url") else ""))
        lines.append("")
    if task.result_summary:
        result = task.result_summary
        if len(result) > RESULT_MAX_CHARS:
            result = result[: RESULT_MAX_CHARS - len(RESULT_TRUNCATION_MARKER)] + RESULT_TRUNCATION_MARKER
        lines += ["## result", result, ""]
    if task.error:
        lines += ["## error", task.error, ""]
    if task.blocked_reason:
        lines += ["## blocked", task.blocked_reason, ""]
    path = Path(memory_root) / "tasks" / f"{task.task_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redactor.redact("\n".join(lines)))
    return path


def read_summary(memory_root: str, task_id: str) -> str | None:
    path = Path(memory_root) / "tasks" / f"{task_id}.md"
    return path.read_text() if path.exists() else None


def parent_context(store: Store, memory_root: str, task: Task) -> str | None:
    """the immediate parent's summary only — it already compresses earlier lineage, bounding size (MEM-009/010)."""
    if not task.parent_task_id:
        return None
    return read_summary(memory_root, task.parent_task_id)
