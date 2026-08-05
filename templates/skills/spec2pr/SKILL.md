---
name: spec2pr
description: Implement one stored issue spec in that issue's repository, open a PR, and record the outcome. Invoked internally as `/spec2pr {issue_id}`.
model: sonnet
profile: standard
internal_tools: [issues]
---

# spec2pr

Implement one issue spec and open a pull request in the issue's target repository.

## Step 1 — Load the issue

The first argument is the issue id. Call `get_issue`; if it is missing, has no spec, or is not `in_progress`, call `report_blocked` and stop. Treat the returned `repo` as authoritative.

## Step 2 — Implement

Clone the issue's repo, work only on the assigned `agent/` branch, and never modify or merge a protected branch. Follow the stored spec and the target repository's own conventions.

- Make the smallest complete change.
- Add or update relevant tests and run the repo's validation commands.
- Open a human-review PR whose title references the issue and whose body includes Summary, Testing performed, and Known limitations.

## Step 3 — Record the outcome

Call `finish_issue` with `done` and the PR URL on success. If a PR cannot be opened, call `finish_issue` with `failed`, then `report_blocked` with the reason.

## Reply

Give the PR link or the blocking reason in 1–2 sentences.

## Final Report

Include the issue id, target repo, PR or blocking reason, validation performed, and one short line per tool failure.
