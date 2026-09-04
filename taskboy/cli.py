"""operator cli: inject tasks without slack (testing), inspect, cancel, pause intake."""

import argparse
import asyncio
import time

from taskboy import settings
from taskboy.config import load_config
from taskboy.models import CANCELLED, TERMINAL_STATES, utcnow
from taskboy.notify import StdoutNotifier
from taskboy.orchestrator import accept_task
from taskboy.store import Store, TransitionRaced


def main() -> None:
    parser = argparse.ArgumentParser(prog="taskboy")
    sub = parser.add_subparsers(dest="command", required=True)

    inject = sub.add_parser("inject", help="create a task without slack, through the identical intake path")
    inject.add_argument("text")
    inject.add_argument("--user", default="cli")
    inject.add_argument("--channel", default="cli")
    inject.add_argument("--model", default=None, help="model alias override (recorded and audited, MOD-008)")
    inject.add_argument("--watch", action="store_true", help="poll until the task reaches a terminal state")

    sub.add_parser("list", help="show recent tasks")

    cancel = sub.add_parser("cancel", help="cancel a task")
    cancel.add_argument("task_id")

    permissions = sub.add_parser("permissions", help="list a task's permission requests")
    permissions.add_argument("task_id")

    for verb in ("grant", "deny"):
        decide = sub.add_parser(verb, help=f"{verb} a sub-agent's permission request (granting a blocked task resumes it)")
        decide.add_argument("task_id")
        decide.add_argument("kind", choices=["tool", "repo", "access"])
        decide.add_argument("target", help="the tool name, owner/name repository, or system:scope access target")

    resume = sub.add_parser("resume", help="requeue a blocked task on its existing session after fixing the blocker out of band")
    resume.add_argument("task_id")

    sub.add_parser("pause-intake", help="refuse new tasks; running tasks continue (REL-009)")
    sub.add_parser("resume-intake", help="accept new tasks again")
    sub.add_parser("run", help="run the service in the foreground")

    extract = sub.add_parser("assets", help="copy a packaged asset tree out of the installed package")
    extract.add_argument("name", choices=["templates", "deploy"], help="templates = config example, per-service configs, personalities, skill templates, slack manifest; deploy = systemd units, env example, git credential helper")
    extract.add_argument("dest", nargs="?", default=".", help="directory to copy into (default: current directory)")

    from taskboy import setup_wizard

    setup_wizard.add_parser(sub)

    args = parser.parse_args()
    if args.command == "run":
        from taskboy.main import main as run_main

        run_main()
        return
    if args.command == "setup":
        raise SystemExit(setup_wizard.run(args))
    if args.command == "assets":
        from taskboy import assets

        print(f"copied packaged {args.name} to {assets.extract(args.name, args.dest)}")
        return

    store = Store(settings.DB_PATH)
    try:
        if args.command == "inject":
            asyncio.run(_inject(store, args))
        elif args.command == "list":
            _list(store)
        elif args.command == "cancel":
            _cancel(store, args.task_id)
        elif args.command == "permissions":
            _permissions(store, args.task_id)
        elif args.command == "resume":
            _resume(store, args.task_id)
        elif args.command in ("grant", "deny"):
            asyncio.run(_decide_permission(store, args.command, args.task_id, args.kind, args.target))
        elif args.command == "pause-intake":
            store.meta_set("intake_paused", "1")
            print("intake paused")
        elif args.command == "resume-intake":
            store.meta_set("intake_paused", "0")
            print("intake resumed")
    finally:
        store.close()


async def _inject(store: Store, args) -> None:
    config = load_config(settings.CONFIG_PATH)
    message_ts = str(time.time_ns())  # unique per injection, so every inject is a new task
    task, status = await accept_task(
        store,
        config,
        StdoutNotifier(),
        team_id="cli",
        channel_id=args.channel,
        thread_ts=message_ts,
        message_ts=message_ts,
        user_id=args.user,
        text=args.text,
        model_override=args.model,
    )
    print(f"status: {status}" + (f", task: {task.task_id}" if task else ""))
    if not args.watch or task is None:
        return
    last_state = task.state
    while last_state not in TERMINAL_STATES:
        await asyncio.sleep(0.5)
        current = store.get_task(task.task_id)
        assert current is not None
        if current.state != last_state:
            last_state = current.state
            print(f"[{task.task_id}] -> {last_state}")
    final = store.get_task(task.task_id)
    assert final is not None
    print(final.result_summary or final.error or final.blocked_reason or "")


def _list(store: Store) -> None:
    for task in store.recent_tasks(limit=20):
        print(f"{task.task_id}  {task.state:9}  {task.created_at}  {task.request_text[:70]}")


def _permissions(store: Store, task_id: str) -> None:
    if store.get_task(task_id) is None:
        print(f"no such task: {task_id}")
        return
    rows = store.permission_requests_for(task_id)
    if not rows:
        print("no permission requests")
        return
    for row in rows:
        decided = f" by {row['decided_by']}" if row["decided_by"] else ""
        print(f"{row['status']:8} {row['kind']:6} {row['target']}{decided}  ({row['reason'][:80]})")


async def _decide_permission(store: Store, decision_verb: str, task_id: str, kind: str, target: str) -> None:
    from taskboy.task_actions import decide_permission

    decision = "granted" if decision_verb == "grant" else "denied"
    task, status = await decide_permission(store, StdoutNotifier(), task_id, kind, target, decision, "cli")
    print(f"{task_id}: {status}" + (f" (now {task.state})" if task else ""))


def _resume(store: Store, task_id: str) -> None:
    from taskboy.task_actions import resume_task

    task, status = resume_task(store, task_id, "cli")
    print(f"{task_id}: {status}" + (f" (now {task.state})" if task else ""))


def _cancel(store: Store, task_id: str) -> None:
    task = store.get_task(task_id)
    if task is None:
        print(f"no such task: {task_id}")
        return
    if task.state in TERMINAL_STATES:
        print(f"{task_id} is already {task.state}")
        return
    try:
        store.transition(task_id, task.state, CANCELLED, "cancelled via cli", finished_at=utcnow())
        print(f"{task_id} cancelled" + (" (the running session will stop shortly)" if task.state == "running" else ""))
    except TransitionRaced:
        print(f"{task_id} changed state while cancelling — check `taskboy list` and retry")


if __name__ == "__main__":
    main()
