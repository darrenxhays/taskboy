"""interactive first-run setup: `taskboy setup`.

walks each integration step by step: prints the manual admin-ui instructions, prompts for the
resulting ids/tokens, validates them live, and writes config/config.yaml plus one
config/services/<name>.yaml per connected service (comment-preserving, via ruamel) and a
sourceable .env for local secrets. every completed step is written immediately and every write
must pass load_config, so quitting mid-run and re-running resumes naturally — there is no
separate state file.

secrets are read with getpass or from a file path, never from argv, and are echoed back only
as `set (…last4)`.
"""

import getpass
import json
import shutil
import sys
from pathlib import Path

from taskboy import assets, settings, setup_checks, skills
from taskboy.config import KNOWN_SERVICES, ConfigError, load_config, service_enabled_in

CONFIG_PATH = Path(settings.CONFIG_PATH)
ENV_PATH = Path(".env")
# packaged seed material; module-level so tests can monkeypatch
TEMPLATES_ROOT = assets.TEMPLATES_ROOT
EXAMPLE_PATH = TEMPLATES_ROOT / "config.example.yaml"

# .env keys that hold credentials (everything the service reads in secrets.py, plus claude)
SECRET_ENV_KEYS = [
    "CLAUDE_CODE_OAUTH_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_INSTALLATION_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "REVIEWER_GITHUB_APP_ID",
    "REVIEWER_GITHUB_INSTALLATION_ID",
    "REVIEWER_GITHUB_APP_PRIVATE_KEY",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "SENTRY_TOKEN",
    "DASHBOARD_GITHUB_TOKEN",
]


# -- terminal helpers ---------------------------------------------------------


def say(text: str = "") -> None:
    print(text)


def status(ok: bool | None, label: str, detail: str) -> None:
    tag = "[ok]" if ok else "[--]" if ok is None else "[!!]"
    print(f"  {tag} {label}: {detail}")


def masked(value: str) -> str:
    return f"set (…{value[-4:]})" if value else "not set"


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"  {prompt}{suffix}: ").strip()
    return answer or default


def ask_yes(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"  {prompt} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def ask_secret(prompt: str, current: str = "") -> str:
    """hidden input; empty keeps the current value."""
    suffix = f" [{masked(current)}]" if current else ""
    answer = getpass.getpass(f"  {prompt}{suffix}: ").strip()
    return answer or current


def ask_pem(prompt: str, current: str = "") -> str:
    """private keys are pasted as a file path, not typed."""
    suffix = f" [{masked(current)}]" if current else ""
    answer = input(f"  {prompt} (path to .pem file){suffix}: ").strip()
    if not answer:
        return current
    path = Path(answer).expanduser()
    if not path.is_file():
        say(f"  file not found: {path}")
        return ask_pem(prompt, current)
    return path.read_text()


def ask_list(prompt: str, default: list[str] | None = None) -> list[str]:
    current = ", ".join(default or [])
    answer = ask(f"{prompt} (comma-separated)", current)
    return [item.strip() for item in answer.split(",") if item.strip()]


# -- config + env io ----------------------------------------------------------


def _yaml():
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 100000  # never rewrap the long allowed_tools lines
    return yaml


def service_config_path(name: str) -> Path:
    return CONFIG_PATH.parent / "services" / f"{name}.yaml"


def seed_config() -> None:
    """fresh instance directory: create the config dir and seed it from the packaged example."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
    services_dir = CONFIG_PATH.parent / "services"
    services_dir.mkdir(exist_ok=True)
    for file in sorted((TEMPLATES_ROOT / "services").glob("*.yaml")):
        target = services_dir / file.name
        if not target.exists():
            shutil.copyfile(file, target)
    messages = CONFIG_PATH.parent / "task_started_messages.yaml"
    if not messages.exists():
        shutil.copyfile(TEMPLATES_ROOT / "task_started_messages.yaml", messages)
    say(f"created {CONFIG_PATH} and {services_dir}/ from the packaged templates")


def load_config_data() -> dict:
    """one merged mapping for the steps to mutate; save_config_data splits service sections back out."""
    if not CONFIG_PATH.exists():
        seed_config()
    data = _yaml().load(CONFIG_PATH.read_text())
    for name in KNOWN_SERVICES:
        target = service_config_path(name)
        if target.exists():
            data[name] = _yaml().load(target.read_text())
    return data


def save_config_data(data) -> None:
    """write config.yaml + services/*.yaml, but only if the result still passes load_config (exit-64 guard)."""
    import io

    def dump(value) -> str:
        buffer = io.StringIO()
        _yaml().dump(value, buffer)
        return buffer.getvalue()

    # sections whose service file exists are written there; everything else stays in config.yaml
    writes: list[tuple[Path, str]] = []
    split_out: dict[str, object] = {}
    for name in KNOWN_SERVICES:
        target = service_config_path(name)
        if name in data and target.exists():
            split_out[name] = data.pop(name)
            writes.append((target, dump(split_out[name])))
    writes.append((CONFIG_PATH, dump(data)))
    for name, section in split_out.items():
        data[name] = section  # restore the merged in-memory view for later steps
    previous = {path: path.read_text() if path.exists() else None for path, _ in writes}
    for path, content in writes:
        path.write_text(content)
    try:
        load_config(str(CONFIG_PATH))
    except ConfigError:
        for path, _ in writes:
            original = previous[path]
            if original is not None:
                path.write_text(original)
            else:
                path.unlink(missing_ok=True)
        raise


def read_env() -> dict[str, str]:
    """parse the sourceable .env this wizard writes: export KEY='value' (single-quoted, may span lines)."""
    if not ENV_PATH.exists():
        return {}
    values: dict[str, str] = {}
    text = ENV_PATH.read_text()
    index = 0
    while True:
        start = text.find("export ", index)
        if start == -1:
            break
        equals = text.find("='", start)
        if equals == -1:
            break
        key = text[start + len("export ") : equals].strip()
        end = equals + 2
        raw = []
        while end < len(text):
            if text[end] == "'":
                if text[end : end + 4] == "'\\''":  # escaped single quote
                    raw.append("'")
                    end += 4
                    continue
                break
            raw.append(text[end])
            end += 1
        values[key] = "".join(raw)
        index = end + 1
    return values


def write_env(values: dict[str, str]) -> None:
    lines = [
        "# local secrets for taskboy, written by `taskboy setup`.",
        "# load with:  source .env   (gitignored; on a deployed host use /etc/taskboy/env or AWS Secrets Manager)",
    ]
    for key in SECRET_ENV_KEYS:
        value = values.get(key, "")
        if value:
            quoted = value.replace("'", "'\\''")
            lines.append(f"export {key}='{quoted}'")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    ENV_PATH.chmod(0o600)


# -- steps --------------------------------------------------------------------
# each step is step_<name>(data, env) -> None, mutating the ruamel config mapping and the
# env dict in place; run() saves both after every step.


def step_identity(data, env) -> None:
    say("\n== Identity ==")
    say("  Names appear in Slack, prompts, commits, and the dashboard. Pick anything you like.")
    agent = data.setdefault("agent", {})
    agent["name"] = ask("Main agent name", str(agent.get("name") or "Agent"))
    github = data.setdefault("github", {})
    github["commit_name"] = ask("Git commit author for the agent", str(github.get("commit_name") or agent["name"]))
    github["commit_email"] = ask("Git commit email for the agent (GitHub derives the commit avatar from it)", str(github.get("commit_email") or ""))
    cli_update = data.setdefault("cli_update", {})
    cli_update["tzname"] = ask("Timezone (IANA name, for scheduled maintenance windows)", str(cli_update.get("tzname") or "America/Los_Angeles"))


def step_claude(data, env) -> None:
    say("\n== Claude auth ==")
    say("  The harness runs sub-agents through your Claude subscription.")
    say("  On this machine, run:  claude setup-token   and paste the resulting token here.")
    token = ask_secret("CLAUDE_CODE_OAUTH_TOKEN", env.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
    if not token:
        say("  skipped — the echo runner works without it; the claude runner will not.")
        return
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    ok, detail = setup_checks.check_claude(token)
    status(ok, "claude", detail)
    if ok and str((data.get("orchestrator") or {}).get("runner")) == "echo" and ask_yes("Switch orchestrator.runner from echo to claude?", default=True):
        data["orchestrator"]["runner"] = "claude"


def step_slack(data, env) -> None:
    say("\n== Slack ==")
    slack = data.setdefault("slack", {})
    if not ask_yes("Configure Slack intake?", default=True):
        slack["enabled"] = False
        return
    say("  1. Create the app from the manifest: https://api.slack.com/apps -> From an app manifest")
    say(f"     manifest file: {TEMPLATES_ROOT / 'slack_app_manifest.yaml'}")
    say("  2. Socket Mode -> generate an app-level token with connections:write (xapp-…)")
    say("  3. Install App -> Install to Workspace, copy the bot token (xoxb-…)")
    say("  4. Invite the bot to every channel you plan to allow.")
    env["SLACK_BOT_TOKEN"] = ask_secret("Bot token (xoxb-…)", env.get("SLACK_BOT_TOKEN", ""))
    env["SLACK_APP_TOKEN"] = ask_secret("App-level token (xapp-…)", env.get("SLACK_APP_TOKEN", ""))
    slack["team_id"] = ask("Workspace team id (T…)", str(slack.get("team_id") or ""))
    slack["enabled"] = bool(slack["team_id"])
    if not slack["enabled"]:
        say("  slack left disabled — set the team id and re-run `taskboy setup --step slack`")
    if env["SLACK_BOT_TOKEN"]:
        ok, detail = setup_checks.check_slack(env["SLACK_BOT_TOKEN"], slack["team_id"])
        status(ok, "slack", detail)
        ok, detail = setup_checks.check_slack_app_token(env["SLACK_APP_TOKEN"])
        status(ok, "slack app token", detail)
    channels = ask_list("Allowed channel ids (empty = any channel the bot is invited to)", list(slack.get("allowed_channels") or []))
    slack["allowed_channels"] = channels
    slack["debug_channel"] = ask("Debug channel id (empty disables the debug feed)", str(slack.get("debug_channel") or ""))
    if env["SLACK_BOT_TOKEN"]:
        for channel in channels + ([slack["debug_channel"]] if slack["debug_channel"] else []):
            ok, detail = setup_checks.check_slack_channel(env["SLACK_BOT_TOKEN"], channel)
            status(ok, "channel", detail)
    roles = data.setdefault("roles", {})
    admin = roles.setdefault("admin", {"members": [], "allowed_profiles": ["read_only", "standard", "deep"], "model_override": True, "max_budget_usd": None})
    members = [member for member in ask_list("Admin Slack user ids (U…)", [m for m in admin.get("members") or [] if m not in ("cli", "YOUR_SLACK_USER_ID")])]
    admin["members"] = members + ["cli"]


def step_github(data, env) -> None:
    say("\n== GitHub App (main agent) ==")
    github = data.setdefault("github", {})
    if not ask_yes("Configure GitHub?", default=True):
        github["enabled"] = False
        return
    github["enabled"] = True
    say("  Create a GitHub App (org Settings -> Developer settings -> GitHub Apps -> New):")
    say("    permissions: Contents read/write, Metadata read, Pull requests read/write; no webhook.")
    say("    install it on every repository the agent may touch, then note the App ID and the")
    say("    installation id (the number in the installation page URL) and download a private key.")
    env["GITHUB_APP_ID"] = ask("App ID", env.get("GITHUB_APP_ID", ""))
    env["GITHUB_INSTALLATION_ID"] = ask("Installation id", env.get("GITHUB_INSTALLATION_ID", ""))
    env["GITHUB_APP_PRIVATE_KEY"] = ask_pem("Private key", env.get("GITHUB_APP_PRIVATE_KEY", ""))
    github["approved_repos"] = ask_list("Approved repositories (owner/repo)", list(github.get("approved_repos") or []))
    self_repo = ask("This agent's own repo, if it may work on itself (owner/repo, empty = none)", str(github.get("self_repo") or ""))
    github["self_repo"] = self_repo
    if self_repo and self_repo not in github["approved_repos"]:
        github["approved_repos"].append(self_repo)
    github["protected_branch_patterns"] = ask_list("Protected branch patterns", list(github.get("protected_branch_patterns") or ["main"]))
    if env["GITHUB_APP_ID"]:
        ok, detail = setup_checks.check_github_app(env["GITHUB_APP_ID"], env["GITHUB_INSTALLATION_ID"], env["GITHUB_APP_PRIVATE_KEY"], github["approved_repos"])
        status(ok, "github app", detail)
    review_requests = github.setdefault("review_requests", {"enabled": False, "poll_interval_seconds": 60, "notify_channel": ""})
    review_requests["enabled"] = ask_yes("Poll for PRs where the agent is a requested reviewer?", bool(review_requests.get("enabled")))


def step_reviewer(data, env) -> None:
    say("\n== Reviewer persona (optional) ==")
    say("  A second GitHub-only persona that reviews the main agent's PRs. It needs its OWN")
    say("  GitHub App because GitHub forbids requesting changes on your own pull requests.")
    reviewer = data.setdefault("reviewer", {})
    if not ask_yes("Enable the reviewer?", bool(reviewer.get("enabled"))):
        reviewer["enabled"] = False
        return
    reviewer["name"] = ask("Reviewer name", str(reviewer.get("name") or "Reviewer"))
    reviewer["commit_name"] = ask("Reviewer git commit author", str(reviewer.get("commit_name") or reviewer["name"]))
    reviewer["commit_email"] = ask("Reviewer git commit email (required)", str(reviewer.get("commit_email") or ""))
    say("  Create a second GitHub App with the same permissions as the main one (a distinct avatar helps).")
    env["REVIEWER_GITHUB_APP_ID"] = ask("Reviewer App ID", env.get("REVIEWER_GITHUB_APP_ID", ""))
    env["REVIEWER_GITHUB_INSTALLATION_ID"] = ask("Reviewer installation id", env.get("REVIEWER_GITHUB_INSTALLATION_ID", ""))
    env["REVIEWER_GITHUB_APP_PRIVATE_KEY"] = ask_pem("Reviewer private key", env.get("REVIEWER_GITHUB_APP_PRIVATE_KEY", ""))
    if env["REVIEWER_GITHUB_APP_ID"]:
        approved = list((data.get("github") or {}).get("approved_repos") or [])
        ok, detail = setup_checks.check_github_app(env["REVIEWER_GITHUB_APP_ID"], env["REVIEWER_GITHUB_INSTALLATION_ID"], env["REVIEWER_GITHUB_APP_PRIVATE_KEY"], approved)
        status(ok, "reviewer github app", detail)
    reviewer["enabled"] = True
    reviewer["review_agent_prs"] = ask_yes("Auto-review every PR the main agent opens?", bool(reviewer.get("review_agent_prs", True)))


def step_jira(data, env) -> None:
    say("\n== Jira + Confluence (optional) ==")
    jira = data.setdefault("jira", {})
    if not ask_yes("Configure Jira?", default=False):
        jira["enabled"] = False
        return
    say("  Use a dedicated service account with a scoped project role; create an API token at")
    say("  https://id.atlassian.com/manage-profile/security/api-tokens")
    jira["site"] = ask("Jira site url (https://your-org.atlassian.net)", str(jira.get("site") or ""))
    jira["enabled"] = bool(jira["site"])
    env["JIRA_EMAIL"] = ask("Service account email", env.get("JIRA_EMAIL", ""))
    env["JIRA_API_TOKEN"] = ask_secret("API token", env.get("JIRA_API_TOKEN", ""))
    ok, detail = setup_checks.check_jira(jira["site"], env["JIRA_EMAIL"], env["JIRA_API_TOKEN"])
    status(ok, "jira", detail)
    jira["projects"] = ask_list("Project keys the agent may touch", list(jira.get("projects") or []))
    for key in jira["projects"]:
        ok, detail = setup_checks.check_jira_project(jira["site"], env["JIRA_EMAIL"], env["JIRA_API_TOKEN"], key)
        status(ok, "project", detail)
    jira["story_points_field"] = ask("Story points custom field (e.g. customfield_10016, empty = unused)", str(jira.get("story_points_field") or ""))
    confluence = data.setdefault("confluence", {})
    if ask_yes("Configure Confluence with the same account?", default=False):
        confluence["site"] = ask("Confluence site url", str(confluence.get("site") or jira["site"]))
        confluence["enabled"] = bool(confluence["site"])
        ok, detail = setup_checks.check_confluence(confluence["site"], env["JIRA_EMAIL"], env["JIRA_API_TOKEN"])
        status(ok, "confluence", detail)
    else:
        confluence["enabled"] = False


def step_sentry(data, env) -> None:
    say("\n== Sentry (optional) ==")
    sentry = data.setdefault("sentry", {})
    if not ask_yes("Configure Sentry?", default=False):
        sentry["enabled"] = False
        return
    say("  Create an internal integration with read-only Project / Issue & Event / Organization scopes.")
    sentry["organization"] = ask("Organization slug", str(sentry.get("organization") or ""))
    sentry["enabled"] = bool(sentry["organization"])
    env["SENTRY_TOKEN"] = ask_secret("Integration token", env.get("SENTRY_TOKEN", ""))
    ok, detail = setup_checks.check_sentry(sentry["organization"], env["SENTRY_TOKEN"])
    status(ok, "sentry", detail)
    sentry["projects"] = ask_list("Project slugs (empty = all readable)", list(sentry.get("projects") or []))


def step_aws(data, env) -> None:
    say("\n== AWS diagnostics (optional) ==")
    aws = data.setdefault("aws", {})
    if not ask_yes("Configure read-only AWS diagnostics?", default=False):
        aws["enabled"] = False
        return
    aws["allowed_services"] = ask_list("Allowed services (e.g. logs, cloudwatch, lambda, s3)", list(aws.get("allowed_services") or []))
    aws["enabled"] = bool(aws["allowed_services"])
    aws["allowed_regions"] = ask_list("Allowed regions", list(aws.get("allowed_regions") or ["us-east-1"]))
    roles = dict(aws.get("diagnostics_role_arns") or {})
    while ask_yes("Add a per-environment diagnostics role?", default=not roles):
        environment = ask("Environment name (e.g. staging)")
        arn = ask("Role ARN")
        if environment and arn:
            roles[environment] = arn
            ok, detail = setup_checks.check_aws_role(arn, "taskboy")
            status(ok, f"assume-role {environment}", detail + ("" if ok else " (needs local aws credentials — best-effort check)"))
    if roles:
        aws["diagnostics_role_arns"] = roles


def step_dashboard(data, env) -> None:
    say("\n== Dashboard (optional) ==")
    dashboard = data.setdefault("dashboard", {})
    if not ask_yes("Enable the web dashboard?", bool(dashboard.get("enabled"))):
        dashboard["enabled"] = False
        return
    dashboard["enabled"] = True
    dashboard["allowed_email_domain"] = ask("Allowed email domain (viewers)", str(dashboard.get("allowed_email_domain") or ""))
    dashboard["admin_emails"] = ask_list("Admin emails", list(dashboard.get("admin_emails") or []))
    dashboard["public_url"] = ask("Public url (empty disables slack links)", str(dashboard.get("public_url") or ""))
    dashboard["dev_user_email"] = ask("Local-dev stand-in identity", str(dashboard.get("dev_user_email") or "dev@example.com"))
    auto_commit = dashboard.setdefault("auto_commit", {})
    repo = ask("Auto-commit dashboard edits to repo (owner/repo, empty disables)", str(auto_commit.get("repo") or ""))
    auto_commit["repo"] = repo
    if repo:
        auto_commit["committer_email"] = ask("Auto-commit committer email", str(auto_commit.get("committer_email") or ""))
        env["DASHBOARD_GITHUB_TOKEN"] = ask_secret("Fine-grained PAT (contents read/write on that repo)", env.get("DASHBOARD_GITHUB_TOKEN", ""))
        ok, detail = setup_checks.check_github_pat_repo(env["DASHBOARD_GITHUB_TOKEN"], repo)
        status(ok, "dashboard pat", detail)


def step_content(data, env) -> None:
    say("\n== Conventions, personality & help ==")
    conventions = data.setdefault("conventions", {})
    current = str(conventions.get("file") or "")
    if ask_yes("Set up an engineering-conventions doc? (injected into every repo task)", default=not current):
        target = CONFIG_PATH.parent / "conventions.md"
        if not target.exists():
            shutil.copyfile(TEMPLATES_ROOT / "conventions.md", target)
            say(f"  created {target} from the template — fill it in with your organization's rules.")
        conventions["file"] = ask("Conventions file (relative to config.yaml)", current or "conventions.md")
    agent = data.setdefault("agent", {})
    if ask_yes("Give the agent a personality file?", default=bool(agent.get("personality_file"))):
        target = CONFIG_PATH.parent / "personality_agent.md"
        if not target.exists():
            shutil.copyfile(TEMPLATES_ROOT / "personality_agent.example.md", target)
        agent["personality_file"] = ask("Personality file", str(agent.get("personality_file") or "personality_agent.md"))
    reviewer = data.setdefault("reviewer", {})
    if reviewer.get("enabled") and ask_yes("Give the reviewer a personality file?", default=bool(reviewer.get("personality_file"))):
        target = CONFIG_PATH.parent / "personality_reviewer.md"
        if not target.exists():
            shutil.copyfile(TEMPLATES_ROOT / "personality_reviewer.example.md", target)
        reviewer["personality_file"] = ask("Reviewer personality file", str(reviewer.get("personality_file") or "personality_reviewer.md"))
    help_section = data.setdefault("help", {})
    if ask_yes("Set up a curated /help reply? (answered instantly in Slack, no task created)", default=bool(help_section.get("file"))):
        target = CONFIG_PATH.parent / "help.md"
        if not target.exists():
            content = (TEMPLATES_ROOT / "help.example.md").read_text()
            # drop the instruction-comment header; it's for readers of the template, not slack users
            content = "\n".join(line for line in content.splitlines() if not line.startswith("#")).lstrip("\n") + "\n"
            content = content.replace("{{agent_name}}", str((data.get("agent") or {}).get("name") or "Agent"))
            content = content.replace("Dashboard: {{dashboard_url}}\n\n", f"Dashboard: {data['dashboard']['public_url']}\n\n" if (data.get("dashboard") or {}).get("public_url") else "")
            target.write_text(content)
            say(f"  created {target} from the template — trim it to the skills you actually installed.")
        help_section["file"] = ask("Help file (relative to config.yaml)", str(help_section.get("file") or "help.md"))


def template_variables(data) -> dict[str, str]:
    """the {{var}} values used when instantiating skill templates, derived from config answers."""
    github = data.get("github") or {}
    approved = list(github.get("approved_repos") or [])
    org = approved[0].split("/")[0] if approved else ""
    jira = data.get("jira") or {}
    site = str(jira.get("site") or "").removeprefix("https://").removeprefix("http://")
    return {
        "agent_name": str((data.get("agent") or {}).get("name") or "Agent"),
        "reviewer_name": str((data.get("reviewer") or {}).get("name") or "Reviewer"),
        "github_org": org,
        "repo_list": ", ".join(f"`{repo.split('/', 1)[-1]}`" for repo in approved),
        "self_repo": str(github.get("self_repo") or ""),
        "pr_target_branch": "",  # asked below
        "jira_project": str((jira.get("projects") or [""])[0]),
        "jira_site": site,
        "conventions_file": str((data.get("conventions") or {}).get("file") or ""),
    }


def step_skills(data, env) -> None:
    say("\n== Skills ==")
    templates_dir = TEMPLATES_ROOT / "skills"
    available = sorted(entry.name for entry in templates_dir.iterdir() if (entry / "SKILL.md").is_file())
    if not available:
        say("  no skill templates found — skipping")
        return
    if not ask_yes("Install skills from the template library?", default=True):
        return
    variables = template_variables(data)
    github = data.get("github") or {}
    jira_configured = service_enabled_in(data, "jira")
    self_repo = service_enabled_in(data, "github") and bool(github.get("self_repo"))
    say("  Available templates (× = needs an integration you haven't configured):")
    disabled: dict[str, str] = {}
    for name in available:
        needs_jira = name in ("jira2pr", "slack2jira")
        needs_self = name in ("release", "discoverissues")
        if needs_jira and not jira_configured:
            disabled[name] = "needs jira"
        if needs_self and not self_repo:
            disabled[name] = "needs github.self_repo"
    for i, name in enumerate(available, 1):
        note = f"  × {disabled[name]}" if name in disabled else ""
        say(f"    {i:2}. /{name}{note}")
    answer = ask("Install which? (numbers/names, comma-separated, or 'all')", "all")
    if answer.strip().lower() == "all":
        selected = [name for name in available if name not in disabled]
    else:
        selected = []
        for token in answer.split(","):
            token = token.strip().lstrip("/")
            if token.isdigit() and 1 <= int(token) <= len(available):
                selected.append(available[int(token) - 1])
            elif token in available:
                selected.append(token)
    if any(name in ("jira2pr", "slack2pr") for name in selected):
        variables["pr_target_branch"] = ask("Default PR target branch", "main")
    skills_root = Path(settings.SKILLS_ROOT)
    for name in selected:
        content = (templates_dir / name / "SKILL.md").read_text()
        # transitive requires must be installed too, or the skill fails to render at task time
        for required in skills.load(str(templates_dir), name).requires:
            if required not in selected and required not in skills.available(str(skills_root)):
                selected.append(required)
        for key, value in variables.items():
            content = content.replace("{{" + key + "}}", value)
        if "{{" in content:
            leftover = content[content.find("{{") : content.find("{{") + 40]
            say(f"  [!!] /{name}: unfilled placeholder {leftover!r} — skipped")
            continue
        destination = skills_root / name / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        skills.load(str(skills_root), name)  # same validation the dashboard editor runs
        say(f"  installed /{name}")


def step_secrets(data, env) -> None:
    say("\n== Secrets destination ==")
    say(f"  Local secrets are in {ENV_PATH} (source it before `taskboy run`).")
    say("  For a deployed host you can either put the same variables in /etc/taskboy/env")
    say("  or push one JSON bundle to AWS Secrets Manager (what the shell repo's deploy setup expects).")
    if not ask_yes("Push the bundle to AWS Secrets Manager now?", default=False):
        return
    environment = ask("Environment name", "staging")
    region = ask("AWS region", settings.REGION)
    secret_name = ask("Secret name", f"TASKBOY_SECRETS_{environment.upper()}")
    bundle = {key.lower(): env[key] for key in SECRET_ENV_KEYS if env.get(key)}
    try:
        import boto3
        from botocore.exceptions import ClientError

        client = boto3.client("secretsmanager", region_name=region)
        try:
            client.create_secret(Name=secret_name, SecretString=json.dumps(bundle))
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceExistsException":
                raise
            client.put_secret_value(SecretId=secret_name, SecretString=json.dumps(bundle))
    except Exception as e:
        status(False, "secrets push", f"{type(e).__name__}: {e}")
        return
    ok, detail = setup_checks.check_aws_secret(secret_name, region)
    status(ok, "secrets push", detail)


STEPS = [
    ("identity", step_identity),
    ("claude", step_claude),
    ("slack", step_slack),
    ("github", step_github),
    ("reviewer", step_reviewer),
    ("jira", step_jira),
    ("sentry", step_sentry),
    ("aws", step_aws),
    ("dashboard", step_dashboard),
    ("content", step_content),
    ("skills", step_skills),
    ("secrets", step_secrets),
]


# -- check mode ---------------------------------------------------------------


def run_checks(no_network: bool = False) -> int:
    """non-interactive validation of config.yaml + reachable credentials; exit 64 on config errors."""
    import os

    try:
        config = load_config(str(CONFIG_PATH))
    except ConfigError as e:
        status(False, "config", str(e))
        return 64
    status(True, "config", f"{CONFIG_PATH} loads (agent: {config.agent_name})")
    env = {**read_env(), **{key: os.environ[key] for key in SECRET_ENV_KEYS if os.environ.get(key)}}
    if no_network:
        for key in SECRET_ENV_KEYS:
            status(True if env.get(key) else None, key, masked(env.get(key, "")))
        return 0
    failures = 0

    def report(label: str, result: tuple[bool, str]) -> None:
        nonlocal failures
        ok, detail = result
        status(ok, label, detail)
        failures += 0 if ok else 1

    if env.get("CLAUDE_CODE_OAUTH_TOKEN") and config.runner == "claude":
        report("claude", setup_checks.check_claude(env["CLAUDE_CODE_OAUTH_TOKEN"]))
    if config.slack.enabled:
        if env.get("SLACK_BOT_TOKEN"):
            report("slack", setup_checks.check_slack(env["SLACK_BOT_TOKEN"], config.slack.team_id))
        else:
            status(False, "slack", "slack is enabled but SLACK_BOT_TOKEN missing")
            failures += 1
    github = config.raw.get("github") or {}
    if config.service_enabled("github") and env.get("GITHUB_APP_ID"):
        report("github app", setup_checks.check_github_app(env["GITHUB_APP_ID"], env.get("GITHUB_INSTALLATION_ID", ""), env.get("GITHUB_APP_PRIVATE_KEY", ""), list(github.get("approved_repos") or [])))
    if config.reviewer.enabled:
        if env.get("REVIEWER_GITHUB_APP_ID"):
            report("reviewer github app", setup_checks.check_github_app(env["REVIEWER_GITHUB_APP_ID"], env.get("REVIEWER_GITHUB_INSTALLATION_ID", ""), env.get("REVIEWER_GITHUB_APP_PRIVATE_KEY", "")))
        else:
            status(False, "reviewer", "reviewer.enabled but REVIEWER_GITHUB_APP_ID missing")
            failures += 1
    jira = config.raw.get("jira") or {}
    if config.service_enabled("jira") and env.get("JIRA_API_TOKEN"):
        report("jira", setup_checks.check_jira(str(jira["site"]), env.get("JIRA_EMAIL", ""), env["JIRA_API_TOKEN"]))
    sentry = config.raw.get("sentry") or {}
    if config.service_enabled("sentry") and env.get("SENTRY_TOKEN"):
        report("sentry", setup_checks.check_sentry(str(sentry["organization"]), env["SENTRY_TOKEN"]))
    if config.dashboard.auto_commit_enabled and env.get("DASHBOARD_GITHUB_TOKEN"):
        report("dashboard pat", setup_checks.check_github_pat_repo(env["DASHBOARD_GITHUB_TOKEN"], config.dashboard.commit_repo))
    say("")
    say("all checks passed" if failures == 0 else f"{failures} check(s) failed")
    return 0 if failures == 0 else 1


# -- entry point --------------------------------------------------------------


def run(args) -> int:
    if args.check:
        return run_checks(no_network=args.no_validate)

    if args.local:
        if not CONFIG_PATH.exists():
            seed_config()
        say(f"local demo ready: {CONFIG_PATH} (runner: echo — no credentials needed)")
        say("try it:")
        say("  taskboy run                          # start the service in the foreground")
        say('  taskboy inject "say hi" --watch      # in another terminal')
        say("then run `taskboy setup` for the full guided setup.")
        return 0

    say("taskboy setup — answers are written to config/config.yaml, config/services/*.yaml, and .env")
    say("after every step, so you can quit (ctrl-c) and re-run anytime; existing values show as defaults.")
    data = load_config_data()
    env = read_env()
    steps = STEPS if not args.step else [(name, fn) for name, fn in STEPS if name == args.step]
    if args.step and not steps:
        say(f"unknown step {args.step!r} — choose from: {', '.join(name for name, _ in STEPS)}")
        return 2
    for name, fn in steps:
        try:
            fn(data, env)
        except KeyboardInterrupt:
            say("\ninterrupted — progress so far is saved; re-run `taskboy setup` to continue")
            return 130
        try:
            save_config_data(data)
        except ConfigError as e:
            say(f"  [!!] {e} — this step was not saved; re-run `taskboy setup --step {name}`")
            data = load_config_data()
        write_env(env)
    say("\n== Final check ==")
    result = run_checks(no_network=args.no_validate)
    say("\nnext steps:")
    say("  source .env && taskboy run             # run locally")
    say("  see SETUP.md in your shell repo (taskboy-shell template) for deploying to a host and CI/CD")
    return result


def add_parser(subparsers) -> None:
    setup = subparsers.add_parser("setup", help="interactive first-run setup (config.yaml, credentials, skills)")
    setup.add_argument("--step", default=None, help="run a single step (e.g. slack, github, skills)")
    setup.add_argument("--check", action="store_true", help="non-interactive: validate config + live credentials and exit")
    setup.add_argument("--local", action="store_true", help="zero-credential local demo: copy the example config and print how to run")
    setup.add_argument("--no-validate", action="store_true", help="skip network validation")


if __name__ == "__main__":
    sys.exit(0)
