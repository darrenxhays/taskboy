"""json api for mission control. reads are domain-wide; writes require dashboard.admin_emails."""

import asyncio
import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse

from taskboy import memory, settings, skills
from taskboy.config import KNOWN_SERVICES, ConfigError
from taskboy.dashboard import gitops
from taskboy.dashboard.auth import Viewer, require_admin, require_viewer, require_viewer_write
from taskboy.dashboard.editors import EDITABLE_KINDS, atomic_write, contains_secret_submission, content_hash, target_for, unified_diff, validate
from taskboy.dashboard.render import bounded_text, redact_bounded_value, redact_value, safe_text
from taskboy.issue_runs import start_implementation_run, start_issue_task, start_refine_task
from taskboy.models import CANCELLED, EFFORT_LEVELS, FAILED, RUNNING, STATES, TERMINAL_STATES, Task, utcnow
from taskboy.scheduler import fire_schedule_now, next_run_after
from taskboy.skills import SkillError
from taskboy.task_actions import cancel_task, decide_permission, retry_task

router = APIRouter()
TASK_ID = re.compile(r"^t[0-9]{8}-[a-f0-9]{8}$")

# skills an operator can trigger from the issues page; both run as the system identity
ISSUE_SKILLS = {"discoverissues", "implementapprovedissues"}
# ranked statuses get a 1..N importance order; decided-and-done ones drop out of the ranking
RANKED_STATUSES = ("proposed", "approved", "in_progress")
MAX_ISSUE_UPLOAD_BYTES = 5 * 1024 * 1024


def _put_object(bucket: str, key: str, body: bytes, content_type: str | None) -> None:
    """S3 upload seam, kept lazy so local/test imports never need AWS configuration."""
    import boto3

    kwargs = {"Bucket": bucket, "Key": key, "Body": body}
    if content_type:
        kwargs["ContentType"] = content_type
    boto3.client("s3").put_object(**kwargs)


def _presign(bucket: str, key: str) -> str:
    import boto3

    return boto3.client("s3").generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=300)


def _approved_repos(config) -> list[str]:
    return list(((config.raw.get("github") or {}).get("approved_repos")) or [])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _slim(store, task: Task, names: dict[str, str | None]) -> dict:
    if task.slack_user_id not in names:
        user = store.get_slack_user(task.slack_user_id)
        names[task.slack_user_id] = (user.get("real_name") or user.get("display_name")) if user else None
    return {
        "task_id": task.task_id,
        "state": task.state,
        "request_text": bounded_text(task.request_text, 140),
        "task_type": task.task_type,
        "complexity": task.complexity,
        "model_alias": task.model_alias,
        "effort": task.effort_override or task.effort,
        "persona": task.persona,
        "profile": task.profile,
        "attempt": task.attempt,
        "cost_usd": task.cost_usd,
        "num_turns": task.num_turns,
        "slack_user_id": task.slack_user_id,
        "requester": names[task.slack_user_id],
        "parent_task_id": task.parent_task_id,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }


@router.get("/api/me")
async def me(request: Request, viewer: Viewer = Depends(require_viewer)) -> dict:
    config = request.app.state.config
    return {"email": viewer.email, "admin": viewer.admin, "bot_name": config.agent_name, "reviewer_name": config.reviewer.name}


@router.get("/api/overview")
async def overview(request: Request, viewer: Viewer = Depends(require_viewer)) -> dict:
    store = request.app.state.store
    config = request.app.state.config
    names: dict[str, str | None] = {}
    counts = store.task_counts()
    return {
        "counts": {state: counts.get(state, 0) for state in STATES},
        "running": [_slim(store, task, names) for task in store.tasks_in_state(RUNNING)],
        "recent": [_slim(store, task, names) for task in store.recent_tasks(15)],
        "errors": [redact_bounded_value(row) for row in store.recent_errors(5)],
        "intake_paused": store.meta_get("intake_paused") == "1",
        "usage_5h": store.usage_totals(since_iso=_iso(_now() - timedelta(hours=5))),
        "environment": settings.ENVIRONMENT,
        "bot_name": config.agent_name,
        "reviewer_name": config.reviewer.name,
        "max_concurrency": config.max_concurrency,
        "queue_max": config.queue_max,
    }


@router.get("/api/tasks")
async def list_tasks(request: Request, state: str | None = None, q: str | None = None, page: int = 1, viewer: Viewer = Depends(require_viewer)) -> dict:
    if state and state not in STATES:
        raise HTTPException(status_code=400, detail="unknown task state")
    store = request.app.state.store
    config = request.app.state.config
    names: dict[str, str | None] = {}
    page = max(page, 1)
    tasks = store.list_tasks(state=state, query=q, limit=50, offset=(page - 1) * 50)
    return {"tasks": [_slim(store, task, names) for task in tasks], "page": page, "page_size": 50, "bot_name": config.agent_name, "reviewer_name": config.reviewer.name}


@router.get("/api/tasks/{task_id}")
async def task_detail(request: Request, task_id: str, event_page: int = 1, viewer: Viewer = Depends(require_viewer)) -> dict:
    if not TASK_ID.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    store = request.app.state.store
    config = request.app.state.config
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    event_page = max(event_page, 1)
    names: dict[str, str | None] = {}
    own_memory = memory.read_summary(settings.MEMORY_ROOT, task_id)
    parent_memory = memory.read_summary(settings.MEMORY_ROOT, task.parent_task_id) if task.parent_task_id else None
    return {
        "task": redact_bounded_value(asdict(task)),
        "bot_name": config.agent_name,
        "reviewer_name": config.reviewer.name,
        "requester": _slim(store, task, names)["requester"],
        "events": [redact_bounded_value(row) for row in store.events_for(task_id, limit=100, offset=(event_page - 1) * 100)],
        "event_page": event_page,
        "event_count": store.event_count(task_id),
        "children": [_slim(store, child, names) for child in store.children_of(task_id)],
        "errors": [redact_bounded_value(row) for row in store.errors_for(task_id)],
        "usage": [redact_bounded_value(row) for row in store.usage_for(task_id)],
        "timings": [redact_bounded_value(row) for row in store.events_for_kinds(task_id, {"timing"})],
        "artifacts": [redact_bounded_value(row) for row in store.artifacts_for(task_id)],
        "permission_requests": [redact_bounded_value(row) for row in store.permission_requests_for(task_id)],
        "questions": [redact_bounded_value(row) for row in store.questions_for(task_id)],
        "feedback": [redact_bounded_value(row) for row in store.feedback_for(task_id)],
        "own_memory": bounded_text(own_memory, 100000) if own_memory else None,
        "parent_memory": bounded_text(parent_memory, 100000) if parent_memory else None,
        "can_cancel": task.state not in TERMINAL_STATES,
        "can_retry": task.state in (FAILED, CANCELLED),
    }


@router.post("/api/tasks/{task_id}/cancel")
async def task_cancel(request: Request, task_id: str, viewer: Viewer = Depends(require_admin)) -> dict:
    if not TASK_ID.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    store = request.app.state.store
    task, status = cancel_task(store, task_id, viewer.email)
    store.add_admin_event(viewer.email, "task_cancel", task_id, status)
    orchestrator = request.app.state.orchestrator
    if orchestrator is not None:
        orchestrator.wake.set()  # same process: pick up the cancel now instead of on the next poll
    return {"status": status, "state": task.state if task else None}


@router.post("/api/tasks/{task_id}/retry")
async def task_retry(request: Request, task_id: str, viewer: Viewer = Depends(require_admin)) -> dict:
    if not TASK_ID.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    store = request.app.state.store
    retried, status = await retry_task(store, request.app.state.config, request.app.state.notifier, task_id, viewer.email)
    store.add_admin_event(viewer.email, "task_retry", task_id, status, {"new_task_id": retried.task_id if retried and status == "created" else None})
    orchestrator = request.app.state.orchestrator
    if orchestrator is not None:
        orchestrator.wake.set()
    return {"status": status, "new_task_id": retried.task_id if retried and status == "created" else None}


@router.post("/api/tasks/{task_id}/permissions")
async def task_permission(request: Request, task_id: str, viewer: Viewer = Depends(require_admin)) -> dict:
    if not TASK_ID.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    kind = str(body.get("kind") or "")
    target = str(body.get("target") or "")
    decision = str(body.get("decision") or "")
    if kind not in ("tool", "repo"):
        raise HTTPException(status_code=400, detail="kind must be tool or repo")
    if decision not in ("granted", "denied"):
        raise HTTPException(status_code=400, detail="decision must be granted or denied")
    store = request.app.state.store
    task, status = await decide_permission(store, request.app.state.notifier, task_id, kind, target, decision, viewer.email)
    store.add_admin_event(viewer.email, f"permission_{decision}", f"{task_id}:{kind}:{target}", status)
    orchestrator = request.app.state.orchestrator
    if orchestrator is not None:
        orchestrator.wake.set()  # same process: pick up a resumed task now instead of on the next poll
    return {"status": status, "state": task.state if task else None}


@router.post("/api/tasks/{task_id}/feedback")
async def task_feedback(request: Request, task_id: str, viewer: Viewer = Depends(require_viewer_write)) -> dict:
    if not TASK_ID.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    store = request.app.state.store
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    rating = body.get("rating")
    if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
        raise HTTPException(status_code=400, detail="rating must be an integer between 1 and 5")
    comment = body.get("comment") or ""
    if not isinstance(comment, str):
        raise HTTPException(status_code=400, detail="comment must be a string")
    row = store.add_feedback(task_id, viewer.email, rating, bounded_text(comment.strip(), 4000) or None)
    return {"status": "recorded", "feedback": redact_bounded_value(row)}


@router.get("/api/issues")
async def list_issues(request: Request, status: str | None = None, viewer: Viewer = Depends(require_viewer)) -> dict:
    store = request.app.state.store
    rows = store.list_issues(status=status)
    comment_counts = store.count_issue_comments_by_issue()
    refine_tasks = store.active_refine_tasks_by_issue()
    # rank is the importance order over still-actionable issues; new high-priority items shift the rest
    rank = 0
    result = []
    for row in rows:
        if row["status"] in RANKED_STATUSES:
            rank += 1
            row["rank"] = rank
        else:
            row["rank"] = None
        row["comment_count"] = comment_counts.get(row["id"], 0)
        row["refine_task_id"] = refine_tasks.get(row["id"])
        result.append(redact_bounded_value(row))
    configured = _approved_repos(request.app.state.config)
    repos = configured + sorted({row["repo"] for row in rows if row["repo"] not in configured})
    return {"issues": result, "repos": repos, "implementation_active": store.active_implementation_run()}


@router.post("/api/issues")
async def create_issue(request: Request, viewer: Viewer = Depends(require_admin)) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    repo = bounded_text(str(body.get("repo") or "").strip(), 200)
    summary = bounded_text(str(body.get("summary") or "").strip(), 300)
    issue_type = bounded_text(str(body.get("issue_type") or "").strip(), 60)
    details = bounded_text(str(body.get("details") or "").strip(), 4000)
    if not (repo and summary and issue_type and details):
        raise HTTPException(status_code=400, detail="repo, summary, issue_type, and details are required")
    if repo not in _approved_repos(request.app.state.config):
        raise HTTPException(status_code=400, detail=f"repo must be one of {sorted(_approved_repos(request.app.state.config))}")
    priority = body.get("priority", 50)
    if not isinstance(priority, int) or isinstance(priority, bool) or not 1 <= priority <= 100:
        raise HTTPException(status_code=400, detail="priority must be an integer between 1 and 100")
    store = request.app.state.store
    # a unique key per submission: operator entries never refresh an existing discovery proposal
    row = store.record_issue(f"dashboard:{uuid.uuid4().hex[:12]}", repo, summary, issue_type, details, priority, source={"created_by": viewer.email})
    store.add_admin_event(viewer.email, "issue_create", str(row["id"]), "created", {"repo": repo, "issue_type": issue_type})
    return {"status": "created", "issue": redact_bounded_value(row)}


def _thread_issue_comments(store, issue_id: int) -> list[dict]:
    roots: list[dict] = []
    by_id: dict[int, dict] = {}
    for row in store.list_issue_comments(issue_id):
        item = {**row, "resolved": bool(row["resolved"]), "replies": []}
        by_id[row["id"]] = item
        if row["parent_comment_id"] is None:
            roots.append(item)
        elif row["parent_comment_id"] in by_id:
            by_id[row["parent_comment_id"]]["replies"].append(item)
    return roots


def _public_attachment(row: dict) -> dict:
    return {key: row[key] for key in ("id", "issue_id", "comment_id", "filename", "content_type", "size_bytes", "uploaded_by", "created_at")}


@router.get("/api/issues/{issue_id}")
async def issue_detail(request: Request, issue_id: int, viewer: Viewer = Depends(require_viewer)) -> dict:
    store = request.app.state.store
    row = store.get_issue(issue_id)
    if row is None:
        raise HTTPException(status_code=404, detail="issue not found")
    row["rank"] = None
    row["comment_count"] = store.count_issue_comments(issue_id)
    row["refine_task_id"] = store.active_refine_task(issue_id)
    return {
        "issue": redact_bounded_value(row),
        "comments": redact_bounded_value(_thread_issue_comments(store, issue_id)),
        "attachments": redact_bounded_value([_public_attachment(attachment) for attachment in store.list_issue_attachments(issue_id)]),
        "refine_task_id": row["refine_task_id"],
    }


@router.post("/api/issues/{issue_id}/decision")
async def issue_decision(request: Request, issue_id: int, viewer: Viewer = Depends(require_admin)) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    decision = str(body.get("decision") or "")
    if decision not in ("approved", "denied", "proposed"):
        raise HTTPException(status_code=400, detail="decision must be approved, denied, or proposed")
    store = request.app.state.store
    row = store.decide_issue(issue_id, decision, viewer.email)
    if row is None:
        raise HTTPException(status_code=409, detail="issue not found or already being implemented")
    store.add_admin_event(viewer.email, "issue_decision", str(issue_id), decision)
    return {"status": "recorded", "issue": redact_bounded_value(row)}


@router.post("/api/issues/{issue_id}/update")
async def update_issue(request: Request, issue_id: int, viewer: Viewer = Depends(require_admin)) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    summary = bounded_text(str(body.get("summary") or "").strip(), 300)
    details = bounded_text(str(body.get("details") or "").strip(), 4000)
    if not (summary and details):
        raise HTTPException(status_code=400, detail="summary and details are required")
    store = request.app.state.store
    row = store.update_issue(issue_id, summary, details)
    if row is None:
        raise HTTPException(status_code=409, detail="issue not found or not editable")
    store.add_admin_event(viewer.email, "issue_update", str(issue_id), "updated")
    return {"status": "updated", "issue": redact_bounded_value(row)}


@router.post("/api/issues/{issue_id}/priority")
async def update_issue_priority(request: Request, issue_id: int, viewer: Viewer = Depends(require_admin)) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    priority = body.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 1 <= priority <= 100:
        raise HTTPException(status_code=400, detail="priority must be an integer between 1 and 100")
    row = request.app.state.store.set_issue_priority(issue_id, priority)
    if row is None:
        raise HTTPException(status_code=409, detail="issue not found or priority is locked")
    request.app.state.store.add_admin_event(viewer.email, "issue_priority", str(issue_id), "updated", {"priority": priority})
    return {"status": "updated", "issue": redact_bounded_value(row)}


@router.post("/api/issues/{issue_id}/refine")
async def refine_issue(request: Request, issue_id: int, viewer: Viewer = Depends(require_admin)) -> dict:
    store = request.app.state.store
    if store.get_issue(issue_id) is None:
        raise HTTPException(status_code=404, detail="issue not found")
    task, status, active_task_id = await start_refine_task(store, request.app.state.config, request.app.state.notifier, issue_id, source=f"dashboard:{viewer.email}")
    task_id = task.task_id if task else active_task_id
    if status == "already_running":
        raise HTTPException(status_code=409, detail=f"refine task already running: {task_id}")
    store.add_admin_event(viewer.email, "issue_refine", str(issue_id), status, {"task_id": task_id})
    orchestrator = request.app.state.orchestrator
    if orchestrator is not None:
        orchestrator.wake.set()
    return {"status": status, "task_id": task_id}


@router.delete("/api/issues/{issue_id}")
async def delete_issue(request: Request, issue_id: int, viewer: Viewer = Depends(require_admin)) -> dict:
    store = request.app.state.store
    existing = store.get_issue(issue_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="issue not found")
    if existing["status"] in ("in_progress", "in_review"):
        raise HTTPException(status_code=409, detail="cannot delete an issue that is in progress or in review")
    if store.delete_issue(issue_id) is None:
        raise HTTPException(status_code=409, detail="issue not found or not deletable")
    store.add_admin_event(viewer.email, "issue_delete", str(issue_id), "deleted", {"repo": existing["repo"]})
    return {"status": "deleted"}


@router.post("/api/issues/bulk")
async def bulk_issues(request: Request, viewer: Viewer = Depends(require_admin)) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    ids = body.get("ids")
    action = str(body.get("action") or "")
    if not isinstance(ids, list) or not ids or not all(isinstance(value, int) and not isinstance(value, bool) for value in ids):
        raise HTTPException(status_code=400, detail="ids must be a non-empty list of integers")
    if action not in ("approve", "deny", "refine", "delete"):
        raise HTTPException(status_code=400, detail="action must be approve, deny, refine, or delete")
    store = request.app.state.store
    results = []
    for issue_id in dict.fromkeys(ids):
        if action in ("approve", "deny"):
            row = store.decide_issue(issue_id, "approved" if action == "approve" else "denied", viewer.email)
            results.append({"id": issue_id, "status": ("approved" if action == "approve" else "denied") if row else "skipped"})
        elif action == "delete":
            # delete_issue already refuses in_progress/in_review rows
            results.append({"id": issue_id, "status": "deleted" if store.delete_issue(issue_id) else "skipped"})
        elif store.get_issue(issue_id) is None:
            results.append({"id": issue_id, "status": "not_found"})
        else:
            task, status, active_task_id = await start_refine_task(store, request.app.state.config, request.app.state.notifier, issue_id, source=f"dashboard-bulk:{viewer.email}")
            results.append({"id": issue_id, "status": status, "task_id": task.task_id if task else active_task_id})
    store.add_admin_event(viewer.email, "issue_bulk", action, "completed", {"ids": ids, "results": results})
    orchestrator = request.app.state.orchestrator
    if orchestrator is not None and action == "refine":
        orchestrator.wake.set()
    return {"results": results}


@router.post("/api/issues/{issue_id}/comments")
async def add_issue_comment(request: Request, issue_id: int, viewer: Viewer = Depends(require_viewer_write)) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    comment_body = bounded_text(str(body.get("body") or "").strip(), 4000)
    parent_comment_id = body.get("parent_comment_id")
    if not comment_body:
        raise HTTPException(status_code=400, detail="body is required")
    if parent_comment_id is not None and (not isinstance(parent_comment_id, int) or isinstance(parent_comment_id, bool)):
        raise HTTPException(status_code=400, detail="parent_comment_id must be an integer or null")
    store = request.app.state.store
    try:
        row = store.add_issue_comment(issue_id, viewer.email, comment_body, parent_comment_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="issue not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    store.add_admin_event(viewer.email, "issue_comment_create", str(row["id"]), "created", {"issue_id": issue_id, "parent_comment_id": parent_comment_id})
    return {"status": "created", "comment": redact_bounded_value(row)}


def _editable_comment(store, issue_id: int, comment_id: int, viewer: Viewer, action: str) -> dict:
    comment = store.get_issue_comment(comment_id)
    if comment is None or comment["issue_id"] != issue_id:
        raise HTTPException(status_code=404, detail="comment not found")
    if comment["author"].lower() != viewer.email.lower():
        raise HTTPException(status_code=403, detail=f"only the comment author can {action} it")
    if comment["deleted_at"] is not None:
        raise HTTPException(status_code=409, detail="comment is already deleted")
    return comment


@router.post("/api/issues/{issue_id}/comments/{comment_id}/update")
async def update_issue_comment(request: Request, issue_id: int, comment_id: int, viewer: Viewer = Depends(require_viewer_write)) -> dict:
    store = request.app.state.store
    _editable_comment(store, issue_id, comment_id, viewer, "edit")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    comment_body = bounded_text(str(body.get("body") or "").strip(), 4000)
    if not comment_body:
        raise HTTPException(status_code=400, detail="body is required")
    row = store.update_issue_comment(comment_id, comment_body)
    store.add_admin_event(viewer.email, "issue_comment_update", str(comment_id), "updated", {"issue_id": issue_id})
    return {"status": "updated", "comment": redact_bounded_value(row)}


@router.post("/api/issues/{issue_id}/comments/{comment_id}/delete")
async def delete_issue_comment(request: Request, issue_id: int, comment_id: int, viewer: Viewer = Depends(require_viewer_write)) -> dict:
    store = request.app.state.store
    _editable_comment(store, issue_id, comment_id, viewer, "delete")
    row = store.delete_issue_comment(comment_id)
    store.add_admin_event(viewer.email, "issue_comment_delete", str(comment_id), "deleted", {"issue_id": issue_id})
    return {"status": "deleted", "comment": redact_bounded_value(row)}


@router.post("/api/issues/{issue_id}/attachments")
async def upload_issue_attachment(
    request: Request,
    issue_id: int,
    file: UploadFile = File(...),
    comment_id: int | None = Form(None),
    viewer: Viewer = Depends(require_viewer_write),
) -> dict:
    store = request.app.state.store
    if store.get_issue(issue_id) is None:
        raise HTTPException(status_code=404, detail="issue not found")
    if comment_id is not None:
        comment = store.get_issue_comment(comment_id)
        if comment is None or comment["issue_id"] != issue_id:
            raise HTTPException(status_code=400, detail="comment_id must belong to this issue")
    bucket = str(((request.app.state.config.raw.get("issues") or {}).get("uploads_bucket")) or "")
    if not bucket:
        raise HTTPException(status_code=503, detail="issue uploads are not configured")
    original_name = Path(file.filename or "").name.strip()
    if not original_name:
        raise HTTPException(status_code=400, detail="filename is required")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", original_name).strip("._") or "attachment"
    chunks = []
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_ISSUE_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="attachment exceeds the 5 MB limit")
        chunks.append(chunk)
    if size == 0:
        raise HTTPException(status_code=400, detail="attachment must not be empty")
    key = f"issues/{issue_id}/{uuid.uuid4().hex}/{safe_name}"
    try:
        await asyncio.to_thread(_put_object, bucket, key, b"".join(chunks), file.content_type)
    except Exception:
        raise HTTPException(status_code=502, detail="issue attachment upload failed")
    row = store.add_issue_attachment(issue_id, comment_id, original_name, file.content_type, size, key, viewer.email)
    store.add_admin_event(viewer.email, "issue_attachment_upload", str(row["id"]), "uploaded", {"issue_id": issue_id, "comment_id": comment_id, "size_bytes": size})
    return {"status": "uploaded", "attachment": redact_bounded_value(_public_attachment(row))}


@router.get("/api/issues/{issue_id}/attachments/{attachment_id}/download")
async def download_issue_attachment(request: Request, issue_id: int, attachment_id: int, viewer: Viewer = Depends(require_viewer)):
    attachment = request.app.state.store.get_issue_attachment(attachment_id)
    if attachment is None or attachment["issue_id"] != issue_id:
        raise HTTPException(status_code=404, detail="attachment not found")
    bucket = str(((request.app.state.config.raw.get("issues") or {}).get("uploads_bucket")) or "")
    if not bucket:
        raise HTTPException(status_code=503, detail="issue uploads are not configured")
    try:
        url = await asyncio.to_thread(_presign, bucket, attachment["s3_key"])
    except Exception:
        raise HTTPException(status_code=502, detail="issue attachment download failed")
    return RedirectResponse(url, status_code=307)


@router.post("/api/issues/run")
async def issues_run(request: Request, viewer: Viewer = Depends(require_admin)) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    skill = str(body.get("skill") or "")
    if skill not in ISSUE_SKILLS:
        raise HTTPException(status_code=400, detail=f"skill must be one of {sorted(ISSUE_SKILLS)}")
    store = request.app.state.store
    if skill == "implementapprovedissues":
        task, status, active_task_id = await start_implementation_run(store, request.app.state.config, request.app.state.notifier, thread_key=f"dashboard:{viewer.email}@{utcnow()}")
        task_id = task.task_id if task else active_task_id
    else:
        repo = str(body.get("repo") or "").strip()
        if not repo or repo not in _approved_repos(request.app.state.config):
            raise HTTPException(status_code=400, detail=f"repo must be one of {sorted(_approved_repos(request.app.state.config))}")
        task, status = await start_issue_task(store, request.app.state.config, request.app.state.notifier, skill, repo, source=f"dashboard:{viewer.email}")
        task_id = task.task_id if task else None
    store.add_admin_event(viewer.email, "issue_run", skill, status, {"task_id": task_id, "repo": body.get("repo")})
    orchestrator = request.app.state.orchestrator
    if orchestrator is not None:
        orchestrator.wake.set()
    return {"status": status, "task_id": task_id}


def _catalog_models(config) -> list[str]:
    return sorted((config.raw.get("models") or {}).keys())


def _validate_schedule(body: dict, models: set[str], now: datetime) -> dict:
    """normalize and validate a schedule create/update body, computing its first next_run_at (UTC)."""
    name = str(body.get("name") or "").strip()
    request_text = str(body.get("request_text") or "").strip()
    if not name or not request_text:
        raise HTTPException(status_code=400, detail="name and request_text are required")
    kind = str(body.get("kind") or "")
    if kind not in ("once", "interval", "daily"):
        raise HTTPException(status_code=400, detail="kind must be once, interval, or daily")
    model_alias = str(body.get("model_alias") or "").strip() or None
    if model_alias is not None and model_alias not in models:
        raise HTTPException(status_code=400, detail=f"model must be blank or one of {sorted(models)}")
    effort = str(body.get("effort") or "").strip() or None
    if effort is not None and effort not in EFFORT_LEVELS:
        raise HTTPException(status_code=400, detail=f"effort must be blank or one of {EFFORT_LEVELS}")
    tzname = str(body.get("timezone") or "").strip() or None
    if tzname is not None:
        try:
            ZoneInfo(tzname)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(status_code=400, detail=f"timezone {tzname!r} is not a known IANA timezone")
    max_runs = body.get("max_runs")
    if max_runs is not None and (not isinstance(max_runs, int) or isinstance(max_runs, bool) or max_runs < 1):
        raise HTTPException(status_code=400, detail="max_runs must be a positive integer or null")
    interval_minutes = at_time = run_at = None
    if kind == "interval":
        interval_minutes = body.get("interval_minutes")
        if not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool) or interval_minutes < 1:
            raise HTTPException(status_code=400, detail="interval_minutes must be a positive integer")
    elif kind == "daily":
        at_time = str(body.get("at_time") or "")
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at_time) is None:
            raise HTTPException(status_code=400, detail="at_time must be HH:MM in 24-hour time")
    else:  # once
        try:
            local = datetime.fromisoformat(str(body.get("run_at") or "").strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="run_at must be an ISO date-time like 2026-08-01T14:00")
        tzinfo = ZoneInfo(tzname) if tzname else timezone.utc
        if local.tzinfo is None:
            local = local.replace(tzinfo=tzinfo)
        run_at = local.astimezone(timezone.utc).isoformat(timespec="seconds")
        max_runs = 1  # a one-off runs exactly once
    nxt = next_run_after(kind, interval_minutes=interval_minutes, at_time=at_time, run_at=run_at, tzname=tzname, after=now)
    if nxt is None:
        raise HTTPException(status_code=400, detail="that schedule has no future run time — a one-off must be in the future")
    return {
        "name": bounded_text(name, 200),
        "request_text": bounded_text(request_text, 4000),
        "model_alias": model_alias,
        "effort": effort,
        "kind": kind,
        "interval_minutes": interval_minutes,
        "at_time": at_time,
        "run_at": run_at,
        "timezone": tzname,
        "max_runs": max_runs,
        "next_run_at": nxt.isoformat(timespec="seconds"),
    }


@router.get("/api/schedules")
async def list_schedules(request: Request, viewer: Viewer = Depends(require_viewer)) -> dict:
    store = request.app.state.store
    return {"schedules": [redact_bounded_value(row) for row in store.list_schedules()], "models": _catalog_models(request.app.state.config)}


@router.post("/api/schedules")
async def create_schedule(request: Request, viewer: Viewer = Depends(require_admin)) -> dict:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    fields = _validate_schedule(body, set(_catalog_models(request.app.state.config)), _now())
    row = request.app.state.store.create_schedule(created_by=viewer.email, **fields)
    request.app.state.store.add_admin_event(viewer.email, "schedule_create", str(row["id"]), "created", {"kind": fields["kind"]})
    return {"schedule": redact_bounded_value(row)}


@router.post("/api/schedules/{schedule_id}")
async def update_schedule(request: Request, schedule_id: int, viewer: Viewer = Depends(require_admin)) -> dict:
    store = request.app.state.store
    if store.get_schedule(schedule_id) is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    if set(body) <= {"enabled"}:  # lightweight enable/disable toggle
        enabled = bool(body.get("enabled"))
        row = store.update_schedule(schedule_id, enabled=1 if enabled else 0)
        store.add_admin_event(viewer.email, "schedule_update", str(schedule_id), "enabled" if enabled else "disabled")
        return {"schedule": redact_bounded_value(row)}
    fields = _validate_schedule(body, set(_catalog_models(request.app.state.config)), _now())
    row = store.update_schedule(schedule_id, **fields)
    store.add_admin_event(viewer.email, "schedule_update", str(schedule_id), "updated", {"kind": fields["kind"]})
    return {"schedule": redact_bounded_value(row)}


@router.post("/api/schedules/{schedule_id}/run")
async def run_schedule(request: Request, schedule_id: int, viewer: Viewer = Depends(require_admin)) -> dict:
    store = request.app.state.store
    schedule = store.get_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    task, status = await fire_schedule_now(store, request.app.state.config, request.app.state.notifier, schedule)
    store.add_admin_event(viewer.email, "schedule_run", str(schedule_id), status, {"task_id": task.task_id if task else None})
    orchestrator = request.app.state.orchestrator
    if orchestrator is not None:
        orchestrator.wake.set()
    return {"status": status, "task_id": task.task_id if task else None}


@router.delete("/api/schedules/{schedule_id}")
async def delete_schedule(request: Request, schedule_id: int, viewer: Viewer = Depends(require_admin)) -> dict:
    store = request.app.state.store
    if not store.delete_schedule(schedule_id):
        raise HTTPException(status_code=404, detail="schedule not found")
    store.add_admin_event(viewer.email, "schedule_delete", str(schedule_id), "deleted")
    return {"status": "deleted"}


@router.get("/api/memory")
async def memory_index(request: Request, q: str = "", viewer: Viewer = Depends(require_viewer)) -> dict:
    root = Path(settings.MEMORY_ROOT) / "tasks"
    records = []
    if root.is_dir():
        for path in sorted(root.glob("*.md"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                content = path.read_text()
                if q.lower() not in path.stem.lower() and q.lower() not in content.lower():
                    continue
                info = path.stat()
                records.append({"task_id": path.stem, "modified": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec="seconds"), "size": info.st_size, "preview": bounded_text(content, 200)})
            except (OSError, UnicodeError):
                continue
            if len(records) >= 200:
                break
    return {"records": records}


@router.get("/api/memory/{task_id}")
async def memory_detail(request: Request, task_id: str, viewer: Viewer = Depends(require_viewer)) -> dict:
    if not TASK_ID.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="memory record not found")
    content = memory.read_summary(settings.MEMORY_ROOT, task_id)
    if content is None:
        raise HTTPException(status_code=404, detail="memory record not found")
    task = request.app.state.store.get_task(task_id)
    return {"task_id": task_id, "content": bounded_text(content, 100000), "state": task.state if task else None}


def _usage_card(store, label: str, since: datetime | None = None, model: str | None = None, limit_tokens: int | None = None, observed: dict | None = None) -> dict:
    since_iso = _iso(since) if since else None
    totals = store.usage_totals(since_iso=since_iso, model=model)
    total_tokens = totals["input_tokens"] + totals["output_tokens"] + totals["cache_read_tokens"] + totals["cache_write_tokens"]
    return {"label": label, "since": since_iso, "totals": totals, "by_model": store.usage_by_model(since_iso=since_iso, model=model), "total_tokens": total_tokens, "limit_tokens": limit_tokens, "observed": observed}


@router.get("/api/usage")
async def usage(request: Request, viewer: Viewer = Depends(require_viewer)) -> dict:
    store = request.app.state.store
    now = _now()
    fable_model = str((((request.app.state.config.raw.get("models") or {}).get("fable") or {}).get("id") or "claude-fable-5"))
    limits = request.app.state.config.raw.get("usage_limits") or {}
    windows = store.rate_limit_windows()
    now_epoch = int(now.timestamp())

    def window_for(card: str) -> dict | None:
        if card == "five_hour":
            candidates = (row for row in windows if row["rate_limit_type"] == "five_hour")
        elif card == "weekly":
            candidates = (row for row in windows if row["rate_limit_type"] == "seven_day")
        else:
            candidates = (row for row in windows if "fable" in row["rate_limit_type"].lower())
        row = next(candidates, None)
        if row is None or row["utilization"] is None or row["resets_at"] is None or row["resets_at"] <= now_epoch:
            return None
        return {key: row[key] for key in ("utilization", "resets_at", "status", "observed_at")}

    def configured_limit(key: str) -> int | None:
        try:
            value = int(limits.get(key) or 0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    return {
        "generated_at": _iso(now),
        "fable_model": fable_model,
        "cards": {
            "five_hour": _usage_card(store, "Rolling 5 hours", now - timedelta(hours=5), limit_tokens=configured_limit("five_hour_tokens"), observed=window_for("five_hour")),
            "weekly": _usage_card(store, "Rolling 7 days", now - timedelta(days=7), limit_tokens=configured_limit("weekly_tokens"), observed=window_for("weekly")),
            "fable": _usage_card(store, "Fable — rolling 7 days", now - timedelta(days=7), model=fable_model, limit_tokens=configured_limit("fable_weekly_tokens"), observed=window_for("fable")),
        },
        "timeseries": store.usage_timeseries(_iso(now - timedelta(days=7))),
    }


@router.get("/api/config")
async def config_view(request: Request, viewer: Viewer = Depends(require_viewer)) -> dict:
    config = request.app.state.config
    app_secrets = request.app.state.secrets
    runtime = {
        "environment": settings.ENVIRONMENT,
        "database_path": settings.DB_PATH,
        "config_path": settings.CONFIG_PATH,
        "workspaces_root": settings.WORKSPACES_ROOT,
        "repositories_root": settings.REPOS_ROOT,
        "memory_root": settings.MEMORY_ROOT,
        "skills_root": settings.SKILLS_ROOT,
        "aws_region": settings.REGION,
        "secrets_bundle_name": settings.SECRETS_NAME,
    }
    secret_presence = {name: bool(getattr(app_secrets, name)) for name in app_secrets.__dataclass_fields__} if app_secrets else {}
    services_dir = Path(settings.CONFIG_PATH).parent / "services"
    return {
        "runtime": redact_value(runtime),
        "policy": redact_value(config.raw),
        "dashboard": redact_value(asdict(config.dashboard)),
        "secret_presence": secret_presence,
        "skills": skills.available(settings.SKILLS_ROOT),
        # editable = the section lives in its own services/<name>.yaml file (legacy inline sections are edited via config.yaml)
        "services": {name: {"enabled": config.service_enabled(name), "editable": (services_dir / f"{name}.yaml").is_file()} for name in KNOWN_SERVICES},
    }


@router.get("/api/admin-events")
async def admin_events(request: Request, viewer: Viewer = Depends(require_admin)) -> dict:
    return {"events": [redact_bounded_value(row) for row in request.app.state.store.admin_events(100)]}


@router.get("/api/manage/{kind}")
async def manage_read(request: Request, kind: str, name: str | None = None, viewer: Viewer = Depends(require_admin)) -> dict:
    if kind not in EDITABLE_KINDS:
        raise HTTPException(status_code=404, detail="editable target not found")
    target, repo_path, title = target_for(request.app.state.config, kind, name)
    content = target.read_text() if target.exists() else ""
    if contains_secret_submission(kind, content):
        request.app.state.store.add_admin_event(viewer.email, "edit", str(target), "blocked", {"reason": "existing file contains secret-looking values"})
        raise HTTPException(status_code=409, detail="this file contains secret-looking values and cannot be edited here")
    return {"kind": kind, "name": name, "title": title, "content": content, "base_hash": content_hash(content), "repo_path": repo_path, "auto_commit": request.app.state.config.dashboard.auto_commit_enabled}


@router.post("/api/manage/{kind}")
async def manage_write(request: Request, kind: str, viewer: Viewer = Depends(require_admin)) -> dict:
    if kind not in EDITABLE_KINDS:
        raise HTTPException(status_code=404, detail="editable target not found")
    store = request.app.state.store
    config = request.app.state.config
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="json body required")
    name = body.get("name") or None
    content = str(body.get("content") or "")
    base_hash = str(body.get("base_hash") or "")
    action = str(body.get("action") or "preview")
    if len(content) > 2_000_000:
        raise HTTPException(status_code=413, detail="submitted content is too large")
    target, repo_path, title = target_for(config, kind, name)
    previous = target.read_text() if target.exists() else ""
    if content_hash(previous) != base_hash:
        store.add_admin_event(viewer.email, "edit", str(target), "stale", {"previous_hash": content_hash(previous), "submitted_base_hash": base_hash})
        raise HTTPException(status_code=409, detail="this file changed since you loaded it — reload before saving")
    try:
        validate(kind, name, content, target)
    except (ConfigError, SkillError, ValueError, OSError, UnicodeError) as e:
        store.add_admin_event(viewer.email, "edit", str(target), "rejected", {"reason": safe_text(str(e))})
        raise HTTPException(status_code=422, detail=safe_text(str(e)))
    diff = unified_diff(previous, content, title)
    if action != "confirm":
        return {"saved": False, "diff": safe_text(diff), "title": title}
    try:
        atomic_write(target, content)
    except OSError as e:
        store.add_admin_event(viewer.email, "edit", str(target), "failed", {"reason": type(e).__name__})
        raise HTTPException(status_code=500, detail="the validated file could not be written")
    store.add_admin_event(viewer.email, "edit", str(target), "success", {"previous_hash": content_hash(previous), "new_hash": content_hash(content), "diff": safe_text(diff)})
    commit_result = None
    commit_error = None
    token = getattr(request.app.state.secrets, "dashboard_github_token", "") if request.app.state.secrets else ""
    if config.dashboard.auto_commit_enabled and token:
        try:
            commit_result = await gitops.commit_file(
                token, config.dashboard.commit_repo, config.dashboard.commit_branch, repo_path, content, f"mission control: {viewer.email} edited {repo_path}", viewer.email, committer_name=config.dashboard.committer_name, committer_email=config.dashboard.committer_email
            )
            store.add_admin_event(viewer.email, "commit", repo_path, "success" if not commit_result.get("unchanged") else "unchanged", {"sha": commit_result.get("commit_sha", "")})
        except (gitops.GitOpsError, OSError) as e:
            commit_error = safe_text(str(e))
            store.add_admin_event(viewer.email, "commit", repo_path, "failed", {"reason": commit_error})
    elif config.dashboard.auto_commit_enabled:
        commit_error = "auto-commit is configured but dashboard_github_token is not set"
    if kind in ("config", "service"):
        message = "Saved. Configuration changes apply after a service restart."
    else:
        message = "Saved and live immediately."
    return {"saved": True, "message": message, "diff": safe_text(diff), "commit": commit_result, "commit_error": commit_error}


@router.get("/api/stream")
async def stream(request: Request, viewer: Viewer = Depends(require_viewer)) -> StreamingResponse:
    store = request.app.state.store

    async def events():
        after = request.query_params.get("after", "")
        last_id = int(after) if after.isdigit() else store.latest_event_id()
        last_counts: dict = {}
        idle = 0.0
        yield f"event: hello\ndata: {json.dumps({'last_event_id': last_id})}\n\n"
        while True:
            if await request.is_disconnected():
                return
            rows = store.events_after(last_id, limit=500)
            for row in rows:
                yield f"event: task_event\ndata: {json.dumps(redact_bounded_value(row))}\n\n"
            if rows:
                last_id = rows[-1]["id"]
            counts = store.task_counts()
            if counts != last_counts:
                last_counts = counts
                yield f"event: counts\ndata: {json.dumps(counts)}\n\n"
            idle += 2
            if idle >= 20:
                idle = 0
                yield ": keep-alive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
