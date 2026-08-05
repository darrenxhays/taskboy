---
name: implementapprovedissues
description: Turn the reserved batch of approved multi-repo issues into grounded specs and enqueue one PR task per issue. Invoked as `@{{agent_name}} /implementapprovedissues`.
model: fable
profile: standard
internal_tools: [issues, enqueue]
---

# implementapprovedissues

Turn the current reserved issue batch into implementation specs, then hand each spec to `/spec2pr` with `enqueue_spec_pr`. Do not open PRs yourself.

## Step 1 — Load the batch

Call `list_accepted_issues`. It returns up to five issues reserved for this coordinator, including each issue's target `repo`. If there are none, call `report_blocked` saying there are no approved issues and stop.

Clone each distinct target repo as needed. A batch may span repositories; never assume an issue targets `{{self_repo}}`.

## Step 2 — Write repo-specific specs

For every issue in priority order, verify its description against the current target repo. Skip work that is already complete or no longer applies and explain why in the final report.

Each markdown spec must explicitly name the target repo and include:

- Goal and rationale tied to the issue.
- Concrete files/areas and changes.
- Tests and validation commands.
- Existing repository conventions and compatibility constraints.
- The smallest acceptable scope and explicit non-goals.

## Step 3 — Enqueue PR work

Call `enqueue_spec_pr` once per verified issue with its id and full spec. The child `/spec2pr` task reads the issue's repo, implements the spec there, opens a PR, and records the result. Do not wait for child tasks.

## Reply

In 1–3 sentences, report how many specs were enqueued and which issue ids/repos they target.

## Final Report

List each issue and child handoff, skipped issues and reasons, and one short line per tool failure.
