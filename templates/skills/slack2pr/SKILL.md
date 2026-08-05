---
name: slack2pr
description: Read a Slack thread and open a PR that implements what it asks for — straight from Slack to a pull request, no Jira ticket in between. Invoked as `@{{agent_name}} /slack2pr {slack_thread_url} {context}` — context may be empty and may name the repo. Simplest thing that works (KISS + YAGNI).
---

# slack2pr

Turn a Slack thread directly into a pull request.

- The first URL in the arguments (if any) is the Slack thread to read. When invoked **inside** the target thread, the conversation is already included in your prompt context and no URL is needed.
- The rest of the arguments are free-form context, possibly empty. Parse for intent — most importantly a **repo hint**, plus any implementation notes.

If there is neither a thread URL nor thread context in your prompt, call `report_blocked` asking for the thread, then stop.

Don't pre-check auth or tool availability — just call the tool. If it fails, note it in the Final Report.

## Keep it simple

Simplest thing that works — KISS + YAGNI:

- **Do the least** — fewest lines, files, concepts. No new abstractions, files, folders, config, or options "for later" unless the thread genuinely can't be done without them.
- **No fluff** — no comments restating the code, no docstrings on obvious methods, no defensive/speculative scaffolding.
- **Match neighboring code** and the repo's existing conventions (see the workspace `CONVENTIONS.md` when present): longhand over clever/DRY. Mirror idioms; don't invent.
- **Err hard toward under-building** — too-simple gets flagged on review, not the reverse.

## Step 1 — Read the thread

Use the thread conversation already in your prompt when present. If a Slack thread URL was given instead, read it with `mcp__slack__thread_replies` (resolve the channel id and thread timestamp from the URL — `p1234567890123456` becomes `1234567890.123456`).

Weight the requester's messages above everyone else's — they define scope and decisions; others are supporting context.

Boil the thread down to: what needs to change, and the smallest change that satisfies it.

## Step 2 — Decide the repo

The work happens in one of the repos in the `{{github_org}}` org: {{repo_list}}.

- If the **context names a repo**, use it.
- Otherwise infer from the thread (which service/domain the work clearly belongs to).
- **If you're not confident, call `report_blocked` asking which repo, then stop.** Don't guess on this.

The repo may already be cloned in your workspace; otherwise `git clone https://github.com/{{github_org}}/{repo}` (auth is handled).

## Step 3 — Implement (simplest possible)

1. Branch off **`{{pr_target_branch}}`** (the integration branch and the PR target). Name the branch per {{agent_name}}'s convention: `agent/{task_id}-short-slug`.
2. Make the **smallest change that satisfies the thread.** Re-read "keep it simple" before you write a line. Prefer editing existing files; prefer a few lines over many.
3. Follow the repo's existing patterns exactly. No new deps, no version bumps, no reformatting unrelated lines, no drive-by cleanups.
4. Add a test **only** if the change's logic warrants it, following the repo's testing conventions.

## Step 4 — Open the PR

Commit, push the branch, and open a PR **targeting `{{pr_target_branch}}`** with `mcp__github__create_pull_request`.

- **Title**: short, a terse description of the work, e.g. `fix pagination on the exports list`.
- **Body**: the required Summary, Testing performed, and Known limitations sections — each terse — plus a link to the Slack thread if you have its URL.

Opening the PR automatically triggers a review by {{reviewer_name}}, the reviewer persona (when enabled) — no action is needed, and don't wait for it.

Open it directly when the repo and scope are clear. Call `report_blocked` only if the thread is too underspecified to implement.

## Final Report

The PR URL on its own line. One short line per failure. Nothing else. ({{agent_name}} posts this back to the requesting thread automatically.)
