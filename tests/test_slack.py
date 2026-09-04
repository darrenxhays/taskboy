from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from slack_sdk.errors import SlackApiError

from taskboy.config import Role, SlackConfig
from taskboy.debug_feed import DebugFeed
from taskboy.models import RECEIVED
from taskboy.slack import THREAD_CONTEXT_MAX_CHARS, SlackNotifier, authorization_failure, clean_text, extract_overrides, fetch_dm_transcript, fetch_thread_transcript, handle_dm, handle_mention, is_help_request, normalize_effort

BOT = "UBOT"


def event(text="do the thing", channel="C1", user="U1", ts="100.1", thread_ts=None, team="T1"):
    e = {"text": f"<@{BOT}> {text}", "channel": channel, "user": user, "ts": ts, "team": team}
    if thread_ts:
        e["thread_ts"] = thread_ts
    return e


def test_authorization_allows_configured_and_refuses_others():
    slack = SlackConfig(team_id="T1", allowed_channels=["C1"])
    roles = {"developer": Role("developer", ["U1"], ["read_only"], False, 2.0, None)}
    assert authorization_failure(slack, roles, "T1", "C1", "U1") is None
    assert "workspace" in authorization_failure(slack, roles, "T2", "C1", "U1")
    assert "channel" in authorization_failure(slack, roles, "T1", "C9", "U1")
    assert "configured role" in authorization_failure(slack, roles, "T1", "C1", "U9")
    # missing team field is tolerated: socket mode's app token already binds the workspace
    assert authorization_failure(slack, roles, "", "C1", "U1") is None
    assert authorization_failure(slack, roles, "T1", "D123", "U1") is None
    assert authorization_failure(slack, roles, "T1", "C9", "U1", "im") is None


def test_wildcard_role_allows_any_user_in_any_allowed_channel():
    slack = SlackConfig(team_id="T1", allowed_channels=[])
    roles = {"developer": Role("developer", ["*"], ["read_only"], False, 2.0, None)}
    assert authorization_failure(slack, roles, "T1", "C_anything", "U_anyone") is None


MODEL_ALIASES = ["haiku", "sonnet", "opus", "fable"]


def test_clean_text_and_model_override():
    assert clean_text(f"<@{BOT}> fix the bug", BOT) == "fix the bug"
    override, effort, text = extract_overrides("model:opus fix the bug", MODEL_ALIASES)
    assert override == "opus"
    assert effort is None
    assert text == "fix the bug"
    override, effort, text = extract_overrides("fix the bug", MODEL_ALIASES)
    assert override is None
    assert effort is None


def test_extract_overrides_bang_form_without_effort():
    override, effort, text = extract_overrides("!haiku fix the bug", MODEL_ALIASES)
    assert override == "haiku"
    assert effort is None
    assert text == "fix the bug"


@pytest.mark.parametrize(
    "raw,expected_model,expected_effort",
    [
        ("!Fable-high fix it", "fable", "high"),
        ("!opus-XHIGH fix it", "opus", "xhigh"),
        ("!SONNET-max fix it", "sonnet", "max"),
        ("!fable-xh fix it", "fable", "xhigh"),
    ],
)
def test_extract_overrides_bang_form_case_insensitive_with_effort(raw, expected_model, expected_effort):
    override, effort, text = extract_overrides(raw, MODEL_ALIASES)
    assert override == expected_model
    assert effort == expected_effort
    assert text == "fix it"


def test_extract_overrides_ignores_ordinary_exclamation_marks():
    override, effort, text = extract_overrides("this is great! fix the bug", MODEL_ALIASES)
    assert override is None
    assert effort is None
    assert text == "this is great! fix the bug"


def test_extract_overrides_first_match_wins():
    override, effort, text = extract_overrides("!haiku or !opus, fix it", MODEL_ALIASES)
    assert override == "haiku"
    assert effort is None


def test_extract_overrides_with_no_catalog_never_matches_bang_form():
    override, effort, text = extract_overrides("!haiku fix the bug", [])
    assert override is None
    assert effort is None
    assert text == "!haiku fix the bug"


def test_normalize_effort_variations():
    assert normalize_effort(None) is None
    assert normalize_effort("") is None
    assert normalize_effort("high") == "high"
    assert normalize_effort("HIGH") == "high"
    assert normalize_effort("lo") == "low"
    assert normalize_effort("med") == "medium"
    assert normalize_effort("hi") == "high"
    assert normalize_effort("xh") == "xhigh"
    # no more extra/xtra/extreme/x* synonyms or reverse (cleaned.startswith(level)) matching
    assert normalize_effort("extra") is None
    assert normalize_effort("maximum") is None
    assert normalize_effort("gibberish") is None


@pytest.mark.asyncio
async def test_mention_creates_task_and_acks(store, config, notifier):
    task, status = await handle_mention(store, config, notifier, event(), "Ev1", BOT)
    assert status == "created"
    assert task.state == RECEIVED
    assert task.request_text == "do the thing"
    assert task.slack_thread_ts == "100.1"
    assert ("ack", task.task_id) in notifier.calls


@pytest.mark.asyncio
async def test_duplicate_event_delivery_creates_one_task(store, config, notifier):
    task, status1 = await handle_mention(store, config, notifier, event(), "Ev1", BOT)
    dup, status2 = await handle_mention(store, config, notifier, event(), "Ev1", BOT)
    assert status1 == "created"
    assert status2 == "duplicate_event"
    assert dup is None
    assert store.count_tasks(RECEIVED) == 1


@pytest.mark.asyncio
async def test_unauthorized_mention_is_refused_and_recorded(store, config, notifier):
    task, status = await handle_mention(store, config, notifier, event(user="U_intruder"), "Ev1", BOT)
    assert task is None
    assert status == "unauthorized"
    assert store.count_tasks(RECEIVED) == 0
    assert notifier.calls[0][0] == "refuse_intake"
    denial = store.conn.execute("SELECT * FROM intake_denials").fetchone()
    assert denial["user_id"] == "U_intruder"


@pytest.mark.asyncio
async def test_empty_mention_gets_usage_hint(store, config, notifier):
    task, status = await handle_mention(store, config, notifier, event(text=""), "Ev1", BOT)
    assert status == "empty"
    assert task is None
    assert notifier.calls[0][0] == "refuse_intake"


@pytest.mark.asyncio
async def test_known_skill_creates_task_and_skips_quick_answer(store, config, notifier, tmp_path, monkeypatch):
    path = tmp_path / "review"
    path.mkdir()
    (path / "SKILL.md").write_text("---\nname: review\ndescription: review it\n---\nbody\n")
    monkeypatch.setattr("taskboy.settings.SKILLS_ROOT", str(tmp_path))
    quick = AsyncMock()
    task, status = await handle_mention(store, config, notifier, event(text="/review https://github.com/org/repo/pull/1"), "Ev1", BOT, quick=quick)
    assert status == "created"
    assert task.request_text == "/review https://github.com/org/repo/pull/1"
    quick.try_answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_skill_is_refused_with_available_list(store, config, notifier, tmp_path, monkeypatch):
    for name in ("review", "monitor"):
        path = tmp_path / name
        path.mkdir()
        (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}\n---\nbody\n")
    monkeypatch.setattr("taskboy.settings.SKILLS_ROOT", str(tmp_path))
    task, status = await handle_mention(store, config, notifier, event(text="/nope args"), "Ev1", BOT)
    assert task is None
    assert status == "unknown_skill"
    assert store.count_tasks(RECEIVED) == 0
    # installed skills plus the built-ins, sorted; /review is installed AND built-in — listed once
    assert notifier.calls == [("refuse_intake", "C1", "100.1", "unknown skill /nope — available: /discoverissues, /implementapprovedissues, /monitor, /refineissue, /review, /spec2pr")]


@pytest.mark.asyncio
async def test_builtin_skill_invocation_is_accepted_without_an_installed_copy(store, config, notifier, tmp_path, monkeypatch):
    monkeypatch.setattr("taskboy.settings.SKILLS_ROOT", str(tmp_path))  # empty: nothing installed
    task, status = await handle_mention(store, config, notifier, event(text="/review https://github.com/org/a/pull/1"), "Ev1", BOT)
    assert status == "created"
    assert task is not None and task.request_text.startswith("/review ")


@pytest.mark.asyncio
async def test_help_mention_replies_immediately_without_task_or_model_call(store, config, notifier, tmp_path):
    help_file = tmp_path / "help.md"
    help_file.write_text("Usage guide for Red.")
    config.help_path = str(help_file)
    quick = AsyncMock()
    task, status = await handle_mention(store, config, notifier, event(text="/help"), "Ev1", BOT, quick=quick)
    assert task is None
    assert status == "help"
    assert store.count_tasks(RECEIVED) == 0
    quick.try_answer.assert_not_awaited()
    assert notifier.calls == [("answer", "C1", "100.1", "Usage guide for Red.")]


@pytest.mark.asyncio
async def test_help_mention_falls_through_when_no_help_file_configured(store, config, notifier):
    assert config.help_path is None
    task, status = await handle_mention(store, config, notifier, event(text="/help"), "Ev1", BOT)
    assert status == "unknown_skill"
    assert task is None


@pytest.mark.asyncio
async def test_help_mention_posts_outcome_to_debug_thread(store, config, tmp_path):
    # every other early return in handle_mention posts an outcome to the debug thread — /help must too
    help_file = tmp_path / "help.md"
    help_file.write_text("Usage guide for Red.")
    config.help_path = str(help_file)
    client = AsyncMock()
    client.users_info.return_value = {"user": {"team_id": "T1", "name": "ada", "profile": {}}}
    client.chat_postMessage.return_value = {"ts": "900.1"}
    client.chat_getPermalink.return_value = {"permalink": "https://slack.test/debug/900"}
    debug = DebugFeed(client, store, "CDEBUG")
    notifier = SlackNotifier(client, debug=debug, store=store)
    task, status = await handle_mention(store, config, notifier, event(text="/help"), "Ev1", BOT, client=client)
    assert task is None
    assert status == "help"
    posted_texts = [call.kwargs.get("text") for call in client.chat_postMessage.await_args_list]
    assert any("Answered `/help`" in (text or "") for text in posted_texts)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/help", True),
        ("/help me with a PR", True),
        ("help", True),
        ("HELP", True),
        ("  help  ", True),
        ("helpme", False),
        ("please help me", False),
        ("/helper", False),
        ("investigate PROJ-123", False),
    ],
)
def test_is_help_request_matches_narrowly(text, expected):
    assert is_help_request(text) is expected


@pytest.mark.asyncio
async def test_model_override_honored_for_admins_only(store, config, notifier):
    admin_task, _ = await handle_mention(store, config, notifier, event(text="model:opus fix it", user="U1", ts="1.1"), "Ev1", BOT)
    assert admin_task.model_override == "opus"
    assert admin_task.request_text == "fix it"
    config.roles["developer"] = Role("developer", ["U2"], ["read_only", "standard", "deep"], False, 12.0, None)
    user_task, _ = await handle_mention(store, config, notifier, event(text="model:opus fix it", user="U2", ts="2.2"), "Ev2", BOT)
    assert user_task.model_override is None
    assert user_task.request_text == "fix it"


@pytest.mark.asyncio
async def test_bang_model_and_effort_override_honored_for_admins_only(store, config, notifier):
    config.raw = {"models": {"haiku": {}, "sonnet": {}, "opus": {}, "fable": {}}}
    admin_task, _ = await handle_mention(store, config, notifier, event(text="!fable-high fix it", user="U1", ts="1.1"), "Ev1", BOT)
    assert admin_task.model_override == "fable"
    assert admin_task.effort_override == "high"
    assert admin_task.request_text == "fix it"
    config.roles["developer"] = Role("developer", ["U2"], ["read_only", "standard", "deep"], False, 12.0, None)
    user_task, _ = await handle_mention(store, config, notifier, event(text="!fable-high fix it", user="U2", ts="2.2"), "Ev2", BOT)
    assert user_task.model_override is None
    assert user_task.effort_override is None
    assert user_task.request_text == "fix it"


@pytest.mark.asyncio
async def test_thread_reply_links_parent_task(store, config, notifier):
    root, _ = await handle_mention(store, config, notifier, event(ts="100.1"), "Ev1", BOT)
    reply, _ = await handle_mention(store, config, notifier, event(text="also check the logs", ts="100.2", thread_ts="100.1"), "Ev2", BOT)
    assert reply.parent_task_id == root.task_id
    assert reply.slack_thread_ts == "100.1"


@pytest.mark.asyncio
async def test_paused_intake_refusal_reaches_slack(store, config, notifier):
    store.meta_set("intake_paused", "1")
    task, status = await handle_mention(store, config, notifier, event(), "Ev1", BOT)
    assert status == "paused"
    assert ("refuse_intake", "C1", "100.1", "intake is paused right now — try again soon") in notifier.calls


class FakeSlackClient:
    def __init__(self):
        self.posts = []
        self.reactions = []
        self.uploads = []
        self.replies = []
        self.reply_calls = []
        self.history = []
        self.history_calls = []
        self.reaction_error = None
        self.upload_error = None
        self.conversations_open_result = None
        self.conversations_open_error = None
        self.conversations_open_calls = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)

    async def conversations_open(self, **kwargs):
        self.conversations_open_calls.append(kwargs)
        if self.conversations_open_error:
            raise self.conversations_open_error
        return self.conversations_open_result or {"channel": {"id": "DFALLBACK"}}

    async def reactions_add(self, **kwargs):
        if self.reaction_error:
            raise self.reaction_error
        self.reactions.append(kwargs)

    async def files_upload_v2(self, **kwargs):
        if self.upload_error:
            raise self.upload_error
        self.uploads.append(kwargs)

    async def conversations_replies(self, **kwargs):
        self.reply_calls.append(kwargs)
        if isinstance(self.replies, Exception):
            raise self.replies
        return {"messages": self.replies}

    async def conversations_history(self, **kwargs):
        self.history_calls.append(kwargs)
        if isinstance(self.history, Exception):
            raise self.history
        return {"messages": self.history}


@pytest.mark.asyncio
async def test_slack_notifier_progress_is_rate_limited_and_redacted(store, make_task):
    client = FakeSlackClient()
    slack_notifier = SlackNotifier(client, progress_min_interval_seconds=60)
    task = make_task()
    await slack_notifier.progress(task, "\n# Update\ncloned the repo, token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    await slack_notifier.progress(task, "second update inside the interval")
    assert len(client.posts) == 1  # coalesced (SLK-006)
    assert "ghp_" not in client.posts[0]["text"]
    assert "*Update*" in client.posts[0]["text"]


@pytest.mark.asyncio
async def test_slack_notifier_posts_to_originating_thread(store, make_task):
    client = FakeSlackClient()
    slack_notifier = SlackNotifier(client, ack_reaction=False)
    task = make_task("fix the bug")
    await slack_notifier.ack(task)
    await slack_notifier.failed(task, "it broke")
    await slack_notifier.refuse_intake("C9", "50.5", "not allowed")
    assert client.posts[0]["channel"] == task.slack_channel_id
    assert client.posts[0]["thread_ts"] == task.slack_thread_ts
    assert client.posts[0]["text"] == "On it."
    assert "it broke" in client.posts[1]["text"]
    assert client.posts[2] == {"channel": "C9", "thread_ts": "50.5", "text": "not allowed"}


@pytest.mark.asyncio
async def test_github_origin_notifications_are_non_threaded_or_skipped(store):
    task, created = store.create_task(slack_team_id="github", slack_channel_id="CREVIEWS", slack_thread_ts="org/a#1@sha", slack_message_ts="org/a#1@sha", slack_user_id="github", request_text="/review url")
    assert created
    client = FakeSlackClient()
    notifier = SlackNotifier(client)
    await notifier.ack(task)
    await notifier.started(task)
    assert client.reactions == []
    assert all("thread_ts" not in post for post in client.posts)
    assert all(post["channel"] == "CREVIEWS" for post in client.posts)

    silent, created = store.create_task(slack_team_id="github", slack_channel_id="", slack_thread_ts="org/a#2@sha", slack_message_ts="org/a#2@sha", slack_user_id="github", request_text="/review url")
    assert created
    before = len(client.posts)
    await notifier.ack(silent)
    await notifier.failed(silent, "failed")
    assert len(client.posts) == before


@pytest.mark.asyncio
async def test_blue_task_posts_announced_by_red(store):
    client = AsyncMock()
    notifier = SlackNotifier(client, ack_reaction=False, reviewer_name="Blue")
    task, created = store.create_task(
        slack_team_id="github",
        slack_channel_id="CREVIEWS",
        slack_thread_ts="reviewer:org/a#1@sha",
        slack_message_ts="reviewer:org/a#1@sha",
        slack_user_id="github",
        request_text="/review https://github.com/org/a/pull/1",
        persona="reviewer",
    )
    assert created
    task.reply = "No blocking findings."

    await notifier.started(task)
    await notifier.completed(task)

    started, completed = [call.kwargs for call in client.chat_postMessage.call_args_list]
    assert "Blue started a PR review" in started["text"]
    assert completed["text"].startswith("*Blue finished a PR review*\n")
    assert task.task_id not in completed["text"]
    assert "No blocking findings." in completed["text"]
    for post in (started, completed):
        assert "username" not in post
        assert "icon_url" not in post
        assert "icon_emoji" not in post


@pytest.mark.asyncio
async def test_blue_task_uses_started_message_pool(make_task, tmp_path):
    pool = tmp_path / "started.yaml"
    pool.write_text('agent: ["On it."]\nreviewer: ["{reviewer_name} is starting a PR review. Everyone act natural."]\n')
    task = make_task(persona="reviewer")
    task.model_alias = "opus"
    task.debug_permalink = "https://slack.test/debug/thread"
    client = FakeSlackClient()
    notifier = SlackNotifier(client, reviewer_name="Blue", task_started_messages_path=str(pool))

    await notifier.started(task)

    assert client.posts[-1]["text"] == f"Blue is starting a PR review. Everyone act natural.\n<https://slack.test/debug/thread|Details - Model: opus - Task ID: {task.task_id}>"


@pytest.mark.asyncio
async def test_notifier_post_failure_is_recorded_without_retry(store, make_task):
    client = AsyncMock()
    client.chat_postMessage.side_effect = RuntimeError("slack unavailable")
    task = make_task(persona="reviewer")
    notifier = SlackNotifier(client, ack_reaction=False, store=store)

    with pytest.raises(RuntimeError, match="slack unavailable"):
        await notifier.started(task)

    assert client.chat_postMessage.await_count == 1
    assert store.recent_errors(1)[0]["task_id"] == task.task_id


@pytest.mark.asyncio
async def test_read_only_channel_falls_back_to_dm_instead_of_raising(store, make_task):
    client = FakeSlackClient()
    task = make_task("fix the bug")
    original_post = client.chat_postMessage

    async def failing_first_post(**kwargs):
        if kwargs.get("channel") == task.slack_channel_id:
            raise SlackApiError("read only", {"ok": False, "error": "restricted_action_read_only_channel"})
        await original_post(**kwargs)

    client.chat_postMessage = failing_first_post
    notifier = SlackNotifier(client, ack_reaction=False, store=store)

    await notifier.started(task)  # must not raise (#83)

    assert client.conversations_open_calls == [{"users": "U1"}]
    assert len(client.posts) == 1
    assert client.posts[0]["channel"] == "DFALLBACK"
    assert "DMing you instead" in client.posts[0]["text"]
    errors = store.errors_for(task.task_id)
    assert len(errors) == 1  # the fallback landed on the first try, so no dm_fallback row was added
    assert errors[0]["kind"] == "SlackApiError"


@pytest.mark.asyncio
async def test_read_only_channel_falls_back_to_debug_when_no_user_or_dm_fails(store, make_task):
    client = FakeSlackClient()
    client.conversations_open_error = RuntimeError("dm unavailable")
    debug = AsyncMock()
    task = make_task("fix the bug")
    original_post = client.chat_postMessage

    async def failing_first_post(**kwargs):
        if kwargs.get("channel") == task.slack_channel_id:
            raise SlackApiError("read only", {"ok": False, "error": "restricted_action_read_only_channel"})
        await original_post(**kwargs)

    client.chat_postMessage = failing_first_post
    notifier = SlackNotifier(client, ack_reaction=False, store=store, debug=debug)

    await notifier.started(task)

    debug.system_error.assert_awaited_once()
    assert "slack" in debug.system_error.await_args.args
    assert client.posts == []  # the DM attempt itself failed, so nothing else was posted to slack
    errors = store.errors_for(task.task_id)
    assert len(errors) == 2  # the read-only channel post failure, plus the dm_fallback failure
    assert errors[0]["kind"] == "SlackApiError"
    assert errors[1]["kind"] == "RuntimeError"  # the conversations_open failure ("dm unavailable")


@pytest.mark.asyncio
async def test_read_only_channel_for_github_origin_task_skips_dm_and_goes_to_debug(store):
    # slack_user_id is "github" for system-origin tasks (#83 review) — not a real slack user, so a DM attempt would just fail
    task, created = store.create_task(slack_team_id="github", slack_channel_id="CREVIEWS", slack_thread_ts="org/a#1@sha", slack_message_ts="org/a#1@sha", slack_user_id="github", request_text="/review url")
    assert created
    client = FakeSlackClient()

    async def failing_post(**kwargs):
        raise SlackApiError("read only", {"ok": False, "error": "restricted_action_read_only_channel"})

    client.chat_postMessage = failing_post
    debug = AsyncMock()
    notifier = SlackNotifier(client, ack_reaction=False, store=store, debug=debug)

    await notifier.started(task)

    assert client.conversations_open_calls == []  # no DM attempted for a system identity
    debug.system_error.assert_awaited_once()
    errors = store.errors_for(task.task_id)
    assert [e["kind"] for e in errors] == ["SlackApiError"]  # no dm_fallback row — the DM was never attempted


def test_config_fixture_has_slack_enabled(config):
    assert config.slack.enabled


@pytest.mark.asyncio
async def test_ack_uses_reaction_and_falls_back_to_exact_text(make_task):
    task = make_task()
    client = FakeSlackClient()
    notifier = SlackNotifier(client)
    await notifier.ack(task)
    assert client.reactions == [{"channel": "C1", "timestamp": task.slack_message_ts, "name": "eyes"}]
    assert client.posts == []

    client.reaction_error = RuntimeError("missing scope")
    await notifier.ack(task)
    assert client.posts[-1]["text"] == "On it."

    client = FakeSlackClient()
    await SlackNotifier(client, ack_reaction=False).ack(task)
    assert client.posts[-1]["text"] == "On it."


@pytest.mark.asyncio
async def test_thread_transcript_formats_excludes_and_caps():
    client = FakeSlackClient()
    client.replies = [{"ts": str(i), "user": f"U{i}", "text": "x" * 300} for i in range(35)] + [{"ts": "trigger", "user": "U0", "text": "do it"}, {"ts": "empty", "user": "U0", "text": ""}]
    transcript = await fetch_thread_transcript(client, "C1", "1", "trigger")
    assert "<@U0>" not in transcript
    assert "<@U34>:" in transcript
    assert transcript.startswith("(earlier messages omitted)\n")
    assert len(transcript) <= THREAD_CONTEXT_MAX_CHARS
    assert client.reply_calls == [{"channel": "C1", "ts": "1", "limit": 200}]


@pytest.mark.asyncio
async def test_thread_transcript_paginates_to_newest_messages_and_labels_bot():
    client = AsyncMock()
    pages = [
        {"messages": [{"ts": str(i), "user": f"U{i}", "text": f"message {i}"} for i in range(200)], "response_metadata": {"next_cursor": "page-2"}},
        {"messages": [{"ts": str(i), "user": f"U{i}", "text": f"message {i}"} for i in range(200, 400)], "response_metadata": {"next_cursor": "page-3"}},
        {
            "messages": [{"ts": str(i), "user": BOT if i == 498 else f"U{i}", "text": f"message {i}"} for i in range(400, 500)],
            "response_metadata": {"next_cursor": ""},
        },
    ]
    client.conversations_replies.side_effect = pages

    transcript = await fetch_thread_transcript(client, "C1", "1", "499", bot_user_id=BOT)

    assert "message 497" in transcript
    assert "message 199" not in transcript
    assert "Agent: message 498" in transcript
    assert f"<@{BOT}>: message 498" not in transcript
    assert client.conversations_replies.call_args_list[1].kwargs["cursor"] == "page-2"
    assert client.conversations_replies.call_args_list[2].kwargs["cursor"] == "page-3"


@pytest.mark.asyncio
async def test_thread_transcript_includes_bot_messages_labeled_and_truncated():
    client = FakeSlackClient()
    client.replies = [
        {"ts": "1", "user": "U1", "text": "expand on that"},
        {"ts": "2", "user": BOT, "text": "here is my answer"},
        {"ts": "3", "user": "U1", "text": "do what you suggested"},
        {"ts": "4", "user": BOT, "text": "x" * 600},
        {"ts": "5", "bot_id": "B1", "text": "some other bot's message"},
        {"ts": "trigger", "user": "U1", "text": "follow up"},
    ]
    transcript = await fetch_thread_transcript(client, "C1", "1", "trigger", bot_user_id=BOT, bot_name="Red")
    assert "Red: here is my answer" in transcript
    assert "<@U1>: expand on that" in transcript
    assert "<@U1>: do what you suggested" in transcript
    assert f"Red: {'x' * 500}" in transcript
    assert "x" * 600 not in transcript
    assert "Red: some other bot's message" not in transcript


@pytest.mark.asyncio
async def test_dm_transcript_formats_both_sides_and_recovers_from_history_failure(store):
    client = FakeSlackClient()
    client.history = [
        {"ts": "3", "user": "U1", "text": "newest"},
        {"ts": "2", "user": BOT, "text": "previous answer"},
        {"ts": "1", "user": "U1", "text": "first question"},
    ]
    transcript = await fetch_dm_transcript(client, "D1", "3", BOT, store, "Red")
    assert transcript == "<@U1>: first question\nRed: previous answer"
    assert client.history_calls == [{"channel": "D1", "limit": 20}]

    client.history = RuntimeError("slack down")
    assert await fetch_dm_transcript(client, "D1", "4", BOT, store, "Red") is None
    assert store.recent_errors(1)[0]["component"] == "slack"


@pytest.mark.asyncio
async def test_plain_dm_answers_without_creating_a_queued_task_and_dedupes(store, config, notifier):
    client = AsyncMock()
    client.conversations_history.return_value = {"messages": [{"ts": "1", "user": "U1", "text": "earlier"}]}
    quick = AsyncMock()
    quick.chat.return_value = (SimpleNamespace(reply="Conversational answer.", result_summary=None), "answer")
    dm = {"text": "follow up", "channel": "D1", "channel_type": "im", "user": "U1", "ts": "2", "team": "T1"}

    status = await handle_dm(store, config, notifier, dm, "EvDM1", BOT, quick, client)
    duplicate = await handle_dm(store, config, notifier, dm, "EvDM1", BOT, quick, client)

    assert status == "answer"
    assert duplicate == "duplicate_event"
    assert store.count_tasks(RECEIVED) == 0
    assert notifier.calls == [("answer", "D1", None, "Conversational answer.")]
    assert "earlier" in quick.chat.call_args.kwargs["history"]


# Slack resolves a leading "/help" typed into a DM as a slash command and never delivers it as a
# message event, so only the bare word "help" is exercised here for the DM path. Casing is already
# covered exhaustively by test_is_help_request_matches_narrowly.
@pytest.mark.asyncio
async def test_help_dm_replies_without_consuming_a_quick_answer_slot(store, config, notifier, tmp_path):
    help_file = tmp_path / "help.md"
    help_file.write_text("Usage guide for Red.")
    config.help_path = str(help_file)
    client = AsyncMock()
    quick = AsyncMock()
    dm = {"text": "help", "channel": "D1", "channel_type": "im", "user": "U1", "ts": "1", "team": "T1"}
    status = await handle_dm(store, config, notifier, dm, "EvDMHelp", BOT, quick, client)
    assert status == "help"
    assert store.count_tasks(RECEIVED) == 0
    quick.chat.assert_not_awaited()
    assert notifier.calls == [("answer", "D1", None, "Usage guide for Red.")]


@pytest.mark.asyncio
async def test_help_dm_works_even_without_quick_answer_enabled(store, config, notifier, tmp_path):
    help_file = tmp_path / "help.md"
    help_file.write_text("Usage guide for Red.")
    config.help_path = str(help_file)
    client = AsyncMock()
    dm = {"text": "help", "channel": "D1", "channel_type": "im", "user": "U1", "ts": "1", "team": "T1"}
    status = await handle_dm(store, config, notifier, dm, "EvDMHelp2", BOT, None, client)
    assert status == "help"
    assert notifier.calls == [("answer", "D1", None, "Usage guide for Red.")]


@pytest.mark.asyncio
async def test_dm_escalation_and_unauthorized_user_never_create_tasks(store, config, notifier):
    client = AsyncMock()
    client.conversations_history.return_value = {"messages": []}
    quick = AsyncMock()
    quick.chat.return_value = (None, "escalate")
    dm = {"text": "change the repo", "channel": "D9", "channel_type": "im", "user": "U1", "ts": "1", "team": "T1"}
    assert await handle_dm(store, config, notifier, dm, "EvDM2", BOT, quick, client) == "escalate"
    assert "Mention `@Agent`" in notifier.calls[-1][3]

    unauthorized = {**dm, "user": "U9", "ts": "2"}
    assert await handle_dm(store, config, notifier, unauthorized, "EvDM3", BOT, quick, client) == "unauthorized"
    assert "configured role" in notifier.calls[-1][3]
    assert store.count_tasks(RECEIVED) == 0


@pytest.mark.asyncio
async def test_dm_with_mention_uses_normal_task_intake(store, config, notifier):
    client = AsyncMock()
    client.users_info.return_value = {"user": {"team_id": "T1", "name": "ada", "profile": {}}}
    dm = {"text": f"<@{BOT}> investigate this", "channel": "D1", "channel_type": "im", "user": "U1", "ts": "1", "team": "T1"}

    status = await handle_dm(store, config, notifier, dm, "EvDM4", BOT, None, client)

    assert status == "created"
    assert store.count_tasks(RECEIVED) == 1
    assert notifier.calls[-1][0] == "ack"


@pytest.mark.asyncio
async def test_thread_context_persists_redacted_and_api_failure_does_not_block(store, config, notifier):
    client = FakeSlackClient()
    client.replies = [{"ts": "100.1", "user": "U1", "text": "token ghp_abcdefghijklmnopqrstuvwxyz0123456789"}, {"ts": "100.2", "user": "U1", "text": "follow up"}]
    task, status = await handle_mention(store, config, notifier, event(text="follow up", ts="100.2", thread_ts="100.1"), "Ev1", BOT, client=client)
    assert status == "created"
    assert "ghp_" not in task.thread_context
    assert "[redacted]" in task.thread_context

    client.replies = RuntimeError("slack down")
    task, status = await handle_mention(store, config, notifier, event(text="another", ts="200.2", thread_ts="200.1"), "Ev2", BOT, client=client)
    assert status == "created"
    assert task.thread_context is None


@pytest.mark.asyncio
async def test_top_level_mention_never_fetches_thread(store, config, notifier):
    client = FakeSlackClient()
    await handle_mention(store, config, notifier, event(), "Ev1", BOT, client=client)
    assert client.reply_calls == []


@pytest.mark.asyncio
async def test_long_completion_uploads_markdown_and_falls_back(make_task):
    task = make_task()
    task.result_summary = "# Final Report\n" + "x" * 3600
    client = FakeSlackClient()
    notifier = SlackNotifier(client)
    await notifier.completed(task)
    assert client.posts == []
    assert client.uploads[0]["filename"] == f"{task.task_id}-report.md"
    assert client.uploads[0]["content"].startswith("# Final Report")

    client.upload_error = RuntimeError("missing scope")
    await notifier.completed(task)
    assert task.task_id not in client.posts[-1]["text"]
    assert "*Final Report*" in client.posts[-1]["text"]


@pytest.mark.asyncio
async def test_long_completion_always_posts_debug_once(make_task):
    task = make_task()
    task.result_summary = "# Final Report\n" + "x" * 3600
    client = FakeSlackClient()
    debug = AsyncMock()
    notifier = SlackNotifier(client, debug=debug)

    await notifier.completed(task)

    debug.completed.assert_awaited_once_with(task)
    assert len(client.uploads) == 1


@pytest.mark.asyncio
async def test_terminal_notifications_prune_progress_entries(make_task):
    client = FakeSlackClient()
    notifier = SlackNotifier(client, progress_min_interval_seconds=0)
    completed = make_task()
    failed = make_task()
    blocked = make_task()
    for task in (completed, failed, blocked):
        await notifier.progress(task, "working")
        assert task.task_id in notifier._last_progress

    await notifier.completed(completed)
    await notifier.failed(failed, "nope")
    await notifier.blocked(blocked)

    assert notifier._last_progress == {}


@pytest.mark.asyncio
async def test_long_completion_with_permalink_omits_details_link(make_task):
    task = make_task()
    task.result_summary = "# Final Report\n" + "x" * 3600
    task.debug_permalink = "https://slack.test/debug/thread"
    client = FakeSlackClient()
    notifier = SlackNotifier(client)

    await notifier.completed(task)

    assert client.posts == []
    assert client.uploads[0]["content"] == task.result_summary
    assert "Additional Details" not in client.uploads[0]["initial_comment"]

    client.upload_error = RuntimeError("missing scope")
    await notifier.completed(task)
    assert "Additional Details" not in client.posts[-1]["text"]


@pytest.mark.asyncio
async def test_started_pool_and_completion_reply_link_without_unfurls(make_task, tmp_path):
    pool = tmp_path / "started.yaml"
    pool.write_text('agent: ["Tiny hammer engaged."]\n')
    task = make_task()
    task.model_alias = "haiku"
    task.debug_permalink = "https://slack.test/debug/thread"
    client = FakeSlackClient()
    notifier = SlackNotifier(client, task_started_messages_path=str(pool))
    await notifier.started(task)
    assert client.posts[-1]["text"] == f"Tiny hammer engaged.\n<https://slack.test/debug/thread|Details - Model: haiku - Task ID: {task.task_id}>"
    assert client.posts[-1]["unfurl_links"] is False
    assert client.posts[-1]["unfurl_media"] is False

    task.reply = "Fixed it. The tests are green."
    task.result_summary = "## Final Report\ninternal details"
    await notifier.completed(task)
    assert client.posts[-1]["text"] == "Fixed it. The tests are green."
    assert "Final Report" not in client.posts[-1]["text"]


@pytest.mark.asyncio
async def test_scheduled_task_started_message_skips_quip_pool(make_task, tmp_path):
    pool = tmp_path / "started.yaml"
    pool.write_text('agent: ["Tiny hammer engaged."]\n')
    task = make_task(schedule_name="Implement approved issues (daily)")
    task.model_alias = "fable"
    task.debug_permalink = "https://slack.test/debug/thread"
    client = FakeSlackClient()
    notifier = SlackNotifier(client, task_started_messages_path=str(pool))
    await notifier.started(task)
    text = client.posts[-1]["text"]
    assert text.startswith("Scheduled Task Started Implement approved issues (daily)")
    assert f"Details: Model: fable Task ID: {task.task_id}" in text
    assert "https://slack.test/debug/thread" in text
    assert "Tiny hammer engaged." not in text


@pytest.mark.asyncio
async def test_scheduled_task_ack_posts_nothing(make_task):
    task = make_task(schedule_name="Implement approved issues (daily)")
    client = FakeSlackClient()
    notifier = SlackNotifier(client)
    await notifier.ack(task)
    assert client.posts == []
    assert client.reactions == []


@pytest.mark.asyncio
async def test_scheduled_task_lifecycle_posts_skip_the_duplicate_top_level_message(make_task):
    # channel_id is the debug channel for scheduled tasks, so the debug feed's threaded post and a top-level
    # _post would otherwise both land in the same channel — only the debug feed should post for these states
    task = make_task(schedule_name="Discover issues (daily)")
    task.slack_channel_id = "C-DEBUG"
    task.blocked_reason = "waiting"
    client = FakeSlackClient()
    debug = AsyncMock()
    notifier = SlackNotifier(client, debug=debug)

    await notifier.progress(task, "Working on it.")
    await notifier.completed(task)
    await notifier.failed(task, "it broke")
    await notifier.blocked(task)
    await notifier.questions(task, "1. What env?")
    await notifier.recovered(task)
    await notifier.refused(task, "queue full")

    assert client.posts == []
    debug.progress.assert_awaited_once_with(task, "Working on it.")
    debug.completed.assert_awaited_once_with(task)
    debug.failed.assert_awaited_once_with(task, "it broke")
    debug.blocked.assert_awaited_once_with(task)
    debug.questions.assert_awaited_once_with(task, "1. What env?")
    debug.recovered.assert_awaited_once_with(task)
    debug.refused.assert_awaited_once_with(task, "queue full")


@pytest.mark.asyncio
async def test_completion_appends_unique_pr_and_jira_artifact_links(store, make_task):
    task = make_task()
    pr_url = "https://github.com/example-org/core/pull/42"
    jira_url = "https://example.atlassian.net/browse/RSQ-7"
    task.reply = f"Opened {pr_url}."
    store.add_artifact(task.task_id, "pull_request", "42", pr_url)
    store.add_artifact(task.task_id, "pull_request", "duplicate-url", pr_url)
    store.add_artifact(task.task_id, "jira_issue", "RSQ-7", jira_url)
    store.add_artifact(task.task_id, "branch", "agent/test", None)
    client = FakeSlackClient()

    await SlackNotifier(client, store=store).completed(task)

    text = client.posts[-1]["text"]
    assert text.count(pr_url) == 1
    assert f"Jira: {jira_url}" in text
    assert task.task_id not in text


@pytest.mark.asyncio
async def test_requester_lifecycle_messages_omit_task_id(make_task):
    task = make_task()
    task.blocked_reason = "need a decision"
    client = FakeSlackClient()
    notifier = SlackNotifier(client, ack_reaction=False, progress_min_interval_seconds=0)

    await notifier.progress(task, "Working on it.")
    await notifier.failed(task, "it broke")
    await notifier.blocked(task)
    await notifier.recovered(task)
    await notifier.refused(task, "unsupported")

    assert all(task.task_id not in post["text"] for post in client.posts)
    assert client.posts[0]["text"] == "Working on it."
    assert client.posts[1]["text"] == "*Failed*\nit broke"
    assert client.posts[3]["text"] == "This task hit a transient issue and was requeued; it will resume shortly."
    assert client.posts[4]["text"] == "Can't take this task: unsupported"


@pytest.mark.asyncio
async def test_blocked_message_names_the_resume_path(store, make_task):
    # a thread reply to a blocked task starts a new task, so the message must not claim it continues this one
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "1.1"}
    notifier = SlackNotifier(client, store=store, dashboard_url="https://red.test/")
    task = make_task()
    store.set_fields(task.task_id, blocked_reason="production logs unreadable")
    await notifier.blocked(store.get_task(task.task_id))
    text = client.chat_postMessage.await_args.kwargs["text"]
    assert "*Blocked*" in text and "production logs unreadable" in text
    assert f"https://red.test/tasks/{task.task_id}" in text
    assert "Reply in this thread to continue" not in text
    # with a pending permission request the same post says an admin's grant resumes the task
    store.request_permission(task.task_id, "access", "aws:production", "AssumeRole denied")
    await notifier.blocked(store.get_task(task.task_id))
    text = client.chat_postMessage.await_args.kwargs["text"]
    assert "*Waiting for operator approval*" in text
    assert f"https://red.test/tasks/{task.task_id}" in text


@pytest.mark.asyncio
async def test_issue_blocked_links_to_the_reopened_issue(make_task):
    task = make_task()
    task.blocked_reason = "needs repo access"
    issue = {"id": 42}
    client = FakeSlackClient()
    notifier = SlackNotifier(client, dashboard_url="https://dash.example.test")

    await notifier.issue_blocked(task, issue)

    text = client.posts[0]["text"]
    assert "needs repo access" in text
    assert "https://dash.example.test/issues?issue=42" in text

    client.posts.clear()
    await SlackNotifier(client).issue_blocked(task, issue)  # no dashboard_url configured
    assert "issues?issue=42" not in client.posts[0]["text"]


@pytest.mark.asyncio
async def test_issue_blocked_reports_a_tracked_pr_instead_of_a_reopen(make_task):
    # #87 review: reopen_issue_and_cancel refetches the issue after transition(), so a task cancelled after it
    # already opened a PR must be worded as "tracking that PR", not the default "reopened as proposed"
    task = make_task()
    task.blocked_reason = "runner crashed"
    issue = {"id": 42, "status": "in_review", "pr_url": "https://github.com/example-org/taskboy/pull/9"}
    client = FakeSlackClient()
    notifier = SlackNotifier(client, dashboard_url="https://dash.example.test")

    await notifier.issue_blocked(task, issue)

    text = client.posts[0]["text"]
    assert "https://github.com/example-org/taskboy/pull/9" in text
    assert "reopened" not in text
    assert "as `proposed`" not in text


@pytest.mark.asyncio
async def test_mention_fetches_profile_and_persists_debug_thread(store, config):
    client = AsyncMock()
    client.users_info.return_value = {"user": {"team_id": "T1", "name": "ada", "real_name": "Ada Lovelace", "profile": {"email": "ada@example.test"}}}
    client.chat_postMessage.return_value = {"ts": "900.1"}
    client.chat_getPermalink.return_value = {"permalink": "https://slack.test/debug/900"}
    debug = DebugFeed(client, store, "CDEBUG")
    notifier = SlackNotifier(client, debug=debug, store=store)
    task, status = await handle_mention(store, config, notifier, event(), "Ev1", BOT, client=client)
    assert status == "created"
    assert task.debug_thread_ts == "900.1"
    assert task.debug_permalink == "https://slack.test/debug/900"
    assert store.get_slack_user("U1")["real_name"] == "Ada Lovelace"
    client.users_info.assert_awaited_once_with(user="U1")


@pytest.mark.asyncio
async def test_silent_github_completion_still_reaches_debug(store):
    task, _ = store.create_task(slack_team_id="github", slack_channel_id="", slack_thread_ts="org/a#1@sha", slack_message_ts="org/a#1@sha", slack_user_id="github", request_text="/review url")
    task.result_summary = "## Final Report\nReviewed"
    client = FakeSlackClient()
    debug = AsyncMock()
    notifier = SlackNotifier(client, debug=debug, store=store)
    await notifier.completed(task)
    debug.completed.assert_awaited_once_with(task)
    assert client.posts == []


@pytest.mark.asyncio
async def test_mention_reply_answers_blocked_questions_and_resumes(store, config, notifier):
    from taskboy.models import BLOCKED, QUEUED, RUNNING

    root, _ = await handle_mention(store, config, notifier, event(ts="100.1"), "Ev1", BOT)
    store.transition(root.task_id, RECEIVED, QUEUED, "classified")
    store.transition(root.task_id, QUEUED, RUNNING, "dispatched")
    store.ask_questions(root.task_id, "1. Which env?")
    store.transition(root.task_id, RUNNING, BLOCKED, "runner blocked", blocked_reason="waiting for the requester to answer follow-up questions", session_id="s-q")
    answered, status = await handle_mention(store, config, notifier, event(text="1. staging", ts="100.2", thread_ts="100.1"), "Ev2", BOT)
    assert status == "answered"
    assert answered.task_id == root.task_id
    task = store.get_task(root.task_id)
    assert task.state == QUEUED
    assert task.resume_session_id == "s-q"
    rounds = store.answered_questions_for(root.task_id)
    assert [(r["answer_text"], r["answered_by"]) for r in rounds] == [("1. staging", "U1")]
    assert store.count_tasks(RECEIVED) == 0  # the reply never became a new task
    assert ("answer", "C1", "100.1", "Got it — resuming with your answers.") in notifier.calls


@pytest.mark.asyncio
async def test_help_mention_inside_blocked_thread_shows_usage_instead_of_answering(store, config, notifier, tmp_path):
    from taskboy.models import BLOCKED, QUEUED, RUNNING

    help_file = tmp_path / "help.md"
    help_file.write_text("Usage guide for Red.")
    config.help_path = str(help_file)

    root, _ = await handle_mention(store, config, notifier, event(ts="100.1"), "Ev1", BOT)
    store.transition(root.task_id, RECEIVED, QUEUED, "classified")
    store.transition(root.task_id, QUEUED, RUNNING, "dispatched")
    store.ask_questions(root.task_id, "1. Which env?")
    store.transition(root.task_id, RUNNING, BLOCKED, "runner blocked", session_id="s-q")

    task, status = await handle_mention(store, config, notifier, event(text="/help", ts="100.2", thread_ts="100.1"), "Ev2", BOT)

    assert task is None
    assert status == "help"
    assert store.get_task(root.task_id).state == BLOCKED  # /help never consumed as the pending answer
    assert store.answered_questions_for(root.task_id) == []
    assert notifier.calls[-1] == ("answer", "C1", "100.1", "Usage guide for Red.")


@pytest.mark.asyncio
async def test_mention_reply_from_other_user_stays_a_follow_up_task(store, config, notifier):
    from taskboy.config import Role
    from taskboy.models import BLOCKED, QUEUED, RUNNING

    root, _ = await handle_mention(store, config, notifier, event(ts="100.1"), "Ev1", BOT)
    store.transition(root.task_id, RECEIVED, QUEUED, "classified")
    store.transition(root.task_id, QUEUED, RUNNING, "dispatched")
    store.ask_questions(root.task_id, "1. Which env?")
    store.transition(root.task_id, RUNNING, BLOCKED, "runner blocked", session_id="s-q")
    config.roles["developer"] = Role("developer", ["U2"], ["read_only"], False, 2.0, None)
    reply, status = await handle_mention(store, config, notifier, event(text="try prod", user="U2", ts="100.2", thread_ts="100.1"), "Ev2", BOT)
    assert status == "created"
    assert reply.parent_task_id == root.task_id
    assert store.get_task(root.task_id).state == BLOCKED  # only the requester's reply answers


@pytest.mark.asyncio
async def test_plain_thread_reply_answers_only_blocked_questions(store, config, notifier):
    from taskboy.models import BLOCKED, QUEUED, RUNNING
    from taskboy.slack import handle_thread_reply

    root, _ = await handle_mention(store, config, notifier, event(ts="100.1"), "Ev1", BOT)
    plain = {"text": "1. staging", "channel": "C1", "user": "U1", "ts": "100.2", "thread_ts": "100.1", "team": "T1"}
    assert await handle_thread_reply(store, notifier, plain, "Ev2", BOT) == "ignored"  # not blocked yet
    store.transition(root.task_id, RECEIVED, QUEUED, "classified")
    store.transition(root.task_id, QUEUED, RUNNING, "dispatched")
    store.ask_questions(root.task_id, "1. Which env?")
    store.transition(root.task_id, RUNNING, BLOCKED, "runner blocked", session_id="s-q")
    assert await handle_thread_reply(store, notifier, {**plain, "user": "U9"}, "Ev3", BOT) == "ignored"  # only the requester
    assert await handle_thread_reply(store, notifier, {**plain, "text": f"<@{BOT}> 1. staging"}, "Ev4", BOT) == "ignored"  # mentions belong to app_mention
    assert await handle_thread_reply(store, notifier, {**plain, "bot_id": "B1"}, "Ev5", BOT) == "ignored"
    assert await handle_thread_reply(store, notifier, {**plain, "subtype": "message_changed"}, "Ev6", BOT) == "ignored"
    assert await handle_thread_reply(store, notifier, plain, "Ev7", BOT) == "answered"
    task = store.get_task(root.task_id)
    assert task.state == QUEUED
    assert task.resume_session_id == "s-q"
    assert await handle_thread_reply(store, notifier, plain, "Ev7", BOT) == "ignored"  # no longer blocked; redelivery is inert


@pytest.mark.asyncio
async def test_notifier_questions_posts_numbered_list_to_thread(store, make_task):
    client = FakeSlackClient()
    slack_notifier = SlackNotifier(client, ack_reaction=False)
    task = make_task()
    await slack_notifier.questions(task, "1. Which env?\n2. Postgres or Dynamo?")
    assert client.posts[0]["channel"] == task.slack_channel_id
    assert client.posts[0]["thread_ts"] == task.slack_thread_ts
    assert "1. Which env?" in client.posts[0]["text"]
    assert f"<@{task.slack_user_id}>" in client.posts[0]["text"]
