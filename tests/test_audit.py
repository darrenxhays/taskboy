from unittest.mock import patch

import pytest

from agent_harness import audit
from agent_harness.models import QUEUED, RECEIVED


def _events(store, make_task, n=3):
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED, "classified")
    for i in range(n):
        store.add_event(task.task_id, "tool_call", {"step": i}, tool_name="Bash", is_write=True)
    return task


def test_chain_verifies_and_detects_tampering(store, make_task):
    _events(store, make_task)
    intact, checked = audit.verify_chain(store)
    assert intact is True
    assert checked >= 5  # intake + state_change + 3 tool calls

    # tamper with one historical record: the chain must break
    store.conn.execute("UPDATE task_events SET detail_json = '{\"step\": 999}' WHERE kind = 'tool_call' AND detail_json LIKE '%\"step\": 1%'")
    store.conn.commit()
    intact, _ = audit.verify_chain(store)
    assert intact is False


@pytest.mark.asyncio
async def test_ship_once_advances_cursor_and_is_idempotent(store, make_task):
    _events(store, make_task)
    shipped_bodies = []
    with patch.object(audit, "_put", side_effect=lambda bucket, key, body: shipped_bodies.append((bucket, key, body))):
        first = await audit.ship_once(store, "example-staging-audit")
        second = await audit.ship_once(store, "example-staging-audit")
    assert first >= 5
    assert second == 0  # nothing new: cursor advanced
    bucket, key, body = shipped_bodies[0]
    assert bucket == "example-staging-audit"
    assert key.startswith("audit/events-")
    assert body.count("\n") == first - 1

    # new activity ships incrementally
    _events(store, make_task)
    with patch.object(audit, "_put", side_effect=lambda bucket, key, body: shipped_bodies.append((bucket, key, body))):
        third = await audit.ship_once(store, "example-staging-audit")
    assert third >= 5
    assert len(shipped_bodies) == 2
