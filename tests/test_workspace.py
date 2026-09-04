import os
import subprocess

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


def test_sweep_retries_leaked_workspace_instead_of_forgetting_it(store, make_task, tmp_path, monkeypatch, caplog):
    workspaces = tmp_path / "ws"
    task = make_task("root-owned leftovers")
    old_completed = _finish(store, task, COMPLETED, workspaces)
    task_id = task.task_id
    _age(store, task_id, 4)

    # simulate rmtree silently failing on a root-owned file (ignore_errors=True swallows it)
    monkeypatch.setattr(workspace.shutil, "rmtree", lambda *a, **k: None)

    with caplog.at_level("WARNING", logger="taskboy.workspace"):
        counts = workspace.sweep_once(store, str(workspaces), str(tmp_path / "memory"), RETENTION)
    assert counts["workspaces"] == 0  # the leak wasn't actually removed, so it isn't counted
    assert old_completed.exists()
    assert store.get_task(task_id).workspace_path is not None  # left set so the next sweep retries it
    assert any("could not be fully removed" in record.message for record in caplog.records)


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

    store.add_error("runner", "timeout", "old failure")
    store.conn.execute("UPDATE errors SET ts = datetime('now', '-31 days')")
    store.conn.commit()

    counts = workspace.sweep_once(store, str(tmp_path / "ws"), str(tmp_path / "memory"), RETENTION)
    assert counts["memories"] == 1
    assert counts["slack_events"] == 1
    assert counts["errors"] == 1
    assert not old_file.exists()
    assert (memory_dir / "t2.md").exists()
    assert store.slack_event_seen("EvFresh") is True  # still deduped


def test_create_writes_pre_push_hook(tmp_path):
    ws = workspace.create(str(tmp_path / "ws"), "t1")
    hook = workspace.hooks_dir(ws) / "pre-push"
    assert hook.read_text() == workspace.PRE_PUSH_HOOK
    assert os.access(hook, os.X_OK)


def test_pre_push_hook_blocks_non_agent_refs(tmp_path):
    hook = tmp_path / "pre-push"
    hook.write_text(workspace.PRE_PUSH_HOOK)

    def push_refs(*lines):
        # stdin format git feeds pre-push: <local ref> <local sha> <remote ref> <remote sha>
        stdin = "".join(f"{ref} 1111 {ref} 2222\n" for ref in lines)
        return subprocess.run(["sh", str(hook)], input=stdin, text=True, capture_output=True).returncode

    assert push_refs("refs/heads/agent/t123-fix") == 0
    assert push_refs() == 0  # nothing to push
    assert push_refs("refs/heads/main") != 0
    assert push_refs("refs/tags/v1.2.3") != 0  # release tags go through mcp__github__create_release
    assert push_refs("refs/heads/agent/t123-fix", "refs/heads/main") != 0  # one bad ref fails the whole push
