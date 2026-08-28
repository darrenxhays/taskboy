"""issues triggers: run the discovery/implementation skills on demand as the system identity.

used by the dashboard's "Run discovery"/"Implement approved" buttons. recurring runs go through the
general task scheduler (taskboy/scheduler.py), which seeds the daily discovery/implementation jobs.
"""

import re
import uuid
from datetime import datetime, timedelta, timezone

from taskboy.config import Config
from taskboy.models import BLOCKED, FAILED, Task, utcnow
from taskboy.orchestrator import accept_task
from taskboy.store import Store, TransitionRaced

# system-owned tasks reuse the "github" identity, which the default config maps to the read/standard system role
SYSTEM_TEAM = "github"
SYSTEM_USER = "github"

GITHUB_API = "https://api.github.com"
# not end-anchored so a /files, ?query, or #fragment suffix still parses (#87)
PR_URL_RE = re.compile(r"^https://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")

# a batch this size keeps a run's PRs reviewable (also enforced by the coordinator only seeing its reserved rows)
IMPLEMENTATION_BATCH_SIZE = 5
STALLED_COORDINATOR_MAX_AGE_HOURS = 24


async def start_issue_task(store: Store, config: Config, notifier, skill: str, args: str = "", *, source: str):
    """enqueue one issues skill task as the system identity. returns (task, status) from accept_task."""
    channel = str(((config.raw.get("issues") or {}).get("notify_channel")) or "")
    key = f"issues:{skill}:{source}@{utcnow()}"
    text = f"/{skill}"
    if args.strip():
        text += f" {args.strip()}"
    return await accept_task(
        store,
        config,
        notifier,
        team_id=SYSTEM_TEAM,
        channel_id=channel,
        thread_ts=key,
        message_ts=key,
        user_id=SYSTEM_USER,
        text=text,
    )


async def start_refine_task(store: Store, config: Config, notifier, issue_id: int, *, source: str) -> tuple[Task | None, str, str | None]:
    """start one refine task per issue, returning the active task id when one already exists."""
    existing = store.active_refine_task(issue_id)
    if existing is not None:
        return None, "already_running", existing
    task, status = await start_issue_task(store, config, notifier, "refineissue", str(issue_id), source=source)
    return task, status, None


async def start_implementation_run(
    store: Store,
    config: Config,
    notifier,
    *,
    thread_key: str,
    request_text: str = "/implementapprovedissues",
    user_id: str = SYSTEM_USER,
    channel_id: str | None = None,
    model_override: str | None = None,
    effort_override: str | None = None,
    schedule_name: str | None = None,
    issue_ids: list[int] | None = None,
    actor: str | None = None,
) -> tuple[Task | None, str, str | None]:
    """with `issue_ids` omitted, reserves up to IMPLEMENTATION_BATCH_SIZE approved issues top-priority-first;
    with `issue_ids` given, approves any of those still `proposed` (as `actor`) and reserves exactly that set.
    Returns (task, status, active_task_id): status is "already_running", "no_approved_issues", or whatever
    accept_task reports ("created" or a refusal like "paused"/"queue_full"/"duplicate")."""
    existing = store.active_implementation_run()
    if existing is not None:
        return None, "already_running", existing
    if store.has_pending_implementation_reservation():
        return None, "already_running", None

    marker = f"pending:{uuid.uuid4().hex[:12]}"
    auto_approved_ids: list[int] = []
    if issue_ids is not None:
        for issue_id in issue_ids:
            issue = store.get_issue(issue_id)
            if issue is not None and issue["status"] == "proposed":
                store.decide_issue(issue_id, "approved", actor or user_id)
                auto_approved_ids.append(issue_id)
        reserved = store.reserve_issues_by_id(marker, issue_ids)
    else:
        reserved = store.reserve_issues(marker, IMPLEMENTATION_BATCH_SIZE)
    if not reserved:
        return None, "no_approved_issues", None
    # only the ones we actually reserved under this marker count — a row that missed reservation
    # (e.g. denied by someone else in between) was never ours to roll back
    reserved_ids = {row["id"] for row in reserved}
    auto_approved_ids = [issue_id for issue_id in auto_approved_ids if issue_id in reserved_ids]

    channel = channel_id if channel_id is not None else str(((config.raw.get("issues") or {}).get("notify_channel")) or "")
    assigned = False
    try:
        task, status = await accept_task(
            store,
            config,
            notifier,
            team_id=SYSTEM_TEAM,
            channel_id=channel,
            thread_ts=thread_key,
            message_ts=thread_key,
            user_id=user_id,
            text=request_text,
            model_override=model_override,
            effort_override=effort_override,
            schedule_name=schedule_name,
        )
        if status != "created" or task is None:
            return task, status, None
        store.assign_reservation(marker, task.task_id)
        assigned = True
        return task, "created", None
    finally:
        # covers accept_task raising (incl. cancellation) and the plain refusal case above — either way, don't
        # strand the reservation under a marker no coordinator will ever claim
        if not assigned:
            store.release_reserved_issues(marker)
            # release_reserved_issues put these back to approved; anything we auto-approved from proposed
            # goes back to proposed instead, so a failed rocket click doesn't leave it eligible for the next batch run
            for issue_id in auto_approved_ids:
                store.decide_issue(issue_id, "proposed", actor or user_id)


def fail_stalled_implementation_run(store: Store) -> str | None:
    """fail a coordinator left blocked for over a day and release its reserved issues."""
    task_id = store.blocked_implementation_run()
    if task_id is None:
        return None
    task = store.get_task(task_id)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STALLED_COORDINATOR_MAX_AGE_HOURS)
    if task is None or task.state != BLOCKED or datetime.fromisoformat(task.updated_at) >= cutoff:
        return None
    try:
        store.transition(
            task_id,
            BLOCKED,
            FAILED,
            "recovery: implementation coordinator stalled",
            error="implementation coordinator stalled in blocked for over 24h",
            finished_at=utcnow(),
        )
    except TransitionRaced:
        return None
    store.add_event(task_id, "recovery", {"action": "failed_stalled_coordinator"})
    return task_id


async def _get_pr(repo: str, number: int, token: str) -> dict:
    """the github http seam for sync_in_review — patched in unit tests."""
    import aiohttp

    from taskboy.adapters.github_api import GitHubStatusError
    from taskboy.redact import redactor

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{GITHUB_API}/repos/{repo}/pulls/{number}", headers=headers) as response:
            if response.status >= 300:
                body = redactor.redact(await response.text())[:300]
                raise GitHubStatusError(response.status, f"github api GET pulls/{number} failed: {response.status} — {body}")
            return await response.json()


async def sync_in_review(store: Store, broker) -> int:
    """resolve issues whose PR has since merged (-> done) or closed unmerged (-> failed); still-open PRs
    are left alone. called hourly from housekeeping_loop. one row's error is recorded and skipped, not raised,
    so it never blocks the rest of the sweep."""
    updated = 0
    tokens: dict[str, str] = {}
    for row in store.list_issues(status="in_review"):
        pr_url = row.get("pr_url")
        if not pr_url:
            continue
        match = PR_URL_RE.match(pr_url)
        if not match:
            store.add_error("issue_runs", "BadPrUrl", f"issue #{row['id']} has an unparseable pr_url: {pr_url}", context={"issue_id": row["id"]})
            continue
        repo, number = match.group(1), int(match.group(2))
        try:
            if repo not in tokens:
                token, _ = await broker.read_token([repo], permissions={"pull_requests": "read", "metadata": "read"})
                tokens[repo] = token
            pr = await _get_pr(repo, number, tokens[repo])
        except Exception as e:
            store.add_error("issue_runs", type(e).__name__, str(e), context={"issue_id": row["id"], "repository": repo})
            continue
        if pr.get("merged"):
            store.finish_issue(row["id"], "done", pr_url)
            updated += 1
        elif pr.get("state") == "closed":
            store.finish_issue(row["id"], "failed", pr_url)
            updated += 1
    return updated
