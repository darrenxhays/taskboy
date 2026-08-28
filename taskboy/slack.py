"""slack intake and outbound notifications: bolt asyncapp in socket mode.

intake: app_mention -> dedup -> authz allowlist -> accept_task -> ack in the originating thread.
outbound: SlackNotifier implements the notifier interface over the bolt web client.

all decision logic lives in plain functions (handle_mention, authorization_failure) so tests
never need bolt objects or http mocking — the bolt wiring in build() is a thin shell.
"""

import logging
import re
import time
import traceback

from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from taskboy import settings, skills
from taskboy.config import Config, Role, SlackConfig, role_for
from taskboy.models import BLOCKED, EFFORT_LEVELS, QUEUED, Task
from taskboy.mrkdwn import to_mrkdwn
from taskboy.orchestrator import accept_task
from taskboy.personality import load as load_text_file
from taskboy.redact import redactor
from taskboy.slack_users import cached_user_profile
from taskboy.started_messages import pick as pick_started_message
from taskboy.store import Store, TransitionRaced

logger = logging.getLogger("taskboy.slack")

THREAD_CONTEXT_MAX_MESSAGES = 30
THREAD_CONTEXT_MAX_CHARS = 6000
THREAD_CONTEXT_BOT_MESSAGE_MAX_CHARS = 500
SNIPPET_THRESHOLD = 3500

# non-transient channel-delivery failures (#83): the channel stays read-only/gone on retry, so
# these are worth a fallback delivery instead of a raise-and-lose-the-message loop.
UNDELIVERABLE_CHANNEL_ERRORS = frozenset({"restricted_action_read_only_channel", "channel_not_found", "not_in_channel", "is_archived"})


def authorization_failure(slack: SlackConfig, roles: dict[str, Role], team_id: str, channel_id: str, user_id: str, channel_type: str | None = None, bot_name: str = "Agent") -> str | None:
    """returns a refusal reason, or None when the mention is authorized (SLK-002).

    socket mode's app-level token already binds us to one workspace, so a missing team field is tolerated.
    an empty channel allowlist means "any channel the bot was invited to" — the invite is the gate.
    """
    if slack.team_id and team_id and team_id != slack.team_id:
        return f"this workspace is not authorized to use {bot_name}"
    if slack.allowed_channels and not channel_id.startswith("D") and channel_type != "im" and channel_id not in slack.allowed_channels:
        return f"this channel is not on {bot_name}'s allowlist — ask an admin to add it"
    if role_for(roles, user_id) is None:
        return f"you do not have a configured role for {bot_name} — ask an admin to add you"
    return None


def clean_text(text: str, bot_user_id: str) -> str:
    return re.sub(rf"<@{re.escape(bot_user_id)}>", "", text).strip()


def is_help_request(text: str) -> bool:
    """narrow on purpose: broader matching (e.g. "please help me") would swallow ordinary task requests."""
    stripped = text.strip().lower()
    return stripped == "help" or bool(re.match(r"/help(\s|$)", stripped))


def help_text(config: Config) -> str:
    loaded = load_text_file(config.help_path)
    return loaded[0] if loaded else ""


def normalize_effort(raw: str | None) -> str | None:
    """tolerant mapping of admin-typed effort onto the sdk's levels; None when unrecognizable."""
    if not raw:
        return None
    cleaned = raw.strip().lower().replace("-", "")
    if not cleaned:
        return None
    if cleaned in EFFORT_LEVELS:
        return cleaned
    for level in EFFORT_LEVELS:
        if level.startswith(cleaned):
            return level
    return None


def extract_overrides(text: str, model_aliases: list[str]) -> tuple[str | None, str | None, str]:
    """pull the legacy `model:alias` or admin `!<alias>[-<effort>]` override directive out of the request text, stripping it from the returned text; matches only known catalog aliases so ordinary exclamation marks in prose are never eaten."""
    match = re.search(r"\bmodel:([a-z0-9-]+)\b", text)
    if match:
        return match.group(1), None, (text[: match.start()] + text[match.end() :]).strip()
    if not model_aliases:
        return None, None, text
    aliases = sorted(model_aliases, key=len, reverse=True)
    pattern = rf"(?<![\w!])!({'|'.join(re.escape(a) for a in aliases)})(?:-([a-zA-Z-]+))?\b"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None, None, text
    model = match.group(1).lower()
    effort = normalize_effort(match.group(2))
    return model, effort, (text[: match.start()] + text[match.end() :]).strip()


async def fetch_thread_transcript(client, channel_id: str, thread_ts: str, exclude_ts: str, store: Store | None = None, debug=None, bot_user_id: str | None = None, bot_name: str = "Agent") -> str | None:
    messages: list[dict] = []
    cursor = None
    for _ in range(5):
        kwargs = {"channel": channel_id, "ts": thread_ts, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            response = await client.conversations_replies(**kwargs)
        except Exception as e:
            logger.warning("conversations.replies failed for %s/%s", channel_id, thread_ts, exc_info=True)
            if store is not None:
                store.add_error("slack", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"operation": "conversations_replies", "channel_id": channel_id, "thread_ts": thread_ts})
            if debug is not None:
                await debug.system_error("slack", f"conversations.replies failed: {e}")
            if not messages:
                return None
            break
        messages.extend(response.get("messages") or [])
        next_cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    lines = []
    for message in messages:
        text = str(message.get("text") or "").strip()
        if message.get("ts") == exclude_ts or not text:
            continue
        user = str(message.get("user") or "")
        if user == bot_user_id:
            lines.append(f"{bot_name}: {text[:THREAD_CONTEXT_BOT_MESSAGE_MAX_CHARS]}")
        else:
            lines.append(f"<@{user}>: {text}")
    transcript = "\n".join(lines[-THREAD_CONTEXT_MAX_MESSAGES:])
    if len(transcript) > THREAD_CONTEXT_MAX_CHARS:
        marker = "(earlier messages omitted)\n"
        transcript = marker + transcript[-(THREAD_CONTEXT_MAX_CHARS - len(marker)) :]
    return transcript or None


async def fetch_dm_transcript(client, channel_id: str, exclude_ts: str, bot_user_id: str, store: Store | None = None, bot_name: str = "Agent", limit: int = 20) -> str | None:
    """Fetch recent DM context in chronological order; failures never block a reply."""
    try:
        response = await client.conversations_history(channel=channel_id, limit=limit)
    except Exception as e:
        logger.warning("conversations.history failed for %s", channel_id, exc_info=True)
        if store is not None:
            store.add_error("slack", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"operation": "conversations_history", "channel_id": channel_id})
        return None
    lines = []
    for message in reversed(response.get("messages") or []):
        text = str(message.get("text") or "").strip()
        if message.get("ts") == exclude_ts or not text:
            continue
        user = str(message.get("user") or "")
        speaker = bot_name if user == bot_user_id or message.get("bot_id") else f"<@{user}>"
        lines.append(f"{speaker}: {text}")
    transcript = "\n".join(lines[-limit:])
    if len(transcript) > THREAD_CONTEXT_MAX_CHARS:
        marker = "(earlier messages omitted)\n"
        transcript = marker + transcript[-(THREAD_CONTEXT_MAX_CHARS - len(marker)) :]
    return transcript or None


async def fetch_user_profile(store: Store, client, user_id: str, debug=None) -> dict | None:
    try:
        return await cached_user_profile(store, client, user_id)
    except Exception as e:
        logger.warning("users.info failed for %s", user_id, exc_info=True)
        store.add_error("slack", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"operation": "users_info", "user_id": user_id})
        if debug is not None:
            await debug.system_error("slack", f"users.info failed for {user_id}: {e}")
        return store.get_slack_user(user_id)


async def handle_thread_answer(store: Store, notifier, task: Task, answer_text: str, user_id: str) -> str:
    """route a thread reply as the answers to a blocked task's pending questions, and requeue it for resume."""
    recorded = store.answer_questions(task.task_id, answer_text, user_id)
    if recorded is None:
        return "no_pending_questions"
    store.add_event(task.task_id, "questions_answered", {"user": user_id})
    try:
        store.transition(task.task_id, BLOCKED, QUEUED, "resumed: requester answered questions", resume_session_id=task.session_id)
    except TransitionRaced:
        return "not_blocked"  # cancelled or already resumed; the answer stays recorded for the next run's prompt
    await notifier.answer(task.slack_channel_id, task.slack_thread_ts, "Got it — resuming with your answers.")
    return "answered"


async def handle_thread_reply(store: Store, notifier, event: dict, event_id: str | None, bot_user_id: str) -> str:
    """a plain (no-mention) channel reply only matters when the thread's latest task is waiting on answers; everything else is ignored."""
    if event.get("subtype") or event.get("bot_id") or event.get("user") == bot_user_id:
        return "ignored"
    text = str(event.get("text") or "").strip()
    thread_ts = str(event.get("thread_ts") or "")
    if not thread_ts or not text or f"<@{bot_user_id}>" in text:
        return "ignored"  # mentions are the app_mention handler's job
    task = store.latest_task_in_thread(str(event.get("channel") or ""), thread_ts)
    if task is None or task.state != BLOCKED or task.slack_user_id != event.get("user") or store.pending_questions_for(task.task_id) is None:
        return "ignored"
    if event_id and store.slack_event_seen(event_id):
        return "duplicate_event"
    return await handle_thread_answer(store, notifier, task, text, str(event.get("user") or ""))


async def handle_mention(store: Store, config: Config, notifier, event: dict, event_id: str | None, bot_user_id: str, quick=None, client=None) -> tuple[Task | None, str]:
    """the full intake decision for one app_mention event. returns (task, status) for tests and logging."""
    if event_id and store.slack_event_seen(event_id):
        return None, "duplicate_event"  # slack redelivery — never a second task (SLK-008)
    team_id = event.get("team", "")
    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    message_ts = event.get("ts", "")
    thread_ts = event.get("thread_ts") or message_ts
    text = clean_text(event.get("text", ""), bot_user_id)
    debug = getattr(notifier, "debug", None)
    profile = await fetch_user_profile(store, client, user_id, debug) if client is not None and user_id else store.get_slack_user(user_id)
    debug_ref = await debug.open_thread(user_id=user_id, channel_id=channel_id, request_text=text, user_profile=profile) if debug is not None else None
    debug_thread_ts, debug_permalink = debug_ref or (None, None)

    refusal = authorization_failure(config.slack, config.roles, team_id, channel_id, user_id, str(event.get("channel_type") or ""), bot_name=config.agent_name)
    if refusal:
        store.record_intake_denial(team_id, channel_id, user_id, refusal)
        if debug is not None:
            await debug.intake_refusal(debug_thread_ts, refusal)
        await notifier.refuse_intake(channel_id, thread_ts, refusal)
        return None, "unauthorized"

    model_aliases = list((config.raw.get("models") or {}).keys())
    override, effort, text = extract_overrides(text, model_aliases)
    role = role_for(config.roles, user_id)
    if not (role and role.model_override):
        override = None  # directive stripped but not honored for non-admins
        effort = None
    if not text:
        reason = f"tell me what you need, e.g. `@{config.agent_name} investigate PROJ-123`"
        if debug is not None:
            await debug.intake_refusal(debug_thread_ts, reason)
        await notifier.refuse_intake(channel_id, thread_ts, reason)
        return None, "empty"

    if is_help_request(text) and (guide := help_text(config)):
        await notifier.answer(channel_id, thread_ts, guide)
        if debug is not None:
            await debug.post(debug_thread_ts, "Answered `/help` — no task created.")
        return None, "help"
    invocation = skills.parse_invocation(text)
    if text.startswith("/"):
        names = skills.available(settings.SKILLS_ROOT)
        name = invocation[0] if invocation else ""
        if invocation is None or name not in names:
            available = ", ".join(f"/{item}" for item in names)
            reason = f"unknown skill /{name} — available: {available}"
            if debug is not None:
                await debug.intake_refusal(debug_thread_ts, reason)
            await notifier.refuse_intake(channel_id, thread_ts, reason)
            return None, "unknown_skill"

    # a mention inside an existing thread is a follow-up: link lineage
    parent = store.latest_task_in_thread(channel_id, thread_ts) if thread_ts != message_ts else None
    # a requester reply while their task is waiting on follow-up questions is the answer, not a new task
    if parent is not None and parent.state == BLOCKED and parent.slack_user_id == user_id and store.pending_questions_for(parent.task_id) is not None:
        answer_status = await handle_thread_answer(store, notifier, parent, text, user_id)
        if answer_status == "answered":
            if debug is not None:
                await debug.post(parent.debug_thread_ts, f"Requester answered follow-up questions; task `{parent.task_id}` resumes.", parent.task_id)
            return parent, "answered"
        if answer_status == "not_blocked":
            # the answer was recorded for the next run, but the task raced out of blocked (cancelled or already resumed)
            await notifier.answer(channel_id, thread_ts, "I recorded your answers, but that task is no longer waiting on them.")
            return parent, "answer_raced"
        # no_pending_questions: the round was answered out from under us — treat as a normal follow-up
    thread_context = await fetch_thread_transcript(client, channel_id, thread_ts, message_ts, store, debug, bot_user_id, config.agent_name) if thread_ts != message_ts and client is not None else None
    quick_attempted = quick is not None and override is None and effort is None and invocation is None
    pre_classification = None
    triage_usage = None
    if quick_attempted:
        answered_task, pre_classification = await quick.try_answer(
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
            text=text,
            parent=parent,
            team_id=team_id,
            message_ts=message_ts,
            thread_context=thread_context,
            debug_thread_ts=debug_thread_ts,
            debug_permalink=debug_permalink,
        )
        if answered_task is not None:
            if debug is not None:
                await debug.quick_answer(answered_task)
            answer = answered_task.reply or answered_task.result_summary or ""
            if debug_permalink:
                answer += f"\n\n[Additional Details]({debug_permalink})"
            await notifier.answer(channel_id, thread_ts, answer)
            return None, "quick_answer"
        if pre_classification is not None:
            pre_classification = dict(pre_classification)
            triage_usage = pre_classification.pop("_triage_usage", None)
    task, status = await accept_task(
        store,
        config,
        notifier,
        team_id=team_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        message_ts=message_ts,
        user_id=user_id,
        text=text,
        parent_task_id=parent.task_id if parent else None,
        model_override=override,
        effort_override=effort,
        thread_context=thread_context,
        pre_classification=pre_classification,
        debug_thread_ts=debug_thread_ts,
        debug_permalink=debug_permalink,
    )
    if triage_usage and task is not None and status != "duplicate":
        store.add_usage(task.task_id, "triage", quick.model_id, **triage_usage)
    if quick_attempted and task is not None and status != "duplicate":
        store.add_event(task.task_id, "quick_escalated", {"escalated": True})
    if status in ("paused", "queue_full"):
        reason = "intake is paused right now — try again soon" if status == "paused" else "the task queue is full — try again later"
        if debug is not None:
            await debug.intake_refusal(debug_thread_ts, reason)
        await notifier.refuse_intake(channel_id, thread_ts, reason)
    return task, status


async def handle_dm(store: Store, config: Config, notifier, event: dict, event_id: str | None, bot_user_id: str, quick, client) -> str:
    """Handle one direct message as chat unless the user explicitly mentions the bot."""
    if event.get("subtype") or event.get("bot_id") or event.get("user") == bot_user_id:
        return "ignored"
    text = str(event.get("text") or "").strip()
    if f"<@{bot_user_id}>" in text:
        _, status = await handle_mention(store, config, notifier, event, event_id, bot_user_id, quick=quick, client=client)
        return status
    if event_id and store.slack_event_seen(event_id):
        return "duplicate_event"
    team_id = str(event.get("team") or "")
    channel_id = str(event.get("channel") or "")
    user_id = str(event.get("user") or "")
    message_ts = str(event.get("ts") or "")
    refusal = authorization_failure(config.slack, config.roles, team_id, channel_id, user_id, str(event.get("channel_type") or ""), bot_name=config.agent_name)
    if refusal:
        store.record_intake_denial(team_id, channel_id, user_id, refusal)
        await notifier.answer(channel_id, None, refusal)
        return "unauthorized"
    if not text:
        return "ignored"
    if is_help_request(text) and (guide := help_text(config)):
        await notifier.answer(channel_id, None, guide)
        return "help"
    if quick is None:
        await notifier.answer(channel_id, None, f"This needs a full task. Mention `@{config.agent_name}` to start one.")
        return "escalate"
    history = await fetch_dm_transcript(client, channel_id, message_ts, bot_user_id, store, config.agent_name)
    task, status = await quick.chat(channel_id=channel_id, user_id=user_id, text=text, team_id=team_id, message_ts=message_ts, history=history)
    if status == "rate_limited":
        await notifier.answer(channel_id, None, f"I’ve hit the quick-chat limit for you this hour. Try again later, or mention `@{config.agent_name}` to start a full task.")
    elif status == "escalate":
        await notifier.answer(channel_id, None, f"This needs a full task. Mention `@{config.agent_name}` to start one.")
    else:
        await notifier.answer(channel_id, None, task.reply or task.result_summary or "")
    return status


class SlackNotifier:
    """posts lifecycle updates to the originating thread (SLK-005/006/007). all outbound text is redacted."""

    def __init__(self, client, progress_min_interval_seconds: int = 60, ack_reaction: bool = True, debug=None, task_started_messages_path: str | None = None, store: Store | None = None, reviewer_name: str = "Reviewer", dashboard_url: str = ""):
        self.client = client
        self.progress_min_interval_seconds = progress_min_interval_seconds
        self.ack_reaction = ack_reaction
        self._last_progress: dict[str, float] = {}
        self.debug = debug
        self.task_started_messages_path = task_started_messages_path
        self.store = store
        self.reviewer_name = reviewer_name
        self.dashboard_url = dashboard_url.rstrip("/")

    def _record_error(self, operation: str, error: Exception, task_id: str | None = None) -> None:
        if self.store is not None:
            self.store.add_error("slack", type(error).__name__, str(error), task_id=task_id, traceback=traceback.format_exc(), context={"operation": operation})

    async def _deliver_fallback(self, user_id: str, text: str, task_id: str | None = None) -> None:
        """dm the requester when the channel is read-only/gone (#83); debug feed if that fails."""
        if user_id:
            try:
                opened = await self.client.conversations_open(users=user_id)
                channel_id = str((opened.get("channel") or {}).get("id") or "")
                if not channel_id:
                    raise RuntimeError("slack did not return a dm channel")
                await self.client.chat_postMessage(channel=channel_id, text=f"_(the original channel is read-only or unreachable, so I'm DMing you instead)_\n{text}")
                return
            except Exception as e:
                self._record_error("dm_fallback", e, task_id)
        if self.debug is not None:
            await self.debug.system_error("slack", f"channel is read-only/unreachable and no DM fallback landed; message: {text}")

    async def _post(self, task: Task, text: str, **kwargs) -> None:
        redacted = redactor.redact(to_mrkdwn(text))
        post = {"channel": task.slack_channel_id, "text": redacted, **kwargs}
        if task.slack_team_id == "github":
            if not task.slack_channel_id:
                return
        else:
            post["thread_ts"] = task.slack_thread_ts
        try:
            await self.client.chat_postMessage(**post)
        except Exception as e:
            self._record_error("chat_postMessage", e, task.task_id)
            if isinstance(e, SlackApiError) and e.response.get("error") in UNDELIVERABLE_CHANNEL_ERRORS:
                # system-origin tasks (slack_team_id = "github", slack_user_id = "cli") have no real Slack user to DM — go straight to debug
                fallback_user_id = task.slack_user_id if task.slack_team_id != "github" else ""
                await self._deliver_fallback(fallback_user_id, redacted, task.task_id)
                return
            raise

    async def progress(self, task: Task, message: str) -> None:
        # agent-declared milestones, rate-limited per task (SLK-006); dropped posts stay in the audit trail
        if self.debug is not None:
            await self.debug.progress(task, message)
        if task.schedule_name:
            return  # debug feed already threads this into the debug channel; skip the duplicate top-level post
        now = time.monotonic()
        if now - self._last_progress.get(task.task_id, 0.0) < self.progress_min_interval_seconds:
            return
        self._last_progress[task.task_id] = now
        await self._post(task, message)

    async def ensure_debug(self, task: Task) -> None:
        if self.debug is not None and not task.debug_thread_ts:
            opened = await self.debug.open_thread(user_id=task.slack_user_id, channel_id=task.slack_channel_id, request_text=task.request_text, user_profile=self.store.get_slack_user(task.slack_user_id) if self.store else None)
            if opened:
                task.debug_thread_ts, task.debug_permalink = opened
                if self.store is not None:
                    self.store.set_fields(task.task_id, debug_thread_ts=task.debug_thread_ts, debug_permalink=task.debug_permalink)

    async def ack(self, task: Task) -> None:
        await self.ensure_debug(task)
        if task.schedule_name:
            return  # scheduled tasks announce themselves in started(); no "On it." quip
        if self.ack_reaction and task.slack_team_id != "github":
            try:
                await self.client.reactions_add(channel=task.slack_channel_id, timestamp=task.slack_message_ts, name="eyes")
                return
            except Exception as e:
                self._record_error("reactions_add", e, task.task_id)
                logger.warning("reactions.add failed for %s — falling back to text ack", task.task_id)
        await self._post(task, "On it.")

    async def started(self, task: Task) -> None:
        if task.schedule_name:
            details = f"Details: Model: {task.model_alias or 'n/a'} Task ID: {task.task_id}"
            line = f"[{details}]({task.debug_permalink})" if task.debug_permalink else details
            text = f"Scheduled Task Started {task.schedule_name}\n{line}"
            if self.debug is not None:
                await self.debug.classified(task)
                await self.debug.started(task)
            await self._post(task, text, unfurl_links=False, unfurl_media=False)
            return
        label = f"Details - Model: {task.model_alias} - Task ID: {task.task_id}" if task.model_alias else f"Details - Task ID: {task.task_id}"
        task_line = f"[{label}]({task.debug_permalink})" if task.debug_permalink else ""
        if task.persona == "reviewer":
            selected = pick_started_message(self.task_started_messages_path, "reviewer")
            if selected:
                text = selected.replace("{reviewer_name}", self.reviewer_name) + (f"\n{task_line}" if task_line else "")
            else:
                text = f"{self.reviewer_name} started a PR review" + (f"\n{task_line}" if task_line else "")
        else:
            selected = pick_started_message(self.task_started_messages_path, "agent")
            if selected:
                text = selected + (f"\n{task_line}" if task_line else "")
            else:
                model = f" on `{task.model_alias}`" if task.model_alias else ""
                text = f"Task started{model}." + (f"\n{task_line}" if task_line else "")
        if self.debug is not None:
            await self.debug.classified(task)
            await self.debug.started(task)
        await self._post(task, text, unfurl_links=False, unfurl_media=False)

    async def completed(self, task: Task) -> None:
        self._last_progress.pop(task.task_id, None)
        text = task.reply or task.result_summary or ""
        links: list[str] = []
        seen: set[str] = set()
        if self.store is not None:
            for artifact in self.store.artifacts_for(task.task_id):
                url = str(artifact.get("url") or "")
                kind = artifact.get("kind")
                if kind not in ("pull_request", "jira_issue") or not url or url in text or url in seen:
                    continue
                seen.add(url)
                links.append(f"{'PR' if kind == 'pull_request' else 'Jira'}: {url}")
        links_text = "\n".join(links)
        if links_text:
            text = f"{text}\n\n{links_text}" if text else links_text
        if task.persona == "reviewer":
            text = f"*{self.reviewer_name} finished a PR review*\n{text}"
        elif not text:
            text = "*Done*"
        if self.debug is not None:
            await self.debug.completed(task)
        if task.schedule_name:
            return  # debug feed already threads this into the debug channel; skip the duplicate top-level post
        if len(text) > SNIPPET_THRESHOLD and task.slack_team_id != "github":
            try:
                initial_comment = "*Done* — full response attached."
                if links_text:
                    initial_comment += f"\n\n{links_text}"
                await self.client.files_upload_v2(
                    channel=task.slack_channel_id,
                    thread_ts=task.slack_thread_ts,
                    filename=f"{task.task_id}-report.md",
                    title=f"Task {task.task_id} report",
                    content=redactor.redact(task.reply or task.result_summary or ""),
                    initial_comment=redactor.redact(to_mrkdwn(initial_comment)),
                )
                return
            except Exception as e:
                self._record_error("files_upload_v2", e, task.task_id)
                logger.warning("report upload failed for %s — falling back to inline post", task.task_id, exc_info=True)
                text = f"*Done*\n{text}"
        if task.slack_team_id == "github" and not task.slack_channel_id:
            return
        await self._post(task, text, unfurl_links=False, unfurl_media=False)

    async def failed(self, task: Task, error: str) -> None:
        self._last_progress.pop(task.task_id, None)
        if self.debug is not None:
            await self.debug.failed(task, error)
        if task.schedule_name:
            return  # debug feed already threads this into the debug channel; skip the duplicate top-level post
        await self._post(task, f"*Failed*\n{error}")

    async def blocked(self, task: Task) -> None:
        self._last_progress.pop(task.task_id, None)
        if self.debug is not None:
            await self.debug.blocked(task)
        if task.schedule_name:
            return  # debug feed already threads this into the debug channel; skip the duplicate top-level post
        await self._post(task, f"*Blocked*\n{task.blocked_reason or ''}\nReply in this thread to continue.")

    async def issue_blocked(self, task: Task, issue: dict) -> None:
        """this task is cancelled and its issue is either reopened as `proposed` or, if it had already opened a PR,
        left tracking that PR instead (#87); points at the issue instead of the now-inert thread reply."""
        self._last_progress.pop(task.task_id, None)
        if self.debug is not None:
            await self.debug.blocked(task)
        if task.schedule_name:
            return  # debug feed already threads this into the debug channel; skip the duplicate top-level post
        reason = task.blocked_reason or "the task could not continue"
        if issue.get("status") == "in_review":
            text = f"*Blocked* — it had already opened {issue['pr_url']}, so I'm tracking that PR on issue #{issue['id']} instead of reopening it.\n{reason}"
        else:
            text = f"*Blocked* — reopened issue #{issue['id']} as `proposed` so you can pick up where this left off.\n{reason}"
        if self.dashboard_url:
            text += f"\n{self.dashboard_url}/issues?issue={issue['id']}"
        await self._post(task, text)

    async def questions(self, task: Task, questions: str) -> None:
        self._last_progress.pop(task.task_id, None)
        if self.debug is not None:
            await self.debug.questions(task, questions)
        if task.schedule_name:
            return  # debug feed already threads this into the debug channel; skip the duplicate top-level post
        await self._post(task, f"*I need a few answers before I continue*\n{questions}\n\n<@{task.slack_user_id}> reply in this thread with your answers by number and I'll pick the task back up.")

    async def recovered(self, task: Task) -> None:
        if self.debug is not None:
            await self.debug.recovered(task)
        if task.schedule_name:
            return  # debug feed already threads this into the debug channel; skip the duplicate top-level post
        await self._post(task, "The orchestrator restarted; this task was requeued and will resume.")

    async def refused(self, task: Task, reason: str) -> None:
        if self.debug is not None:
            if task.classification_json:
                await self.debug.classified(task)
            await self.debug.refused(task, reason)
        if task.schedule_name:
            return  # debug feed already threads this into the debug channel; skip the duplicate top-level post
        await self._post(task, f"Can't take this task: {reason}")

    async def refuse_intake(self, channel_id: str, thread_ts: str, reason: str) -> None:
        # refusals for mentions that never became a task (SLK-010)
        try:
            await self.client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=redactor.redact(to_mrkdwn(reason)))
        except Exception as e:
            self._record_error("refuse_intake", e)
            raise

    async def answer(self, channel_id: str, thread_ts: str | None, text: str) -> None:
        post = {"channel": channel_id, "text": redactor.redact(to_mrkdwn(text)), "unfurl_links": False, "unfurl_media": False}
        if thread_ts is not None:
            post["thread_ts"] = thread_ts
        try:
            await self.client.chat_postMessage(**post)
        except Exception as e:
            self._record_error("answer", e)
            raise


async def build(store: Store, config: Config, bot_token: str) -> tuple[AsyncApp, SlackNotifier]:
    """wire the bolt app: resolve our bot user id, register the mention handler."""
    app = AsyncApp(token=bot_token)
    from taskboy.debug_feed import DebugFeed

    debug = DebugFeed(app.client, store, config.slack.debug_channel, dashboard_url=config.dashboard.public_url) if config.slack.debug_channel else None
    notifier = SlackNotifier(app.client, config.progress_min_interval_seconds, config.slack.ack_reaction, debug=debug, task_started_messages_path=config.slack.task_started_messages_path, store=store, reviewer_name=config.reviewer.name, dashboard_url=config.dashboard.public_url)
    quick = None
    if (config.raw.get("quick_answer") or {}).get("enabled"):
        from taskboy.quick import QuickAnswer

        quick = QuickAnswer(store, config, debug=debug)
    try:
        auth = await app.client.auth_test()
    except Exception as e:
        store.add_error("slack", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"operation": "auth_test"})
        if debug is not None:
            await debug.system_error("slack", f"auth.test failed: {e}")
        raise
    bot_user_id = auth["user_id"]

    @app.event("app_mention")
    async def on_mention(body, event):
        if str(event.get("channel") or "").startswith("D"):
            return
        try:
            task, status = await handle_mention(store, config, notifier, event, body.get("event_id"), bot_user_id, quick=quick, client=app.client)
            logger.info("mention handled: status=%s task=%s", status, task.task_id if task else None)
        except Exception as e:
            store.add_error("slack", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"operation": "handle_mention"})
            if debug is not None:
                await debug.system_error("slack", f"mention handling failed: {e}")
            logger.exception("mention handling failed")

    @app.event("message")
    async def on_message(body, event):
        if event.get("channel_type") != "im":
            # channel traffic only matters when it answers a blocked task's questions (needs the message.channels event subscription)
            try:
                status = await handle_thread_reply(store, notifier, event, body.get("event_id"), bot_user_id)
                if status != "ignored":
                    logger.info("thread reply handled: status=%s", status)
            except Exception as e:
                store.add_error("slack", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"operation": "handle_thread_reply"})
                if debug is not None:
                    await debug.system_error("slack", f"thread reply handling failed: {e}")
                logger.exception("thread reply handling failed")
            return
        try:
            dm_event = {**event, "team": event.get("team") or body.get("team_id") or (body.get("team") or {}).get("id") or ""}
            status = await handle_dm(store, config, notifier, dm_event, body.get("event_id"), bot_user_id, quick, app.client)
            logger.info("dm handled: status=%s", status)
        except Exception as e:
            store.add_error("slack", type(e).__name__, str(e), traceback=traceback.format_exc(), context={"operation": "handle_dm"})
            if debug is not None:
                await debug.system_error("slack", f"dm handling failed: {e}")
            logger.exception("dm handling failed")

    return app, notifier
