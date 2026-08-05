"""cheap attempt-first answers for mentions that do not need tools or investigation."""

import asyncio
import logging
import re
import time
import traceback
from collections import deque

from agent_harness import memory, settings
from agent_harness.classifier import validate_classification
from agent_harness.config import Config, ConfigError, role_for
from agent_harness.llm import structured_call
from agent_harness.models import Task
from agent_harness.personality import load as load_personality
from agent_harness.prompts import CLASSIFICATION_SCHEMA, QUICK_ANSWER_SCHEMA, TRIAGE_SCHEMA, dm_chat_prompt, triage_prompt, trim_context
from agent_harness.redact import redactor
from agent_harness.store import Store

logger = logging.getLogger("agent_harness.quick")

TASK_ID_RE = re.compile(r"\bt\d{8}-[0-9a-f]{8}\b")


class QuickAnswer:
    def __init__(self, store: Store, config: Config):
        self.store = store
        self.config = config
        section = config.raw.get("quick_answer") or {}
        tier = section.get("tier", "haiku")
        models = config.raw.get("models") or {}
        if tier not in models:
            raise ConfigError(f"quick_answer.tier {tier!r} is not in the model catalog")
        self.model_alias = str(tier)
        self.model_id = str(models[tier]["id"])
        self.timeout_seconds = float(section.get("timeout_seconds", 20))
        self.max_per_user_per_hour = int(section.get("max_per_user_per_hour", 30))
        self._attempts: dict[str, deque[float]] = {}

    async def try_answer(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        user_id: str,
        text: str,
        parent: Task | None,
        team_id: str,
        message_ts: str,
        thread_context: str | None = None,
        debug_thread_ts: str | None = None,
        debug_permalink: str | None = None,
    ) -> tuple[Task | None, dict | None]:
        """return either a recorded answer task or a reusable classification."""
        started = time.monotonic()
        try:
            if not self._allow_attempt(user_id, started):
                logger.info("quick answer rate cap reached for %s", user_id)
                return None, None
            context = self._task_context(text, parent)
            personality = load_personality(self.config.personality_path)
            github = self.config.raw.get("github") or {}
            approved_repos = github.get("approved_repos") or []
            role = role_for(self.config.roles, user_id)
            if role is not None and role.repos is not None:
                approved_repos = [repo for repo in approved_repos if repo in role.repos]
            self_repo = str(github.get("self_repo") or "")
            prompt = triage_prompt(
                redactor.redact(text)[:6000],
                self.config.agent_name,
                context,
                approved_repos,
                ["github", "aws", "sentry", "jira", "confluence"],
                trim_context(thread_context),
                personality[0] if personality else None,
                self_repo if self_repo in approved_repos else None,
            )
            async with asyncio.timeout(self.timeout_seconds):
                result, usage = await self._call_model(prompt)
            if result.get("action") == "classify":
                validated = validate_classification(result)
                classification = {key: validated[key] for key in CLASSIFICATION_SCHEMA["required"]}
                if usage:
                    classification["_triage_usage"] = usage
                return None, classification
            if result.get("action") != "answer" or not str(result.get("answer") or "").strip():
                return None, None
            answer = str(result["answer"]).strip()
            latency = time.monotonic() - started
            task = self.store.record_quick_answer(
                slack_team_id=team_id,
                slack_channel_id=channel_id,
                slack_thread_ts=thread_ts,
                slack_message_ts=message_ts,
                slack_user_id=user_id,
                request_text=text,
                answer_text=answer,
                model_alias=self.model_alias,
                model_id=self.model_id,
                parent_task_id=parent.task_id if parent else None,
                latency_s=latency,
                debug_thread_ts=debug_thread_ts,
                debug_permalink=debug_permalink,
            )
            if personality:
                self.store.add_event(task.task_id, "personality", {"hash": personality[1], "path": self.config.personality_path})
            if usage:
                self.store.add_usage(task.task_id, "quick_answer", self.model_id, **usage)
            logger.info("quick answer completed for %s in %.3fs", task.task_id, latency)
            return task, None
        except Exception as e:
            logger.warning("quick answer escalated after failure: %s", e)
            self.store.add_error("quick_answer", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"user_id": user_id, "channel_id": channel_id})
            return None, None

    async def chat(
        self,
        *,
        channel_id: str,
        user_id: str,
        text: str,
        team_id: str,
        message_ts: str,
        history: str | None = None,
    ) -> tuple[Task | None, str]:
        """Answer one DM without ever creating a queued task."""
        started = time.monotonic()
        if not self._allow_attempt(user_id, started):
            logger.info("quick chat rate cap reached for %s", user_id)
            return None, "rate_limited"
        try:
            personality = load_personality(self.config.personality_path)
            prompt = dm_chat_prompt(
                redactor.redact(text)[:6000],
                self.config.agent_name,
                trim_context(history, 6000),
                personality[0] if personality else None,
            )
            async with asyncio.timeout(self.timeout_seconds):
                result, usage = await self._call_chat_model(prompt)
            if result.get("action") != "answer" or not str(result.get("answer") or "").strip():
                return None, "escalate"
            answer = str(result["answer"]).strip()
            task = self.store.record_quick_answer(
                slack_team_id=team_id,
                slack_channel_id=channel_id,
                slack_thread_ts=message_ts,
                slack_message_ts=message_ts,
                slack_user_id=user_id,
                request_text=text,
                answer_text=answer,
                model_alias=self.model_alias,
                model_id=self.model_id,
                latency_s=time.monotonic() - started,
            )
            if personality:
                self.store.add_event(task.task_id, "personality", {"hash": personality[1], "path": self.config.personality_path})
            if usage:
                self.store.add_usage(task.task_id, "quick_answer", self.model_id, **usage)
            return task, "answer"
        except Exception as e:
            logger.warning("quick chat escalated after failure: %s", e)
            self.store.add_error("quick_answer", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"user_id": user_id, "channel_id": channel_id, "mode": "dm"})
            return None, "escalate"

    def _allow_attempt(self, user_id: str, now: float) -> bool:
        cutoff = now - 3600
        for existing_user, existing_attempts in list(self._attempts.items()):
            while existing_attempts and existing_attempts[0] <= cutoff:
                existing_attempts.popleft()
            if not existing_attempts:
                del self._attempts[existing_user]
        attempts = self._attempts.setdefault(user_id, deque())
        if len(attempts) >= self.max_per_user_per_hour:
            return False
        attempts.append(now)
        return True

    def _task_context(self, text: str, parent: Task | None) -> str | None:
        blocks: list[str] = []
        seen: set[str] = set()
        if parent is not None:
            blocks.append(_task_block(parent, memory.read_summary(settings.MEMORY_ROOT, parent.task_id)))
            seen.add(parent.task_id)
        for task_id in TASK_ID_RE.findall(text)[:3]:
            if task_id in seen:
                continue
            task = self.store.get_task(task_id)
            if task is not None:
                blocks.append(_task_block(task, memory.read_summary(settings.MEMORY_ROOT, task.task_id)))
                seen.add(task_id)
        return "\n\n".join(blocks) or None

    async def _call_model(self, prompt: str) -> tuple[dict, dict | None]:
        """the per-call API seam, kept conversation-free and patched in tests."""
        return await structured_call(self.model_id, prompt, TRIAGE_SCHEMA)

    async def _call_chat_model(self, prompt: str) -> tuple[dict, dict | None]:
        return await structured_call(self.model_id, prompt, QUICK_ANSWER_SCHEMA)


def _task_block(task: Task, summary: str | None) -> str:
    detail = task.result_summary or task.error or task.blocked_reason or ""
    lines = [
        f"task {task.task_id}",
        f"state: {task.state}",
        f"model: {task.model_alias or 'n/a'}",
        f"created: {task.created_at}",
        f"finished: {task.finished_at or 'n/a'}",
    ]
    if detail:
        lines.append(f"result/error: {detail[:500]}")
    if summary:
        lines.append(f"memory summary: {summary[:500]}")
    return redactor.redact("\n".join(lines))[:1500]
