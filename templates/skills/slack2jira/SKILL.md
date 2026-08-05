---
name: slack2jira
description: Read a Slack thread and create a brief, concise Jira ticket on the {{jira_project}} board. Invoked as `@{{agent_name}} /slack2jira {slack_thread_url} {context}` — context describes the epic, story points, and assignee.
---

# slack2jira

Turn a Slack thread into a concise Jira ticket on the **{{jira_project}}** board.

- The first URL in the arguments (if any) is the Slack thread to read. When invoked **inside** the target thread, the conversation is already included in your prompt context and no URL is needed.
- The rest of the arguments are free-form context. Parse it for: the **epic**, **story points**, and **assignee**. Don't require a rigid syntax; read intent.

If there is neither a thread URL nor thread context in your prompt, call `report_blocked` asking for the thread, then stop.

Don't pre-check auth or tool availability — just call the tool. If it fails, note it in the Final Report.

## Step 1 — Read the Slack thread

Use the thread conversation already in your prompt when present. If a Slack thread URL was given instead, read it with `mcp__slack__thread_replies` (resolve the channel id and thread timestamp from the URL — `p1234567890123456` becomes `1234567890.123456`).

Weight the requester's messages above everyone else's — they define scope and decisions; others are supporting context.

Summarize the thread into: the problem/ask, the decision or desired outcome, and any concrete details (repro steps, links, acceptance criteria).

## Step 2 — Parse the context argument

- **Epic** — match it to an existing epic on {{jira_project}}: `mcp__jira__search_issues` with JQL like `project = {{jira_project}} AND issuetype = Epic AND summary ~ "{hint}"`. If nothing matches confidently, call `report_blocked` asking which epic, then stop.
- **Story points** — a number.
- **Assignee** — resolve to a Jira account id with `mcp__jira__search_users`. If it can't be resolved confidently, call `report_blocked`, then stop.

## Step 3 — Draft the ticket

Keep it super simple — KISS + YAGNI. Title and description matter most; make them as short as possible.

- **Title**: one very short line — just enough to name the work.
- **Description**: keep it minimal, but **always include a `Repositories:` line** naming the repo(s) the work will touch — determine them from the thread and context, matched against the approved repositories; if it genuinely cannot be determined, write `Repositories: unknown`. Put the Slack thread link at the bottom ({{agent_name}} appends its task footer automatically). Add terse step bullets only when there is a clear list.
- No prose, no fluff, no restating the obvious — but the repositories must always be clear.

## Step 4 — Create it on {{jira_project}}

Create the issue with `mcp__jira__create_issue`: project `{{jira_project}}`, summary, description, `parent_key` (the epic), `story_points`, and `assignee_account_id`.

Create directly when epic, story points, and assignee all resolved cleanly. Call `report_blocked` only when something is ambiguous or missing.

## Final Report

The ticket URL on its own line. One short line per failure. Nothing else. ({{agent_name}} posts this back to the requesting thread automatically.)
