"""task-channel-scoped Slack history as a read-only in-process mcp tool."""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from taskboy.adapters._util import _error, _text, wrap
from taskboy.models import Task
from taskboy.mrkdwn import to_mrkdwn
from taskboy.redact import redactor
from taskboy.slack_users import cached_user_profile
from taskboy.store import Store

logger = logging.getLogger("taskboy.slack_history")

HISTORY_MAX_LIMIT = 50
HISTORY_DEFAULT_LIMIT = 20
FILE_MAX_BYTES = 20 * 1024 * 1024
INLINE_TEXT_MAX_BYTES = 50 * 1024
INLINE_TEXT_MAX_CHARS = 3000
TEXT_FILETYPES = {"csv", "html", "javascript", "json", "log", "markdown", "python", "shell", "text", "xml", "yaml"}


class SlackHistoryAdapter:
    def __init__(self, store: Store, task: Task, client, allowed_channels: list[str] | None = None, files_dir: str | Path | None = None):
        self.store = store
        self.task = task
        self.client = client
        self.allowed_channels = allowed_channels or []
        self.files_dir = Path(files_dir) if files_dir is not None else None

    async def channel_history(self, args: dict) -> dict:
        oldest = str(args.get("oldest") or "") or None
        latest = str(args.get("latest") or "") or None
        cursor = str(args.get("cursor") or "") or None
        limit = min(max(int(args.get("limit", HISTORY_DEFAULT_LIMIT)), 1), HISTORY_MAX_LIMIT)
        self.store.add_event(self.task.task_id, "tool_call", {"oldest": oldest, "latest": latest, "limit": limit, "cursor": cursor}, tool_name="mcp__slack__channel_history", is_write=False)
        data = await self._fetch(oldest, latest, limit, cursor)
        lines = []
        for message in reversed(data.get("messages") or []):
            content = _message_content(message)
            if not content:
                continue
            lines.append(f"[{_iso(message.get('ts'))}] <@{message.get('user', '')}>: {content}")
        next_cursor = str((data.get("response_metadata") or {}).get("next_cursor") or "")
        if next_cursor:
            lines.append(f"more: pass cursor={next_cursor}")
        return _text("\n".join(lines) or "no messages matched")

    async def thread_replies(self, args: dict) -> dict:
        channel = str(args.get("channel") or self.task.slack_channel_id)
        if channel != self.task.slack_channel_id and channel not in self.allowed_channels:
            return _error(f"channel {channel!r} is not allowed for this task")
        thread_ts = str(args.get("thread_ts") or "")
        if not thread_ts:
            return _error("thread_ts is required")
        limit = min(max(int(args.get("limit", HISTORY_DEFAULT_LIMIT)), 1), HISTORY_MAX_LIMIT)
        self.store.add_event(self.task.task_id, "tool_call", {"channel": channel, "thread_ts": thread_ts, "limit": limit}, tool_name="mcp__slack__thread_replies", is_write=False)
        data = await self.client.conversations_replies(channel=channel, ts=thread_ts, limit=limit)
        lines = []
        for message in data.get("messages") or []:
            content = _message_content(message)
            if content:
                lines.append(f"[{_iso(message.get('ts'))}] <@{message.get('user', '')}>: {content}")
        return _text("\n".join(lines) or "no messages matched")

    async def get_file(self, args: dict) -> dict:
        file_id = str(args.get("file_id") or "").strip()
        if not file_id:
            return _error("file_id is required")
        self.store.add_event(self.task.task_id, "tool_call", {"file_id": file_id}, tool_name="mcp__slack__get_file", is_write=False)
        if self.files_dir is None:
            return _error("file downloads are not available for this task")
        data = await self.client.files_info(file=file_id)
        file = data.get("file") or {}
        shared_channels = {str(channel) for channel in (file.get("channels") or []) + (file.get("groups") or []) + (file.get("ims") or [])}
        readable_channels = {self.task.slack_channel_id, *self.allowed_channels}
        if not shared_channels.intersection(readable_channels):
            return _error(f"file {file_id} is not shared in a channel this task may read")
        size = int(file.get("size") or 0)
        if size > FILE_MAX_BYTES:
            return _error(f"file {file_id} is larger than the 20 MB download limit")
        url = str(file.get("url_private_download") or file.get("url_private") or "")
        if not url:
            return _error(f"file {file_id} has no download url")
        content = await self._download_file(url)
        if len(content) > FILE_MAX_BYTES:
            return _error(f"file {file_id} is larger than the 20 MB download limit")
        name = str(file.get("name") or file.get("title") or "file")
        safe_name = re.sub(r"[/\\\\\x00]+", "_", name).strip() or "file"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        destination = self.files_dir / f"{file_id}-{safe_name}"
        destination.write_bytes(content)
        relative_path = f"{self.files_dir.name}/{destination.name}"
        mimetype = str(file.get("mimetype") or "application/octet-stream")
        result = f"saved {relative_path} (name={name}, type={mimetype}, size={len(content)})"
        filetype = str(file.get("filetype") or "").lower()
        if len(content) <= INLINE_TEXT_MAX_BYTES and (mimetype.startswith("text/") or filetype in TEXT_FILETYPES):
            result += f"\n\ncontent:\n{content.decode('utf-8', errors='replace')[:INLINE_TEXT_MAX_CHARS]}"
        return _text(result)

    async def user_info(self, args: dict) -> dict:
        user_id = str(args.get("user") or "")
        if not user_id:
            return _error("user is required")
        self.store.add_event(self.task.task_id, "tool_call", {"user": user_id}, tool_name="mcp__slack__user_info", is_write=False)
        profile = self.store.get_slack_user(user_id)
        try:
            profile = await cached_user_profile(self.store, self.client, user_id)
        except Exception as e:
            self.store.add_error("slack", type(e).__name__, str(e), task_id=self.task.task_id, context={"operation": "users_info", "user_id": user_id})
            if profile is None:
                raise
        return _text(json.dumps(profile, sort_keys=True))

    async def send_dm(self, args: dict) -> dict:
        user_id = str(args.get("user") or "").strip()
        message = str(args.get("message") or "").strip()
        if not user_id:
            return _error("user is required")
        if not message:
            return _error("message is required")
        self.store.add_event(self.task.task_id, "tool_call", {"user": user_id}, tool_name="mcp__slack__send_dm", is_write=True)
        try:
            opened = await self.client.conversations_open(users=user_id)
            channel_id = str((opened.get("channel") or {}).get("id") or "")
            if not channel_id:
                raise RuntimeError("slack did not return a dm channel")
            await self.client.chat_postMessage(channel=channel_id, text=redactor.redact(to_mrkdwn(message)))
        except Exception as e:
            self.store.add_error("slack", type(e).__name__, str(e), task_id=self.task.task_id, context={"operation": "send_dm", "user_id": user_id})
            return _error(str(e))
        return _text(f"sent a direct message to <@{user_id}>")

    async def _fetch(self, oldest, latest, limit, cursor) -> dict:
        kwargs = {"channel": self.task.slack_channel_id, "limit": limit}
        if oldest:
            kwargs["oldest"] = oldest
        if latest:
            kwargs["latest"] = latest
        if cursor:
            kwargs["cursor"] = cursor
        return await self.client.conversations_history(**kwargs)

    async def _download_file(self, url: str) -> bytes:
        import aiohttp

        headers = {"Authorization": f"Bearer {self.client.token}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status >= 300:
                    raise RuntimeError(redactor.redact(f"slack file download failed: {response.status}"))
                return await response.read()


def _iso(value) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(value or "")


def _message_content(message: dict) -> str:
    parts = []
    text = str(message.get("text") or "").strip()
    if text:
        parts.append(text)
    for file in message.get("files") or []:
        parts.append(f"(file: {file.get('name')} id={file.get('id')} type={file.get('mimetype')})")
    return " ".join(parts)


def build_slack_server(adapter: SlackHistoryAdapter):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    channel_history = tool(
        "channel_history",
        "Read only the Slack channel this task originated in. Thread replies appear only as parents; page older history with cursor or oldest.",
        {"oldest": str, "latest": str, "limit": int, "cursor": str},
    )(wrap(adapter.channel_history, logger))
    thread_replies = tool(
        "thread_replies",
        "Read one Slack thread in the originating channel or another configured allowed channel.",
        {
            "type": "object",
            "properties": {"channel": {"type": "string"}, "thread_ts": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["thread_ts"],
        },
    )(wrap(adapter.thread_replies, logger))
    user_info = tool("user_info", "Look up a Slack user's cached profile, refreshing it when stale.", {"user": str})(wrap(adapter.user_info, logger))
    get_file = tool(
        "get_file",
        "Download a file attached to a Slack message in an allowed channel into the workspace (slack_files/) so it can be inspected. Pass the file id shown in channel_history/thread_replies output.",
        {"file_id": str},
    )(wrap(adapter.get_file, logger))
    send_dm = tool(
        "send_dm",
        "Send a Slack direct message only when the task genuinely calls for contacting that user directly.",
        {"user": str, "message": str},
    )(wrap(adapter.send_dm, logger))
    tools = [channel_history, thread_replies, user_info, get_file, send_dm]
    return create_sdk_mcp_server(name="slack", version="1.0.0", tools=tools)
