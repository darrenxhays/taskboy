from taskboy import workspace
from taskboy.models import COMPLETED, FAILED, QUEUED, RECEIVED, RUNNING

RETENTION = {"workspace_completed_days": 3, "workspace_failed_days": 7, "memory_days": 90, "slack_events_days": 7}


def _finish(store, task, state, workspaces_root):
    ws = workspace.create(str(workspaces_root), task.task_id)
    store.transition(task.task_id, RECEIVED, QUEUED, "classified")
    store.transition(task.task_id, QUEUED, RUNNING, "dispatched", workspace_path=str(ws))
    store.transition(task.task_id, RUNNING, state, "done")
    return ws


def _age(store, task_id, days):
    store.conn.execute("UPDATE tasks SET updated_at = datetime('now', ?) WHERE task_id = ?", (f"-{days} days", task_id))
    store.conn.commit()


def test_sweep_respects_per_state_retention(store, make_task, tmp_path):
    workspaces = tmp_path / "ws"
    old_completed = _finish(store, make_task("old completed"), COMPLETED, workspaces)
    _age(store, store.recent_tasks(1)[0].task_id, 4)
    fresh_completed = _finish(store, make_task("fresh completed"), COMPLETED, workspaces)
    old_failed = _finish(store, make_task("old failed"), FAILED, workspaces)
    failed_id = store.recent_tasks(1)[0].task_id
    _age(store, failed_id, 5)  # older than completed cutoff, younger than failed cutoff

    counts = workspace.sweep_once(store, str(workspaces), str(tmp_path / "memory"), RETENTION)
    assert counts["workspaces"] == 1
    assert not old_completed.exists()  # 4 days > 3-day completed retention
    assert fresh_completed.exists()
    assert old_failed.exists()  # failed keeps 7 days for diagnosis (REL-007)


def test_sweep_purges_old_memory_and_slack_events(store, tmp_path):
    memory_dir = tmp_path / "memory" / "tasks"
    memory_dir.mkdir(parents=True)
    old_file = memory_dir / "t1.md"
    old_file.write_text("old")
    import os
    import time

    os.utime(old_file, (time.time() - 91 * 86400, time.time() - 91 * 86400))
    (memory_dir / "t2.md").write_text("fresh")

    store.slack_event_seen("EvOld")
    store.conn.execute("UPDATE slack_events SET received_at = datetime('now', '-8 days')")
    store.conn.commit()
    store.slack_event_seen("EvFresh")

    counts = workspace.sweep_once(store, str(tmp_path / "ws"), str(tmp_path / "memory"), RETENTION)
    assert counts["memories"] == 1
    assert counts["slack_events"] == 1
    assert not old_file.exists()
    assert (memory_dir / "t2.md").exists()
    assert store.slack_event_seen("EvFresh") is True  # still deduped
