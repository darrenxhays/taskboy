"""per-task workspace directories (EC2-008/009) and the retention sweep (MEM-012, ORC-014)."""

import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_harness.models import COMPLETED
from agent_harness.store import Store

logger = logging.getLogger("agent_harness.workspace")


def create(workspaces_root: str, task_id: str) -> Path:
    workspace = Path(workspaces_root) / task_id
    for sub in ("repo", "notes", "home"):
        (workspace / sub).mkdir(parents=True, exist_ok=True)
    workspace.chmod(0o700)
    return workspace


def delete(workspaces_root: str, task_id: str) -> None:
    workspace = Path(workspaces_root) / task_id
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)


def sweep_once(store: Store, workspaces_root: str, memory_root: str, retention: dict) -> dict:
    """delete expired workspaces, memory summaries, and dedup rows per the retention config.

    completed workspaces go early; failed/cancelled are kept longer for diagnosis (REL-007).
    blocked tasks are not terminal and are never swept — their workspace is needed for resume.
    """
    now = datetime.now(timezone.utc)
    completed_cutoff = _cutoff(now, retention.get("workspace_completed_days", 3))
    failed_cutoff = _cutoff(now, retention.get("workspace_failed_days", 7))
    workspaces = 0
    for task in store.terminal_tasks_updated_before(max(completed_cutoff, failed_cutoff)):
        cutoff = completed_cutoff if task.state == COMPLETED else failed_cutoff
        if task.updated_at < cutoff:
            delete(workspaces_root, task.task_id)
            store.set_fields(task.task_id, workspace_path=None)
            workspaces += 1

    memories = 0
    memory_deadline = time.time() - retention.get("memory_days", 90) * 86400
    memory_dir = Path(memory_root) / "tasks"
    if memory_dir.is_dir():
        for path in memory_dir.glob("*.md"):
            if path.stat().st_mtime < memory_deadline:
                path.unlink(missing_ok=True)
                memories += 1

    events = store.purge_slack_events(_cutoff(now, retention.get("slack_events_days", 7)))
    if workspaces or memories or events:
        logger.info("retention sweep: %s workspaces, %s memory files, %s slack events removed", workspaces, memories, events)
    return {"workspaces": workspaces, "memories": memories, "slack_events": events}


def _cutoff(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat(timespec="seconds")
