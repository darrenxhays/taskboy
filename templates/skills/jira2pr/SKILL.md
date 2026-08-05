---
name: jira2pr
description: Read a {{jira_project}} Jira ticket (and its linked Slack thread, if any) and open a PR that implements it. Invoked as `@{{agent_name}} /jira2pr {ticket_id} {context}` — ticket_id is the bare number (e.g. `514`); context may name the repo. Solutions must be the simplest thing that works (KISS + YAGNI).
---

# jira2pr

Turn a **{{jira_project}}** ticket into a pull request.

- The first argument is the ticket id, a **bare number** like `514`. Prepend `{{jira_project}}-` to get the key (`{{jira_project}}-514`). Tolerate the full key or its lowercase form too.
- The rest of the arguments are free-form context. Parse for intent — most importantly a **repo hint**, plus any implementation notes.

If the ticket id is missing, call `report_blocked` asking for it, then stop.

Don't pre-check auth or tool availability — just call the tool. If it fails, note it in the Final Report.

## Keep it simple

Simplest thing that works — KISS + YAGNI:

- **Do the least** — fewest lines, files, concepts. No new abstractions, files, folders, config, or options "for later" unless the ticket genuinely can't be done without them.
- **No fluff** — no comments restating the code, no docstrings on obvious methods, no defensive/speculative scaffolding.
- **Match neighboring code** and the repo's existing conventions (see the workspace `CONVENTIONS.md` when present): longhand over clever/DRY. Mirror idioms; don't invent.
- **Err hard toward under-building** — too-simple gets flagged on review, not the reverse.

## Step 1 — Read the ticket

Fetch `{{jira_project}}-{id}` with `mcp__jira__get_issue`. Pull: summary, description, status, assignee, and any linked URLs. Use `mcp__jira__search_issues` if you need the parent epic for context.

If the description contains a **Slack thread link**, read that thread with `mcp__slack__thread_replies` (resolve the channel id and thread timestamp from the URL — `p1234567890123456` becomes `1234567890.123456`). This only works for channels {{agent_name}} is allowed in; if it fails, note it and continue with the ticket alone.

Boil it down to: what needs to change, and the smallest change that satisfies it.

## Step 2 — Decide the repo

The work happens in one of the repos in the `{{github_org}}` org: {{repo_list}}.

- If the **context names a repo**, use it.
- Otherwise infer from the ticket (which service/domain the work clearly belongs to).
- **If you're not confident, call `report_blocked` asking which repo, then stop.** Don't guess on this.

The repo may already be cloned in your workspace; otherwise `git clone https://github.com/{{github_org}}/{repo}` (auth is handled).

## Step 3 — Implement (simplest possible)

1. Branch off **`{{pr_target_branch}}`** (the integration branch and the PR target). Name the branch per {{agent_name}}'s convention with the ticket key: `agent/{task_id}-{lowercase_ticket_key}-short-slug`.
2. Make the **smallest change that satisfies the ticket.** Re-read "keep it simple" before you write a line. Prefer editing existing files; prefer a few lines over many.
3. Follow the repo's existing patterns exactly. No new deps, no version bumps, no reformatting unrelated lines, no drive-by cleanups.
4. Add a test **only** if the ticket's logic warrants it, following the repo's testing conventions.

## Step 4 — Open the PR

Commit, push the branch, and open a PR **targeting `{{pr_target_branch}}`** with `mcp__github__create_pull_request`.

- **Title**: short — the ticket key + a terse description, e.g. `{{jira_project}}-514: fix pagination on the exports list`.
- **Body**: the required Summary, Testing performed, and Known limitations sections — each terse — plus the ticket link (`https://{{jira_site}}/browse/{{jira_project}}-{id}`).

Opening the PR automatically triggers a review by {{reviewer_name}}, the reviewer persona (when enabled) — no action is needed, and don't wait for it.

Then attach the PR to the ticket with `mcp__jira__link_pr`.

Open it directly when the repo and scope are clear. Call `report_blocked` only if the ticket is too underspecified to implement.

## Final Report

The PR URL on its own line. One short line per failure. Nothing else.
