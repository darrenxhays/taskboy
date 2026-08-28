"""best-effort, token-free Slack debug threads built only from stored text."""

import json
import logging
import time
import traceback

from taskboy.models import Task
from taskboy.mrkdwn import to_mrkdwn
from taskboy.redact import redactor
from taskboy.store import Store

logger = logging.getLogger("taskboy.debug_feed")

SNIPPET_THRESHOLD = 3500

# repeats of the same (component, message) are suppressed this long; in-memory, resets on restart
SYSTEM_ERROR_COOLDOWN_SECONDS = 900


def _now() -> float:
    return time.monotonic()


class DebugFeed:
    def __init__(self, client, store: Store, channel_id: str, dashboard_url: str = ""):
        self.client = client
        self.store = store
        self.channel_id = channel_id
        self.dashboard_url = dashboard_url.rstrip("/")
        # (component, message) -> last_posted_monotonic
        self._system_error_state: dict[tuple[str, str], float] = {}

    def _record_failure(self, operation: str, error: Exception, task_id: str | None = None) -> None:
        logger.warning("debug feed %s failed", operation, exc_info=True)
        try:
            self.store.add_error("debug_feed", type(error).__name__, str(error), task_id=task_id, traceback=traceback.format_exc(), context={"operation": operation})
        except Exception:
            logger.exception("could not record debug feed failure")

    async def open_thread(self, *, user_id: str, channel_id: str, request_text: str, user_profile: dict | None = None) -> tuple[str, str] | None:
        profile = user_profile or {}
        name = profile.get("real_name") or profile.get("display_name") or profile.get("username")
        requester = f"<@{user_id}>" + (f" ({name})" if name else "")
        source_channel = f"<#{channel_id}>" if channel_id else "n/a"
        text = f"Requester: {requester}\nSource channel: {source_channel}\nRequest:\n{request_text}"
        try:
            response = await self.client.chat_postMessage(channel=self.channel_id, text=redactor.redact(to_mrkdwn(text)), unfurl_links=False, unfurl_media=False)
            thread_ts = str(response.get("ts") or "")
            if not thread_ts:
                raise RuntimeError("debug root post returned no timestamp")
            link = await self.client.chat_getPermalink(channel=self.channel_id, message_ts=thread_ts)
            permalink = str(link.get("permalink") or "")
            return thread_ts, permalink
        except Exception as e:
            self._record_failure("open_thread", e)
            return None

    async def post(self, thread_ts: str | None, text: str, task_id: str | None = None) -> None:
        if not thread_ts:
            return
        try:
            await self.client.chat_postMessage(channel=self.channel_id, thread_ts=thread_ts, text=redactor.redact(to_mrkdwn(text))[:39000], unfurl_links=False, unfurl_media=False)
        except Exception as e:
            self._record_failure("post", e, task_id)

    async def post_file(self, thread_ts: str | None, filename: str, title: str, content: str, initial_comment: str = "", task_id: str | None = None) -> None:
        if not thread_ts:
            return
        try:
            await self.client.files_upload_v2(
                channel=self.channel_id,
                thread_ts=thread_ts,
                filename=filename,
                title=title,
                content=redactor.redact(content),
                initial_comment=redactor.redact(to_mrkdwn(initial_comment)),
            )
        except Exception as e:
            self._record_failure("post_file", e, task_id)
            await self.post(thread_ts, f"{initial_comment}\n\n{content[:SNIPPET_THRESHOLD]}", task_id)

    async def classified(self, task: Task) -> None:
        await self.post(task.debug_thread_ts, f"Classification: {task.classification_json or '{}'}\nRouting: model={task.model_alias or 'n/a'} ({task.model_id or 'n/a'}), profile={task.profile or 'n/a'}\nRationale: {task.routing_rationale or 'n/a'}", task.task_id)

    async def started(self, task: Task) -> None:
        await self.post(task.debug_thread_ts, f"Started task `{task.task_id}`\nModel: {task.model_alias or 'n/a'} ({task.model_id or 'n/a'})\nProfile: {task.profile or 'n/a'}\nAttempt: {task.attempt}", task.task_id)

    async def progress(self, task: Task, message: str) -> None:
        await self.post(task.debug_thread_ts, f"Progress: {message}", task.task_id)

    async def prompt_file(self, task: Task, prompt_text: str) -> None:
        await self.post_file(task.debug_thread_ts, f"{task.task_id}-prompt.md", f"Task {task.task_id} sub-agent prompt", prompt_text, "Full sub-agent prompt attached.", task.task_id)

    async def completed(self, task: Task) -> None:
        report = task.result_summary or ""
        if len(report) > SNIPPET_THRESHOLD:
            await self.post_file(task.debug_thread_ts, f"{task.task_id}-report.md", f"Task {task.task_id} Final Report", report, "Task completed. Full Final Report attached.", task.task_id)
        else:
            await self.post(task.debug_thread_ts, f"Task completed.\n\n{report}", task.task_id)
        footer = self._metrics(task)
        if self.dashboard_url:
            footer += f"\n[Rate this task]({self.dashboard_url}/tasks/{task.task_id}/feedback)"
        await self.post(task.debug_thread_ts, footer, task.task_id)

    async def failed(self, task: Task, error: str) -> None:
        await self.post(task.debug_thread_ts, f"Task failed: {error}\n\n{self._metrics(task)}", task.task_id)

    async def blocked(self, task: Task) -> None:
        await self.post(task.debug_thread_ts, f"Task blocked: {task.blocked_reason or ''}\n\n{self._metrics(task)}", task.task_id)

    async def questions(self, task: Task, questions: str) -> None:
        await self.post(task.debug_thread_ts, f"Task waiting on requester answers:\n{questions}\n\n{self._metrics(task)}", task.task_id)

    async def recovered(self, task: Task) -> None:
        await self.post(task.debug_thread_ts, f"Task recovered and requeued (attempt {task.attempt}).", task.task_id)

    async def refused(self, task: Task, reason: str) -> None:
        await self.post(task.debug_thread_ts, f"Task refused: {reason}", task.task_id)

    async def quick_answer(self, task: Task, latency: float | None = None, usage: dict | None = None) -> None:
        if latency is None:
            for event in self.store.events_for(task.task_id):
                if event["kind"] != "quick_answer":
                    continue
                try:
                    latency = float(json.loads(event["detail_json"]).get("latency_s") or 0)
                except (TypeError, ValueError, json.JSONDecodeError):
                    latency = 0.0
        detail = f"Quick answer selected.\nLatency: {latency if latency is not None else 0:.3f}s\nAnswer:\n{task.reply or task.result_summary or ''}"
        await self.post(task.debug_thread_ts, detail, task.task_id)
        await self.post(task.debug_thread_ts, self._metrics(task), task.task_id)

    async def intake_refusal(self, thread_ts: str | None, reason: str) -> None:
        await self.post(thread_ts, f"Intake refused: {reason}")

    async def system_error(self, component: str, message: str) -> bool:
        """returns False when the repeat was suppressed, so callers don't mark it as delivered."""
        key = (component, message)
        now = _now()
        last_posted = self._system_error_state.get(key)
        if last_posted is not None and now - last_posted < SYSTEM_ERROR_COOLDOWN_SECONDS:
            return False
        # evict entries whose cooldown has fully elapsed, to keep the dict from growing unbounded
        self._system_error_state = {k: v for k, v in self._system_error_state.items() if now - v < SYSTEM_ERROR_COOLDOWN_SECONDS}
        # claim the window before the post so concurrent calls suppress instead of all posting
        self._system_error_state[key] = now
        try:
            await self.client.chat_postMessage(channel=self.channel_id, text=redactor.redact(to_mrkdwn(f"System error ({component}): {message}")), unfurl_links=False, unfurl_media=False)
        except Exception as e:
            self._record_failure("system_error", e)
            # the claim didn't hold, and restoring an expired last_posted is the same as having no entry
            self._system_error_state.pop(key, None)
            return False
        return True

    def _metrics(self, task: Task) -> str:
        usage_lines = []
        for row in self.store.usage_for(task.task_id):
            usage_lines.append(f"- {row['source']} ({row['model']}): input={row['input_tokens'] or 0}, output={row['output_tokens'] or 0}, cache_read={row['cache_read_tokens'] or 0}, cache_write={row['cache_write_tokens'] or 0}, cost=${float(row['cost_usd'] or 0):.6f}")
        timing_lines = []
        for event in self.store.events_for(task.task_id):
            if event["kind"] != "timing":
                continue
            try:
                detail = json.loads(event["detail_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            timing_lines.append(f"- {detail.get('stage', 'unknown')}: {float(detail.get('seconds') or 0):.3f}s")
        return "Usage:\n" + ("\n".join(usage_lines) or "- none recorded") + "\nTiming:\n" + ("\n".join(timing_lines) or "- none recorded")
