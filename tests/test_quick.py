import asyncio
from unittest.mock import AsyncMock

import pytest

from taskboy.config import Role
from taskboy.models import COMPLETED, QUEUED, RECEIVED, RUNNING
from taskboy.quick import QuickAnswer
from taskboy.slack import handle_mention

BOT = "UBOT"

CLASSIFICATION = {
    "task_type": "investigation",
    "complexity": "standard",
    "risk": "read_only",
    "expected_duration": "under_hour",
    "required_integrations": ["github"],
    "target_repos": [],
    "jira_keys": [],
}


def event(text="what does HTTP 429 mean?", user="U1", ts="1.1", thread_ts=None):
    value = {"text": f"<@{BOT}> {text}", "channel": "C1", "user": user, "ts": ts, "team": "T1"}
    if thread_ts:
        value["thread_ts"] = thread_ts
    return value


def make_quick(store, config, **overrides):
    config.raw = {
        "models": {"haiku": {"id": "claude-haiku", "fallbacks": []}},
        "quick_answer": {"enabled": True, "tier": "haiku", "timeout_seconds": 1, "max_per_user_per_hour": 30, **overrides},
    }
    return QuickAnswer(store, config)


@pytest.mark.asyncio
async def test_answer_path_posts_and_records_born_terminal_row(store, config, notifier):
    quick = make_quick(store, config)
    quick._call_model = AsyncMock(return_value=({"action": "answer", "answer": "HTTP 429 means too many requests."}, {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": None, "cache_write_tokens": None, "cost_usd": 0.001}))
    task, status = await handle_mention(store, config, notifier, event(), "Ev1", BOT, quick=quick)
    assert (task, status) == (None, "quick_answer")
    assert notifier.calls[-1] == ("answer", "C1", "1.1", "HTTP 429 means too many requests.")
    completed = store.tasks_in_state(COMPLETED)
    assert len(completed) == 1
    assert completed[0].routing_rationale == "quick-answer"
    assert store.count_tasks(RECEIVED) == 0
    assert store.next_queued() is None
    assert [item["kind"] for item in store.events_for(completed[0].task_id)] == ["intake", "quick_answer"]
    usage = store.conn.execute("SELECT * FROM usage WHERE task_id = ?", (completed[0].task_id,)).fetchone()
    assert usage["source"] == "quick_answer"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [({"action": "classify", "answer": ""}, None), RuntimeError("model down")])
async def test_escalation_and_exception_fall_through(store, config, notifier, response):
    quick = make_quick(store, config)
    if isinstance(response, Exception):
        quick._call_model = AsyncMock(side_effect=response)
    else:
        quick._call_model = AsyncMock(return_value=response)
    task, status = await handle_mention(store, config, notifier, event(), "Ev1", BOT, quick=quick)
    assert status == "created"
    assert task.state == RECEIVED
    assert "quick_escalated" in [item["kind"] for item in store.events_for(task.task_id)]
    error = store.recent_errors(1)[0]
    assert error["component"] == "quick_answer"
    assert error["traceback"]  # was null before this fix, hampering investigation of dropped-field errors


@pytest.mark.asyncio
async def test_timeout_and_rate_cap_never_raise(store, config):
    quick = make_quick(store, config, timeout_seconds=0.001, max_per_user_per_hour=1)

    async def slow(prompt):
        await asyncio.sleep(1)

    quick._call_model = slow
    kwargs = dict(channel_id="C1", thread_ts="1", user_id="U1", text="question", parent=None, team_id="T1", message_ts="1")
    assert await quick.try_answer(**kwargs) == (None, None)
    assert await quick.try_answer(**{**kwargs, "message_ts": "2"}) == (None, None)


@pytest.mark.asyncio
async def test_context_contains_parent_and_referenced_task_blocks(store, config, make_task, monkeypatch):
    parent = make_task("parent")
    store.transition(parent.task_id, RECEIVED, QUEUED, "classified")
    store.transition(parent.task_id, QUEUED, RUNNING, "dispatched")
    parent = store.transition(parent.task_id, RUNNING, COMPLETED, "done", result_summary="parent result")
    referenced = make_task("referenced")
    monkeypatch.setattr("taskboy.quick.memory.read_summary", lambda root, task_id: f"summary for {task_id}")
    quick = make_quick(store, config)
    quick._call_model = AsyncMock(return_value=({"action": "classify", "answer": ""}, None))
    await quick.try_answer(channel_id="C1", thread_ts="1", user_id="U1", text=f"compare {referenced.task_id}", parent=parent, team_id="T1", message_ts="new")
    prompt = quick._call_model.call_args.args[0]
    assert parent.task_id in prompt
    assert "parent result" in prompt
    assert referenced.task_id in prompt
    assert f"summary for {referenced.task_id}" in prompt


@pytest.mark.asyncio
async def test_override_and_unauthorized_mentions_skip_quick(store, config, notifier):
    quick = make_quick(store, config)
    quick.try_answer = AsyncMock()
    task, status = await handle_mention(store, config, notifier, event(text="model:haiku investigate this"), "Ev1", BOT, quick=quick)
    assert status == "created"
    assert task.model_override == "haiku"
    quick.try_answer.assert_not_awaited()

    config.roles["admin"] = Role("admin", ["U1"], ["read_only"], True, None, None)
    task, status = await handle_mention(store, config, notifier, event(user="U9", ts="2"), "Ev2", BOT, quick=quick)
    assert (task, status) == (None, "unauthorized")
    quick.try_answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_quick_answer_uses_personality_and_records_hash(store, config, tmp_path):
    personality = tmp_path / "personality_red.md"
    personality.write_text("Dry and concise.")
    config.personality_path = str(personality)
    quick = make_quick(store, config)
    quick._call_model = AsyncMock(return_value=({"action": "answer", "answer": "Use backoff."}, None))
    task, classification = await quick.try_answer(channel_id="C1", thread_ts="1", user_id="U1", text="what is 429?", parent=None, team_id="T1", message_ts="1")
    assert task.reply == "Use backoff."
    assert classification is None
    assert "Dry and concise." in quick._call_model.call_args.args[0]
    assert any(event["kind"] == "personality" for event in store.events_for(task.task_id))


@pytest.mark.asyncio
async def test_triage_classification_is_persisted_with_usage(store, config, notifier):
    quick = make_quick(store, config)
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": None, "cache_write_tokens": None, "cost_usd": None}
    quick._call_model = AsyncMock(return_value=({"action": "classify", "answer": "", **CLASSIFICATION}, usage))

    task, status = await handle_mention(store, config, notifier, event(text="investigate the service"), "Ev1", BOT, quick=quick)

    assert status == "created"
    assert __import__("json").loads(task.classification_json) == CLASSIFICATION
    recorded = store.conn.execute("SELECT * FROM usage WHERE task_id = ?", (task.task_id,)).fetchone()
    assert (recorded["source"], recorded["model"]) == ("triage", "claude-haiku")
    quick._call_model.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_triage_classification_falls_through_without_persisting(store, config, notifier):
    quick = make_quick(store, config)
    quick._call_model = AsyncMock(return_value=({"action": "classify", "answer": "", "task_type": "bug_fix"}, None))

    task, status = await handle_mention(store, config, notifier, event(text="fix it"), "Ev1", BOT, quick=quick)

    assert status == "created"
    assert task.classification_json is None


@pytest.mark.asyncio
async def test_triage_classification_missing_optional_fields_fills_safe_defaults(store, config, notifier):
    quick = make_quick(store, config)
    partial = {key: value for key, value in CLASSIFICATION.items() if key != "jira_keys"}
    quick._call_model = AsyncMock(return_value=({"action": "classify", "answer": "", **partial}, None))

    task, status = await handle_mention(store, config, notifier, event(text="investigate the service"), "Ev1", BOT, quick=quick)

    assert status == "created"
    assert __import__("json").loads(task.classification_json) == CLASSIFICATION
    quick._call_model.assert_awaited_once()


def test_attempt_map_prunes_inactive_users(store, config):
    quick = make_quick(store, config)
    quick._attempts["old"] = __import__("collections").deque([1.0])
    assert quick._allow_attempt("new", 4000.0) is True
    assert "old" not in quick._attempts


@pytest.mark.asyncio
async def test_dm_chat_answers_records_usage_and_uses_history(store, config):
    quick = make_quick(store, config)
    usage = {"input_tokens": 4, "output_tokens": 2, "cache_read_tokens": None, "cache_write_tokens": None, "cost_usd": 0.001}
    quick._call_chat_model = AsyncMock(return_value=({"action": "answer", "answer": "Yes — that follows."}, usage))

    task, status = await quick.chat(channel_id="D1", user_id="U1", text="and the next step?", team_id="T1", message_ts="2", history="<@U1>: earlier\nRed: previous")

    assert status == "answer"
    assert task.reply == "Yes — that follows."
    assert task.slack_channel_id == "D1"
    assert task.slack_thread_ts == "2"
    assert "Red: previous" in quick._call_chat_model.call_args.args[0]
    assert store.usage_for(task.task_id)[0]["source"] == "quick_answer"


@pytest.mark.asyncio
async def test_dm_chat_escalation_and_rate_cap_do_not_create_tasks(store, config):
    quick = make_quick(store, config, max_per_user_per_hour=1)
    quick._call_chat_model = AsyncMock(return_value=({"action": "escalate", "answer": ""}, None))

    assert await quick.chat(channel_id="D1", user_id="U1", text="change code", team_id="T1", message_ts="1") == (None, "escalate")
    assert await quick.chat(channel_id="D1", user_id="U1", text="try again", team_id="T1", message_ts="2") == (None, "rate_limited")
    assert store.tasks_in_state(COMPLETED) == []
