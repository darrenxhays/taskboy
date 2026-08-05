# Setting up agent-harness

This runbook takes a new installation from empty accounts to a running, branded agent: a Slack-driven main agent, an optional second GitHub reviewer persona, and the SSO-protected Mission Control dashboard. The names, personalities, and integrations are all yours to choose — nothing identity-shaped is hardcoded.

The fast path is the interactive wizard (`agent-harness setup`, section 2). It prints the manual admin-console instructions inline and validates every credential live; the appendices in section 4 hold the full step-by-step runbooks for the parts that happen in each vendor's admin UI. Prefer to edit the config files yourself instead of answering prompts? Follow [MANUAL_SETUP.md](MANUAL_SETUP.md) — it produces the same `config/config.yaml` and `.env` by hand and reuses the same appendices. Replace every `<placeholder>` locally; never put a real token, private key, or password in this repository.

## 1. Prerequisites

- [ ] Install Git and confirm `git --version` succeeds.
- [ ] Install Python 3.12 and confirm `python3.12 --version` succeeds.
- [ ] Clone this repository (or your private fork) and install it:

  ```bash
  git clone https://github.com/<your-org>/agent-harness.git
  cd agent-harness
  python3.12 -m venv .venv && source .venv/bin/activate
  pip install -e .
  ```

- [ ] A Claude subscription (Max) or a Claude Console account, so `claude setup-token` can mint the long-lived token the sub-agent sessions authenticate with. (The bundled `claude` CLI ships inside the `claude-agent-sdk` package — no separate install.)

Per-integration admin access — confirm each before starting the corresponding wizard step:

- [ ] **Slack** (optional — without it, tasks arrive via `agent-harness inject`): admin rights in the target workspace, enough to create an app from a manifest and install it.
- [ ] **GitHub** (optional, but needed for any repository work): organization owner rights, enough to create GitHub Apps and install them on selected repositories.
- [ ] **Atlassian** (optional): site admin rights, enough to invite a service-account user and edit project permission schemes.
- [ ] **Sentry** (optional): organization admin rights, enough to create an internal integration.
- [ ] **AWS** (optional — only for read-only cloud diagnostics and/or the reference deployment): admin access to each target account, plus the Pulumi CLI and AWS CLI v2 if you use `infrastructure/`.
- [ ] Create a secure password-manager record for every credential generated below; do not use shell history, tickets, or Slack as credential storage.

Want to see it work before creating any accounts? Run the two-minute credential-free demo:

```bash
agent-harness setup --local     # copies the example config; runner stays "echo"
agent-harness run               # then, in another terminal:
agent-harness inject "say hi" --watch
```

## 2. Run the setup wizard

```bash
agent-harness setup
```

The wizard walks the steps below in order. After **every** step it writes `config/config.yaml` (comment-preserving — your annotations survive) and a sourceable `.env` holding local secrets (`chmod 600`; load it with `source .env`). You can quit with ctrl-c at any point and re-run later: existing values appear as defaults and nothing is lost. Useful flags:

- `agent-harness setup --step <name>` — re-run a single step (`slack`, `github`, `skills`, …).
- `agent-harness setup --check` — non-interactive: validate `config.yaml` plus every reachable credential and exit (exit code 64 on config errors).
- `agent-harness setup --no-validate` — skip the live network validation.
- `agent-harness setup --local` — the zero-credential demo described above.

### identity

Asks for the main agent's display name (`agent.name`), its git commit author name and email (GitHub derives the commit avatar from the email — verify it on a GitHub account to give the agent a custom avatar), and your IANA timezone for scheduled maintenance windows.

### claude

Run `claude setup-token` on this machine and paste the resulting token; it is stored as `CLAUDE_CODE_OAUTH_TOKEN` in `.env` and validated live. If the token checks out, the wizard offers to switch `orchestrator.runner` from `echo` to `claude`.

### slack

Creates the Slack connection: paste the app manifest from `templates/slack_app_manifest.yaml`, then supply the bot token (`xoxb-…`), the Socket Mode app-level token (`xapp-…`), the workspace team id (`T…`), allowed channel ids, an optional private debug channel, and the Slack user ids that get the `admin` role. Every token and channel is verified live. The manual app-creation runbook is **Appendix A**.

### github

The main agent's GitHub App: App ID, installation id, and the private key (given as a path to the downloaded `.pem` — never pasted). Also asks for `github.approved_repos`, an optional `github.self_repo` (the agent's own source repo, auto-added to the approved list), protected branch patterns, and whether to poll for PRs where the agent is a requested reviewer. App creation is **Appendix B**.

### reviewer

The optional second, GitHub-only persona that adversarially reviews the main agent's PRs. It needs its **own** GitHub App because GitHub forbids a `REQUEST_CHANGES` review on your own pull request. Asks for the reviewer's name, commit identity, its App credentials, and whether it auto-reviews every PR the main agent opens (`reviewer.review_agent_prs`). App creation is **Appendix C**.

### jira

Optional Jira (and Confluence) via one dedicated service account: site URL, service-account email, API token, approved project keys (each verified live), and an optional story-points custom field. The service-account and permission-scheme runbook is **Appendix D**.

### sentry

Optional read-only Sentry: organization slug, internal-integration token, and approved project slugs. Integration creation is **Appendix E**.

### aws

Optional read-only cloud diagnostics: allowed services, allowed regions, and per-environment diagnostics role ARNs the orchestrator assumes (validated best-effort with external id `agent-harness` when local AWS credentials exist). Role provisioning is **Appendix F**.

### dashboard

The Mission Control web UI: allowed viewer email domain, admin emails, optional public URL (enables dashboard links in Slack posts), the local-dev stand-in identity (`dashboard.dev_user_email`), and optional auto-commit of dashboard edits back to your fork — which needs a fine-grained PAT with Contents read/write on that one repository (`DASHBOARD_GITHUB_TOKEN`, verified live).

### content

Conventions and personalities. Offers to create `config/conventions.md` from the blank commented template at `templates/conventions.md` (set as `conventions.file`, injected into every repo task as `CONVENTIONS.md`), and personality files for the agent and reviewer from `config/personality_agent.example.md` and `config/personality_reviewer.example.md`.

### skills

The skills picker. `skills/` ships empty; the 13 workflow templates live in `templates/skills/` (`/review`, `/slack2pr`, `/jira2pr`, `/discoverissues`, …). The wizard lists them (marking any whose integration you skipped — `jira2pr`/`slack2jira` need Jira, `release`/`discoverissues` need `github.self_repo`), instantiates your selection into `skills/` with every `{{variable}}` filled from your earlier answers, and pulls in transitive `requires` automatically. Manual installation is documented in `templates/skills/README.md`.

### secrets

Where credentials live from here. Locally they stay in `.env`. For a deployed host you either put the same variables in `/etc/agent-harness/env` (no-AWS install) or push one JSON bundle to AWS Secrets Manager — the wizard offers to push it now, defaulting to the name `AGENT_HARNESS_SECRETS_STAGING` (`AGENT_HARNESS_SECRETS_<ENV>` by convention). The full bundle key table is in **Appendix F**.

When the wizard finishes it runs the same validation as `--check` and prints the next commands:

```bash
source .env && agent-harness run
```

## 3. Deploying

### 3a. Any Linux box with systemd (recommended start)

The harness needs nothing more than a Linux host with systemd. `deploy/install.sh` is written for Amazon Linux 2023 (`dnf`); on another distribution, adapt the package-install step and keep the rest.

- [ ] Push your configured fork (including `config/config.yaml` if you version it) and clone it on the host.
- [ ] Run the installer as root from the checkout:

  ```bash
  sudo ./deploy/install.sh
  ```

  It installs to a fixed layout: code and venv in `/opt/agent-harness`, config in `/etc/agent-harness`, state in `/var/lib/agent-harness`, runtime sockets in `/run/agent-harness`; service user `agentharness`; systemd units `agent-harness.service` plus `agent-harness-restart.path`/`agent-harness-restart.service` (the root-owned restart path that lets the unprivileged service request its own off-peak restart).

- [ ] Review `/etc/agent-harness/env` (seeded from `deploy/env.example`, root-owned mode 640). It carries the non-secret paths — `ENVIRONMENT`, `AGENT_HARNESS_DB_PATH`, `AGENT_HARNESS_CONFIG_PATH`, `AGENT_HARNESS_WORKSPACES_ROOT`, `AGENT_HARNESS_REPOS_ROOT`, `AGENT_HARNESS_MEMORY_ROOT`, `AGENT_HARNESS_SKILLS_ROOT`, `AGENT_HARNESS_UI_DIST`, `AGENT_HARNESS_BROKER_SOCKET`, `AGENT_HARNESS_REVIEWER_BROKER_SOCKET`, `AGENT_HARNESS_GIT_CRED_HELPER`, `AWS_REGION`.
- [ ] Choose the secrets source:
  - **AWS**: leave the env file non-secret; the service reads the `AGENT_HARNESS_SECRETS_<ENV>` JSON bundle from Secrets Manager via the instance role.
  - **No AWS, single box**: export the secret variables (`SLACK_BOT_TOKEN`, `GITHUB_APP_ID`, `REVIEWER_GITHUB_APP_ID`, … — the same names `agent_harness/secrets.py` reads, and the ones the wizard wrote to `.env`) directly in `/etc/agent-harness/env`. The file's 640 mode is what protects them.
- [ ] Review `/etc/agent-harness/config.yaml` (the installer ships your repo's `config/` directory verbatim, minus the example files, and seeds from the example on a first install with no `config.yaml`).
- [ ] Start and follow the service:

  ```bash
  sudo systemctl start agent-harness
  sudo journalctl -fu agent-harness
  ```

- [ ] Require a log line containing `credential broker started, github app credentials verified` (when GitHub is configured).
- [ ] If the reviewer is enabled, require `reviewer credential broker started, github app credentials verified`.
- [ ] Require either `slack intake enabled (team=…)` or, when Slack is not configured, `slack intake disabled (slack.team_id not set)`.
- [ ] Require a `session integrations: github=… jira=… confluence=… sentry=… aws=… slack=…` line whose booleans match your configuration.
- [ ] Require the final `agent-harness started (environment=…, db=…, audit_shipping=…)` line.
- [ ] If any verification fails, stop and fix the credential, installation, config, or permission; do not accept the corresponding warning as a healthy deployment.

### 3b. AWS reference deployment

`infrastructure/` is a **reference implementation**, one way to host the harness on AWS: a single private EC2 instance (no inbound, SSM-managed), an S3 deployment bucket for CI tarballs, a write-only Object Lock audit bucket, the Secrets Manager bundle shell, per-environment read-only diagnostics roles, and an optional SSO-protected ALB for the dashboard. It is a flat Pulumi Python project named `agent-harness`; one stack file, `infrastructure/Pulumi.staging.yaml`, ships with the repo.

Stack config (namespace `agent-harness`, per `infrastructure/README.md`):

| key | meaning | default |
|---|---|---|
| `environment` | name of this stack's environment | required |
| `host_account_id` | AWS account id where the host instance lives | required |
| `host_environment` | the single environment that hosts the harness; host-only resources (EC2, buckets, secret, orchestrator/deployer roles, ALB) are created when `environment == host_environment` | `staging` |
| `resource_prefix` | prefix for every physical name: `{resource_prefix}-{environment}-<thing>`; also the EC2 `Name` tag CI targets via SSM | `agent-harness` |
| `dashboard_domain` | public hostname for the dashboard (e.g. `agent.example.com`); empty/unset skips the ALB, target group, DNS records, and the Auth0 secret read entirely | unset |
| `route53_zone` | hosted zone for `dashboard_domain` (e.g. `example.com.`) | unset |
| `vpc_source` | `stack-reference` (read VPC outputs from another stack) or `lookup` (take ids from config) | `lookup` |
| `vpc_stack_ref` | stack to reference when `vpc_source: stack-reference` (e.g. `my-org/vpc/staging`) | unset |
| `vpc_id` / `private_subnet_ids` / `public_subnet_ids` | VPC + subnet ids when `vpc_source: lookup` (public only needed for the dashboard) | unset |
| `github_repo` | repo (e.g. `your-org/your-fork`) trusted by the OIDC deployer role; unset skips the role | unset |
| `assume_external_id` | STS external id the orchestrator presents to diagnostics roles | `agent-harness` |
| `secret_name` | Secrets Manager bundle name | `AGENT_HARNESS_SECRETS_{ENV}` |

**Ordering constraint — secrets before `pulumi up`.** Pulumi does not seed secret values; it only creates the empty secret shell. But when `dashboard_domain` is set, `infrastructure/alb.py` reads `auth0_domain`, `auth0_client_id`, and `auth0_client_secret` from the secret **at Pulumi eval time**. So push the bundle first — either the wizard's secrets step or a manual `aws secretsmanager create-secret` / `put-secret-value` with the full JSON — and only then run:

```bash
cd infrastructure
pulumi up --stack <your-pulumi-org>/agent-harness/staging
```

Dashboard SSO notes (the reference uses Auth0; any OIDC provider the ALB supports works the same way):

- [ ] Create a Regular Web Application in your OIDC provider and add exactly `https://<dashboard_domain>/oauth2/idpresponse` to its allowed callback URLs (the ALB's fixed OIDC callback path).
- [ ] The ALB requests scope `openid email`; the provider connection used by your operators must supply a verified `email` claim — the app restricts viewers by `dashboard.allowed_email_domain` and grants mutations only to `dashboard.admin_emails`.
- [ ] Record the provider domain as a **bare hostname** (no `https://`, no trailing slash) plus the client id and secret; store them as the `auth0_domain` / `auth0_client_id` / `auth0_client_secret` bundle keys.
- [ ] On the host, set `dashboard.enabled: true`, `dashboard.bind: 0.0.0.0`, and `dashboard.port: 8787` — the instance security group only admits the ALB on that port.
- [ ] After `pulumi up`, browse to `https://<dashboard_domain>`, complete sign-in, and confirm an allowed-domain non-admin can read but not mutate while an admin email can edit.

For additional environments the agent should diagnose, create more stacks (each gets only the `{resource_prefix}-{environment}-diagnostics` role) and add their ARNs to `aws.diagnostics_role_arns` in config.

To rotate any credential later: update the whole JSON bundle with `put-secret-value`, then `sudo systemctl restart agent-harness` and verify the startup log lines. Never rerun Pulumi merely to rotate a value.

### 3c. CI/CD releases

Pull requests to `main` run `.github/workflows/pull_request_checks.yaml`: Python lint/format/type/tests plus a Node 22 dashboard build. Pushing a `vX.Y.Z` tag runs `.github/workflows/deploy.yaml`: `pulumi up` on the staging stack, then a UI build, release tarball upload to S3, and an SSM-invoked `/opt/agent-harness/deploy/remote-update.sh` on the host. There is a single deployed environment (staging).

Repository **variables** (Settings → Secrets and variables → Actions → Variables):

| variable | meaning |
|---|---|
| `DEPLOY_BUCKET` | S3 deployment bucket name (`{resource_prefix}-{host_environment}-deployment-bucket` from the stack) |
| `AWS_REGION` | region of the host account resources (e.g. `us-east-1`) |
| `AWS_DEPLOYER_ROLE_ARN` | ARN of the OIDC deployer role Pulumi creates when `github_repo` is set (`{resource_prefix}-{host_environment}-deployer`) |
| `HOST_INSTANCE_TAG` | value of the EC2 `Name` tag the SSM command targets — must equal `resource_prefix` |
| `PULUMI_ORG` | Pulumi organization owning the `agent-harness/<env>` stacks |

Repository **secret**: `PULUMI_ACCESS_TOKEN`, with access to the `agent-harness` stacks.

- [ ] In the host AWS account, confirm the GitHub Actions OIDC provider for `token.actions.githubusercontent.com` exists; the deployer role's trust policy restricts it to `repo:<your-org>/<your-fork>:*`.
- [ ] Confirm the deployer role can only upload to the deployment bucket and send/watch SSM commands — no broader permissions.
- [ ] Open a pull request and require both check jobs green before merging.
- [ ] Create and push a semantic release tag:

  ```bash
  git switch main && git pull --ff-only
  git tag vX.Y.Z
  git push origin vX.Y.Z
  ```

- [ ] Watch the `deploy` workflow: the Pulumi job, then the package upload, then the SSM update must all succeed.
- [ ] Verify `systemctl is-active agent-harness` and the startup log lines from section 3a after deployment.
- [ ] To roll back, identify an older commit whose checks were green, create a **new, higher** `vX.Y.Z` tag pointing at it, and push that tag. Never move or overwrite an existing release tag.

## 4. Appendices

### Appendix A. Slack app via manifest

One Slack app handles Socket Mode intake and every outbound message. The reviewer never posts with a separate Slack identity — the main agent announces the reviewer's activity — so `chat:write.customize` is not needed and is deliberately absent from the manifest.

- [ ] Open `https://api.slack.com/apps`, choose **Create New App** → **From an app manifest**, pick the target workspace, and paste the contents of `templates/slack_app_manifest.yaml`.
- [ ] Before pasting, edit `display_information.name` and `features.bot_user.display_name` to your agent's name.
- [ ] Review the manifest's bot scopes — they must stay in lockstep with what the service uses: `app_mentions:read`, `chat:write`, `channels:history`, `groups:history`, `reactions:write`, `files:write`, `im:history`, `im:write`, `users:read`, `users:read.email`.
- [ ] Create the app, then open **Basic Information** → **Display Information**, upload your agent's icon, and save.
- [ ] Open **Socket Mode**, enable it, and generate an app-level token with the `connections:write` scope; copy the `xapp-…` token into the password manager as `SLACK_APP_TOKEN`. (Socket Mode apps need no public request URL.)
- [ ] Open **Install App** → **Install to Workspace**, authorize, and copy the `xoxb-…` **Bot User OAuth Token** as `SLACK_BOT_TOKEN`.
- [ ] Record the workspace/team ID (`T…` — visible in the workspace URL or the `team_id` from `auth.test`) for `slack.team_id`.
- [ ] If any allowed source channel is **private**, uncomment `message.groups` under `event_subscriptions.bot_events` (the `groups:history` scope is already present) and reinstall the app.
- [ ] Create or choose the source channels, a private debug channel, and an optional review-notification channel; in each, use **Add apps** or `/invite @<your-agent>` to add the bot.
- [ ] Copy each channel's `C…` ID from **View channel details** → **About** → **Channel ID**: source channels go to `slack.allowed_channels`, the debug channel to `slack.debug_channel`, the optional review channel to `github.review_requests.notify_channel`.
- [ ] Reinstall the app from **Install App** whenever scopes or event subscriptions change.

### Appendix B. Main GitHub App

The main agent's GitHub App is the credential authority for ordinary tasks and requested reviews. The credential broker mints repository-scoped installation tokens per task, so the App itself must carry the maximum permissions any execution profile uses.

- [ ] In GitHub, open the organization **Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**.
- [ ] Name the App after your agent, set a unique homepage URL, and leave webhook delivery disabled; the harness polls GitHub and receives no webhooks.
- [ ] Under **Repository permissions**, set **Contents** to **Read and write**, **Metadata** to **Read-only**, and **Pull requests** to **Read and write**. Grant no administration, actions-secrets, or repository-secrets access.
- [ ] Choose **Only on this account** if the App is organization-owned, then create it.
- [ ] Upload the agent's avatar on the App settings page and save.
- [ ] Record the numeric **App ID** as `GITHUB_APP_ID`.
- [ ] Under **Private keys**, choose **Generate a private key**, download the PEM once, and move it to a password-manager-backed secure location (`GITHUB_APP_PRIVATE_KEY`; the wizard takes the file path).
- [ ] Open **Install App**, install on the organization with **Only select repositories**, and select every repository that will appear in `github.approved_repos` — including this harness's own repository when `github.self_repo` is set.
- [ ] Record the numeric installation ID from the installation URL ending in `/installations/<installation-id>` as `GITHUB_INSTALLATION_ID`.
- [ ] Confirm the App appears in each approved repository under **Settings** → **Integrations** → **GitHub Apps**.

### Appendix C. Reviewer GitHub App

The reviewer persona requires a separate GitHub identity because GitHub rejects a `REQUEST_CHANGES` review submitted by the pull request's own author. With its own App, the reviewer can adversarially review PRs the main agent opens. The reviewer posts only on GitHub; the main agent owns every Slack announcement.

- [ ] Repeat **Organization Settings** → **Developer settings** → **GitHub Apps** → **New GitHub App**, using the reviewer's distinct name and homepage details.
- [ ] Disable webhooks and set **Contents** to **Read and write**, **Metadata** to **Read-only**, and **Pull requests** to **Read and write**; grant no administrative or secrets permissions.
- [ ] Upload a distinct avatar for the reviewer App and save it.
- [ ] For a distinct avatar on the reviewer's *commits* as well: `reviewer.commit_email` drives GitHub's commit-avatar lookup — verify that email on a GitHub account that carries the reviewer's avatar.
- [ ] Record the reviewer's numeric App ID as `REVIEWER_GITHUB_APP_ID`.
- [ ] Generate the reviewer's private key, store the PEM securely, and record it as `REVIEWER_GITHUB_APP_PRIVATE_KEY`.
- [ ] Install the reviewer App on every repository in `github.approved_repos` using **Only select repositories**.
- [ ] Record the reviewer's numeric installation ID as `REVIEWER_GITHUB_INSTALLATION_ID`.
- [ ] Confirm both Apps appear as installed GitHub Apps on each approved repository.
- [ ] Confirm no second Slack app, Slack username override, or reviewer Slack avatar is being configured — the reviewer has no Slack identity.

### Appendix D. Jira + Confluence service account

Jira and Confluence use one dedicated service account. Jira permissions, not model instructions, enforce the no-delete boundary.

- [ ] In `admin.atlassian.com`, open **Directory** → **Users** → **Invite users**, invite a dedicated address such as `<agent-service-email>`, and grant Jira product access.
- [ ] Accept the invitation from that mailbox, then open `id.atlassian.com/manage-profile`, set the display name to your agent's name, and upload its avatar.
- [ ] In Jira, open **Settings** → **System** → **Project roles** and create a role named `Agent`.
- [ ] Open each approved project's permission scheme; copy a shared scheme before editing it if other projects rely on it.
- [ ] Grant `Agent` only the needed permissions: **Browse Projects**, **Create Issues**, **Edit Issues**, **Add Comments**, **Link Issues**, **Transition Issues**, and **Assignable User**.
- [ ] Confirm `Agent` appears in none of **Delete Issues**, **Delete All Comments**, **Delete Own Comments**, **Delete All Attachments**, **Delete Own Attachments**, or **Administer Projects**.
- [ ] Open each approved project → **Project settings** → **People**, add the service account with only the `Agent` role, and remove it from broad groups that confer delete or administration rights.
- [ ] While signed in as the service account, open `id.atlassian.com/manage-profile` → **Security** → **Create and manage API tokens** → **Create API token**, name it `agent-harness`, record its expiry, and save it as `JIRA_API_TOKEN`.
- [ ] Record the service account address as `JIRA_EMAIL`; set `jira.site`, `jira.projects`, `jira.issue_types`, and optional `jira.story_points_field` in config.
- [ ] If Confluence reads are wanted, grant the same account read access only to the intended spaces and configure `confluence.site` and `confluence.spaces`.
- [ ] Verify authentication and read access:

  ```bash
  curl --fail --silent --show-error \
    --user '<agent-service-email>:<jira-api-token>' \
    --header 'Accept: application/json' \
    'https://<your-org>.atlassian.net/rest/api/3/myself'
  ```

- [ ] Against a disposable test issue, verify the service account cannot delete it; the response must be `403`:

  ```bash
  curl --silent --output /dev/null --write-out '%{http_code}\n' \
    --user '<agent-service-email>:<jira-api-token>' \
    --request DELETE \
    'https://<your-org>.atlassian.net/rest/api/3/issue/<test-issue-key>'
  ```

### Appendix E. Sentry internal integration

Sentry access is token-based and read-only. The adapter exposes no mutation tools, and the integration scopes should independently enforce that boundary.

- [ ] In Sentry, open **Organization Settings** → **Developer Settings** → **Custom Integrations** → **Create New Integration**.
- [ ] Choose **Internal Integration**, name it `agent-harness`, and continue.
- [ ] Set **Project** to **Read**, **Issue & Event** to **Read**, and **Organization** to **Read**; leave every other scope unset.
- [ ] Save the integration and copy its token once into the password manager as `SENTRY_TOKEN`.
- [ ] Record the organization slug from the Sentry URL and the approved project slugs for `sentry.organization` and `sentry.projects`.
- [ ] Confirm the integration page shows no write or administration scopes.

### Appendix F. AWS, Pulumi, and the secrets bundle

A single AWS account is the default: the host stack creates both the instance-side resources and that account's diagnostics role. Add more accounts only if the agent should diagnose environments living elsewhere — each extra account gets its own stack containing just a diagnostics role.

- [ ] Log in to Pulumi and create the stack(s) under `<your-pulumi-org>/agent-harness/<env>`; set `agent-harness:host_account_id` (and any optional keys from the section 3b table) in each `Pulumi.<env>.yaml`.
- [ ] Confirm the correct account before every update: `aws sts get-caller-identity`.
- [ ] Diagnostics role trust: each `{resource_prefix}-{environment}-diagnostics` role trusts only the orchestrator role `arn:aws:iam::<host_account_id>:role/{resource_prefix}-{host_environment}-orchestrator`, and only with the STS external id `agent-harness` (configurable via `assume_external_id`). It attaches `ViewOnlyAccess` plus a small diagnostics supplement, and carries explicit denies on secrets, parameter store, KMS decrypt, S3 objects, and DynamoDB items.
- [ ] Copy each diagnostics role ARN into `aws.diagnostics_role_arns` in config.
- [ ] For CI/CD, set the five repository variables and the `PULUMI_ACCESS_TOKEN` secret from section 3c, and ensure the GitHub Actions OIDC provider exists in the host account.

The deployed service reads one JSON secret, by default `AGENT_HARNESS_SECRETS_<ENV>` (override with `secret_name` in the stack, or `AGENT_HARNESS_SECRETS_NAME` in the host env file). The wizard's secrets step pushes it for you; to build it manually, use exactly these 16 keys:

| Bundle key | Source |
|---|---|
| `slack_bot_token` | Slack **Bot User OAuth Token** (`xoxb-…`) — Appendix A |
| `slack_app_token` | Slack Socket Mode app-level token (`xapp-…`) — Appendix A |
| `github_app_id` | Main GitHub App ID — Appendix B |
| `github_installation_id` | Main GitHub App installation ID — Appendix B |
| `github_app_private_key` | Full main GitHub App PEM contents — Appendix B |
| `reviewer_github_app_id` | Reviewer GitHub App ID — Appendix C |
| `reviewer_github_installation_id` | Reviewer GitHub App installation ID — Appendix C |
| `reviewer_github_app_private_key` | Full reviewer GitHub App PEM contents — Appendix C |
| `jira_email` | Jira service account email — Appendix D |
| `jira_api_token` | Jira service account API token — Appendix D |
| `sentry_token` | Sentry internal integration token — Appendix E |
| `claude_oauth_token` | Output of `claude setup-token` |
| `dashboard_github_token` | Fine-grained PAT (Contents read/write on your fork only) for dashboard auto-commits |
| `auth0_domain` | Bare OIDC tenant hostname, without `https://` or a trailing slash |
| `auth0_client_id` | OIDC Regular Web Application client ID |
| `auth0_client_secret` | OIDC Regular Web Application client secret |

Notes:

- Keys for integrations you skipped may simply be absent; the corresponding features stay disabled.
- The three `auth0_*` keys are consumed only by `infrastructure/alb.py` (the reference ALB SSO) — required whenever `dashboard_domain` is set, and required **before** `pulumi up` (section 3b ordering constraint).
- Preserve PEM newlines as JSON string escapes; validate the file without printing it: `python3.12 -c "import json; json.load(open('<bundle.json>'))"`.
- Verify the current version's key names without displaying values:

  ```bash
  aws secretsmanager get-secret-value \
    --secret-id AGENT_HARNESS_SECRETS_STAGING \
    --query SecretString --output text \
    | python3.12 -c 'import json,sys; print("\n".join(sorted(json.load(sys.stdin))))'
  ```

- To rotate: replace the credential at its source, `put-secret-value` the whole bundle, `sudo systemctl restart agent-harness`, and check the startup logs.

## 5. End-to-end verification

The installation is complete only after each external identity and policy boundary is observed — including the reviewer's distinct GitHub authorship and the main agent's exclusive Slack identity. Substitute your agent name, reviewer name, project keys, and org throughout; skip only the checks for integrations you did not configure.

- [ ] Run `agent-harness setup --check` and require every configured integration to report `[ok]`.
- [ ] In an allowed Slack channel, as an authorized role member, mention `@<your-agent>` with a simple request and confirm it reacts or acknowledges, posts a started message, answers in the originating thread, and links Additional Details when the debug feed is enabled.
- [ ] Ask the agent to comment on a disposable issue in an approved Jira project and confirm Jira shows the service-account identity as author.
- [ ] Ask the agent for an approved Sentry project's unresolved issues and confirm the read succeeds with no mutation capability.
- [ ] Ask the agent for an allowed AWS diagnostic read and confirm it succeeds; then request a delete operation and confirm both the adapter and IAM refuse it.
- [ ] Request a review from the main GitHub App on a test pull request and confirm the poller creates one `/review` task for that head SHA and the main App submits the review.
- [ ] Open a test pull request authored by the main agent in an approved repository and confirm the reviewer submits the adversarial GitHub review as the reviewer App, with its own avatar and identity.
- [ ] If `github.review_requests.notify_channel` is configured, confirm the channel receives the reviewer's started and finished announcements plus the review summary, all visibly authored by the main agent's Slack app. Confirm no reviewer username or avatar appears in Slack.
- [ ] If `github.review_requests.notify_channel` is empty, confirm the reviewer's GitHub review still lands and no GitHub-origin lifecycle message is posted to Slack.
- [ ] Run a task whose profile allows `mcp__slack__send_dm`, ask it to contact a test user, and confirm the DM is sent by the main agent's app identity with no custom identity override.
- [ ] Browse to the dashboard (`https://<dashboard_domain>` behind the ALB, or locally with `dashboard.dev_user_email`), complete sign-in where applicable, confirm an allowed-domain viewer can read, and confirm only an email in `dashboard.admin_emails` can cancel/retry or edit.
- [ ] As a dashboard admin, on the **Config** page confirm the editable targets include `config.yaml`, the agent's personality, the reviewer's personality (when enabled), the task-started phrases, and each installed skill. Make a harmless edit, confirm it previews a diff, saves, is audited, and commits through `dashboard.auto_commit`, then revert it through the normal reviewed Git workflow.
- [ ] As a dashboard admin, open **Issues**, choose a repo and run discovery (`/discoverissues <owner>/<repo>`); confirm repo-scoped issues appear with details and discussion. Approve one, trigger implementation, and confirm a child PR task targets that issue's repo and the row moves to `in_progress`.
- [ ] As a dashboard admin, open **Scheduler**, confirm the seeded default schedules are present, create a one-off schedule a few minutes out with a chosen model, and confirm it fires as a task attributed to `cli` with that model, then advances or disables per its recurrence and max-runs.
- [ ] Restart `agent-harness` during a disposable running task and confirm reconciliation requeues and resumes it rather than losing the task.
- [ ] Record the deployment version, verification date, operator, and any accepted limitations in your organization's operational system of record — without copying credentials.
