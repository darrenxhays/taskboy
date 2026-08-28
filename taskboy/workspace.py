"""per-task workspace directories (EC2-008/009) and the retention sweep (MEM-012, ORC-014)."""

import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from taskboy.models import COMPLETED
from taskboy.store import Store

logger = logging.getLogger("taskboy.workspace")


# task workspaces may only push agent/ branches (#95); git hands the hook the resolved refs
PRE_PUSH_HOOK = """#!/bin/sh
while read -r local_ref local_sha remote_ref remote_sha; do
    case "$remote_ref" in
        refs/heads/agent/*) ;;
        *)
            echo "push to $remote_ref is blocked: task workspaces may only push agent/ branches" >&2
            exit 1
            ;;
    esac
done
exit 0
"""


def hooks_dir(workspace: Path) -> Path:
    return workspace / "githooks"


def create(workspaces_root: str, task_id: str) -> Path:
    workspace = Path(workspaces_root) / task_id
    for sub in ("repo", "notes", "home", "githooks"):
        (workspace / sub).mkdir(parents=True, exist_ok=True)
    # advisory: the session can rewrite this file — remote branch protection is the enforcement point
    hook = hooks_dir(workspace) / "pre-push"
    hook.write_text(PRE_PUSH_HOOK)
    hook.chmod(0o755)
    workspace.chmod(0o700)
    return workspace


def delete(workspaces_root: str, task_id: str) -> None:
    workspace = Path(workspaces_root) / task_id
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)


def sweep_once(store: Store, workspaces_root: str, memory_root: str, retention: dict) -> dict:
    """delete expired workspaces, memory summaries, dedup rows, and error rows per the retention config.

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
    errors = store.purge_errors(_cutoff(now, retention.get("errors_days", 30)))
    if workspaces or memories or events or errors:
        logger.info("retention sweep: %s workspaces, %s memory files, %s slack events, %s errors removed", workspaces, memories, events, errors)
    return {"workspaces": workspaces, "memories": memories, "slack_events": events, "errors": errors}


def _cutoff(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat(timespec="seconds")
