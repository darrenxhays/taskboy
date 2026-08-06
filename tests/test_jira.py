from unittest.mock import AsyncMock

import pytest

from taskboy.adapters.jira import JiraAdapter, _adf, _adf_to_text
from taskboy.models import QUEUED, RECEIVED

SITE = "https://example.atlassian.net"


@pytest.fixture
def adapter(store, make_task):
    task = make_task("create a story")
    task = store.transition(task.task_id, RECEIVED, QUEUED, "classified", profile="standard")
    a = JiraAdapter(store, task, SITE, "red@example.com", "token", ["RISK"], ["Story", "Bug", "Task"], on_milestone=AsyncMock(), story_points_field="customfield_10016")
    a._request = AsyncMock()
    return a


@pytest.mark.asyncio
async def test_create_issue_labels_footer_and_artifact(adapter, store):
    adapter._request.side_effect = [{"issues": []}, {"key": "RISK-101"}]
    result = await adapter.create_issue({"project": "RISK", "issue_type": "Story", "summary": "Add retry logic", "description": "Retry the poller"})
    assert "RISK-101" in result["content"][0]["text"]
    payload = adapter._request.call_args.args[2]
    assert payload["fields"]["labels"] == ["taskboy", f"agent-task-{adapter.task.task_id}"]  # JIR-008
    assert adapter.task.task_id in _adf_to_text(payload["fields"]["description"])  # task footer
    assert "Created by Agent" in _adf_to_text(payload["fields"]["description"])
    artifacts = store.artifacts_for(adapter.task.task_id)
    assert ("jira_issue", "RISK-101") in {(a["kind"], a["external_id"]) for a in artifacts}
    adapter.on_milestone.assert_awaited()


@pytest.mark.asyncio
async def test_create_issue_footer_uses_configured_bot_name(store, make_task):
    """the footer names the acting persona from config, not a hardcoded "Red" (e.g. Blue's own PR/task footer)."""
    task = make_task("create a story")
    task = store.transition(task.task_id, RECEIVED, QUEUED, "classified", profile="standard")
    adapter = JiraAdapter(store, task, SITE, "blue@example.com", "token", ["RISK"], ["Story", "Bug", "Task"], bot_name="Blue")
    adapter._request = AsyncMock(side_effect=[{"issues": []}, {"key": "RISK-102"}])
    result = await adapter.create_issue({"project": "RISK", "issue_type": "Story", "summary": "Add retry logic", "description": "Retry the poller"})
    assert "RISK-102" in result["content"][0]["text"]
    payload = adapter._request.call_args.args[2]
    assert "Created by Blue" in _adf_to_text(payload["fields"]["description"])


@pytest.mark.asyncio
async def test_create_issue_dedup_via_artifacts_then_jql(adapter, store):
    store.add_artifact(adapter.task.task_id, "jira_issue", "RISK-77", f"{SITE}/browse/RISK-77")
    result = await adapter.create_issue({"project": "RISK", "issue_type": "Story", "summary": "x"})
    assert "RISK-77" in result["content"][0]["text"]
    adapter._request.assert_not_awaited()  # JIR-009: no create call


@pytest.mark.asyncio
async def test_create_issue_dedup_via_jql_label_search(adapter, store):
    adapter._request.side_effect = [{"issues": [{"key": "RISK-88", "fields": {"summary": "x"}}]}]
    result = await adapter.create_issue({"project": "RISK", "issue_type": "Story", "summary": "x"})
    assert "RISK-88" in result["content"][0]["text"]
    assert adapter._request.await_count == 1  # only the jql search, never a POST


@pytest.mark.asyncio
async def test_unapproved_project_and_type_refused(adapter):
    refused = await adapter.create_issue({"project": "OTHER", "issue_type": "Story", "summary": "x"})
    assert refused.get("isError") is True
    refused = await adapter.create_issue({"project": "RISK", "issue_type": "Epic", "summary": "x"})
    assert refused.get("isError") is True
    adapter._request.assert_not_awaited()  # JIR-010


@pytest.mark.asyncio
async def test_transition_matches_by_name_and_refuses_unknown(adapter, store):
    adapter._request.side_effect = [{"transitions": [{"id": "31", "name": "In Progress"}, {"id": "41", "name": "Done"}]}, {}]
    ok = await adapter.transition_issue({"key": "risk-5", "transition": "done"})
    assert "Done" in ok["content"][0]["text"]
    assert adapter._request.call_args.args[2] == {"transition": {"id": "41"}}
    adapter._request.side_effect = [{"transitions": [{"id": "31", "name": "In Progress"}]}]
    refused = await adapter.transition_issue({"key": "RISK-5", "transition": "reopen"})
    assert refused.get("isError") is True


@pytest.mark.asyncio
async def test_link_pr_validates_url_and_records_artifact(adapter, store):
    refused = await adapter.link_pr({"key": "RISK-5", "pr_url": "https://evil.example/pr", "title": "x"})
    assert refused.get("isError") is True
    adapter._request.side_effect = [{}]
    await adapter.link_pr({"key": "RISK-5", "pr_url": "https://github.com/example-org/risk-nextgen-model/pull/3", "title": "fix"})
    kinds = {(a["kind"], a["external_id"]) for a in store.artifacts_for(adapter.task.task_id)}
    assert ("jira_link", "RISK-5 -> https://github.com/example-org/risk-nextgen-model/pull/3") in kinds


def test_adf_roundtrip():
    doc = _adf("first paragraph\n\nsecond paragraph")
    assert doc["version"] == 1
    assert len(doc["content"]) == 2
    text = _adf_to_text(doc)
    assert "first paragraph" in text
    assert "second paragraph" in text
    assert _adf("")["content"]  # never an empty doc (jira rejects it)


@pytest.mark.asyncio
async def test_get_issue_output_is_trimmed(adapter):
    adapter._request.side_effect = [{"key": "RISK-9", "fields": {"summary": "s", "status": {"name": "To Do"}, "issuetype": {"name": "Bug"}, "assignee": None, "labels": ["taskboy"], "priority": {"name": "High"}, "description": _adf("body " * 2000)}}]
    result = await adapter.get_issue({"key": "risk-9"})
    text = result["content"][0]["text"]
    assert len(text) <= 4000
    assert '"key": "RISK-9"' in text


@pytest.mark.asyncio
async def test_create_issue_sets_parent_points_and_assignee(adapter):
    adapter._request.side_effect = [{"issues": []}, {"key": "RISK-102"}]
    await adapter.create_issue({"project": "RISK", "issue_type": "Story", "summary": "Work", "parent_key": "risk-1", "story_points": 3, "assignee_account_id": "acct-1"})
    fields = adapter._request.call_args.args[2]["fields"]
    assert fields["parent"] == {"key": "RISK-1"}
    assert fields["assignee"] == {"accountId": "acct-1"}
    assert fields["customfield_10016"] == 3.0


@pytest.mark.asyncio
async def test_create_issue_without_points_field_adds_note(adapter):
    adapter.story_points_field = ""
    adapter._request.side_effect = [{"issues": []}, {"key": "RISK-103"}]
    result = await adapter.create_issue({"project": "RISK", "issue_type": "Story", "summary": "Work", "story_points": 2})
    assert "story points not set" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_search_users_formats_hidden_email(adapter):
    adapter._request.return_value = [
        {"accountId": "a1", "displayName": "Ada", "emailAddress": "ada@example.test"},
        {"accountId": "a2", "displayName": "Grace"},
    ]
    text = (await adapter.search_users({"query": "a"}))["content"][0]["text"]
    assert "a1: Ada (ada@example.test)" in text
    assert "a2: Grace (hidden)" in text
    assert adapter._request.call_args.kwargs["params"] == {"query": "a", "maxResults": 10}


@pytest.mark.asyncio
async def test_list_boards_and_sprints_use_agile_api(adapter):
    adapter._request.side_effect = [
        {"values": [{"id": 12, "name": "Risk board", "type": "scrum"}]},
        {"values": [{"id": 34, "name": "Sprint 9", "state": "active"}]},
    ]
    boards = await adapter.list_boards({"project": "risk"})
    sprints = await adapter.list_sprints({"board_id": 12})
    assert "12: Risk board (scrum)" in boards["content"][0]["text"]
    assert "34: Sprint 9 [active]" in sprints["content"][0]["text"]
    assert adapter._request.call_args_list[0].args[:2] == ("GET", "/rest/agile/1.0/board")
    assert adapter._request.call_args_list[0].kwargs["params"] == {"maxResults": 20, "projectKeyOrId": "RISK"}
    assert adapter._request.call_args_list[1].kwargs["params"] == {"state": "active,future", "maxResults": 20}


@pytest.mark.asyncio
async def test_assign_move_epic_and_story_points_write_and_audit(adapter, store):
    adapter._request.return_value = {}
    await adapter.assign_issue({"key": "risk-1", "account_id": "acct-1"})
    await adapter.move_to_sprint({"key": "risk-1", "sprint_id": 55})
    await adapter.set_epic({"key": "risk-1", "epic_key": "risk-99"})
    await adapter.set_story_points({"key": "risk-1", "points": 5})

    calls = adapter._request.call_args_list
    assert calls[0].args == ("PUT", "/rest/api/3/issue/RISK-1/assignee", {"accountId": "acct-1"})
    assert calls[1].args == ("POST", "/rest/agile/1.0/sprint/55/issue", {"issues": ["RISK-1"]})
    assert calls[2].args == ("PUT", "/rest/api/3/issue/RISK-1", {"fields": {"parent": {"key": "RISK-99"}}})
    assert calls[3].args == ("PUT", "/rest/api/3/issue/RISK-1", {"fields": {"customfield_10016": 5.0}})
    tool_names = {event["tool_name"] for event in store.events_for(adapter.task.task_id)}
    assert {"mcp__jira__assign_issue", "mcp__jira__move_to_sprint", "mcp__jira__set_epic", "mcp__jira__set_story_points"} <= tool_names


@pytest.mark.asyncio
async def test_set_story_points_requires_configured_field(adapter):
    adapter.story_points_field = ""
    result = await adapter.set_story_points({"key": "RISK-1", "points": 3})
    assert result.get("isError") is True
    assert "story points field is not configured" in result["content"][0]["text"]
    adapter._request.assert_not_awaited()
