# agent-harness

agent-harness is a self-hosted, Slack-native AI engineering agent you brand and configure as your own. It turns Slack mentions and GitHub review requests into durable, policy-controlled engineering tasks that run in isolated Claude Agent SDK sessions — under a name, personality, tool policy, and integration set that you choose at setup time.

Everything identity-shaped is configuration: the agent's name, an optional second reviewer persona, personalities, engineering conventions, skills, approved repositories, and integrations all live in `config/config.yaml` and operator-editable files — never in code.

## Quickstart (zero credentials, ~2 minutes)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .
agent-harness setup --local     # copies the example config (echo runner, no integrations)
agent-harness run               # start the service in the foreground
```

In another terminal:

```bash
agent-harness inject "say hi" --watch
```

You should see the task accepted, run, and complete. That's the whole lifecycle — intake, queue, runner, durable record — with no external accounts.

## Going live

Run the interactive setup wizard:

```bash
agent-harness setup
```

It walks each step — agent identity, Claude auth, Slack app, GitHub App(s), optional Jira/Confluence/Sentry/AWS, dashboard, conventions, personalities, and the skills picker — printing the manual admin-console instructions where needed, validating every credential live, and writing `config/config.yaml` (comment-preserving) plus a sourceable `.env`. Every step is saved as you go, so you can quit and re-run anytime; `agent-harness setup --check` re-validates everything non-interactively.

Deployment options and the full end-to-end verification checklist are in [SETUP.md](SETUP.md). The harness itself only needs a Linux box with systemd (`deploy/install.sh`); `infrastructure/` ships a reference AWS/Pulumi deployment.

## Features

- **Durable task orchestration.** SQLite acts as both the queue and operational record. Guarded state transitions, retries, session IDs, results, costs, errors, and hash-chained events survive restarts.
- **Model routing.** A classifier selects a configured model tier and execution profile per task. Simple questions can use an optional bounded quick-answer path.
- **Isolated, resumable sessions.** Each task gets its own workspace, repository context, conventions, skills, personality, tool policy, budget, turn limit, and runtime limit. Interrupted sessions are requeued and resumed.
- **Policy-controlled integrations.** GitHub, Jira, Confluence, Sentry, AWS, Slack history, and Slack DMs are exposed only when configured and allowed by the routed profile. Hooks enforce repository, branch, environment, workspace, and metadata-service boundaries.
- **Per-task permission grants.** A session can request access to an approved repository or tool outside its initial profile; operators grant or deny from the dashboard or CLI, and approved tasks resume the same session.
- **Slack-native request handling.** The agent accepts authorized mentions over Socket Mode, acknowledges work, posts concise requester-facing replies, and links to a detailed debug thread.
- **GitHub review automation.** Review-request polling uses the same durable intake path. An optional second **reviewer persona** (its own GitHub App identity) provides adversarial reviews of the main agent's pull requests.
- **Mission Control dashboard.** Task exploration, audit trails, memory and usage views, redacted configuration, live updates, task controls, and in-browser editing of config, personalities, conventions, and skills.
- **Skill template library.** Thirteen battle-tested workflows (`/review`, `/slack2pr`, `/jira2pr`, `/discoverissues`, …) ship as templates in `templates/skills/`; the setup wizard instantiates the ones you want with your org's names filled in.
- **Auditable operations.** Tool calls, routing, timing, usage, permission decisions, admin actions, and lifecycle changes are stored and redacted; audit records can ship to an S3 Object Lock bucket.

## How it works

1. Slack Socket Mode or the GitHub review poller deduplicates and authorizes a request, then creates a durable task.
2. The request is classified and routed, unless an explicit `/skill` invocation already selects its model tier and profile.
3. The concurrency-limited runner creates a task workspace, injects the relevant context and policy (including your `CONVENTIONS.md`), and starts or resumes an isolated session.
4. The requester receives a brief started message and a conversational final reply; the debug feed records factual lifecycle, prompt, progress, result, usage, and timing details.
5. Terminal records remain queryable through SQLite, the CLI, and the dashboard; retention and audit shipping are configuration.

## Operational notes

- `config/config.yaml` is your operator policy. It is gitignored in this template repo — commit it to your own private fork if you want it version-controlled (the dashboard's auto-commit feature expects that). Most changes apply on restart.
- Personalities, task-started message pools, conventions, and skills are separate operator-editable files, re-read per task and editable live from the dashboard.
- All free-text persistence and Slack delivery pass through redaction. The GitHub credential broker mints repository-scoped installation tokens per task; sessions never see private keys.
- AWS diagnostics are read-only at both the adapter and IAM layers.
- Releases are green `vX.Y.Z` tags; restarts reconcile in-flight tasks.

## Development

Python 3.12+; Node.js 22+ for the dashboard; Docker for the CI-equivalent checks.

```bash
make check          # flake8 + mypy + black/isort --check + pytest, in docker (== CI)
agent-harness run   # local service (echo or claude runner per config)
cd ui && npm ci && npm run dev   # dashboard dev server, proxies /api to :8787
```

## Repository map

```text
agent_harness/main.py             service wiring and housekeeping
agent_harness/orchestrator.py     classify, queue, dispatch, recovery, and timing
agent_harness/store.py            schema and all SQL
agent_harness/slack.py            mention intake and requester notifications
agent_harness/runner.py           Claude Agent SDK sessions and reply extraction
agent_harness/setup_wizard.py     interactive first-run setup (agent-harness setup)
agent_harness/adapters/           integration MCP servers
agent_harness/dashboard/          Mission Control API
ui/                               Mission Control React application
config/                           operator policy and editable behavior (example files)
templates/                        skill templates, conventions template, Slack app manifest
skills/                           installed skills (empty until setup)
deploy/                           systemd units, installer, and release updater
infrastructure/                   reference AWS deployment (Pulumi)
```
