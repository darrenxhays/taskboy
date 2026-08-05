import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_harness.adapters.slack_history import HISTORY_MAX_LIMIT, SlackHistoryAdapter, build_slack_server


@pytest.mark.asyncio
async def test_channel_history_is_scoped_formatted_capped_and_audited(store, make_task):
    task = make_task()
    adapter = SlackHistoryAdapter(store, task, client=object())
    adapter._fetch = AsyncMock(
        return_value={
            "messages": [
                {"ts": "2", "user": "U2", "text": "new"},
                {"ts": "1", "user": "U1", "text": "old"},
            ],
            "response_metadata": {"next_cursor": "next-page"},
        }
    )
    result = await adapter.channel_history({"limit": 500, "oldest": "0", "cursor": "cursor"})
    text = result["content"][0]["text"]
    assert text.index("<@U1>: old") < text.index("<@U2>: new")
    assert "more: pass cursor=next-page" in text
    adapter._fetch.assert_awaited_once_with("0", None, HISTORY_MAX_LIMIT, "cursor")
    event = store.events_for(task.task_id)[-1]
    assert (event["kind"], event["tool_name"], event["is_write"]) == ("tool_call", "mcp__slack__channel_history", 0)


@pytest.mark.asyncio
async def test_channel_history_redacts_and_bounds_output(store, make_task):
    adapter = SlackHistoryAdapter(store, make_task(), client=object())
    adapter._fetch = AsyncMock(return_value={"messages": [{"ts": "1", "user": "U1", "text": "ghp_abcdefghijklmnopqrstuvwxyz0123456789 " + "x" * 5000}]})
    text = (await adapter.channel_history({}))["content"][0]["text"]
    assert "ghp_" not in text
    assert len(text) <= 4000


@pytest.mark.asyncio
async def test_history_and_replies_include_files_and_file_only_messages(store, make_task):
    client = AsyncMock()
    client.conversations_replies.return_value = {"messages": [{"ts": "2", "user": "U2", "text": "", "files": [{"id": "F2", "name": "trace.log", "mimetype": "text/plain"}]}]}
    adapter = SlackHistoryAdapter(store, make_task(), client)
    adapter._fetch = AsyncMock(return_value={"messages": [{"ts": "1", "user": "U1", "text": "see this", "files": [{"id": "F1", "name": "screen.png", "mimetype": "image/png"}]}]})

    history = (await adapter.channel_history({}))["content"][0]["text"]
    replies = (await adapter.thread_replies({"thread_ts": "1"}))["content"][0]["text"]

    assert "see this (file: screen.png id=F1 type=image/png)" in history
    assert "<@U2>: (file: trace.log id=F2 type=text/plain)" in replies


@pytest.mark.asyncio
async def test_fetch_bakes_in_task_channel(store, make_task):
    client = AsyncMock()
    client.conversations_history.return_value = {"messages": []}
    adapter = SlackHistoryAdapter(store, make_task(), client)
    await adapter._fetch(None, None, 20, None)
    client.conversations_history.assert_awaited_once_with(channel="C1", limit=20)


@pytest.mark.asyncio
async def test_thread_replies_allows_origin_and_configured_channels_and_audits(store, make_task):
    client = AsyncMock()
    client.conversations_replies.return_value = {"messages": [{"ts": "1", "user": "U1", "text": "root"}, {"ts": "2", "user": "U2", "text": "reply"}]}
    task = make_task()
    adapter = SlackHistoryAdapter(store, task, client, allowed_channels=["C2"])
    text = (await adapter.thread_replies({"channel": "C2", "thread_ts": "1", "limit": 500}))["content"][0]["text"]
    assert "<@U1>: root" in text
    assert "<@U2>: reply" in text
    client.conversations_replies.assert_awaited_once_with(channel="C2", ts="1", limit=HISTORY_MAX_LIMIT)
    event = store.events_for(task.task_id)[-1]
    assert (event["tool_name"], event["is_write"]) == ("mcp__slack__thread_replies", 0)

    client.reset_mock()
    await adapter.thread_replies({"channel": "", "thread_ts": "1", "limit": 2})
    client.conversations_replies.assert_awaited_once_with(channel="C1", ts="1", limit=2)


@pytest.mark.asyncio
async def test_thread_replies_refuses_disallowed_channel(store, make_task):
    client = AsyncMock()
    adapter = SlackHistoryAdapter(store, make_task(), client, allowed_channels=["C2"])
    result = await adapter.thread_replies({"channel": "C9", "thread_ts": "1"})
    assert result["isError"] is True
    client.conversations_replies.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_file_downloads_sanitizes_and_inlines_small_text(store, make_task, tmp_path):
    client = AsyncMock()
    client.files_info.return_value = {"file": {"id": "F123", "name": "../../crash.log", "mimetype": "text/plain", "filetype": "text", "size": 12, "channels": ["C1"], "groups": [], "url_private_download": "https://files.slack.test/F123"}}
    adapter = SlackHistoryAdapter(store, make_task(), client, files_dir=tmp_path / "slack_files")
    adapter._download_file = AsyncMock(return_value=b"failure details")

    result = await adapter.get_file({"file_id": "F123"})

    text = result["content"][0]["text"]
    destination = tmp_path / "slack_files" / "F123-.._.._crash.log"
    assert destination.read_bytes() == b"failure details"
    assert "slack_files/F123-.._.._crash.log" in text
    assert "content:\nfailure details" in text
    adapter._download_file.assert_awaited_once_with("https://files.slack.test/F123")
    event = store.events_for(adapter.task.task_id)[-1]
    assert (event["tool_name"], event["is_write"]) == ("mcp__slack__get_file", 0)


@pytest.mark.asyncio
async def test_get_file_allows_file_shared_in_originating_dm(store, make_task, tmp_path):
    client = AsyncMock()
    client.files_info.return_value = {"file": {"id": "F1", "name": "dm.txt", "size": 4, "ims": ["D1"], "url_private": "https://files.slack.test/F1"}}
    task = make_task()
    task.slack_channel_id = "D1"
    adapter = SlackHistoryAdapter(store, task, client, files_dir=tmp_path / "slack_files")
    adapter._download_file = AsyncMock(return_value=b"test")

    result = await adapter.get_file({"file_id": "F1"})

    assert result.get("isError") is not True
    assert (tmp_path / "slack_files" / "F1-dm.txt").read_bytes() == b"test"


@pytest.mark.asyncio
async def test_get_file_rejects_disallowed_channel(store, make_task, tmp_path):
    client = AsyncMock()
    client.files_info.return_value = {"file": {"id": "F1", "name": "x", "size": 1, "channels": ["C9"], "groups": [], "url_private": "https://files.slack.test/F1"}}
    adapter = SlackHistoryAdapter(store, make_task(), client, allowed_channels=["C2"], files_dir=tmp_path / "slack_files")
    adapter._download_file = AsyncMock()

    result = await adapter.get_file({"file_id": "F1"})

    assert result["isError"] is True
    assert "not shared in a channel this task may read" in result["content"][0]["text"]
    adapter._download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_file_rejects_oversized_file(store, make_task, tmp_path):
    client = AsyncMock()
    client.files_info.return_value = {"file": {"id": "F1", "name": "huge.zip", "size": 20 * 1024 * 1024 + 1, "channels": ["C1"], "url_private": "https://files.slack.test/F1"}}
    adapter = SlackHistoryAdapter(store, make_task(), client, files_dir=tmp_path / "slack_files")
    adapter._download_file = AsyncMock()

    result = await adapter.get_file({"file_id": "F1"})

    assert result["isError"] is True
    assert "20 MB" in result["content"][0]["text"]
    adapter._download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_file_errors_when_download_directory_is_unset(store, make_task):
    client = AsyncMock()

    result = await SlackHistoryAdapter(store, make_task(), client).get_file({"file_id": "F1"})

    assert result["isError"] is True
    assert "file downloads are not available for this task" in result["content"][0]["text"]
    client.files_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_info_refreshes_and_then_uses_cache(store, make_task):
    client = AsyncMock()
    client.users_info.return_value = {"user": {"team_id": "T1", "name": "ada", "real_name": "Ada", "profile": {"display_name": "ada", "email": "ada@example.test"}}}
    task = make_task()
    adapter = SlackHistoryAdapter(store, task, client)
    first = await adapter.user_info({"user": "U2"})
    second = await adapter.user_info({"user": "U2"})
    assert "Ada" in first["content"][0]["text"]
    assert "ada@example.test" in second["content"][0]["text"]
    client.users_info.assert_awaited_once_with(user="U2")
    assert store.events_for(task.task_id)[-1]["tool_name"] == "mcp__slack__user_info"


@pytest.mark.asyncio
async def test_send_dm_opens_posts_as_app_redacts_and_audits(store, make_task):
    client = AsyncMock()
    client.conversations_open.return_value = {"channel": {"id": "D123"}}
    task = make_task(persona="reviewer")
    adapter = SlackHistoryAdapter(store, task, client)

    result = await adapter.send_dm({"user": "U2", "message": "# Update\ntoken ghp_abcdefghijklmnopqrstuvwxyz0123456789"})

    assert result["content"][0]["text"] == "sent a direct message to <@U2>"
    client.conversations_open.assert_awaited_once_with(users="U2")
    post = client.chat_postMessage.call_args.kwargs
    assert post["channel"] == "D123"
    assert "ghp_" not in post["text"]
    assert "*Update*" in post["text"]
    assert "username" not in post
    assert "icon_url" not in post
    assert "icon_emoji" not in post
    event = store.events_for(task.task_id)[-1]
    assert (event["tool_name"], event["is_write"]) == ("mcp__slack__send_dm", 1)
    assert json.loads(event["detail_json"]) == {"user": "U2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("args,error", [({"message": "hello"}, "user is required"), ({"user": "U2"}, "message is required")])
async def test_send_dm_validates_required_fields(store, make_task, args, error):
    client = AsyncMock()
    result = await SlackHistoryAdapter(store, make_task(), client).send_dm(args)
    assert result["isError"] is True
    assert error in result["content"][0]["text"]
    client.conversations_open.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_dm_returns_error_when_slack_fails(store, make_task):
    client = AsyncMock()
    client.conversations_open.side_effect = RuntimeError("unknown user")
    task = make_task()
    result = await SlackHistoryAdapter(store, task, client).send_dm({"user": "U404", "message": "hello"})
    assert result["isError"] is True
    assert "unknown user" in result["content"][0]["text"]
    assert store.events_for(task.task_id)[-1]["is_write"] == 1
    assert store.recent_errors(1)[0]["component"] == "slack"


@pytest.mark.asyncio
async def test_server_wrapper_returns_is_error(monkeypatch):
    captured = []

    def fake_tool(name, description, schema):
        def decorate(fn):
            captured.append(fn)
            return fn

        return decorate

    monkeypatch.setattr("claude_agent_sdk.tool", fake_tool)
    monkeypatch.setattr("claude_agent_sdk.create_sdk_mcp_server", lambda **kwargs: kwargs)
    adapter = SimpleNamespace(channel_history=AsyncMock(side_effect=RuntimeError("slack down")), thread_replies=AsyncMock(), user_info=AsyncMock(), get_file=AsyncMock(), send_dm=AsyncMock())
    build_slack_server(adapter)
    result = await captured[0]({})
    assert result["isError"] is True
    assert "slack down" in result["content"][0]["text"]
