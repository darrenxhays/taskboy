import sys
from unittest.mock import ANY, AsyncMock

from taskboy import cli, settings
from taskboy.models import BLOCKED, QUEUED, RECEIVED, RUNNING
from taskboy.store import Store


def _create_task(store):
    task, created = store.create_task(
        slack_team_id="T1",
        slack_channel_id="C1",
        slack_thread_ts="1",
        slack_message_ts="1",
        slack_user_id="U1",
        request_text="do the thing",
    )
    assert created
    return task


def test_resume_requeues_blocked_task_on_its_session(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "cli.db")
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    store = Store(db_path)
    task = _create_task(store)
    store.transition(task.task_id, RECEIVED, QUEUED)
    store.transition(task.task_id, QUEUED, RUNNING, session_id="sess-cli")
    store.transition(task.task_id, RUNNING, BLOCKED, "needs operator action")
    monkeypatch.setattr(sys, "argv", ["taskboy", "resume", task.task_id])

    cli.main()

    assert "resumed" in capsys.readouterr().out
    resumed = store.get_task(task.task_id)
    assert resumed.state == QUEUED
    assert resumed.resume_session_id == "sess-cli"
    store.close()


def test_resume_reports_missing_task(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setattr(sys, "argv", ["taskboy", "resume", "t20990101-deadbeef"])

    cli.main()

    assert "not found" in capsys.readouterr().out


def test_grant_accepts_access_permission(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "cli.db"))
    decide_permission = AsyncMock()
    monkeypatch.setattr(cli, "_decide_permission", decide_permission)
    monkeypatch.setattr(sys, "argv", ["taskboy", "grant", "task-id", "access", "aws:production"])

    cli.main()

    decide_permission.assert_awaited_once_with(ANY, "grant", "task-id", "access", "aws:production")
