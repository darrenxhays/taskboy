# Manual setup (editing the config files directly)

This is the file-editing alternative to the interactive wizard described in [SETUP.md](SETUP.md). Everything `agent-harness setup` does lands in exactly two files — `config/config.yaml` and `.env` — so you can produce the same result with a text editor. The admin-console work (creating the Slack app, GitHub Apps, Jira service account, Sentry integration, AWS roles) is identical either way; this guide points at the SETUP.md appendices for those runbooks rather than repeating them.

Complete [SETUP.md section 1 (Prerequisites)](SETUP.md#1-prerequisites) first. Replace every `<placeholder>`; never commit a real token, private key, or password.

Even if you configure by hand, keep the wizard's non-interactive checker in your toolbox — it validates a hand-edited setup exactly as it validates a wizard-produced one:

```bash
agent-harness setup --check          # validate config.yaml + every reachable credential (exit 64 on config errors)
agent-harness setup --check --no-validate   # config + which secrets are set, no network calls
```

## 1. The two files

### `config/config.yaml` — non-secret operator policy

Start from the fully commented example:

```bash
cp config/config.example.yaml config/config.yaml
```

`config.yaml` is gitignored in this template repo; commit it to your private fork if you want it version-controlled. On a deployed host the file lives at `/etc/agent-harness/config.yaml` (the path comes from `AGENT_HARNESS_CONFIG_PATH`). Config changes apply on service restart; restarts are safe — running tasks are requeued and resumed.

### `.env` — local secrets

Credentials never go in `config.yaml`. Locally they live in a sourceable `.env` at the repo root (gitignored). Create it by hand in this exact shape — one `export KEY='value'` per secret, single-quoted:

```bash
# local secrets for agent-harness
export CLAUDE_CODE_OAUTH_TOKEN='<token>'
export SLACK_BOT_TOKEN='xoxb-<...>'
export SLACK_APP_TOKEN='xapp-<...>'
export GITHUB_APP_ID='<numeric-app-id>'
export GITHUB_INSTALLATION_ID='<numeric-installation-id>'
export GITHUB_APP_PRIVATE_KEY='-----BEGIN RSA PRIVATE KEY-----
<key body, real newlines preserved inside the single quotes>
-----END RSA PRIVATE KEY-----'
export REVIEWER_GITHUB_APP_ID='<numeric-app-id>'
export REVIEWER_GITHUB_INSTALLATION_ID='<numeric-installation-id>'
export REVIEWER_GITHUB_APP_PRIVATE_KEY='<pem contents, as above>'
export JIRA_EMAIL='<agent-service-email>'
export JIRA_API_TOKEN='<token>'
export SENTRY_TOKEN='<token>'
export DASHBOARD_GITHUB_TOKEN='<fine-grained-pat>'
```

Rules the service and checker rely on:

- **Single quotes, `export` keyword.** `agent-harness setup --check` parses this exact format (a literal `'` inside a value is written `'\''`). Multi-line values are fine inside the single quotes.
- **Private keys are the PEM *contents*, not a path.** Paste the whole downloaded `.pem` file body into the variable, newlines intact.
- **Omit what you skip.** Any integration whose variables are absent simply stays disabled; leave the line out rather than exporting an empty value.
- Lock it down and load it before running: `chmod 600 .env`, then `source .env && agent-harness run`.

These are the same variable names `agent_harness/secrets.py` reads, so the identical lines work in `/etc/agent-harness/env` on a deployed host (section 3a of SETUP.md), and their lowercased forms are the AWS Secrets Manager bundle keys (Appendix F).

## 2. Section-by-section

Each subsection below matches one wizard step: the `config.yaml` keys to set, the `.env` variables to add, and the SETUP.md appendix that produces the credentials. The example file's comments document every remaining key (orchestrator tuning, models catalog, routing rules, execution profiles, retention) — the defaults are sensible and rarely need editing on a first install.

### Identity

```yaml
agent:
  name: <your-agent-name>          # display name in slack, prompts, PR/Jira text, the dashboard
github:
  commit_name: <your-agent-name>   # git author on commits; empty defaults to agent.name
  commit_email: <verified-email>   # GitHub derives the commit avatar from this — verify it on a GitHub account
cli_update:
  tzname: <IANA-timezone>          # e.g. America/Los_Angeles; used for scheduled maintenance windows
```

### Claude auth

Run `claude setup-token` on this machine and put the result in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`. Then switch the runner on:

```yaml
orchestrator:
  runner: claude   # ships as `echo` (dev, no model calls); `claude` runs real sub-agent sessions
```

Leave `runner: echo` if you want to try the harness without credentials first.

### Slack (optional — without it, tasks arrive via `agent-harness inject`)

Create the app from the manifest and collect the tokens per **[Appendix A](SETUP.md#appendix-a-slack-app-via-manifest)**. `.env`: `SLACK_BOT_TOKEN` (`xoxb-…`), `SLACK_APP_TOKEN` (`xapp-…`).

```yaml
slack:
  team_id: T<...>                 # non-empty enables slack intake
  allowed_channels: [C<...>]      # empty = any channel the bot is invited to
  debug_channel: C<...>           # optional private debug feed; empty disables

roles:
  admin:
    members: [U<your-slack-user-id>, cli]   # keep `cli` so `agent-harness inject` keeps admin rights
```

Role rules the loader enforces: a Slack user id may appear in only one role; at most one role may use the wildcard member `"*"`; every `allowed_profiles` entry must name a profile defined in the `profiles` section.

### GitHub App (main agent)

Create and install the App per **[Appendix B](SETUP.md#appendix-b-main-github-app)**. `.env`: `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY` (PEM contents).

```yaml
github:
  approved_repos: [<org>/<repo-a>, <org>/<repo-b>]
  self_repo: ""                       # the agent's own source repo; MUST also appear in approved_repos
  protected_branch_patterns: [main, develop]
  review_requests:
    enabled: false                    # true = poll approved repos for PRs where the agent is a requested reviewer
    poll_interval_seconds: 60
    notify_channel: ""                # optional slack channel id for github-triggered review updates
```

The loader rejects a `self_repo` that is not listed in `approved_repos`.

### Reviewer persona (optional)

Needs its **own** GitHub App (GitHub forbids a `REQUEST_CHANGES` review on your own PR) — create it per **[Appendix C](SETUP.md#appendix-c-reviewer-github-app)**. `.env`: `REVIEWER_GITHUB_APP_ID`, `REVIEWER_GITHUB_INSTALLATION_ID`, `REVIEWER_GITHUB_APP_PRIVATE_KEY`.

```yaml
reviewer:
  enabled: true
  name: <reviewer-name>
  commit_name: <reviewer-name>
  commit_email: <verified-email>   # REQUIRED when enabled — the loader refuses an empty value
  review_agent_prs: true           # auto-review every PR the main agent opens
```

### Jira + Confluence (optional)

Service account, project role, and API token per **[Appendix D](SETUP.md#appendix-d-jira--confluence-service-account)**. `.env`: `JIRA_EMAIL`, `JIRA_API_TOKEN`.

```yaml
jira:
  site: https://<your-org>.atlassian.net
  projects: [<KEY>]                # project keys the agent may touch
  issue_types: [Story, Bug, Task]
  story_points_field: ""           # e.g. customfield_10016; empty = story points not set

confluence:                        # same service account; only if Confluence reads are wanted
  site: https://<your-org>.atlassian.net
  spaces: []                       # empty = any space the account can read
```

### Sentry (optional)

Read-only internal integration per **[Appendix E](SETUP.md#appendix-e-sentry-internal-integration)**. `.env`: `SENTRY_TOKEN`.

```yaml
sentry:
  organization: <org-slug>
  projects: [<project-slug>]       # empty = all readable
```

### AWS diagnostics (optional)

Diagnostics roles per **[Appendix F](SETUP.md#appendix-f-aws-pulumi-and-the-secrets-bundle)**.

```yaml
aws:
  allowed_services: [logs, cloudwatch, lambda]   # services the read-only adapter may call
  allowed_regions: [us-east-1]
  diagnostics_role_arns:                          # per-environment roles the orchestrator assumes
    staging: arn:aws:iam::<account-id>:role/<prefix>-staging-diagnostics
```

Omit `diagnostics_role_arns` entirely for local-dev default credentials.

### Dashboard (optional)

```yaml
dashboard:
  enabled: true
  bind: 127.0.0.1                    # 0.0.0.0 on a deployed host (the security group only admits the ALB)
  port: 8787
  allowed_email_domain: example.com  # REQUIRED when enabled; bare domain, read-only access for its accounts
  admin_emails: [<you>@example.com]  # who may edit, cancel, retry
  public_url: ""                     # e.g. https://agent.example.com; empty disables dashboard links in slack
  dev_user_email: dev@example.com    # local-only stand-in identity when no ALB header is present
  auto_commit:                       # commit dashboard edits back to your fork; empty repo disables
    repo: ""                         # <org>/<your-fork>
    branch: main
    committer_email: ""              # REQUIRED when repo is set
```

When `auto_commit.repo` is set, add `DASHBOARD_GITHUB_TOKEN` to `.env` — a fine-grained PAT with Contents read/write on that one repository only.

### Conventions & personality

The wizard just copies templates; do the same:

```bash
cp templates/conventions.md config/conventions.md                      # fill in your org's rules
cp config/personality_agent.example.md config/personality_agent.md
cp config/personality_reviewer.example.md config/personality_reviewer.md   # only if the reviewer is enabled
```

```yaml
conventions:
  file: conventions.md               # relative to config.yaml; injected into repo tasks as CONVENTIONS.md
agent:
  personality_file: personality_agent.md
reviewer:
  personality_file: personality_reviewer.md
```

Paths are relative to `config.yaml`'s directory, and every configured file must exist at startup — the loader fails fast on a missing one. Leave a key `""` to skip it.

### Skills

`skills/` ships empty; the workflow templates live in `templates/skills/`. To install one by hand, copy the template directory into `skills/` and replace **every** `{{variable}}` in the copied `SKILL.md` — the loader does no substitution at runtime and rejects leftovers:

```bash
cp -r templates/skills/review skills/review
# then edit skills/review/SKILL.md and fill in {{agent_name}}, {{repo_list}}, ...
```

The variable table, frontmatter reference, and the `requires` rule (any skill listed in another's `requires` must be installed too) are in [`templates/skills/README.md`](templates/skills/README.md). Integration constraints: `jira2pr` and `slack2jira` need Jira configured; `release` and `discoverissues` need `github.self_repo`.

### Secrets destination

Nothing extra locally — `.env` is it. For a deployed host, either copy the same `export` lines into `/etc/agent-harness/env` (root-owned, mode 640) or build the AWS Secrets Manager JSON bundle by hand; the bundle's 16 keys are the lowercased `.env` names plus the SSO trio, all tabulated in **[Appendix F](SETUP.md#appendix-f-aws-pulumi-and-the-secrets-bundle)**:

```bash
aws secretsmanager create-secret \
  --name AGENT_HARNESS_SECRETS_STAGING \
  --secret-string file://<bundle.json>     # build bundle.json outside shell history; delete it after pushing
```

Preserve PEM newlines as JSON string escapes and validate the file without printing it: `python3.12 -c "import json; json.load(open('<bundle.json>'))"`.

## 3. Hand-editing gotchas

The loader validates `config.yaml` on every start (and `setup --check` exits 64 on failure). The rules that most often bite manual edits:

- `github.self_repo` must also be listed in `github.approved_repos`.
- `reviewer.commit_email` is required whenever `reviewer.enabled: true`.
- `dashboard.allowed_email_domain` is required (bare domain, no scheme) whenever `dashboard.enabled: true`; `dashboard.auto_commit.committer_email` is required whenever `auto_commit.repo` is set.
- Every configured `personality_file`, `conventions.file`, and `task_started_messages_file` must exist on disk, relative to `config.yaml`.
- Roles: one role per user, at most one wildcard `"*"` role, `allowed_profiles` entries must exist in `profiles`, and any `roles.<name>.repos` list may only contain repos from `github.approved_repos`.
- `orchestrator.runner` is exactly `echo` or `claude`; `cli_update.at_time` is 24-hour `HH:MM`; `cli_update.tzname` must be a real IANA timezone.
- Legacy keys are rejected with pointers: `slack.bot_name` moved to `agent.name`, and per-user Slack settings moved to the top-level `roles` section.

## 4. Validate and run

```bash
agent-harness setup --check     # every configured integration must report [ok]
source .env && agent-harness run
```

From here the paths converge with the wizard flow: deploy per [SETUP.md section 3](SETUP.md#3-deploying) and verify end-to-end per [section 5](SETUP.md#5-end-to-end-verification).
