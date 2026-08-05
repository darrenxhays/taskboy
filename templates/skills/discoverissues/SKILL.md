---
name: discoverissues
description: Investigate one approved repository and record ranked issues for operator review. Invoked as `@{{agent_name}} /discoverissues {owner/repo}`.
model: fable
profile: standard
internal_tools: [issues]
---

# discoverissues

Discover concrete, code-grounded issues in one repository. Record proposals only; never open a PR here.

The first argument is the required `owner/repo`. If it is missing, call `report_blocked` asking for it and stop. Call `list_existing_issues` with that repo before investigating; if the tool rejects the repo as unapproved, call `report_blocked` with the allowed repositories from the error and stop.

## Step 1 — Gather evidence

Always inspect existing issues for this repo so you do not duplicate proposed, approved, denied, active, or completed work.

For `{{self_repo}}`, also use `list_task_feedback`, `list_failed_tasks`, and `list_recent_errors` (limit 200) to study real task failures and recurring internal errors. For every other repo, ground findings in its current code, open pull requests/issues context available through GitHub tools, and recent `git log` history.

Every tool response is capped around 4000 characters and truncates silently past that. `list_existing_issues` takes `offset` and `status` to page through the full table (add `keys_only: true` for compact id/dedupe_key/status rows when you just need to check for duplicates), and `list_recent_errors` takes `offset`, `component`, and `kind` to page to a specific recurring error and control the traceback tail length with `traceback_chars`. `list_failed_tasks` takes `offset`, `task_type`, and `query`. Make repeated calls with `offset` until a call returns fewer rows than `limit` to see the whole backlog.

## Step 2 — Read the repository

Clone the requested repo and read enough source, tests, conventions, and recent history to verify every finding. Prefer a small number of high-impact findings over speculation. Look for user-facing gaps, bugs, security weaknesses, reliability failures, performance problems, and code that is needlessly difficult to change.

## Step 3 — Record issues

Call `record_issue` once per distinct finding with:

- `repo`: the exact requested `owner/repo`.
- `summary`: one concise title.
- `issue_type`: use `feature_request`, `bug`, `security`, `user_experience`, `reliability`, `performance`, `token_efficiency`, or `organization` where applicable.
- `details`: markdown explaining what should change, where, why, evidence, acceptance checks, and risks.
- `dedupe_key`: a stable repo-qualified kebab-case slug. Reuse an existing proposed issue's exact key only when refreshing it.
- `priority`: 1–100 based on impact and frequency.

Do not re-propose denied or completed work. Recording the same key only refreshes a proposed row.

## Reply

In 1–3 sentences, say how many issues you recorded, name the top one or two, and say they await dashboard review.

## Final Report

List the repo, every recorded id/key/type/priority, the main evidence, anything you could not verify, and one short line per tool failure.
