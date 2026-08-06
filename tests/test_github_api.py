import json
from unittest.mock import AsyncMock

import pytest

from taskboy.adapters.github_api import GitHubAdapter, GitHubStatusError, _text
from taskboy.models import QUEUED, RECEIVED

APPROVED = ["org/service-a"]


def routed(store, make_task):
    task = make_task("fix the bug")
    return store.transition(task.task_id, RECEIVED, QUEUED, "classified", profile="standard", classification_json=json.dumps({"target_repos": APPROVED, "jira_keys": ["PROJ-9"]}))


@pytest.fixture
def adapter(store, make_task):
    broker = AsyncMock()
    broker.token_for_task.return_value = "ghs_tok"
    task = routed(store, make_task)
    a = GitHubAdapter(broker, store, task, APPROVED, on_milestone=AsyncMock(), bot_logins=["red[bot]", "blue[bot]"])
    a._request = AsyncMock()
    a._graphql = AsyncMock(return_value={"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}})
    return a


@pytest.mark.asyncio
async def test_create_pull_request_records_artifacts_and_footer(adapter, store):
    adapter._request.side_effect = [[], {"number": 12, "html_url": "https://github.com/org/service-a/pull/12"}]
    result = await adapter.create_pull_request({"repo": "org/service-a", "title": "fix", "head": "agent/t1-fix", "base": "main", "body": "Summary\nTesting\nLimitations"})
    assert "pull/12" in result["content"][0]["text"]
    body_sent = adapter._request.call_args.args[2]["body"]
    assert adapter.task.task_id in body_sent  # GIT-012 slack task reference
    kinds = {(a["kind"], a["external_id"]) for a in store.artifacts_for(adapter.task.task_id)}
    assert ("pull_request", "org/service-a#12") in kinds
    assert ("branch", "org/service-a:agent/t1-fix") in kinds
    adapter.on_milestone.assert_awaited()  # artifact auto-milestone


@pytest.mark.asyncio
async def test_create_pull_request_is_idempotent_via_artifacts(adapter, store):
    store.add_artifact(adapter.task.task_id, "pull_request", "org/service-a#7", "https://github.com/org/service-a/pull/7")
    result = await adapter.create_pull_request({"repo": "org/service-a", "title": "fix", "head": "agent/t1-fix", "body": ""})
    assert "already exists" in result["content"][0]["text"]
    adapter._request.assert_not_awaited()  # no create call (ORC-012)


@pytest.mark.asyncio
async def test_create_pull_request_finds_existing_open_pr_on_github(adapter, store):
    adapter._request.side_effect = [[{"number": 8, "html_url": "https://github.com/org/service-a/pull/8"}]]
    result = await adapter.create_pull_request({"repo": "org/service-a", "title": "fix", "head": "agent/t1-fix", "body": ""})
    assert "already exists" in result["content"][0]["text"]
    assert adapter._request.await_count == 1  # only the GET, never a POST
    assert ("pull_request", "org/service-a#8") in {(a["kind"], a["external_id"]) for a in store.artifacts_for(adapter.task.task_id)}


@pytest.mark.asyncio
async def test_unapproved_repo_is_refused_without_any_request(adapter):
    result = await adapter.create_pull_request({"repo": "org/other", "title": "x", "head": "b", "body": ""})
    assert result.get("isError") is True
    assert "not on the approved list" in result["content"][0]["text"]
    adapter._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_events_are_restricted(adapter, store):
    result = await adapter.create_pr_review({"repo": "org/service-a", "number": 3, "body": "lgtm", "event": "APPROVE"})
    assert result.get("isError") is True
    adapter._request.assert_not_awaited()
    adapter._request.side_effect = [{"id": 55, "html_url": "u"}]
    ok = await adapter.create_pr_review({"repo": "org/service-a", "number": 3, "body": "needs tests", "event": "REQUEST_CHANGES"})
    assert "REQUEST_CHANGES" in ok["content"][0]["text"]
    assert ("pr_comment", "org/service-a#3/review/55") in {(a["kind"], a["external_id"]) for a in store.artifacts_for(adapter.task.task_id)}


@pytest.mark.asyncio
async def test_comment_and_reply_record_artifacts(adapter, store):
    adapter._request.side_effect = [{"id": 1, "html_url": "u1"}, {"id": 2, "html_url": "u2"}]
    await adapter.comment_on_pull_request({"repo": "org/service-a", "number": 4, "body": "hi"})
    await adapter.reply_to_pr_comment({"repo": "org/service-a", "number": 4, "comment_id": 9, "body": "done"})
    kinds = {a["external_id"] for a in store.artifacts_for(adapter.task.task_id) if a["kind"] == "pr_comment"}
    assert kinds == {"org/service-a#4/comment/1", "org/service-a#4/comment/2"}


@pytest.mark.asyncio
async def test_get_pull_request_output_is_bounded_and_trimmed(adapter):
    adapter._request.side_effect = [
        {"number": 5, "title": "t", "state": "open", "html_url": "u", "user": {"login": "dev"}, "body": "b" * 9000, "head": {"ref": "h", "sha": "abcdef"}, "base": {"ref": "main"}, "changed_files": 3, "additions": 10, "deletions": 2, "draft": True, "requested_reviewers": [{"login": "red[bot]"}]}
    ]
    result = await adapter.get_pull_request({"repo": "org/service-a", "number": 5})
    text = result["content"][0]["text"]
    assert len(text) <= 4000  # TOL-006
    parsed = json.loads(text)
    assert parsed["user"] == "dev"
    assert parsed["head_sha"] == "abcdef"
    assert parsed["draft"] is True
    assert parsed["requested_reviewers"] == ["red[bot]"]


@pytest.mark.asyncio
async def test_list_pull_requests_formats_state_and_metadata(adapter):
    adapter._request.return_value = [{"number": 2, "state": "open", "draft": True, "user": {"login": "dev"}, "title": "work", "head": {"sha": "abcdef123"}, "updated_at": "2026-01-01T00:00:00Z"}]
    result = await adapter.list_pull_requests({"repo": "org/service-a", "state": "all"})
    assert adapter._request.call_args.args[1].endswith("state=all&per_page=50&sort=created&direction=desc")
    assert "#2 [open, draft] dev: work (head abcdef1, updated 2026-01-01T00:00:00Z)" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_list_pr_files_pages_and_renders_patch(adapter):
    adapter._request.return_value = [{"filename": "app.py", "status": "modified", "additions": 3, "deletions": 1, "patch": "@@ -1 +1 @@\n-old\n+new"}]
    result = await adapter.list_pr_files({"repo": "org/service-a", "number": 4, "page": 2})
    assert adapter._request.call_args.args[1].endswith("/pulls/4/files?per_page=10&page=2")
    assert "--- app.py (modified, +3/-1)" in result["content"][0]["text"]
    assert "+new" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_list_pr_files_bounds_each_patch_and_keeps_all_headers(adapter):
    adapter._request.return_value = [{"filename": f"file-{index}.py", "status": "modified", "additions": 1, "deletions": 1, "patch": str(index) * 3000} for index in range(10)]
    text = (await adapter.list_pr_files({"repo": "org/service-a", "number": 4}))["content"][0]["text"]
    for index in range(10):
        assert f"--- file-{index}.py" in text
    assert "patch truncated — fetch the full diff with git" in text


def test_shared_text_marks_truncation_and_stays_bounded():
    text = _text("x" * 5000)["content"][0]["text"]
    assert len(text) == 4000
    assert text.endswith("…(output truncated at 4000 chars — narrow the request or use git/the source system for full content)")


@pytest.mark.asyncio
async def test_list_pr_comments_includes_timestamp_and_kind(adapter):
    adapter._request.side_effect = [
        [{"id": 1, "created_at": "2026-01-01T00:00:00Z", "user": {"login": "dev"}, "body": "issue body"}],
        [{"id": 2, "created_at": "2026-01-02T00:00:00Z", "user": {"login": "agent"}, "body": "review body"}],
    ]
    text = (await adapter.list_pr_comments({"repo": "org/service-a", "number": 4}))["content"][0]["text"]
    assert "[1] [2026-01-01T00:00:00Z] dev (issue): issue body" in text
    assert "[2] [2026-01-02T00:00:00Z] agent (review): review body" in text


@pytest.mark.asyncio
async def test_list_pr_comments_annotates_review_thread_resolution(adapter):
    adapter._request.side_effect = [[], [{"id": 2, "created_at": "2026-01-02T00:00:00Z", "user": {"login": "agent"}, "body": "open"}, {"id": 3, "created_at": "2026-01-03T00:00:00Z", "user": {"login": "reviewer"}, "body": "done"}]]
    adapter._graphql.return_value = {
        "repository": {"pullRequest": {"reviewThreads": {"nodes": [{"id": "T1", "isResolved": False, "comments": {"nodes": [{"databaseId": 2, "author": {"login": "red[bot]"}}]}}, {"id": "T2", "isResolved": True, "comments": {"nodes": [{"databaseId": 3, "author": {"login": "blue[bot]"}}]}}]}}}
    }

    text = (await adapter.list_pr_comments({"repo": "org/service-a", "number": 4}))["content"][0]["text"]

    assert "(review): open (unresolved)" in text
    assert "(review): done (resolved)" in text


@pytest.mark.asyncio
async def test_list_pr_comments_degrades_when_thread_query_fails(adapter):
    adapter._request.side_effect = [[], [{"id": 2, "created_at": "now", "user": {"login": "agent"}, "body": "finding"}]]
    adapter._graphql.side_effect = RuntimeError("graphql unavailable")

    text = (await adapter.list_pr_comments({"repo": "org/service-a", "number": 4}))["content"][0]["text"]

    assert "agent (review): finding" in text
    assert "(resolved)" not in text
    assert "(unresolved)" not in text


def review_threads(*, root_login="red[bot]", resolved=False):
    return {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "nodes": [
                        {
                            "id": "PRRT_1",
                            "isResolved": resolved,
                            "comments": {"nodes": [{"databaseId": 10, "author": {"login": root_login}}, {"databaseId": 11, "author": {"login": "dev"}}]},
                        }
                    ]
                }
            }
        }
    }


@pytest.mark.asyncio
async def test_resolve_pr_thread_resolves_red_authored_thread(adapter):
    adapter._graphql.side_effect = [review_threads(), {"resolveReviewThread": {"thread": {"isResolved": True}}}]

    result = await adapter.resolve_pr_thread({"repo": "org/service-a", "number": 4, "comment_id": 11})

    assert result["content"][0]["text"] == "resolved review thread for comment 11 on org/service-a#4"
    mutation = adapter._graphql.await_args_list[1]
    assert "resolveReviewThread" in mutation.args[0]
    assert mutation.args[1] == {"id": "PRRT_1"}


@pytest.mark.asyncio
async def test_resolve_pr_thread_refuses_human_authored_thread(adapter):
    adapter._graphql.return_value = review_threads(root_login="human")

    result = await adapter.resolve_pr_thread({"repo": "org/service-a", "number": 4, "comment_id": 10})

    assert result["isError"] is True
    assert "only review threads started by Agent or Reviewer can be resolved" in result["content"][0]["text"]
    assert adapter._graphql.await_count == 1


@pytest.mark.asyncio
async def test_resolve_pr_thread_refusal_uses_configured_names(store, make_task):
    """the refusal message names the configured personas, not a hardcoded "Red"/"Blue"."""
    broker = AsyncMock()
    broker.token_for_task.return_value = "ghs_tok"
    task = routed(store, make_task)
    a = GitHubAdapter(broker, store, task, APPROVED, on_milestone=AsyncMock(), bot_logins=["crimson[bot]", "cyan[bot]"], bot_name="Crimson", other_bot_name="Cyan")
    a._graphql = AsyncMock(return_value=review_threads(root_login="human"))

    result = await a.resolve_pr_thread({"repo": "org/service-a", "number": 4, "comment_id": 10})

    assert "only review threads started by Crimson or Cyan can be resolved" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_resolve_pr_thread_is_idempotent(adapter):
    adapter._graphql.return_value = review_threads(resolved=True)

    result = await adapter.resolve_pr_thread({"repo": "org/service-a", "number": 4, "comment_id": 10})

    assert "already resolved" in result["content"][0]["text"]
    assert adapter._graphql.await_count == 1


@pytest.mark.asyncio
async def test_resolve_pr_thread_rejects_unknown_comment_id(adapter):
    adapter._graphql.return_value = review_threads()

    result = await adapter.resolve_pr_thread({"repo": "org/service-a", "number": 4, "comment_id": 404})

    assert result["isError"] is True
    assert "review-comment id from list_pr_comments" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_resolve_pr_thread_refuses_without_bot_logins(adapter):
    adapter.bot_logins = []

    result = await adapter.resolve_pr_thread({"repo": "org/service-a", "number": 4, "comment_id": 10})

    assert result["isError"] is True
    assert "cannot verify thread authorship" in result["content"][0]["text"]
    adapter._graphql.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_pull_request_closes_and_records_milestone(adapter):
    adapter._request.side_effect = [{"number": 6, "state": "open", "html_url": "u6"}, {"number": 6, "state": "closed", "html_url": "u6"}]
    result = await adapter.close_pull_request({"repo": "org/service-a", "number": 6})
    assert "closed pull request #6: u6" in result["content"][0]["text"]
    assert adapter._request.call_args.args[:2] == ("PATCH", "/repos/org/service-a/pulls/6")
    assert adapter._request.call_args.args[2] == {"state": "closed"}
    adapter.on_milestone.assert_awaited()


@pytest.mark.asyncio
async def test_close_pull_request_is_idempotent_for_already_closed(adapter):
    adapter._request.side_effect = [{"number": 6, "state": "closed", "html_url": "u6"}]
    result = await adapter.close_pull_request({"repo": "org/service-a", "number": 6})
    assert "already closed" in result["content"][0]["text"]
    assert adapter._request.await_count == 1  # only the GET, never a PATCH
    adapter.on_milestone.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_branch_deletes_agent_prefixed_branch(adapter):
    adapter._request.return_value = None
    result = await adapter.delete_branch({"repo": "org/service-a", "branch": "agent/t1-fix"})
    assert "deleted branch agent/t1-fix from org/service-a" in result["content"][0]["text"]
    assert adapter._request.call_args.args == ("DELETE", "/repos/org/service-a/git/refs/heads/agent/t1-fix")


@pytest.mark.asyncio
async def test_delete_branch_strips_refs_heads_prefix(adapter):
    adapter._request.return_value = None
    await adapter.delete_branch({"repo": "org/service-a", "branch": "refs/heads/agent/t1-fix"})
    assert adapter._request.call_args.args[1] == "/repos/org/service-a/git/refs/heads/agent/t1-fix"


@pytest.mark.asyncio
async def test_delete_branch_refuses_non_agent_branches(adapter):
    result = await adapter.delete_branch({"repo": "org/service-a", "branch": "main"})
    assert result.get("isError") is True
    assert "only agent/-prefixed branches" in result["content"][0]["text"]
    adapter._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_branch_requires_a_branch_name(adapter):
    result = await adapter.delete_branch({"repo": "org/service-a", "branch": ""})
    assert result.get("isError") is True
    adapter._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_branch_is_idempotent_for_a_missing_ref(adapter):
    adapter._request.side_effect = GitHubStatusError(422, "github api DELETE ... failed: 422")
    result = await adapter.delete_branch({"repo": "org/service-a", "branch": "agent/t1-fix"})
    assert "already deleted" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_delete_branch_reraises_other_status_errors(adapter):
    adapter._request.side_effect = GitHubStatusError(403, "github api DELETE ... failed: 403")
    with pytest.raises(GitHubStatusError):
        await adapter.delete_branch({"repo": "org/service-a", "branch": "agent/t1-fix"})


@pytest.mark.asyncio
async def test_create_release_records_artifact_and_milestone(adapter, store):
    adapter._request.return_value = {"html_url": "https://github.com/org/service-a/releases/tag/v1.4.0"}
    result = await adapter.create_release({"repo": "org/service-a", "tag_name": "v1.4.0", "body": "- added things"})
    assert "created release v1.4.0" in result["content"][0]["text"]
    assert adapter._request.call_args.args[:2] == ("POST", "/repos/org/service-a/releases")
    payload = adapter._request.call_args.args[2]
    assert payload == {"tag_name": "v1.4.0", "name": "v1.4.0", "body": "- added things"}
    assert ("release", "org/service-a@v1.4.0") in {(a["kind"], a["external_id"]) for a in store.artifacts_for(adapter.task.task_id)}
    adapter.on_milestone.assert_awaited()


@pytest.mark.asyncio
async def test_create_release_accepts_name_and_target_commitish(adapter):
    adapter._request.return_value = {"html_url": "u"}
    await adapter.create_release({"repo": "org/service-a", "tag_name": "v1.4.0", "name": "1.4.0 release", "body": "notes", "target_commitish": "release-branch"})
    payload = adapter._request.call_args.args[2]
    assert payload == {"tag_name": "v1.4.0", "name": "1.4.0 release", "body": "notes", "target_commitish": "release-branch"}


@pytest.mark.asyncio
async def test_create_release_skips_when_artifact_already_recorded(adapter, store):
    store.add_artifact(adapter.task.task_id, "release", "org/service-a@v1.4.0", "https://github.com/org/service-a/releases/tag/v1.4.0")
    result = await adapter.create_release({"repo": "org/service-a", "tag_name": "v1.4.0", "body": "notes"})
    assert "already exists" in result["content"][0]["text"]
    adapter._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_release_treats_422_as_already_released(adapter):
    adapter._request.side_effect = GitHubStatusError(422, "github api POST /repos/org/service-a/releases failed: 422")
    result = await adapter.create_release({"repo": "org/service-a", "tag_name": "v1.4.0", "body": "notes"})
    assert result.get("isError") is not True
    assert "already exists" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_create_release_reraises_non_422_status(adapter):
    adapter._request.side_effect = GitHubStatusError(500, "github api POST /repos/org/service-a/releases failed: 500")
    with pytest.raises(GitHubStatusError):
        await adapter.create_release({"repo": "org/service-a", "tag_name": "v1.4.0", "body": "notes"})


@pytest.mark.asyncio
async def test_create_release_rejects_malformed_tag_name(adapter):
    result = await adapter.create_release({"repo": "org/service-a", "tag_name": "1.4.0", "body": "notes"})
    assert result.get("isError") is True
    assert "vX.Y.Z" in result["content"][0]["text"]
    adapter._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_release_requires_body(adapter):
    result = await adapter.create_release({"repo": "org/service-a", "tag_name": "v1.4.0", "body": "  "})
    assert result.get("isError") is True
    adapter._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_release_refuses_unapproved_repo(adapter):
    result = await adapter.create_release({"repo": "org/other", "tag_name": "v1.4.0", "body": "notes"})
    assert result.get("isError") is True
    assert "not on the approved list" in result["content"][0]["text"]
    adapter._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_review_accepts_inline_comments_json_and_rejects_malformed(adapter):
    adapter._request.return_value = {"id": 9, "html_url": "u"}
    await adapter.create_pr_review({"repo": "org/service-a", "number": 3, "body": "findings", "event": "COMMENT", "comments_json": json.dumps([{"path": "app.py", "line": 8, "body": "simplify"}])})
    payload = adapter._request.call_args.args[2]
    assert payload["comments"] == [{"path": "app.py", "line": 8, "body": "simplify", "side": "RIGHT"}]
    adapter._request.reset_mock()
    malformed = await adapter.create_pr_review({"repo": "org/service-a", "number": 3, "event": "COMMENT", "comments_json": "not json"})
    assert malformed["isError"] is True
    adapter._request.assert_not_awaited()
