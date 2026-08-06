---
name: reviews
description: Run the /review simplicity review across many GitHub PRs — pass a list of PR URLs, or a single repo (URL or bare name) to review every open PR in it. Invoked as `@{{agent_name}} /reviews {pr_url} {pr_url} ...` or `@{{agent_name}} /reviews {repo}`. Use /review for a single PR.
requires: [review]
---

# reviews

Run the `/review` procedure (included below) across a batch of pull requests.

## Step 1 — Figure out which PRs to review

Decide from the arguments:

- **PR URLs** — one or more GitHub PR URLs (each contains `/pull/<n>`). Review exactly those.
- **A repo** — a single argument naming a repo, not a PR. Resolve to `owner/repo` (repo URL → take owner/repo; `owner/repo` → as-is; bare name → `{{github_org}}/{name}`). Then `mcp__github__list_pull_requests` with state `open`. Drop drafts and bot-authored PRs. If more than 10 remain, review the 10 most recently updated and list the skipped ones in the Final Report.

If the arguments have neither, call `report_blocked` asking what to review, then stop.

## Step 2 — Review each PR

Work through the PRs **one at a time, in this session** — do not hand work to sub-agents. For each PR, run the included `/review` procedure end to end (read, scrutinize, reconcile, post). After each PR, post one `report_progress` line: `reviewed {repo}#{n} — N comments` (or `already simple`).

## Final Report

One line per PR: repo#number → comment count or "already simple". One short line per PR whose review failed, so it can be retried. Skipped PRs, if any. Don't restate individual findings — they're inline on each PR.
