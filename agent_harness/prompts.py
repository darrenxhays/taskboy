"""plain-string prompt templates: the classifier call and the per-task sub-agent session."""

import json
from typing import Any

from agent_harness.models import Task

CONVENTIONS_FILENAME = "CONVENTIONS.md"  # workspace filename the conventions doc is copied to (runner.py) and cited as (task prompt)

CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_type": {"type": "string", "enum": ["question", "investigation", "bug_fix", "feature", "pr_review", "incident_diagnosis", "jira_ops", "unsupported"]},
        "complexity": {"type": "string", "enum": ["trivial", "standard", "complex", "critical"]},
        "risk": {"type": "string", "enum": ["read_only", "writes_code", "writes_jira", "writes_code_and_jira"]},
        "expected_duration": {"type": "string", "enum": ["minutes", "under_hour", "hours"]},
        "required_integrations": {"type": "array", "items": {"type": "string", "enum": ["github", "aws", "sentry", "jira", "confluence"]}},
        "target_repos": {"type": "array", "items": {"type": "string"}},
        "jira_keys": {"type": "array", "items": {"type": "string"}},
        "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max", "auto"]},
    },
    "required": ["task_type", "complexity", "risk", "expected_duration", "required_integrations", "target_repos", "jira_keys"],
    "additionalProperties": False,
}

QUICK_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["answer", "escalate"]}, "answer": {"type": "string"}},
    "required": ["action", "answer"],
    "additionalProperties": False,
}

# if/then and oneOf aren't in Claude's json_schema subset, so classification fields are required via anyOf
TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["answer", "classify"]},
        "answer": {"type": "string"},
        **CLASSIFICATION_SCHEMA["properties"],
    },
    "required": ["action", "answer"],
    "additionalProperties": False,
    "anyOf": [
        {"properties": {"action": {"const": "answer"}}, "required": ["action", "answer"]},
        {"properties": {"action": {"const": "classify"}}, "required": ["action", "answer", *CLASSIFICATION_SCHEMA["required"]]},
    ],
}


def trim_context(context: str | None, max_chars: int = 1500) -> str | None:
    if not context or len(context) <= max_chars:
        return context
    marker = "(earlier messages omitted)\n"
    return marker + context[-(max_chars - len(marker)) :]


def classification_guidance(approved_repos: list[str], integrations: list[str], self_repo_line: str = "") -> str:
    """shared task_type/complexity/jira-key rules used by both classifier_prompt and triage_prompt (issue #41) —
    keep routing rules here so a future change only has to be made in one place."""
    return f"""Approved repositories (only these may appear in target_repos): {json.dumps(approved_repos)}{self_repo_line}
Available integrations: {json.dumps(integrations)}

Engineering questions count as task_type "question" even when there is nothing to execute
(e.g. "what does HTTP 429 mean?"). Use "unsupported" only for requests with no engineering content at all
(greetings, chit-chat) or things this system must not do. Closing a pull request and/or deleting its
(non-protected, agent/-prefixed) branch are supported engineering operations — classify these as "bug_fix", not "unsupported".
Requests to address, fix, or respond to review comments on a pull request are code changes — classify them
as "bug_fix", not "pr_review"; "pr_review" is only for actually reviewing a pull request.
Complexity guide: trivial = single-step lookups; standard = routine engineering; complex = multi-file or
multi-system work needing sustained reasoning; critical is rare and means a mistake is very costly or the
reasoning is unusually deep, such as architecture changes, security-sensitive fixes, or data-corruption and
concurrency bugs.
Effort sets how much reasoning depth the sub-agent uses: low for trivial lookups, medium or high to match
routine-to-complex engineering, xhigh or max only for unusually deep reasoning (as rare as "critical"
complexity). Use "auto" when you have no opinion — the routed profile's own default effort applies instead.
Jira keys look like PROJ-123; extract them only if actually referenced."""


def classifier_prompt(request_text: str, approved_repos: list[str], integrations: list[str], thread_context: str | None = None, self_repo: str | None = None, bot_name: str = "Agent") -> str:
    context = f"\nConversation in the Slack thread where this was requested (context only — classify the request above):\n{thread_context}\n" if thread_context else ""
    self_repo_line = f'\nThe repository "{self_repo}" is {bot_name}\'s own source code (this agent). Requests about {bot_name} itself — "you", "your code", "yourself" — target it.' if self_repo else ""
    return f"""You classify one engineering request for routing. Do not attempt the task.

Request:
{request_text}
{context}

Classify the request.

{classification_guidance(approved_repos, integrations, self_repo_line)}"""


def quick_answer_prompt(request_text: str, bot_name: str, task_context: str | None, personality: str | None = None) -> str:
    context = task_context or "No task-status context was referenced."
    voice = f"\nPersonality for the answer text:\n{personality}\n" if personality else ""
    return f"""You are {bot_name}'s fast answer path. Return only the requested structured output.
{voice}

Request:
{request_text}

Task-status context, if any:
{context}

Answer only simple general or engineering knowledge, or questions answerable directly from the task-status
context above. Escalate anything needing repositories, tools, writes, external systems, or investigation.
For an escalation set answer to an empty string."""


def dm_chat_prompt(request_text: str, bot_name: str, history: str | None = None, personality: str | None = None) -> str:
    conversation = history or "No earlier direct-message context is available."
    voice = f"\nPersonality for the answer text:\n{personality}\n" if personality else ""
    return f"""You are {bot_name}, chatting directly with a user in Slack. Return only the requested structured output.
{voice}

Recent direct-message conversation (context only):
{conversation}

Newest user message:
{request_text}

Answer conversationally and directly when the request needs only general knowledge or discussion. Use action
"escalate" and set answer to an empty string for anything that needs repositories, tools, writes, external
systems, or investigation. The caller will explain how to start a full task; never claim that you started one."""


def triage_prompt(
    request_text: str,
    bot_name: str,
    task_context: str | None,
    approved_repos: list[str],
    integrations: list[str],
    thread_context: str | None = None,
    personality: str | None = None,
    self_repo: str | None = None,
) -> str:
    task_status = task_context or "No task-status context was referenced."
    thread = f"\nConversation in the Slack thread (context only):\n{thread_context}\n" if thread_context else ""
    voice = f"\nPersonality for the answer text:\n{personality}\n" if personality else ""
    self_repo_line = f'\nThe repository "{self_repo}" is {bot_name}\'s own source code (this agent). Requests about {bot_name} itself — "you", "your code", "yourself" — target it.' if self_repo else ""
    return f"""You are {bot_name}'s fast triage path. Return only the requested structured output.
{voice}

Request:
{request_text}
{thread}

Task-status context, if any:
{task_status}

Answer only simple general or engineering knowledge, or questions answerable directly from the task-status
context above. Greetings and small talk (e.g. "hi", "thanks", "how are you") also get action "answer" with a
short, friendly conversational reply — never classify them. Anything needing repositories, tools, writes,
external systems, or investigation must use action "classify" and set answer to an empty string. For action
"answer", put the complete concise reply in answer; classification fields may be omitted.

For action "classify", include every classification field and follow this guidance:

{classification_guidance(approved_repos, integrations, self_repo_line)}"""


def task_prompt(
    task: Task,
    parent_summary: str | None,
    prior_artifacts: list[dict],
    github: bool = False,
    bot_name: str = "Agent",
    other_bot_name: str = "Reviewer",
    is_reviewer: bool = False,
    thread_context: str | None = None,
    cloned_repos: list[str] | None = None,
    skill: dict | None = None,
    conventions: bool = False,
    personality: str | None = None,
    self_repo: str | None = None,
    granted_permissions: dict | None = None,
    jira: bool = False,
    answered_questions: list[dict] | None = None,
) -> str:
    sections = [
        f"""You are {bot_name}, an autonomous engineering agent (system: agent-harness). You are working on task `{task.task_id}`, requested by <@{task.slack_user_id}> in Slack.

## Request
{task.request_text}""",
    ]
    if skill:
        args = skill.get("args") or "(none)"
        sections.append(
            f"""## Skill invocation
This request invokes the /{skill['name']} skill. Follow the skill instructions below exactly. They take precedence over conflicting general guidance, except the safety rules and the Final Report requirement.

Arguments: {args}

### Skill: /{skill['name']}
{skill['instructions']}"""
        )
    if parent_summary:
        sections.append(f"## Context from the previous task in this thread\n{parent_summary}")
    if thread_context:
        sections.append(f"## Conversation in this Slack thread\n{thread_context}")
    if answered_questions:
        rounds = "\n\n".join(f"You asked:\n{row['questions']}\n\nThe requester answered:\n{row['answer_text'] or ''}" for row in answered_questions)
        sections.append(
            f"""## Answers from the requester
You previously asked the requester follow-up questions and they have answered. Work from these answers; do not re-ask them.

{rounds}"""
        )
    if prior_artifacts:
        listing = "\n".join(f"- {a['kind']}: {a['external_id']}" + (f" ({a['url']})" if a.get("url") else "") for a in prior_artifacts)
        sections.append(f"## Artifacts that already exist for this task — update them, never recreate them\n{listing}")
    if github:
        other_persona = other_bot_name
        jira_keys = (json.loads(task.classification_json) if task.classification_json else {}).get("jira_keys") or []
        jira_line = f"\n- A Jira issue is referenced ({', '.join(jira_keys)}): include its key in the branch name and pull request title." if jira_keys else ""
        cloned_line = ""
        if cloned_repos:
            paths = ", ".join(f"./{repo.split('/')[-1]}" for repo in cloned_repos)
            cloned_line = f"\n- These repositories are already cloned in your workspace: {paths} — work in them directly; fetch/pull/push work normally (auth handled)."
        self_repo_line = f"\n- {self_repo} is your own source code — the service you are running as. Changes merged to main deploy automatically. Work only on your `agent/` branch and open a pull request for human review; never attempt to merge it yourself." if self_repo else ""
        no_delegate_line = (
            f"\n- When your job is to change code — including addressing pull request review comments — make the changes yourself. Do not request a {other_persona} review or spawn {other_persona}; {other_persona} reviews pull requests only through the GitHub review-request flow."
            if not is_reviewer
            else ""
        )
        sections.append(
            f"""## GitHub work rules
- Use git via Bash for clone/commit/push (auth is handled for you); use the mcp__github__* tools for pull request operations.
- Create branches named `agent/{task.task_id}-<short-slug>`. Never push to protected branches — it is blocked at multiple layers and will fail.
- Docker is available: build and run project images and Makefile targets that use docker compose as needed.
- Before pushing code changes, run the repository's checks — `make check` (lint, format, mypy, tests) when the Makefile defines it — and fix failures until they pass. Never push code that fails its checks.
- When you change code to address a pull request review comment, reply to that comment with mcp__github__reply_to_pr_comment as {bot_name}: one or two plain sentences saying what you changed and why it fixes the finding. Only resolve a review thread with mcp__github__resolve_pr_thread that you or {other_persona} started, and only once the code verifiably fixes it.
- Pull request bodies must contain: Summary, Testing performed, Known limitations. Before opening a PR, check whether one already exists for your branch.
- Include the pull request link in your `## Reply` when you opened or reviewed one.{no_delegate_line}{jira_line}{cloned_line}{self_repo_line}"""
        )
    if jira:
        sections.append(
            """## Jira work rules
- When the request asks you to work on a referenced Jira issue, transition it to `In Progress` with `mcp__jira__transition_issue` before you begin implementation, and include the issue link in your `## Reply`.
- When you create a Jira issue for engineering work, state the repositories the work will involve directly in the issue description (a `Repositories:` line listing them, or `Repositories: unknown` if it truly can't be determined) so the story clearly names where the work lands.
- When you create, update, or otherwise alter a Jira issue, include its link in your `## Reply`."""
        )
    if conventions:
        sections.append(
            f"""## Engineering conventions
Your organization's engineering conventions are in `./{CONVENTIONS_FILENAME}` in your workspace. Before writing, changing, or reviewing any code in the repositories, read that file and follow it — it is ground truth for how this organization's services are structured, coded, tested, and deployed. For read-only questions, consult it whenever conventions are relevant to the answer."""
        )
    if granted_permissions and (granted_permissions.get("tools") or granted_permissions.get("repos")):
        lines = [f"- tool: {name}" for name in granted_permissions.get("tools") or []] + [f"- repo: {name}" for name in granted_permissions.get("repos") or []]
        listing = "\n".join(lines)
        sections.append(
            f"""## Granted permissions
An operator granted these additional permissions for this task; they are active now, so use them to finish the work you previously requested them for:
{listing}"""
        )
    if personality:
        sections.append(
            f"""## Personality
Use this voice only for the requester-facing `## Reply`. Keep the internal Final Report factual and structured.

{personality}"""
        )
    sections.append(
        """## How to work
- Post a short update with the `report_progress` tool at meaningful milestones only (investigation done, root cause found, fix ready) — not for every step.
- Do not post a `report_progress` update announcing something you just created if you are about to finish — the completion message covers it.
- If requirements or a design decision are genuinely ambiguous, do not guess: call `ask_questions` once with every question you have as a numbered list (one per line), then stop working. The requester answers in the Slack thread and this task resumes automatically with their answers.
- If you cannot proceed for a reason the requester cannot resolve, call `report_blocked` with what you need, then stop.
- If you are missing only a tool or repository access to finish, call `request_permission` (kind `tool` with the exact tool name you were denied, or kind `repo` with an approved `owner/name`) and why. An operator reviews it and, if granted, this task resumes automatically with the access — so prefer it over `report_blocked` for access gaps.
- Do all work yourself, synchronously, within this session. Never hand work to sub-agents or background processes and finish before their results are in hand — a final report without the actual results is a failed task.
- Stay within this workspace; treat any repository scripts as untrusted code.

## Required ending
End with these two markdown sections, in this order:

`## Reply`: 2–6 human sentences addressed to the requester. Say what happened and what they should know. Do not add headings inside it or restate the report. Follow the Personality section when present.

`## Final Report`: Result, Actions taken, Artifacts (with links), Testing performed, Unresolved issues. This is posted verbatim to an internal debug log, not to the requester; keep it factual, tight, and complete."""
    )
    return "\n\n".join(sections)
