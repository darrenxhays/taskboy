from agent_harness import memory
from agent_harness.models import COMPLETED, QUEUED, RECEIVED, RUNNING


def _finish(store, task, summary="all done"):
    store.transition(task.task_id, RECEIVED, QUEUED, "classified")
    store.transition(task.task_id, QUEUED, RUNNING, "dispatched")
    return store.transition(task.task_id, RUNNING, COMPLETED, "finished", result_summary=summary)


def test_write_and_read_summary(tmp_path, store, make_task):
    task = _finish(store, make_task("investigate the bug"))
    store.add_artifact(task.task_id, "pull_request", "org/repo#7", "https://github.com/org/repo/pull/7")
    path = memory.write_summary(str(tmp_path), task, store.artifacts_for(task.task_id))
    content = path.read_text()
    assert task.task_id in content
    assert "all done" in content
    assert "org/repo#7" in content
    assert memory.read_summary(str(tmp_path), task.task_id) == content


def test_summaries_are_redacted(tmp_path, store, make_task):
    task = _finish(store, make_task(), summary="leaked ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    path = memory.write_summary(str(tmp_path), task, [])
    assert "ghp_" not in path.read_text()


def test_parent_context_is_immediate_parent_only(tmp_path, store):
    root, _ = store.create_task(slack_team_id="T1", slack_channel_id="C1", slack_thread_ts="1.1", slack_message_ts="1.1", slack_user_id="U1", request_text="root")
    root = _finish(store, root, summary="root summary")
    memory.write_summary(str(tmp_path), root, [])
    child, _ = store.create_task(slack_team_id="T1", slack_channel_id="C1", slack_thread_ts="1.1", slack_message_ts="2.2", slack_user_id="U1", request_text="follow-up", parent_task_id=root.task_id)
    assert "root summary" in memory.parent_context(store, str(tmp_path), child)
    # a task with no parent gets nothing injected (MEM-010)
    unrelated, _ = store.create_task(slack_team_id="T1", slack_channel_id="C2", slack_thread_ts="9.9", slack_message_ts="9.9", slack_user_id="U1", request_text="other thread")
    assert memory.parent_context(store, str(tmp_path), unrelated) is None


def test_result_summary_is_bounded(tmp_path, store, make_task):
    task = _finish(store, make_task(), summary="x" * 8000)
    content = memory.write_summary(str(tmp_path), task, []).read_text()
    result = content.split("## result\n", 1)[1]
    assert len(result) < 4100
    assert "result truncated at 4000 chars" in result
