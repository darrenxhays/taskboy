import asyncio
from unittest.mock import AsyncMock

import pytest

from taskboy import debug_feed as debug_feed_module
from taskboy.debug_feed import DebugFeed


@pytest.mark.asyncio
async def test_debug_feed_opens_thread_and_gets_permalink(store):
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "123.4"}
    client.chat_getPermalink.return_value = {"permalink": "https://slack.test/archive/p123"}
    debug = DebugFeed(client, store, "CDEBUG")
    result = await debug.open_thread(user_id="U1", channel_id="C1", request_text="fix it", user_profile={"real_name": "Ada"})
    assert result == ("123.4", "https://slack.test/archive/p123")
    post = client.chat_postMessage.call_args.kwargs
    assert post["channel"] == "CDEBUG"
    assert "Ada" in post["text"]
    assert post["unfurl_links"] is False


@pytest.mark.asyncio
async def test_debug_feed_uses_na_for_missing_source_channel(store):
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "123.4"}
    client.chat_getPermalink.return_value = {"permalink": "https://slack.test/archive/p123"}
    debug = DebugFeed(client, store, "CDEBUG")

    await debug.open_thread(user_id="github", channel_id="", request_text="review it")

    assert "Source channel: n/a" in client.chat_postMessage.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_debug_feed_never_raises_and_records_failures(store):
    client = AsyncMock()
    client.chat_postMessage.side_effect = RuntimeError("slack down")
    debug = DebugFeed(client, store, "CDEBUG")
    assert await debug.open_thread(user_id="U1", channel_id="C1", request_text="fix", user_profile=None) is None
    await debug.post("1", "update")
    await debug.system_error("housekeeping", "broken")
    errors = store.recent_errors(10)
    assert len(errors) == 3
    assert all(row["component"] == "debug_feed" for row in errors)


@pytest.mark.asyncio
async def test_debug_file_upload_falls_back_to_inline(store):
    client = AsyncMock()
    client.files_upload_v2.side_effect = RuntimeError("files scope missing")
    debug = DebugFeed(client, store, "CDEBUG")
    await debug.post_file("1", "prompt.md", "Prompt", "full prompt", "attached", "t1")
    client.chat_postMessage.assert_awaited_once()
    assert "full prompt" in client.chat_postMessage.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_system_error_dedupes_within_cooldown_and_reposts_after(store, monkeypatch):
    client = AsyncMock()
    debug = DebugFeed(client, store, "CDEBUG")
    clock = [1000.0]
    monkeypatch.setattr(debug_feed_module, "_now", lambda: clock[0])

    assert await debug.system_error("review_poller", "403 forbidden") is True
    clock[0] += 60
    assert await debug.system_error("review_poller", "403 forbidden") is False  # suppressed — caller must not treat this as delivered
    assert client.chat_postMessage.await_count == 1

    clock[0] += debug_feed_module.SYSTEM_ERROR_COOLDOWN_SECONDS
    assert await debug.system_error("review_poller", "403 forbidden") is True
    assert client.chat_postMessage.await_count == 2


@pytest.mark.asyncio
async def test_system_error_does_not_suppress_across_components_or_messages(store, monkeypatch):
    client = AsyncMock()
    debug = DebugFeed(client, store, "CDEBUG")
    monkeypatch.setattr(debug_feed_module, "_now", lambda: 2000.0)

    await debug.system_error("review_poller", "403 forbidden")
    await debug.system_error("other_component", "403 forbidden")
    await debug.system_error("review_poller", "different message")

    assert client.chat_postMessage.await_count == 3


@pytest.mark.asyncio
async def test_system_error_does_not_rate_limit_retry_after_post_failure(store, monkeypatch):
    client = AsyncMock()
    debug = DebugFeed(client, store, "CDEBUG")
    clock = [1000.0]
    monkeypatch.setattr(debug_feed_module, "_now", lambda: clock[0])

    client.chat_postMessage.side_effect = RuntimeError("slack down")
    assert await debug.system_error("review_poller", "403 forbidden") is False

    # a retry a minute later must not be suppressed by a cooldown that was never actually earned
    clock[0] += 60
    client.chat_postMessage.side_effect = None
    client.chat_postMessage.return_value = {"ts": "1"}
    assert await debug.system_error("review_poller", "403 forbidden") is True


@pytest.mark.asyncio
async def test_system_error_claims_window_before_post_to_prevent_concurrent_duplicates(store, monkeypatch):
    client = AsyncMock()
    debug = DebugFeed(client, store, "CDEBUG")
    monkeypatch.setattr(debug_feed_module, "_now", lambda: 5000.0)

    async def slow_post(*args, **kwargs):
        await asyncio.sleep(0)  # force a real context switch, like the network round trip it stands in for
        return {"ts": "1"}

    client.chat_postMessage.side_effect = slow_post

    results = await asyncio.gather(*[debug.system_error("review_poller", "concurrent") for _ in range(4)])

    assert client.chat_postMessage.await_count == 1  # the window is claimed before the await, so only one wins
    assert results.count(True) == 1
    assert results.count(False) == 3


@pytest.mark.asyncio
async def test_completed_links_to_dashboard_feedback(store, make_task):
    client = AsyncMock()
    debug = DebugFeed(client, store, "CDEBUG", dashboard_url="https://agent.example.com/")
    task = make_task(debug_thread_ts="123.4")
    await debug.completed(task)
    texts = [call.kwargs["text"] for call in client.chat_postMessage.call_args_list]
    assert any(f"https://agent.example.com/tasks/{task.task_id}/feedback" in text for text in texts)


@pytest.mark.asyncio
async def test_completed_omits_feedback_link_without_dashboard_url(store, make_task):
    client = AsyncMock()
    debug = DebugFeed(client, store, "CDEBUG")
    task = make_task(debug_thread_ts="123.4")
    await debug.completed(task)
    texts = [call.kwargs["text"] for call in client.chat_postMessage.call_args_list]
    assert not any("/feedback" in text for text in texts)
