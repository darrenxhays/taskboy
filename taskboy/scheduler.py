"""dashboard-defined task scheduler: fire due schedules as tasks, then advance them.

deterministic Python like the rest of the orchestrator — the loop only enqueues tasks; the classifier
and runner do the model work. schedules run as the system identity (team 'github') with requester 'cli'
so an explicit model choice is honored like any operator-initiated task; lifecycle posts land top-level
in the debug channel with a "Scheduled Task Started" announcement instead of the quip pool.
"""

import asyncio
import logging
import sys
import traceback
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from taskboy.config import Config
from taskboy.issue_runs import start_implementation_run
from taskboy.models import utcnow
from taskboy.orchestrator import accept_task
from taskboy.store import Store

logger = logging.getLogger("taskboy.scheduler")

TICK_SECONDS = 30
SCHEDULE_TEAM = "github"  # notifier treats github-team tasks as silent when no channel is set
SCHEDULE_USER = "cli"  # the operator-task identity; its role permits the model dropdown's overrides

CLI_UPDATE_NEXT_RUN_META_KEY = "cli_update_next_run_at"
CLI_UPDATE_PACKAGE = "claude-agent-sdk"  # bundles the claude cli (see the packaged install.sh)
CLI_UPDATE_SERVICE_UNIT = "taskboy"
# NoNewPrivileges=yes blocks sudo/setuid, so we can't restart the unit from inside this process. instead
# touch this flag; the root-owned taskboy-restart.path unit watches it and restarts us (packaged install.sh)
CLI_UPDATE_RESTART_FLAG = "/run/taskboy/restart-requested"


def _zone(name: str | None):
    if not name:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def next_run_after(kind: str, *, interval_minutes: int | None, at_time: str | None, run_at: str | None, tzname: str | None, after: datetime) -> datetime | None:
    """the next UTC fire time strictly after `after`, or None when the schedule has no future run (a one-off already due).

    `after` must be an aware UTC datetime. used both to seed a schedule's first run (after=now) and to advance it after a fire."""
    if kind == "once":
        target = datetime.fromisoformat(run_at) if run_at else None
        return target if target is not None and target > after else None
    if kind == "interval":
        minutes = max(int(interval_minutes or 0), 1)
        return after + timedelta(minutes=minutes)
    if kind == "daily":
        tz = _zone(tzname)
        local = after.astimezone(tz)
        hour, minute = (int(part) for part in str(at_time or "00:00").split(":"))
        target = datetime.combine(local.date(), time(hour, minute), tzinfo=tz)
        if target <= local:
            target = datetime.combine(local.date() + timedelta(days=1), time(hour, minute), tzinfo=tz)
        return target.astimezone(timezone.utc)
    raise ValueError(f"unknown schedule kind {kind!r}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_implementation_run(request_text: str) -> bool:
    # matches store.active_implementation_run()'s own LIKE '/implementapprovedissues%' check
    return request_text.strip().startswith("/implementapprovedissues")


async def fire_due(store: Store, config: Config, notifier, now: datetime) -> int:
    """fire every schedule due at `now`, advancing each. returns how many fired."""
    fired = 0
    for schedule in store.due_schedules(now.isoformat(timespec="seconds")):
        try:
            # key the task to the slot that triggered it so a double-tick can't create two tasks for one slot
            slot = str(schedule["next_run_at"])
            if _is_implementation_run(schedule["request_text"]):
                # reserve the batch before the coordinator exists so an empty queue never starts one
                task, status, _active = await start_implementation_run(
                    store,
                    config,
                    notifier,
                    request_text=schedule["request_text"],
                    user_id=SCHEDULE_USER,
                    channel_id=config.slack.debug_channel,
                    thread_key=f"schedule:{schedule['id']}@{slot}",
                    model_override=schedule["model_alias"] or None,
                    effort_override=schedule["effort"] or None,
                    schedule_name=schedule["name"],
                )
            else:
                task, status = await accept_task(
                    store,
                    config,
                    notifier,
                    team_id=SCHEDULE_TEAM,
                    channel_id=config.slack.debug_channel,
                    thread_ts=f"schedule:{schedule['id']}@{slot}",
                    message_ts=f"schedule:{schedule['id']}@{slot}",
                    user_id=SCHEDULE_USER,
                    text=schedule["request_text"],
                    model_override=schedule["model_alias"] or None,
                    effort_override=schedule["effort"] or None,
                    schedule_name=schedule["name"],
                )
            task_id = task.task_id if task else None
            nxt = next_run_after(schedule["kind"], interval_minutes=schedule["interval_minutes"], at_time=schedule["at_time"], run_at=schedule["run_at"], tzname=schedule["timezone"], after=now)
            run_count_after = int(schedule["run_count"]) + 1
            exhausted = schedule["max_runs"] is not None and run_count_after >= int(schedule["max_runs"])
            enabled = nxt is not None and not exhausted
            store.record_schedule_fire(schedule["id"], task_id, nxt.isoformat(timespec="seconds") if nxt else slot, enabled)
            fired += 1
            logger.info("schedule %s fired -> task %s (%s), next=%s enabled=%s", schedule["id"], task_id, status, nxt, enabled)
        except Exception as e:
            # advance past this slot so a broken schedule doesn't refire every tick; keep it enabled for a retry next slot
            nxt = next_run_after(schedule["kind"], interval_minutes=schedule["interval_minutes"], at_time=schedule["at_time"], run_at=schedule["run_at"], tzname=schedule["timezone"], after=now)
            store.record_schedule_fire(schedule["id"], None, nxt.isoformat(timespec="seconds") if nxt else str(schedule["next_run_at"]), enabled=nxt is not None)
            store.add_error("scheduler", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"schedule_id": schedule["id"]})
            logger.exception("schedule %s failed to fire", schedule["id"])
    return fired


async def fire_schedule_now(store: Store, config: Config, notifier, schedule: dict):
    """fire a schedule immediately without advancing its next_run_at (a manual run-now from the dashboard)."""
    slot = utcnow()
    if _is_implementation_run(schedule["request_text"]):
        task, status, _active = await start_implementation_run(
            store,
            config,
            notifier,
            request_text=schedule["request_text"],
            user_id=SCHEDULE_USER,
            channel_id=config.slack.debug_channel,
            thread_key=f"schedule:{schedule['id']}:manual@{slot}",
            model_override=schedule["model_alias"] or None,
            effort_override=schedule["effort"] or None,
            schedule_name=schedule["name"],
        )
        return task, status
    return await accept_task(
        store,
        config,
        notifier,
        team_id=SCHEDULE_TEAM,
        channel_id=config.slack.debug_channel,
        thread_ts=f"schedule:{schedule['id']}:manual@{slot}",
        message_ts=f"schedule:{schedule['id']}:manual@{slot}",
        user_id=SCHEDULE_USER,
        text=schedule["request_text"],
        model_override=schedule["model_alias"] or None,
        effort_override=schedule["effort"] or None,
        schedule_name=schedule["name"],
    )


def cli_update_due(next_run_iso: str | None, at_time: str, tzname: str, now: datetime) -> tuple[bool, str]:
    """pure scheduling decision for the off-peak cli auto-update: whether it is due now, and the next_run_at
    to persist either way. `next_run_iso` is None on the very first check (seeds a run time without firing)."""
    if next_run_iso is None:
        nxt = next_run_after("daily", interval_minutes=None, at_time=at_time, run_at=None, tzname=tzname, after=now)
        assert nxt is not None  # "daily" always has a next run
        return False, nxt.isoformat(timespec="seconds")
    next_run = datetime.fromisoformat(next_run_iso)
    if now < next_run:
        return False, next_run_iso
    # advance immediately, before the upgrade runs, so a slow or crashing upgrade can't refire the same slot
    nxt = next_run_after("daily", interval_minutes=None, at_time=at_time, run_at=None, tzname=tzname, after=now)
    assert nxt is not None
    return True, nxt.isoformat(timespec="seconds")


async def _run_subprocess(*args: str) -> tuple[bool, str]:
    """the process-execution seam — patched in unit tests so they never actually pip install."""
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    stdout, _ = await proc.communicate()
    output = (stdout or b"").decode(errors="replace")
    return proc.returncode == 0, output


def _request_restart() -> tuple[bool, str]:
    """the restart seam — patched in unit tests. touches the flag the root-side path unit watches; this needs
    no privileges (NoNewPrivileges-safe), unlike sudo/systemctl from inside the sandboxed service process."""
    try:
        with open(CLI_UPDATE_RESTART_FLAG, "w") as flag:
            flag.write(utcnow())
        return True, CLI_UPDATE_RESTART_FLAG
    except OSError as e:
        return False, str(e)


async def run_cli_update(store: Store) -> None:
    """off-peak upgrade of the claude-agent-sdk package (which bundles the claude cli, packaged install.sh)
    straight from pypi, always the latest release and ungated by ci, then a self-restart so the new version
    takes effect (restarts are safe: reconciliation requeues/resumes interrupted tasks, REL-002/003). a failed
    pip pull is recorded and must never restart the service (a broken upgrade should stay visibly broken, not
    take down a working deployment)."""
    ok, output = await _run_subprocess(sys.executable, "-m", "pip", "install", "--upgrade", CLI_UPDATE_PACKAGE)
    if not ok:
        store.add_error("cli_update", "pip_upgrade_failed", output[-2000:])
        logger.error("cli auto-update: pip upgrade of %s failed: %s", CLI_UPDATE_PACKAGE, output[-500:])
        return
    if "Successfully installed" not in output:
        # pip exits 0 for a no-op upgrade too ("Requirement already satisfied") — only a real
        # version bump needs a restart; skip it so an already-current install doesn't interrupt tasks nightly
        logger.info("cli auto-update: %s already up to date, no restart needed", CLI_UPDATE_PACKAGE)
        return
    logger.info("cli auto-update: %s upgraded; requesting restart of %s", CLI_UPDATE_PACKAGE, CLI_UPDATE_SERVICE_UNIT)
    # sudo/systemctl can't work here (NoNewPrivileges=yes); the root-side taskboy-restart.path unit watches
    # this flag and does the restart outside our sandbox (packaged install.sh)
    requested, restart_output = _request_restart()
    if not requested:
        store.add_error("cli_update", "restart_failed", restart_output[-2000:])
        logger.error("cli auto-update: upgraded %s but restart request failed: %s", CLI_UPDATE_PACKAGE, restart_output[-500:])


async def maybe_run_cli_update(store: Store, config: Config, now: datetime) -> bool:
    """checked every scheduler tick; fires at most once per configured window, off by default (config.cli_update)."""
    cli_update = config.cli_update
    if not cli_update.enabled:
        return False
    due, next_run_at = cli_update_due(store.meta_get(CLI_UPDATE_NEXT_RUN_META_KEY), cli_update.at_time, cli_update.tzname, now)
    store.meta_set(CLI_UPDATE_NEXT_RUN_META_KEY, next_run_at)
    if not due:
        return False
    await run_cli_update(store)
    return True


async def scheduler_loop(store: Store, config: Config, notifier) -> None:
    while True:
        try:
            await fire_due(store, config, notifier, _now())
            await maybe_run_cli_update(store, config, _now())
        except Exception as e:
            store.add_error("scheduler", type(e).__name__, str(e), traceback=traceback.format_exc())
            logger.exception("scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


# shipped defaults, all daily Pacific with the model left blank: discovery/implementation route via the skills'
# own frontmatter; the warm-up is a trivial question that routes to haiku and opens the Claude Max usage
# window before the workday (it replaces the former one-off warmup config — edit or disable it in the UI).
SEED_SCHEDULES = [
    {"seed_key": "implementapprovedissues-daily", "name": "Implement approved issues (daily)", "request_text": "/implementapprovedissues", "at_time": "03:00"},
    {"seed_key": "warmup-daily", "name": "Claude usage-window warm-up (daily)", "request_text": "What does the HTTP status code 200 mean?", "at_time": "04:00"},
]
SEED_TIMEZONE = "America/Los_Angeles"


def seed_default_schedules(store: Store, self_repo: str = "", github_enabled: bool = True) -> None:
    """install the shipped issues schedules exactly once, so an operator can later edit or delete them."""
    if store.meta_get("schedules_seeded") == "1":
        return
    now = _now()
    seeds = list(SEED_SCHEDULES)
    if not github_enabled:
        # the issues pipeline needs github; a github-less install only gets the warm-up
        seeds = [seed for seed in seeds if seed["seed_key"] == "warmup-daily"]
    # daily issue discovery only makes sense against a repo; default to the agent's own when configured
    if self_repo and github_enabled:
        seeds.insert(0, {"seed_key": "discoverissues-daily", "name": "Discover issues (daily)", "request_text": f"/discoverissues {self_repo}", "at_time": "00:00"})
    for seed in seeds:
        nxt = next_run_after("daily", interval_minutes=None, at_time=seed["at_time"], run_at=None, tzname=SEED_TIMEZONE, after=now)
        assert nxt is not None
        store.seed_schedule_once(
            seed["seed_key"],
            name=seed["name"],
            request_text=seed["request_text"],
            model_alias=None,
            kind="daily",
            interval_minutes=None,
            at_time=seed["at_time"],
            run_at=None,
            timezone=SEED_TIMEZONE,
            max_runs=None,
            next_run_at=nxt.isoformat(timespec="seconds"),
            created_by="system",
        )
    store.meta_set("schedules_seeded", "1")
    logger.info("seeded default issues schedules")
