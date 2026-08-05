---
name: monitor
description: Watch a GitHub PR for up to 3 hours, checking every 5 minutes for new pushes or new comments from the PR author; on any change, re-run the /review procedure on it. Invoked as `@{{agent_name}} /monitor {pr_url}`.
requires: [review]
---

# monitor

Watch a GitHub PR and re-review it every time it changes, for up to 3 hours (your session has a hard runtime limit — never plan past it).

The first GitHub PR URL in the arguments is the PR to watch. If it is missing, call `report_blocked` asking for it, then stop.

A "change" is either a **new push** (the PR head SHA moved) or a **new comment from the PR author** (issue or review comment authored by the PR author). Comments you ({{agent_name}}) leave don't count — only the author's.

An author comment counts **even when it's just "done"** with no new push. Never skip the re-review as redundant — a "done" with the code unchanged is exactly what the review's reconciliation step exists for: it confirms whether "done" is actually done, and re-flags if it isn't.

## The loop

Keep watch state in a file at `../notes/monitor.json` (your workspace's notes directory): `end_time` (epoch seconds), `head_sha`, `last_author_comment_ts`.

1. **Baseline** — fetch the PR with `mcp__github__get_pull_request` (note `head_sha`) and the latest author comment timestamp from `mcp__github__list_pr_comments`. Compute `end_time` = now + 10800 (3 hours). Write the state file. Post one `report_progress`: `monitoring {repo}#{n} — checking every 5 min for up to 3h`. Do **not** review at baseline.
2. **Tick** — wait 5 minutes using Bash sleeps in chunks no longer than 100 seconds (`sleep 100` three times); a single long sleep will hit the command timeout. Then:
   - If now ≥ `end_time`, or the PR is closed/merged: stop the loop and write the Final Report.
   - Fetch the current head SHA and latest author-comment timestamp.
   - **Changed** (head moved, or a newer author comment): run the included `/review` procedure end to end, then update the state file and post `report_progress`: `reviewed — left N comments`.
   - **Not changed**: do nothing (no progress post) and continue.

Keep per-tick work minimal — two tool calls and a comparison.

## Final Report

How long you watched, how many ticks ran, and each re-review with its comment count. Nothing else.
