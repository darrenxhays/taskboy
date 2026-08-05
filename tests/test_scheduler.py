from datetime import datetime, timedelta, timezone

import pytest

from agent_harness import scheduler
from agent_harness.config import CliUpdateConfig, SlackConfig
from agent_harness.scheduler import cli_update_due, fire_due, fire_schedule_now, maybe_run_cli_update, next_run_after, run_cli_update, seed_default_schedules
from tests.conftest import RecordingNotifier, make_config

UTC = timezone.utc


def make_schedule(store, *, kind, next_run_at, model_alias=None, effort=None, interval_minutes=None, at_time=None, run_at=None, tzname=None, max_runs=None, request_text="/discoverissues example-org/agent-harness"):
    return store.create_schedule(
        name="t",
        request_text=request_text,
        model_alias=model_alias,
        effort=effort,
        kind=kind,
        interval_minutes=interval_minutes,
        at_time=at_time,
        run_at=run_at,
        timezone=tzname,
        max_runs=max_runs,
        next_run_at=next_run_at,
        created_by="boss@example.com",
    )


# -- next_run_after ---------------------------------------------------------


def test_interval_next_is_after_plus_minutes():
    after = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    assert next_run_after("interval", interval_minutes=30, at_time=None, run_at=None, tzname=None, after=after) == after + timedelta(minutes=30)


def test_daily_next_respects_timezone():
    # 13:00 America/Los_Angeles in July (PDT, UTC-7) is 20:00 UTC
    after = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    nxt = next_run_after("daily", interval_minutes=None, at_time="13:00", run_at=None, tzname="America/Los_Angeles", after=after)
    assert nxt == datetime(2026, 7, 22, 20, 0, tzinfo=UTC)
    # once the time has passed today, it rolls to tomorrow
    after2 = datetime(2026, 7, 22, 21, 0, tzinfo=UTC)
    nxt2 = next_run_after("daily", interval_minutes=None, at_time="13:00", run_at=None, tzname="America/Los_Angeles", after=after2)
    assert nxt2 == datetime(2026, 7, 23, 20, 0, tzinfo=UTC)


def test_once_returns_target_only_when_future():
    after = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
    future = datetime(2026, 8, 1, 21, 0, tzinfo=UTC).isoformat()
    assert next_run_after("once", interval_minutes=None, at_time=None, run_at=future, tzname=None, after=after) == datetime(2026, 8, 1, 21, 0, tzinfo=UTC)
    past = datetime(2026, 1, 1, 0, 0, tzinfo=UTC).isoformat()
    assert next_run_after("once", interval_minutes=None, at_time=None, run_at=past, tzname=None, after=after) is None


# -- fire_due ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_fires_creates_task_and_advances(store):
    now = datetime(2026, 7, 22, 20, 0, 30, tzinfo=UTC)
    sched = make_schedule(store, kind="daily", at_time="13:00", tzname="America/Los_Angeles", next_run_at="2026-07-22T20:00:00+00:00")
    fired = await fire_due(store, make_config(), RecordingNotifier(), now)
    assert fired == 1
    row = store.get_schedule(sched["id"])
    assert row["run_count"] == 1 and row["enabled"] == 1
    assert row["next_run_at"] > now.isoformat()  # advanced into the future
    assert row["last_task_id"] is not None
    task = store.get_task(row["last_task_id"])
    assert task.request_text == "/discoverissues example-org/agent-harness" and task.slack_user_id == "cli"


@pytest.mark.asyncio
async def test_fired_task_carries_schedule_name_and_debug_channel(store):
    now = datetime(2026, 7, 22, 20, 0, 30, tzinfo=UTC)
    sched = make_schedule(store, kind="daily", at_time="13:00", tzname="America/Los_Angeles", next_run_at="2026-07-22T20:00:00+00:00")
    config = make_config(slack=SlackConfig(team_id="T1", allowed_channels=["C1"], debug_channel="C-DEBUG"))
    await fire_due(store, config, RecordingNotifier(), now)
    task = store.get_task(store.get_schedule(sched["id"])["last_task_id"])
    assert task.schedule_name == "t"
    assert task.slack_channel_id == "C-DEBUG"


@pytest.mark.asyncio
async def test_model_alias_becomes_task_override(store):
    now = datetime(2026, 7, 22, 20, 0, 30, tzinfo=UTC)
    sched = make_schedule(store, kind="daily", at_time="13:00", tzname="America/Los_Angeles", next_run_at="2026-07-22T20:00:00+00:00", model_alias="fable")
    await fire_due(store, make_config(), RecordingNotifier(), now)
    task = store.get_task(store.get_schedule(sched["id"])["last_task_id"])
    assert task.model_override == "fable"


@pytest.mark.asyncio
async def test_schedule_effort_becomes_task_effort_override(store):
    now = datetime(2026, 7, 22, 20, 0, 30, tzinfo=UTC)
    sched = make_schedule(store, kind="daily", at_time="13:00", tzname="America/Los_Angeles", next_run_at="2026-07-22T20:00:00+00:00", effort="xhigh")
    await fire_due(store, make_config(), RecordingNotifier(), now)
    task = store.get_task(store.get_schedule(sched["id"])["last_task_id"])
    assert task.effort_override == "xhigh"


@pytest.mark.asyncio
async def test_schedule_without_effort_leaves_task_override_unset(store):
    now = datetime(2026, 7, 22, 20, 0, 30, tzinfo=UTC)
    sched = make_schedule(store, kind="daily", at_time="13:00", tzname="America/Los_Angeles", next_run_at="2026-07-22T20:00:00+00:00")
    await fire_due(store, make_config(), RecordingNotifier(), now)
    task = store.get_task(store.get_schedule(sched["id"])["last_task_id"])
    assert task.effort_override is None


@pytest.mark.asyncio
async def test_interval_disables_after_max_runs(store):
    sched = make_schedule(store, kind="interval", interval_minutes=30, max_runs=2, next_run_at="2026-07-22T12:00:00+00:00")
    await fire_due(store, make_config(), RecordingNotifier(), datetime(2026, 7, 22, 12, 0, 1, tzinfo=UTC))
    assert store.get_schedule(sched["id"])["enabled"] == 1
    # advance to the new next_run_at and fire again -> hits max_runs -> disabled
    row = store.get_schedule(sched["id"])
    await fire_due(store, make_config(), RecordingNotifier(), datetime.fromisoformat(row["next_run_at"]) + timedelta(seconds=1))
    final = store.get_schedule(sched["id"])
    assert final["run_count"] == 2 and final["enabled"] == 0
    assert len(store.list_tasks(limit=50)) == 2


@pytest.mark.asyncio
async def test_once_disables_after_firing(store):
    sched = make_schedule(store, kind="once", run_at="2026-07-22T12:00:00+00:00", next_run_at="2026-07-22T12:00:00+00:00", max_runs=1)
    await fire_due(store, make_config(), RecordingNotifier(), datetime(2026, 7, 22, 12, 0, 5, tzinfo=UTC))
    final = store.get_schedule(sched["id"])
    assert final["run_count"] == 1 and final["enabled"] == 0


@pytest.mark.asyncio
async def test_not_due_does_not_fire(store):
    make_schedule(store, kind="daily", at_time="13:00", tzname="America/Los_Angeles", next_run_at="2026-07-23T20:00:00+00:00")
    fired = await fire_due(store, make_config(), RecordingNotifier(), datetime(2026, 7, 22, 20, 0, 0, tzinfo=UTC))
    assert fired == 0


# -- single-coordinator guard on the implementation schedule -----------------


@pytest.mark.asyncio
async def test_implementation_schedule_skips_while_a_coordinator_is_active(store):
    # an in-flight /implementapprovedissues coordinator (e.g. dashboard-started, or still running from a prior fire)
    store.create_task(slack_team_id="github", slack_channel_id="", slack_thread_ts="prior", slack_message_ts="prior", slack_user_id="github", request_text="/implementapprovedissues")
    now = datetime(2026, 7, 22, 20, 0, 30, tzinfo=UTC)
    sched = make_schedule(store, kind="daily", at_time="13:00", tzname="America/Los_Angeles", next_run_at="2026-07-22T20:00:00+00:00", request_text="/implementapprovedissues")
    fired = await fire_due(store, make_config(), RecordingNotifier(), now)
    assert fired == 1  # the schedule still advances so it doesn't spin on the same slot
    row = store.get_schedule(sched["id"])
    assert row["last_task_id"] is None  # no second coordinator task was created
    assert row["next_run_at"] > now.isoformat()
    assert len(store.list_tasks(limit=50)) == 1  # only the pre-existing coordinator task exists


@pytest.mark.asyncio
async def test_implementation_schedule_fires_normally_with_no_active_coordinator(store):
    now = datetime(2026, 7, 22, 20, 0, 30, tzinfo=UTC)
    sched = make_schedule(store, kind="daily", at_time="13:00", tzname="America/Los_Angeles", next_run_at="2026-07-22T20:00:00+00:00", request_text="/implementapprovedissues")
    fired = await fire_due(store, make_config(), RecordingNotifier(), now)
    assert fired == 1
    row = store.get_schedule(sched["id"])
    assert row["last_task_id"] is not None


@pytest.mark.asyncio
async def test_run_now_reports_already_running_instead_of_starting_a_second_coordinator(store):
    store.create_task(slack_team_id="github", slack_channel_id="", slack_thread_ts="prior", slack_message_ts="prior", slack_user_id="github", request_text="/implementapprovedissues")
    sched = make_schedule(store, kind="daily", at_time="13:00", tzname="America/Los_Angeles", next_run_at="2026-07-22T20:00:00+00:00", request_text="/implementapprovedissues")
    task, status = await fire_schedule_now(store, make_config(), RecordingNotifier(), store.get_schedule(sched["id"]))
    assert task is None
    assert status == "already_running"
    assert len(store.list_tasks(limit=50)) == 1


# -- seeding ----------------------------------------------------------------


def test_seed_is_idempotent(store, monkeypatch):
    monkeypatch.setattr(scheduler, "_now", lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
    seed_default_schedules(store, self_repo="example-org/agent-harness")
    seed_default_schedules(store, self_repo="example-org/agent-harness")
    seeded = [s for s in store.list_schedules() if s["seed_key"]]
    assert {s["seed_key"] for s in seeded} == {"discoverissues-daily", "implementapprovedissues-daily", "warmup-daily"}
    assert "/discoverissues example-org/agent-harness" in {s["request_text"] for s in seeded}
    assert all(s["kind"] == "daily" and s["model_alias"] is None for s in seeded)
    warmup = next(s for s in seeded if s["seed_key"] == "warmup-daily")
    assert warmup["at_time"] == "04:00" and warmup["enabled"] == 1


def test_seed_skips_discovery_without_self_repo(store, monkeypatch):
    monkeypatch.setattr(scheduler, "_now", lambda: datetime(2026, 7, 22, 12, 0, tzinfo=UTC))
    seed_default_schedules(store)
    seeded = [s for s in store.list_schedules() if s["seed_key"]]
    assert {s["seed_key"] for s in seeded} == {"implementapprovedissues-daily", "warmup-daily"}


# -- off-peak cli auto-update (issue #67) ------------------------------------


def test_cli_update_due_seeds_next_run_without_firing():
    after = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    due, next_run = cli_update_due(None, "02:00", "America/Los_Angeles", after)
    assert due is False
    assert next_run > after.isoformat()


def test_cli_update_due_fires_once_the_window_passes():
    scheduled = datetime(2026, 7, 22, 9, 0, tzinfo=UTC).isoformat(timespec="seconds")
    due, next_run = cli_update_due(scheduled, "02:00", "America/Los_Angeles", datetime(2026, 7, 22, 9, 0, 5, tzinfo=UTC))
    assert due is True
    assert next_run > scheduled  # advanced to tomorrow's window


def test_cli_update_due_not_yet(store):
    scheduled = datetime(2026, 7, 23, 9, 0, tzinfo=UTC).isoformat(timespec="seconds")
    due, next_run = cli_update_due(scheduled, "02:00", "America/Los_Angeles", datetime(2026, 7, 22, 9, 0, tzinfo=UTC))
    assert due is False
    assert next_run == scheduled  # unchanged: still waiting for that slot


@pytest.mark.asyncio
async def test_maybe_run_cli_update_is_a_noop_when_disabled(store):
    fired = await maybe_run_cli_update(store, make_config(), datetime(2026, 7, 22, 10, 0, tzinfo=UTC))
    assert fired is False
    assert store.meta_get(scheduler.CLI_UPDATE_NEXT_RUN_META_KEY) is None


@pytest.mark.asyncio
async def test_maybe_run_cli_update_seeds_then_fires(store, monkeypatch):
    calls = []

    async def fake_run_cli_update(s):
        calls.append("ran")

    monkeypatch.setattr(scheduler, "run_cli_update", fake_run_cli_update)
    config = make_config()
    config.cli_update = CliUpdateConfig(enabled=True, at_time="02:00", tzname="America/Los_Angeles")

    # first tick: seeds next_run_at, does not fire
    assert await maybe_run_cli_update(store, config, datetime(2026, 7, 22, 5, 0, tzinfo=UTC)) is False
    assert calls == []
    next_run_at = store.meta_get(scheduler.CLI_UPDATE_NEXT_RUN_META_KEY)
    assert next_run_at is not None

    # second tick, past the window: fires exactly once and advances
    assert await maybe_run_cli_update(store, config, datetime.fromisoformat(next_run_at) + timedelta(seconds=1)) is True
    assert calls == ["ran"]
    assert store.meta_get(scheduler.CLI_UPDATE_NEXT_RUN_META_KEY) != next_run_at


@pytest.mark.asyncio
async def test_run_cli_update_requests_restart_only_on_successful_upgrade(store, monkeypatch):
    calls = []
    restart_calls = []

    async def fake_subprocess(*args):
        calls.append(args)
        return True, "Successfully installed claude-agent-sdk-1.2.3"

    def fake_restart():
        restart_calls.append(True)
        return True, scheduler.CLI_UPDATE_RESTART_FLAG

    monkeypatch.setattr(scheduler, "_run_subprocess", fake_subprocess)
    monkeypatch.setattr(scheduler, "_request_restart", fake_restart)
    await run_cli_update(store)
    assert len(calls) == 1 and "pip" in calls[0]  # only the pip upgrade shells out
    assert len(restart_calls) == 1  # restart requested via the flag file
    assert store.recent_errors() == []


@pytest.mark.asyncio
async def test_run_cli_update_skips_restart_when_already_up_to_date(store, monkeypatch):
    restart_calls = []

    async def fake_subprocess(*args):
        return True, "Requirement already satisfied: claude-agent-sdk in /usr/lib/python3.12/site-packages"

    monkeypatch.setattr(scheduler, "_run_subprocess", fake_subprocess)
    monkeypatch.setattr(scheduler, "_request_restart", lambda: (restart_calls.append(True), (True, "flag"))[1])
    await run_cli_update(store)
    assert restart_calls == []  # nothing changed, no reason to bounce the service
    assert store.recent_errors() == []


@pytest.mark.asyncio
async def test_run_cli_update_never_restarts_after_a_failed_upgrade(store, monkeypatch):
    calls = []
    restart_calls = []

    async def fake_subprocess(*args):
        calls.append(args)
        return False, "pypi is down"

    monkeypatch.setattr(scheduler, "_run_subprocess", fake_subprocess)
    monkeypatch.setattr(scheduler, "_request_restart", lambda: (restart_calls.append(True), (True, "flag"))[1])
    await run_cli_update(store)
    assert len(calls) == 1  # never got to the restart request
    assert restart_calls == []
    errors = store.recent_errors()
    assert len(errors) == 1 and errors[0]["kind"] == "pip_upgrade_failed"


@pytest.mark.asyncio
async def test_run_cli_update_records_error_when_restart_fails(store, monkeypatch):
    async def fake_subprocess(*args):
        return True, "Successfully installed claude-agent-sdk-1.2.3"

    monkeypatch.setattr(scheduler, "_run_subprocess", fake_subprocess)
    monkeypatch.setattr(scheduler, "_request_restart", lambda: (False, "permission denied"))
    await run_cli_update(store)
    errors = store.recent_errors()
    assert len(errors) == 1 and errors[0]["kind"] == "restart_failed"
