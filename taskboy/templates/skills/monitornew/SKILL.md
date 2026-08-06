---
name: monitornew
description: Watch a GitHub repo for the next new PR, checking every 5 minutes for up to 3 hours. When a new PR appears, stop watching and run the /reviewandmonitor procedure on it. Invoked as `@{{agent_name}} /monitornew {repo}` — repo can be `owner/name`, a bare name (assumed {{github_org}}), or a repo URL.
requires: [reviewandmonitor]
---

# monitornew

Watch a repo and, the moment a new PR is opened, review-and-monitor it. Gives up after 3 hours if none appears (your session has a hard runtime limit — never plan past it).

The first repo in the arguments is the repo to watch: `owner/name`, a bare name (→ `{{github_org}}/{name}`), or a repo URL. If it is missing, call `report_blocked` asking for it, then stop.

A "new PR" is any PR whose number is higher than the highest PR number that existed when watching started.

## The loop

Keep watch state in `../notes/monitornew.json`: `end_time` (epoch seconds), `baseline_pr_number`.

1. **Baseline** — `mcp__github__list_pull_requests` with state `all`; the highest PR number (0 if none) is `baseline_pr_number`. `end_time` = now + 10800. Write the state file. Post one `report_progress`: `watching {owner}/{repo} for new PRs — checking every 5 min for up to 3h`.
2. **Tick** — wait 5 minutes using Bash sleeps in chunks no longer than 100 seconds (`sleep 100` three times). Then:
   - If now ≥ `end_time`: stop and write the Final Report (`no new PR in the watch window`).
   - Fetch the highest PR number now. If ≤ `baseline_pr_number`: continue to the next tick.
   - Otherwise: take the **earliest** new PR (lowest number above the baseline), post `report_progress`: `found new PR #{n} — reviewing and monitoring`, and run the included `/reviewandmonitor` procedure on it end to end (its monitor phase inherits your remaining time — watch until your original `end_time`, not a fresh 3 hours).

## Final Report

Either `no new PR in the watch window`, or which PR was found plus the review-and-monitor results (comment counts, changes seen). Nothing else.
