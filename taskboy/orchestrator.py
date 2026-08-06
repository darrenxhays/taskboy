"""deterministic dispatcher: the db is the queue; this loop never talks to a model (the classifier and runners do)."""

import asyncio
import logging
import sqlite3
import time
import traceback
from datetime import datetime

from taskboy.config import Config
from taskboy.models import BLOCKED, CANCELLED, COMPLETED, FAILED, QUEUED, RECEIVED, REFUSED, RUNNING, Task, utcnow
from taskboy.router import RoleRefusal
from taskboy.store import Store, TransitionRaced

logger = logging.getLogger("taskboy.orchestrator")


async def accept_task(
    store: Store,
    config: Config,
    notifier,
    *,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    message_ts: str,
    user_id: str,
    text: str,
    parent_task_id: str | None = None,
    model_override: str | None = None,
    effort_override: str | None = None,
    thread_context: str | None = None,
    pre_classification: dict | None = None,
    persona: str | None = None,
    schedule_name: str | None = None,
    debug_thread_ts: str | None = None,
    debug_permalink: str | None = None,
) -> tuple[Task | None, str]:
    """shared intake for slack and the cli. returns (task, status): created | duplicate | paused | queue_full."""
    if store.meta_get("intake_paused") == "1":
        return None, "paused"
    task, created = store.create_task(
        slack_team_id=team_id,
        slack_channel_id=channel_id,
        slack_thread_ts=thread_ts,
        slack_message_ts=message_ts,
        slack_user_id=user_id,
        request_text=text,
        parent_task_id=parent_task_id,
        model_override=model_override,
        effort_override=effort_override,
        thread_context=thread_context,
        pre_classification=pre_classification,
        persona=persona,
        schedule_name=schedule_name,
        debug_thread_ts=debug_thread_ts,
        debug_permalink=debug_permalink,
    )
    if not created:
        return task, "duplicate"
    ensure_debug = getattr(notifier, "ensure_debug", None)
    if ensure_debug is not None:
        try:
            await ensure_debug(task)
        except Exception as e:
            store.add_error("debug_feed", type(e).__name__, str(e), task_id=task.task_id, traceback=traceback.format_exc(), context={"operation": "ensure_debug"})
            logger.warning("debug thread setup failed for %s", task.task_id, exc_info=True)
    if store.count_tasks(QUEUED) + store.count_tasks(RECEIVED) > config.queue_max:
        # refuse clearly rather than drop silently, and keep the audit trail (ORC-006)
        task = store.transition(task.task_id, RECEIVED, FAILED, "queue full", error="queue_full", finished_at=utcnow())
        await notifier.refused(task, "the task queue is full — try again later")
        return task, "queue_full"
    await notifier.ack(task)
    return task, "created"


class Orchestrator:
    def __init__(self, store: Store, config: Config, classify, run, notifier, memory_root: str | None = None):
        self.store = store
        self.config = config
        self.classify = classify  # async (Task) -> dict of task fields to set when queuing
        self.run = run  # async (Task) -> Outcome
        self.notifier = notifier
        self.memory_root = memory_root  # when set, terminal tasks get a durable markdown summary (MEM-007)
        self.wake = asyncio.Event()
        self.running: dict[str, asyncio.Task] = {}
        self.classifying: set[str] = set()
        self.classify_handles: set[asyncio.Task] = set()
        self.classify_semaphore = asyncio.Semaphore(4)
        self.stopping = False

    async def reconcile(self) -> None:
        """rehome tasks orphaned by a restart, before intake starts (REL-002/003)."""
        released = self.store.release_stale_reservations()
        if released:
            logger.info("released %d stale issue reservation(s) at startup", released)
        for task in self.store.tasks_in_state(RUNNING):
            if task.attempt >= self.config.max_retries:
                failed = self.store.transition(task.task_id, RUNNING, FAILED, "recovery: retry attempts exhausted", error="task was interrupted too many times", finished_at=utcnow())
                self.store.add_event(task.task_id, "recovery", {"action": "failed", "attempt": task.attempt})
                await self._notify_safe(self.notifier.failed, failed, failed.error or "")
            else:
                requeued = self.store.transition(task.task_id, RUNNING, QUEUED, "recovery: orchestrator restarted", resume_session_id=task.session_id, attempt=task.attempt + 1)
                self.store.add_event(task.task_id, "recovery", {"action": "requeued", "attempt": task.attempt + 1, "resume_session_id": task.session_id})
                await self._notify_safe(self.notifier.recovered, requeued)

    async def dispatcher_loop(self) -> None:
        while not self.stopping:
            self.wake.clear()
            self._cancel_tasks_marked_cancelled()
            for task in self.store.tasks_in_state(RECEIVED):
                if task.task_id not in self.classifying:
                    self.classifying.add(task.task_id)
                    handle = asyncio.create_task(self._classify_and_queue(task))
                    self.classify_handles.add(handle)
                    handle.add_done_callback(self.classify_handles.discard)
            while not self.stopping and len(self.running) < self.config.max_concurrency:
                queued = self.store.next_queued()
                if queued is None:
                    break
                try:
                    task = self.store.transition(queued.task_id, QUEUED, RUNNING, "dispatched", started_at=utcnow(), not_before=None)
                except TransitionRaced:
                    continue
                queued_at = _state_change_at(self.store, task.task_id, QUEUED) or task.created_at
                self.store.add_event(task.task_id, "timing", {"stage": "queue_wait", "seconds": _seconds_between(queued_at, task.started_at)})
                self.running[task.task_id] = asyncio.create_task(self._run_supervised(task))
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except TimeoutError:
                pass

    async def shutdown(self) -> None:
        """stop dispatching, interrupt running tasks so they requeue for resume, and let the loop exit."""
        self.stopping = True
        self.wake.set()
        handles = list(self.running.values()) + list(self.classify_handles)
        for handle in handles:
            handle.cancel()
        await asyncio.gather(*handles, return_exceptions=True)

    def _cancel_tasks_marked_cancelled(self) -> None:
        # a cli cancel moves the row to cancelled; here we stop the in-flight coroutine cooperatively
        for task_id, handle in list(self.running.items()):
            current = self.store.get_task(task_id)
            if current and current.state == CANCELLED:
                handle.cancel()

    async def _classify_and_queue(self, task: Task) -> None:
        async with self.classify_semaphore:
            classification_started = time.monotonic()
            try:
                fields = await self.classify(task)
            except asyncio.CancelledError:
                raise  # shutdown mid-classify: row stays received, picked up after restart
            except RoleRefusal as e:
                self.store.add_error("classifier", type(e).__name__, str(e), task_id=task.task_id)
                try:
                    failed = self.store.transition(task.task_id, RECEIVED, FAILED, "role refused routing", error=str(e), finished_at=utcnow())
                    await self._notify_safe(self.notifier.refused, failed, str(e))
                except TransitionRaced:
                    pass
            except Exception as e:
                logger.exception("classification failed for %s", task.task_id)
                self.store.add_error("classifier", type(e).__name__, str(e), task_id=task.task_id, traceback=traceback.format_exc())
                try:
                    failed = self.store.transition(task.task_id, RECEIVED, FAILED, "classification failed", error=str(e), finished_at=utcnow())
                    await self._notify_safe(self.notifier.failed, failed, str(e))
                except TransitionRaced:
                    pass
            else:
                try:
                    if fields.get("task_type") == "unsupported":
                        # clear response, no execution (SLK-010); its own terminal state keeps it out of
                        # failure listings/metrics — it was refused on purpose, not a failure (issue #16)
                        try:
                            refused = self.store.transition(task.task_id, RECEIVED, REFUSED, "unsupported request", error="unsupported request", finished_at=utcnow(), **{k: v for k, v in fields.items() if k in ("classification_json", "task_type", "complexity", "routing_rationale")})
                            await self._notify_safe(self.notifier.refused, refused, "this doesn't look like an engineering task I can take on — try asking for an investigation, a fix, a PR review, or a Jira operation")
                        except sqlite3.IntegrityError as e:
                            # stale schema whose tasks.state CHECK predates REFUSED (issue #58); record it and fall back to FAILED
                            logger.exception("refusal transition failed for %s", task.task_id)
                            self.store.add_error("orchestrator", type(e).__name__, str(e), task_id=task.task_id, traceback=traceback.format_exc(), context={"stage": "refuse"})
                            try:
                                failed = self.store.transition(task.task_id, RECEIVED, FAILED, "refusal transition failed", error=f"refusal transition failed: {e}", finished_at=utcnow())
                                await self._notify_safe(self.notifier.failed, failed, str(e))
                            except (TransitionRaced, sqlite3.IntegrityError):
                                pass
                    else:
                        self.store.transition(task.task_id, RECEIVED, QUEUED, "classified", **fields)
                except TransitionRaced:
                    pass  # cancelled while classifying
            finally:
                self.store.add_event(task.task_id, "timing", {"stage": "classification", "seconds": time.monotonic() - classification_started})
                self.classifying.discard(task.task_id)
                self.wake.set()

    async def _run_supervised(self, task: Task) -> None:
        """one task's lifetime; nothing that happens in here may take down the loop or sibling tasks (REL-005)."""
        await self._notify_safe(self.notifier.started, task)
        # snapshot grants at run start; a grant that lands mid-run makes this differ (see the BLOCKED branch) (§8.4)
        grants_before = self.store.granted_permissions_for(task.task_id)
        try:
            outcome = await self.run(task)
        except asyncio.CancelledError:
            if self.stopping:
                # shutdown: requeue for resume after restart; a user cancel already moved the row to cancelled
                try:
                    self.store.transition(task.task_id, RUNNING, QUEUED, "requeued: orchestrator shutting down", resume_session_id=task.session_id, attempt=task.attempt + 1)
                except TransitionRaced:
                    pass
            else:
                # a user cancel already moved the row to cancelled before this coroutine was torn down
                self._release_reservations(task.task_id)
            self._release(task.task_id)
            raise
        except Exception as e:
            logger.exception("runner crashed for %s", task.task_id)
            self.store.add_error("orchestrator", type(e).__name__, str(e), task_id=task.task_id, traceback=traceback.format_exc(), context={"stage": "run"})
            try:
                failed = self.store.transition(task.task_id, RUNNING, FAILED, "runner crashed", error=str(e), finished_at=utcnow())
                self._record_terminal_timing(failed)
                await self._notify_safe(self.notifier.failed, failed, str(e))
            except TransitionRaced:
                pass
            self._release_reservations(task.task_id)
            self._release(task.task_id)
            return
        try:
            if outcome.state == COMPLETED:
                done = self.store.transition(task.task_id, RUNNING, COMPLETED, "runner finished", result_summary=outcome.result_summary, reply=outcome.reply, session_id=outcome.session_id, cost_usd=outcome.cost_usd, num_turns=outcome.num_turns, finished_at=utcnow())
                self._record_terminal_timing(done)
                self._write_memory(done)
                self._release_reservations(done.task_id)
                await self._notify_safe(self.notifier.completed, done)
            elif outcome.state == BLOCKED:
                blocked = self.store.transition(task.task_id, RUNNING, BLOCKED, "runner blocked", blocked_reason=outcome.blocked_reason, session_id=outcome.session_id, cost_usd=outcome.cost_usd, num_turns=outcome.num_turns)
                self._record_terminal_timing(blocked)
                self._write_memory(blocked)
                # a grant can land while the session is still RUNNING; decide_permission only resumes a BLOCKED
                # task, so that grant would strand this one here. compare against the run-start snapshot rather than
                # timestamps (utcnow is second-resolution): a new grant this run makes the set differ, so resume it (§8.4).
                if self.store.granted_permissions_for(task.task_id) != grants_before:
                    try:
                        self.store.transition(task.task_id, BLOCKED, QUEUED, "resumed: permission granted during run", resume_session_id=blocked.session_id)
                        self.store.add_event(task.task_id, "permission_decision", {"actor": "orchestrator", "decision": "resume", "note": "grant landed while session was running"})
                    except TransitionRaced:
                        pass
                else:
                    pending = self.store.pending_questions_for(task.task_id)
                    ask = getattr(self.notifier, "questions", None)
                    if pending is not None and ask is not None:
                        await self._notify_safe(ask, blocked, pending["questions"])
                    else:
                        await self._notify_safe(self.notifier.blocked, blocked)
            elif outcome.retryable and task.attempt < self.config.max_retries:
                # transient claude usage/session limit: come back after the recorded reset instead of failing outright
                requeued = self.store.transition(task.task_id, RUNNING, QUEUED, "requeued: session hit a usage limit", resume_session_id=outcome.session_id, attempt=task.attempt + 1, not_before=outcome.retry_not_before)
                self.store.add_event(task.task_id, "recovery", {"action": "requeued_rate_limit", "attempt": task.attempt + 1, "not_before": outcome.retry_not_before})
                await self._notify_safe(self.notifier.recovered, requeued)
            else:
                failed = self.store.transition(task.task_id, RUNNING, FAILED, "runner reported failure", error=outcome.error, session_id=outcome.session_id, cost_usd=outcome.cost_usd, num_turns=outcome.num_turns, finished_at=utcnow())
                self.store.add_error("runner", "session_failure", outcome.error, task_id=task.task_id)
                self._record_terminal_timing(failed)
                self._write_memory(failed)
                self._release_reservations(failed.task_id)
                await self._notify_safe(self.notifier.failed, failed, outcome.error)
        except TransitionRaced:
            pass  # cancelled out from under us; the cancel transition already recorded it
        finally:
            self._release(task.task_id)

    def _release(self, task_id: str) -> None:
        self.running.pop(task_id, None)
        self.wake.set()

    def _release_reservations(self, task_id: str) -> None:
        # a no-op for ordinary tasks; restores an implementation_queued batch to approved if task_id was its
        # coordinator and it just died or finished without processing all of its reserved rows
        released = self.store.release_reserved_issues(task_id)
        if released:
            logger.info("released %d reserved issue(s) held by %s", released, task_id)

    def _record_terminal_timing(self, task: Task) -> None:
        end = task.finished_at or utcnow()
        self.store.add_event(task.task_id, "timing", {"stage": "run", "seconds": _seconds_between(task.started_at, end)})
        self.store.add_event(task.task_id, "timing", {"stage": "total", "seconds": _seconds_between(task.created_at, end)})

    def _write_memory(self, task: Task) -> None:
        if self.memory_root is None:
            return
        try:
            from taskboy import memory

            memory.write_summary(self.memory_root, task, self.store.artifacts_for(task.task_id))
        except Exception:
            logger.exception("memory summary write failed for %s", task.task_id)

    async def _notify_safe(self, fn, *args) -> None:
        # a notifier outage must never take a task down with it
        try:
            await fn(*args)
        except Exception:
            logger.exception("notifier call failed")


def _seconds_between(start: str | None, end: str | None) -> float:
    if not start or not end:
        return 0.0
    try:
        return max((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 0.0)
    except ValueError:
        return 0.0


def _state_change_at(store: Store, task_id: str, state: str) -> str | None:
    return store.last_event_ts(task_id, "state_change", "to", state)
