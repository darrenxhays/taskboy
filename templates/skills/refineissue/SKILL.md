---
name: refineissue
description: Reconcile one issue's discussion with the current target repository and improve the issue for implementation. Invoked as `@{{agent_name}} /refineissue {issue_id}`.
model: opus
profile: standard
internal_tools: [issues]
---

# refineissue

Refine one stored issue using its entire discussion and the current target repository.

The first argument is the required issue id. If it is missing or `get_issue` cannot find it, call `report_blocked` and stop.

## Step 1 — Reconcile the discussion

Call `get_issue` and `list_issue_comments`. Read every top-level comment and reply in order. Resolve comments whose questions have been answered. Reconcile {{agent_name}}'s prior comments: edit a still-useful comment when its wording is stale, delete {{agent_name}}'s obsolete or contradictory comments, and never edit or delete another author's comment.

## Step 2 — Verify and enrich

Clone the issue's `repo` and re-check the title and markdown description against current code, tests, conventions, and recent history. Correct claims that are stale or inaccurate. Flesh out thin user-submitted issues with concrete files/areas, rationale, acceptance checks, constraints, risks, and relevant evidence.

Use the discussion to update the issue's title, description, and priority with `update_issue`. The store may reject updates after implementation has begun; if so, leave the durable issue unchanged and explain the locked state.

## Step 3 — Ask what remains

Post concise markdown comments for genuinely outstanding questions. Do not repeat answered or already-open questions. Every question should state why the answer changes scope or implementation.

## Reply

In 1–3 sentences, summarize what was reconciled, what changed, and whether any questions remain.

## Final Report

Include the issue id/repo, resolved comments, edited/deleted {{agent_name}} comments, title/description/priority changes, repository verification, outstanding questions, and one short line per tool failure.
