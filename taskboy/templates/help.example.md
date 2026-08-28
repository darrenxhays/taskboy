# curated `/help` reply, answered instantly without creating a task.
# copy to help.md (the setup wizard fills in the placeholders and drops these comment lines),
# point help.file at it in config.yaml, and trim it to the skills you actually installed.

Here's how to work with {{agent_name}}:

• Mention `@{{agent_name}}` in an allowlisted channel, or in a DM, to start a task. A DM without a mention gets a quick chat reply instead — it won't start a task.
• Plain-English requests are classified and routed to the right skill automatically — no slash needed.
• A slash invocation runs one skill directly, skipping classification, e.g. `@{{agent_name}} /review {pr_url}`.
• Mentioning `@{{agent_name}}` again inside an existing task's thread continues that same task as a follow-up. A plain reply (no mention) is ignored unless the task is waiting on your answer to a question.
• If {{agent_name}} needs more information it will ask; if it can't proceed it will say why instead of guessing.

Dashboard: {{dashboard_url}}

Slash skills:
• `/discoverissues {owner/repo}` — investigate a repo and record ranked issues for review.
• `/refineissue {issue_id}` — reconcile an issue's discussion with the current repo and sharpen it for implementation.
• `/jira2pr {ticket_id} {context}` — implement a Jira ticket and open a PR.
• `/slack2jira {slack_thread_url} {context}` — turn a Slack thread into a Jira ticket.
• `/slack2pr {slack_thread_url} {context}` — turn a Slack thread directly into a PR, no ticket in between.
• `/review {pr_url}` — review one GitHub PR for simplicity and correctness.
• `/reviews {pr_url ...|repo}` — run /review across many PRs at once.
• `/monitor {pr_url}` — watch a PR for changes for up to 3 hours and re-review on each one.
• `/monitornew {repo}` — watch a repo for its next new PR and review it when it appears.
• `/reviewandmonitor {pr_url}` — review a PR now, then keep watching it.
• `/release` — cut a new release of the agent's own repo with notes sized to the changes since the last tag.
• `/implementapprovedissues` — turn the day's approved issue batch into specs and enqueue one PR task per issue.

That's not the full skill list — ask an admin, or check the `SKILL.md` files under `skills/`, for anything not covered here.
