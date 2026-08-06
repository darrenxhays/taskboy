import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from taskboy import settings
from taskboy.config import DashboardConfig
from taskboy.dashboard import create_app
from taskboy.models import FAILED, QUEUED, RECEIVED, REFUSED, RUNNING
from taskboy.secrets import Secrets
from tests.conftest import RecordingNotifier, make_config

KID = "test-kid"
_key = ec.generate_private_key(ec.SECP256R1())
PRIVATE_PEM = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
PUBLIC_PEM = _key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

ADMIN = "boss@example.com"
VIEWER = "person@example.com"


def identity(email: str, expired: bool = False, **extra_claims: object) -> dict[str, str]:
    exp = int(time.time()) + (300 if not expired else -600)
    claims = {"email": email, "exp": exp, **extra_claims}
    token = jwt.encode(claims, PRIVATE_PEM, algorithm="ES256", headers={"kid": KID})
    return {"x-amzn-oidc-data": token}


def admin_headers() -> dict[str, str]:
    return {**identity(ADMIN), "x-harness-dashboard": "1"}


@pytest.fixture
def dashboard(store, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setattr(settings, "SKILLS_ROOT", str(tmp_path / "skills"))
    config_path = tmp_path / "config.yaml"
    config_path.write_text("orchestrator:\n  max_concurrency: 1\n  queue_max: 5\n  max_retries: 1\n  progress_min_interval_seconds: 0\n  runner: echo\n")
    monkeypatch.setattr(settings, "CONFIG_PATH", str(config_path))
    personality_path = tmp_path / "personality_red.md"
    personality_path.write_text("Red is direct and dry.")
    config = make_config(
        personality_path=str(personality_path),
        dashboard=DashboardConfig(enabled=True, allowed_email_domain="example.com", admin_emails=[ADMIN], commit_repo="example-org/taskboy"),
    )
    notifier = RecordingNotifier()
    app = create_app(store, config, notifier, Secrets(dashboard_github_token="pat"), orchestrator=None, ui_dist=str(tmp_path / "dist"))
    app.state.alb_key_cache[KID] = PUBLIC_PEM
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://red.test")
    return client, config, notifier


@pytest.mark.asyncio
async def test_healthz_is_open(dashboard):
    client, _, _ = dashboard
    assert (await client.get("/healthz")).json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_api_requires_identity(dashboard):
    client, _, _ = dashboard
    assert (await client.get("/api/me")).status_code == 401


@pytest.mark.asyncio
async def test_expired_identity_rejected(dashboard):
    client, _, _ = dashboard
    assert (await client.get("/api/me", headers=identity(ADMIN, expired=True))).status_code == 401


@pytest.mark.asyncio
async def test_wrong_domain_rejected(dashboard):
    client, _, _ = dashboard
    assert (await client.get("/api/me", headers=identity("stranger@evil.example"))).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("email_verified", [False, "false", 0, ""])
async def test_unverified_email_rejected(dashboard, email_verified):
    client, _, _ = dashboard
    response = await client.get("/api/me", headers=identity(VIEWER, email_verified=email_verified))
    assert response.status_code == 403
    assert response.json()["detail"] == "email is not verified"


@pytest.mark.asyncio
async def test_verified_email_accepted(dashboard):
    client, _, _ = dashboard
    assert (await client.get("/api/me", headers=identity(VIEWER, email_verified=True))).status_code == 200


@pytest.mark.asyncio
async def test_me_reports_admin_flag(dashboard):
    client, _, _ = dashboard
    assert (await client.get("/api/me", headers=identity(VIEWER))).json() == {"email": VIEWER, "admin": False, "bot_name": "Agent", "reviewer_name": "Reviewer"}
    assert (await client.get("/api/me", headers=identity(ADMIN))).json() == {"email": ADMIN, "admin": True, "bot_name": "Agent", "reviewer_name": "Reviewer"}


@pytest.mark.asyncio
async def test_overview_and_task_listing(dashboard, make_task, store):
    client, _, _ = dashboard
    task = make_task("investigate the sentry alert")
    store.transition(task.task_id, RECEIVED, QUEUED)
    store.transition(task.task_id, QUEUED, RUNNING)
    overview = (await client.get("/api/overview", headers=identity(VIEWER))).json()
    tasks = (await client.get("/api/tasks", headers=identity(VIEWER), params={"state": "running"})).json()
    assert overview["counts"]["running"] == 1
    assert overview["running"][0]["task_id"] == task.task_id
    assert tasks["tasks"][0]["request_text"] == "investigate the sentry alert"


@pytest.mark.asyncio
async def test_task_listing_surfaces_resolved_effort_preferring_override(dashboard, make_task, store):
    client, _, _ = dashboard
    classified = make_task("do the thing")
    store.transition(classified.task_id, RECEIVED, QUEUED, "classified", effort="high")
    overridden = make_task("do the other thing", effort_override="max")
    store.transition(overridden.task_id, RECEIVED, QUEUED, "classified", effort="low")
    tasks = {row["task_id"]: row for row in (await client.get("/api/tasks", headers=identity(VIEWER))).json()["tasks"]}
    assert tasks[classified.task_id]["effort"] == "high"
    assert tasks[overridden.task_id]["effort"] == "max"  # the slack override wins over the classifier's pick


@pytest.mark.asyncio
async def test_task_detail_includes_audit_trail(dashboard, make_task, store):
    client, _, _ = dashboard
    task = make_task()
    store.add_event(task.task_id, "milestone", {"message": "cloned repo"})
    detail = (await client.get(f"/api/tasks/{task.task_id}", headers=identity(VIEWER))).json()
    kinds = [event["kind"] for event in detail["events"]]
    assert detail["task"]["task_id"] == task.task_id
    assert kinds == ["intake", "milestone"]
    assert detail["can_cancel"] is True and detail["can_retry"] is False


@pytest.mark.asyncio
async def test_refused_task_is_terminal_and_not_retryable(dashboard, make_task, store):
    # issue #16: a refused task reads as its own state, is done (can't cancel), and isn't retryable
    client, _, _ = dashboard
    task = make_task()
    store.transition(task.task_id, RECEIVED, REFUSED, "unsupported request", error="unsupported request")
    overview = (await client.get("/api/overview", headers=identity(VIEWER))).json()
    detail = (await client.get(f"/api/tasks/{task.task_id}", headers=identity(VIEWER))).json()
    assert overview["counts"]["refused"] == 1
    assert overview["counts"]["failed"] == 0
    assert detail["task"]["state"] == "refused"
    assert detail["can_cancel"] is False and detail["can_retry"] is False


@pytest.mark.asyncio
async def test_cancel_requires_admin_and_csrf_header(dashboard, make_task, store):
    client, _, _ = dashboard
    task = make_task()
    viewer_attempt = await client.post(f"/api/tasks/{task.task_id}/cancel", headers={**identity(VIEWER), "x-harness-dashboard": "1"})
    no_csrf = await client.post(f"/api/tasks/{task.task_id}/cancel", headers=identity(ADMIN))
    allowed = await client.post(f"/api/tasks/{task.task_id}/cancel", headers=admin_headers())
    assert viewer_attempt.status_code == 403
    assert no_csrf.status_code == 403
    assert allowed.json()["status"] == "cancelled"
    assert store.get_task(task.task_id).state == "cancelled"


@pytest.mark.asyncio
async def test_retry_failed_task(dashboard, make_task, store):
    client, _, _ = dashboard
    task = make_task("do it again")
    store.transition(task.task_id, RECEIVED, FAILED, "boom")
    result = (await client.post(f"/api/tasks/{task.task_id}/retry", headers=admin_headers())).json()
    assert result["status"] == "created"
    assert store.get_task(result["new_task_id"]).parent_task_id == task.task_id


@pytest.mark.asyncio
async def test_task_detail_includes_permission_requests(dashboard, make_task, store):
    client, _, _ = dashboard
    task = make_task()
    store.request_permission(task.task_id, "tool", "mcp__jira__add_comment", "post findings")
    detail = (await client.get(f"/api/tasks/{task.task_id}", headers=identity(VIEWER))).json()
    assert [(r["kind"], r["target"], r["status"]) for r in detail["permission_requests"]] == [("tool", "mcp__jira__add_comment", "pending")]


@pytest.mark.asyncio
async def test_grant_permission_endpoint_resumes_blocked_task(dashboard, make_task, store):
    client, _, _ = dashboard
    task = make_task()
    store.transition(task.task_id, RECEIVED, QUEUED)
    store.transition(task.task_id, QUEUED, RUNNING, session_id="s1")
    store.request_permission(task.task_id, "tool", "mcp__jira__add_comment", "post findings")
    store.transition(task.task_id, RUNNING, "blocked", "needs permission")
    viewer_attempt = await client.post(f"/api/tasks/{task.task_id}/permissions", headers={**identity(VIEWER), "x-harness-dashboard": "1"}, json={"kind": "tool", "target": "mcp__jira__add_comment", "decision": "granted"})
    result = await client.post(f"/api/tasks/{task.task_id}/permissions", headers=admin_headers(), json={"kind": "tool", "target": "mcp__jira__add_comment", "decision": "granted"})
    assert viewer_attempt.status_code == 403
    assert result.json() == {"status": "granted", "state": "queued"}
    assert store.granted_permissions_for(task.task_id)["tools"] == ["mcp__jira__add_comment"]
    assert any(row["action"] == "permission_granted" for row in store.admin_events(10))


@pytest.mark.asyncio
async def test_permission_endpoint_validates_body(dashboard, make_task, store):
    client, _, _ = dashboard
    task = make_task()
    bad_kind = await client.post(f"/api/tasks/{task.task_id}/permissions", headers=admin_headers(), json={"kind": "x", "target": "t", "decision": "granted"})
    bad_decision = await client.post(f"/api/tasks/{task.task_id}/permissions", headers=admin_headers(), json={"kind": "tool", "target": "t", "decision": "maybe"})
    assert bad_kind.status_code == 400
    assert bad_decision.status_code == 400


@pytest.mark.asyncio
async def test_memory_browser(dashboard):
    client, _, _ = dashboard
    memory_dir = Path(settings.MEMORY_ROOT) / "tasks"
    memory_dir.mkdir(parents=True)
    (memory_dir / "t20260101-aaaaaaaa.md").write_text("# task t20260101-aaaaaaaa\n- state: completed")
    index = (await client.get("/api/memory", headers=identity(VIEWER))).json()
    detail = (await client.get("/api/memory/t20260101-aaaaaaaa", headers=identity(VIEWER))).json()
    assert index["records"][0]["task_id"] == "t20260101-aaaaaaaa"
    assert "completed" in detail["content"]


@pytest.mark.asyncio
async def test_usage_cards_shape(dashboard, make_task, store):
    client, config, _ = dashboard
    config.raw["usage_limits"] = {"five_hour_tokens": 1000, "weekly_tokens": 0, "fable_weekly_tokens": 500}
    task = make_task()
    store.add_usage(task.task_id, "subagent", "claude-fable-5", input_tokens=100, output_tokens=40, cost_usd=2.0)
    store.record_rate_limit("five_hour", "allowed_warning", 0.83, int(time.time()) + 3600)
    store.record_rate_limit("seven_day", "allowed_warning", 0.6, int(time.time()) - 1)
    store.record_rate_limit("seven_day_fable", "rejected", 1.0, int(time.time()) + 7200)
    usage = (await client.get("/api/usage", headers=identity(VIEWER))).json()
    assert usage["cards"]["five_hour"]["total_tokens"] == 140
    assert usage["cards"]["five_hour"]["limit_tokens"] == 1000
    assert usage["cards"]["weekly"]["limit_tokens"] is None
    assert usage["cards"]["fable"]["limit_tokens"] == 500
    assert usage["cards"]["five_hour"]["observed"]["utilization"] == 0.83
    assert usage["cards"]["weekly"]["observed"] is None
    assert usage["cards"]["fable"]["observed"]["status"] == "rejected"
    assert usage["cards"]["fable"]["totals"]["cost_usd"] == 2.0
    assert usage["timeseries"][0]["model"] == "claude-fable-5"


@pytest.mark.asyncio
async def test_config_view_masks_secrets(dashboard):
    client, config, _ = dashboard
    config.raw["models"] = {"fable": {"id": "claude-fable-5"}}
    config.raw["github"] = {"api_token": "ghp_0123456789abcdef0123456789abcdef1234"}
    view = (await client.get("/api/config", headers=identity(VIEWER))).json()
    assert view["policy"]["github"]["api_token"] == "••••••••"
    assert view["secret_presence"]["dashboard_github_token"] is True


@pytest.mark.asyncio
async def test_manage_requires_admin(dashboard):
    client, _, _ = dashboard
    assert (await client.get("/api/manage/personality", headers=identity(VIEWER))).status_code == 403


@pytest.mark.asyncio
async def test_manage_personality_preview_and_confirm_commits(dashboard, store):
    client, config, _ = dashboard
    loaded = (await client.get("/api/manage/personality", headers=identity(ADMIN))).json()
    proposed = "Red is chipper now."
    preview = (await client.post("/api/manage/personality", headers=admin_headers(), json={"content": proposed, "base_hash": loaded["base_hash"], "action": "preview"})).json()
    assert preview["saved"] is False and "+Red is chipper now." in preview["diff"]
    with patch("taskboy.dashboard.api.gitops.commit_file", new=AsyncMock(return_value={"commit_sha": "abc123", "html_url": "https://github.com/x", "unchanged": False})) as committed:
        result = (await client.post("/api/manage/personality", headers=admin_headers(), json={"content": proposed, "base_hash": loaded["base_hash"], "action": "confirm"})).json()
    assert result["saved"] is True
    assert result["commit"]["commit_sha"] == "abc123"
    assert Path(config.personality_path).read_text() == proposed
    committed.assert_awaited_once()
    outcomes = [(event["action"], event["outcome"]) for event in store.admin_events(10)]
    assert ("edit", "success") in outcomes and ("commit", "success") in outcomes


@pytest.mark.asyncio
async def test_manage_rejects_stale_and_invalid(dashboard):
    client, _, _ = dashboard
    stale = await client.post("/api/manage/personality", headers=admin_headers(), json={"content": "x", "base_hash": "wrong", "action": "confirm"})
    loaded = (await client.get("/api/manage/config", headers=identity(ADMIN))).json()
    invalid = await client.post("/api/manage/config", headers=admin_headers(), json={"content": "orchestrator: []", "base_hash": loaded["base_hash"], "action": "confirm"})
    assert stale.status_code == 409
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_manage_rejects_secret_submission(dashboard):
    client, _, _ = dashboard
    loaded = (await client.get("/api/manage/personality", headers=identity(ADMIN))).json()
    sneaky = await client.post("/api/manage/personality", headers=admin_headers(), json={"content": "token ghp_0123456789abcdef0123456789abcdef1234", "base_hash": loaded["base_hash"], "action": "confirm"})
    assert sneaky.status_code == 422


@pytest.mark.asyncio
async def test_spa_shell_missing_build_returns_hint(dashboard):
    client, _, _ = dashboard
    response = await client.get("/tasks")
    assert response.status_code == 503
    assert "ui build" in response.json()["detail"]


@pytest.mark.asyncio
async def test_viewer_can_submit_and_revise_feedback(dashboard, make_task, store):
    client, _, _ = dashboard
    task = make_task()
    headers = {**identity(VIEWER), "x-harness-dashboard": "1"}
    response = await client.post(f"/api/tasks/{task.task_id}/feedback", headers=headers, json={"rating": 2, "comment": "missed the point"})
    assert response.status_code == 200
    assert response.json()["feedback"]["rating"] == 2
    response = await client.post(f"/api/tasks/{task.task_id}/feedback", headers=headers, json={"rating": 4, "comment": "better"})
    assert response.json()["feedback"]["comment"] == "better"
    detail = (await client.get(f"/api/tasks/{task.task_id}", headers=identity(VIEWER))).json()
    assert len(detail["feedback"]) == 1
    assert detail["feedback"][0]["submitted_by"] == VIEWER
    assert detail["feedback"][0]["rating"] == 4


@pytest.mark.asyncio
async def test_feedback_requires_dashboard_header(dashboard, make_task):
    client, _, _ = dashboard
    task = make_task()
    response = await client.post(f"/api/tasks/{task.task_id}/feedback", headers=identity(VIEWER), json={"rating": 4})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_feedback_validates_rating_and_task(dashboard, make_task):
    client, _, _ = dashboard
    task = make_task()
    headers = {**identity(VIEWER), "x-harness-dashboard": "1"}
    assert (await client.post(f"/api/tasks/{task.task_id}/feedback", headers=headers, json={"rating": 9})).status_code == 400
    assert (await client.post(f"/api/tasks/{task.task_id}/feedback", headers=headers, json={"rating": "great"})).status_code == 400
    assert (await client.post("/api/tasks/t20990101-00000000/feedback", headers=headers, json={"rating": 3})).status_code == 404


@pytest.mark.asyncio
async def test_issues_list_ranks_actionable_only(dashboard, store):
    store.record_issue("a", "example-org/taskboy", "high one", "security", "d", 90)
    store.record_issue("b", "example-org/taskboy", "mid one", "organization", "d", 50)
    denied = store.record_issue("c", "example-org/taskboy", "low one", "organization", "d", 10)
    store.decide_issue(denied["id"], "denied", ADMIN)
    client, _, _ = dashboard
    response = await client.get("/api/issues", headers=identity(VIEWER))
    assert response.status_code == 200
    rows = response.json()["issues"]
    ranks = {row["dedupe_key"]: row["rank"] for row in rows}
    assert ranks["a"] == 1 and ranks["b"] == 2 and ranks["c"] is None


@pytest.mark.asyncio
async def test_only_admin_decides_issues(dashboard, store):
    row = store.record_issue("a", "example-org/taskboy", "s", "security", "d", 50)
    client, _, _ = dashboard
    forbidden = await client.post(f"/api/issues/{row['id']}/decision", headers={**identity(VIEWER), "x-harness-dashboard": "1"}, json={"decision": "approved"})
    assert forbidden.status_code == 403
    ok = await client.post(f"/api/issues/{row['id']}/decision", headers=admin_headers(), json={"decision": "approved"})
    assert ok.status_code == 200 and ok.json()["issue"]["status"] == "approved"
    assert store.get_issue(row["id"])["status"] == "approved"


@pytest.mark.asyncio
async def test_only_admin_creates_issues(dashboard, store):
    client, _, _ = dashboard
    body = {"repo": "example-org/taskboy", "summary": "add retries", "issue_type": "reliability", "details": "retry transient failures", "priority": 70}
    forbidden = await client.post("/api/issues", headers={**identity(VIEWER), "x-harness-dashboard": "1"}, json=body)
    assert forbidden.status_code == 403
    ok = await client.post("/api/issues", headers=admin_headers(), json=body)
    assert ok.status_code == 200
    created = ok.json()["issue"]
    assert created["status"] == "proposed" and created["priority"] == 70 and created["dedupe_key"].startswith("dashboard:")
    assert store.get_issue(created["id"])["summary"] == "add retries"


@pytest.mark.asyncio
async def test_create_issue_validates_fields(dashboard):
    client, _, _ = dashboard
    assert (await client.post("/api/issues", headers=admin_headers(), json={"summary": "s"})).status_code == 400
    assert (await client.post("/api/issues", headers=admin_headers(), json={"repo": "example-org/taskboy", "summary": "s", "issue_type": "t", "details": "d", "priority": 0})).status_code == 400
    assert (await client.post("/api/issues", headers=admin_headers(), json={"repo": "example-org/taskboy", "summary": "s", "issue_type": "t", "details": "d", "priority": "high"})).status_code == 400


@pytest.mark.asyncio
async def test_only_admin_updates_issue(dashboard, store):
    row = store.record_issue("a", "example-org/taskboy", "s", "security", "d", 50)
    client, _, _ = dashboard
    body = {"summary": "better summary", "details": "better details"}
    forbidden = await client.post(f"/api/issues/{row['id']}/update", headers={**identity(VIEWER), "x-harness-dashboard": "1"}, json=body)
    assert forbidden.status_code == 403
    ok = await client.post(f"/api/issues/{row['id']}/update", headers=admin_headers(), json=body)
    assert ok.status_code == 200
    updated = ok.json()["issue"]
    assert updated["summary"] == "better summary" and updated["details"] == "better details"
    assert store.get_issue(row["id"])["summary"] == "better summary"


@pytest.mark.asyncio
async def test_update_issue_validates_and_locks(dashboard, store):
    row = store.record_issue("a", "example-org/taskboy", "s", "security", "d", 50)
    client, _, _ = dashboard
    missing = await client.post(f"/api/issues/{row['id']}/update", headers=admin_headers(), json={"summary": "s"})
    assert missing.status_code == 400
    store.decide_issue(row["id"], "denied", ADMIN)
    locked = await client.post(f"/api/issues/{row['id']}/update", headers=admin_headers(), json={"summary": "s", "details": "d"})
    assert locked.status_code == 409
    missing_row = await client.post("/api/issues/9999/update", headers=admin_headers(), json={"summary": "s", "details": "d"})
    assert missing_row.status_code == 409


@pytest.mark.asyncio
async def test_run_issues_creates_system_task(dashboard, store):
    client, _, _ = dashboard
    bad = await client.post("/api/issues/run", headers=admin_headers(), json={"skill": "nope"})
    assert bad.status_code == 400
    ok = await client.post("/api/issues/run", headers=admin_headers(), json={"skill": "discoverissues", "repo": "example-org/taskboy"})
    assert ok.status_code == 200 and ok.json()["status"] == "created"
    created = store.get_task(ok.json()["task_id"])
    assert created is not None and created.request_text == "/discoverissues example-org/taskboy" and created.slack_user_id == "github"


@pytest.mark.asyncio
async def test_implement_approved_creates_no_task_when_nothing_approved(dashboard, store):
    client, _, _ = dashboard
    response = await client.post("/api/issues/run", headers=admin_headers(), json={"skill": "implementapprovedissues"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_approved_issues" and body["task_id"] is None
    assert store.list_tasks(state=RECEIVED) == []


@pytest.mark.asyncio
async def test_implement_approved_is_idempotent_across_repeated_clicks(dashboard, store):
    client, _, _ = dashboard
    for i in range(6):
        row = store.record_issue(f"k{i}", "example-org/taskboy", f"summary {i}", "organization", "d", 10 + i)
        store.decide_issue(row["id"], "approved", ADMIN)

    first = await client.post("/api/issues/run", headers=admin_headers(), json={"skill": "implementapprovedissues"})
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["status"] == "created" and first_body["task_id"]

    # the batch of 5 highest-priority rows is reserved immediately (visible as implementation_queued); the
    # lowest-priority 6th stays approved and untouched
    statuses = {row["dedupe_key"]: row["status"] for row in store.list_issues()}
    assert statuses["k0"] == "approved"
    assert all(statuses[f"k{i}"] == "implementation_queued" for i in range(1, 6))

    # clicking again while the coordinator is still active creates no new task and reports the existing one
    second = await client.post("/api/issues/run", headers=admin_headers(), json={"skill": "implementapprovedissues"})
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["status"] == "already_running" and second_body["task_id"] == first_body["task_id"]
    assert len([t for t in store.list_tasks(state=RECEIVED) if t.request_text == "/implementapprovedissues"]) == 1


@pytest.mark.asyncio
async def test_issues_list_reports_implementation_active(dashboard, store):
    client, _, _ = dashboard
    row = store.record_issue("a", "example-org/taskboy", "s", "organization", "d", 50)
    store.decide_issue(row["id"], "approved", ADMIN)

    idle = await client.get("/api/issues", headers=identity(VIEWER))
    assert idle.json()["implementation_active"] is None

    run = await client.post("/api/issues/run", headers=admin_headers(), json={"skill": "implementapprovedissues"})
    task_id = run.json()["task_id"]

    active = await client.get("/api/issues", headers=identity(VIEWER))
    assert active.json()["implementation_active"] == task_id


@pytest.mark.asyncio
async def test_issue_repo_validation_and_list_metadata(dashboard, store):
    client, _, _ = dashboard
    invalid = await client.post(
        "/api/issues",
        headers=admin_headers(),
        json={"repo": "other/repo", "summary": "s", "issue_type": "bug", "details": "d", "priority": 50},
    )
    assert invalid.status_code == 400
    row = store.record_issue("a", "example-org/taskboy", "s", "bug", "d", 50)
    store.add_issue_comment(row["id"], VIEWER, "hello")
    body = (await client.get("/api/issues", headers=identity(VIEWER))).json()
    assert body["repos"] == ["example-org/taskboy"]
    assert body["issues"][0]["repo"] == "example-org/taskboy" and body["issues"][0]["comment_count"] == 1


@pytest.mark.asyncio
async def test_issue_comment_api_author_rules_threading_and_soft_delete(dashboard, store):
    client, _, _ = dashboard
    issue = store.record_issue("a", "example-org/taskboy", "s", "bug", "d", 50)
    viewer_headers = {**identity(VIEWER), "x-harness-dashboard": "1"}
    created = await client.post(f"/api/issues/{issue['id']}/comments", headers=viewer_headers, json={"body": "**question**"})
    assert created.status_code == 200
    root = created.json()["comment"]
    reply = await client.post(f"/api/issues/{issue['id']}/comments", headers=admin_headers(), json={"body": "answer", "parent_comment_id": root["id"]})
    assert reply.status_code == 200
    too_deep = await client.post(f"/api/issues/{issue['id']}/comments", headers=viewer_headers, json={"body": "nested", "parent_comment_id": reply.json()["comment"]["id"]})
    assert too_deep.status_code == 400

    forbidden_edit = await client.post(f"/api/issues/{issue['id']}/comments/{root['id']}/update", headers=admin_headers(), json={"body": "nope"})
    forbidden_delete = await client.post(f"/api/issues/{issue['id']}/comments/{root['id']}/delete", headers=admin_headers())
    assert forbidden_edit.status_code == 403 and forbidden_edit.json()["detail"] == "only the comment author can edit it"
    assert forbidden_delete.status_code == 403 and forbidden_delete.json()["detail"] == "only the comment author can delete it"

    edited = await client.post(f"/api/issues/{issue['id']}/comments/{root['id']}/update", headers=viewer_headers, json={"body": "updated"})
    assert edited.status_code == 200 and edited.json()["comment"]["edited_at"]
    deleted = await client.post(f"/api/issues/{issue['id']}/comments/{root['id']}/delete", headers=viewer_headers)
    assert deleted.status_code == 200 and deleted.json()["comment"]["body"] == ""
    assert (await client.post(f"/api/issues/{issue['id']}/comments/{root['id']}/delete", headers=viewer_headers)).status_code == 409
    detail = (await client.get(f"/api/issues/{issue['id']}", headers=identity(VIEWER))).json()
    assert detail["comments"][0]["deleted_at"] and detail["comments"][0]["replies"][0]["body"] == "answer"


@pytest.mark.asyncio
async def test_issue_priority_refine_and_bulk_endpoints(dashboard, store):
    client, _, _ = dashboard
    editable = store.record_issue("editable", "example-org/taskboy", "editable", "bug", "d", 50)
    locked = store.record_issue("locked", "example-org/taskboy", "locked", "bug", "d", 50)
    store.decide_issue(locked["id"], "approved", ADMIN)
    store.reserve_issues("coordinator", 1)

    priority = await client.post(f"/api/issues/{editable['id']}/priority", headers=admin_headers(), json={"priority": 80})
    assert priority.status_code == 200 and store.get_issue(editable["id"])["priority"] == 80
    assert (await client.post(f"/api/issues/{locked['id']}/priority", headers=admin_headers(), json={"priority": 10})).status_code == 409

    refined = await client.post(f"/api/issues/{editable['id']}/refine", headers=admin_headers())
    assert refined.status_code == 200 and store.get_task(refined.json()["task_id"]).request_text == f"/refineissue {editable['id']}"
    duplicate = await client.post(f"/api/issues/{editable['id']}/refine", headers=admin_headers())
    assert duplicate.status_code == 409 and refined.json()["task_id"] in duplicate.json()["detail"]

    bulk = await client.post("/api/issues/bulk", headers=admin_headers(), json={"ids": [editable["id"], locked["id"], 9999], "action": "approve"})
    assert bulk.status_code == 200
    assert {row["id"]: row["status"] for row in bulk.json()["results"]} == {editable["id"]: "approved", locked["id"]: "skipped", 9999: "skipped"}
    assert (await client.post("/api/issues/bulk", headers=admin_headers(), json={"ids": [], "action": "approve"})).status_code == 400


@pytest.mark.asyncio
async def test_delete_issue_endpoint_requires_admin_and_blocks_active_statuses(dashboard, store):
    client, _, _ = dashboard
    deletable = store.record_issue("deletable", "example-org/taskboy", "s", "bug", "d", 50)
    active = store.record_issue("active", "example-org/taskboy", "s", "bug", "d", 50)
    store.decide_issue(active["id"], "approved", ADMIN)
    [reserved] = store.reserve_issues("coordinator", 1)
    store.start_issue(reserved["id"], None, "spec")

    forbidden = await client.delete(f"/api/issues/{deletable['id']}", headers=identity(VIEWER))
    assert forbidden.status_code == 403

    blocked = await client.delete(f"/api/issues/{active['id']}", headers=admin_headers())
    assert blocked.status_code == 409 and store.get_issue(active["id"]) is not None

    missing = await client.delete("/api/issues/9999", headers=admin_headers())
    assert missing.status_code == 404

    deleted = await client.delete(f"/api/issues/{deletable['id']}", headers=admin_headers())
    assert deleted.status_code == 200 and deleted.json()["status"] == "deleted"
    assert store.get_issue(deletable["id"]) is None


@pytest.mark.asyncio
async def test_issue_attachment_upload_download_limits_and_disabled(dashboard, store, monkeypatch):
    client, config, _ = dashboard
    issue = store.record_issue("a", "example-org/taskboy", "s", "bug", "d", 50)
    comment = store.add_issue_comment(issue["id"], VIEWER, "body")
    headers = {**identity(VIEWER), "x-harness-dashboard": "1"}

    disabled = await client.post(f"/api/issues/{issue['id']}/attachments", headers=headers, files={"file": ("notes.txt", b"data", "text/plain")}, data={"comment_id": str(comment["id"])})
    assert disabled.status_code == 503

    config.raw["issues"]["uploads_bucket"] = "test-bucket"
    put = patch("taskboy.dashboard.api._put_object")
    with put as put_object:
        uploaded = await client.post(f"/api/issues/{issue['id']}/attachments", headers=headers, files={"file": ("../notes.txt", b"data", "text/plain")}, data={"comment_id": str(comment["id"])})
    assert uploaded.status_code == 200
    attachment = uploaded.json()["attachment"]
    assert attachment["filename"] == "notes.txt" and attachment["size_bytes"] == 4
    assert put_object.call_args.args[0] == "test-bucket" and put_object.call_args.args[1].endswith("/notes.txt")

    before = len(store.list_issue_attachments(issue["id"]))
    with patch("taskboy.dashboard.api._put_object", side_effect=RuntimeError("s3 down")):
        failed = await client.post(f"/api/issues/{issue['id']}/attachments", headers=headers, files={"file": ("failed.txt", b"data", "text/plain")})
    assert failed.status_code == 502 and len(store.list_issue_attachments(issue["id"])) == before

    monkeypatch.setattr("taskboy.dashboard.api._presign", lambda bucket, key: "https://example.test/download")
    download = await client.get(f"/api/issues/{issue['id']}/attachments/{attachment['id']}/download", headers=identity(VIEWER))
    assert download.status_code == 307 and download.headers["location"] == "https://example.test/download"

    oversize = await client.post(f"/api/issues/{issue['id']}/attachments", headers=headers, files={"file": ("big.bin", b"x" * (5 * 1024 * 1024 + 1), "application/octet-stream")})
    assert oversize.status_code == 413


@pytest.mark.asyncio
async def test_discovery_run_requires_approved_repo(dashboard):
    client, _, _ = dashboard
    assert (await client.post("/api/issues/run", headers=admin_headers(), json={"skill": "discoverissues"})).status_code == 400
    assert (await client.post("/api/issues/run", headers=admin_headers(), json={"skill": "discoverissues", "repo": "other/repo"})).status_code == 400


@pytest.mark.asyncio
async def test_schedules_seeded_defaults_and_model_catalog(dashboard, store):
    from taskboy.scheduler import seed_default_schedules

    seed_default_schedules(store, self_repo="example-org/taskboy")
    client, _, _ = dashboard
    response = await client.get("/api/schedules", headers=identity(VIEWER))
    assert response.status_code == 200
    body = response.json()
    names = {s["request_text"] for s in body["schedules"]}
    assert "/discoverissues example-org/taskboy" in names and "/implementapprovedissues" in names
    assert isinstance(body["models"], list)


@pytest.mark.asyncio
async def test_create_validate_and_delete_schedule(dashboard, store):
    client, config, _ = dashboard
    config.raw.setdefault("models", {"sonnet": {"id": "s"}, "fable": {"id": "f"}})

    bad = await client.post("/api/schedules", headers=admin_headers(), json={"name": "x", "request_text": "/x", "kind": "daily", "at_time": "99:99"})
    assert bad.status_code == 400

    ok = await client.post("/api/schedules", headers=admin_headers(), json={"name": "Nightly", "request_text": "/discoverissues", "kind": "daily", "at_time": "00:00", "timezone": "America/Los_Angeles"})
    assert ok.status_code == 200
    sid = ok.json()["schedule"]["id"]
    assert ok.json()["schedule"]["next_run_at"]

    toggled = await client.post(f"/api/schedules/{sid}", headers=admin_headers(), json={"enabled": False})
    assert toggled.json()["schedule"]["enabled"] == 0

    deleted = await client.delete(f"/api/schedules/{sid}", headers=admin_headers())
    assert deleted.status_code == 200 and store.get_schedule(sid) is None


@pytest.mark.asyncio
async def test_schedule_model_must_be_in_catalog(dashboard, store):
    client, config, _ = dashboard
    config.raw.setdefault("models", {"sonnet": {"id": "s"}})
    bad = await client.post("/api/schedules", headers=admin_headers(), json={"name": "x", "request_text": "/x", "kind": "interval", "interval_minutes": 30, "model_alias": "nonexistent"})
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_schedule_effort_must_be_a_known_level(dashboard, store):
    client, config, _ = dashboard
    config.raw.setdefault("models", {"sonnet": {"id": "s"}})
    bad = await client.post("/api/schedules", headers=admin_headers(), json={"name": "x", "request_text": "/x", "kind": "interval", "interval_minutes": 30, "effort": "extreme"})
    assert bad.status_code == 400

    ok = await client.post("/api/schedules", headers=admin_headers(), json={"name": "x", "request_text": "/x", "kind": "interval", "interval_minutes": 30, "effort": "xhigh"})
    assert ok.status_code == 200
    assert ok.json()["schedule"]["effort"] == "xhigh"


@pytest.mark.asyncio
async def test_viewer_cannot_create_schedule(dashboard):
    client, _, _ = dashboard
    forbidden = await client.post("/api/schedules", headers={**identity(VIEWER), "x-harness-dashboard": "1"}, json={"name": "x", "request_text": "/x", "kind": "interval", "interval_minutes": 5})
    assert forbidden.status_code == 403
