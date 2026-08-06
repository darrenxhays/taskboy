# Skill templates

These directories are **templates**, not installed skills. The agent never loads them directly — they are instantiated into the top-level `skills/` directory, either by the `taskboy setup` wizard (the skills picker step fills in every variable from your answers) or by hand:

1. Copy the template directory into `skills/` (keep the `<name>/SKILL.md` layout).
2. Replace every `{{variable}}` placeholder in the copied `SKILL.md` with your value. No placeholder may remain — the loader does not substitute variables at runtime.

## Variables

| Variable | Meaning | Example |
| --- | --- | --- |
| `{{agent_name}}` | Main agent display name | `Scout` |
| `{{reviewer_name}}` | Reviewer persona display name | `Critic` |
| `{{github_org}}` | GitHub org bare repo names resolve against | `example-org` |
| `{{repo_list}}` | Comma-separated backtick list of repos the agent may work in | `` `svc-a`, `svc-b` `` |
| `{{self_repo}}` | The agent's own repository (`owner/repo`) | `example-org/taskboy` |
| `{{pr_target_branch}}` | Default PR target / integration branch | `main` |
| `{{jira_project}}` | Jira project key | `ENG` |
| `{{jira_site}}` | Jira site host | `example.atlassian.net` |
| `{{conventions_file}}` | Repo-relative path to the engineering conventions doc (`conventions.file` in config.yaml) | `config/conventions.md` |

Not every template uses every variable — replacing all of them is always safe.

## Frontmatter

Each `SKILL.md` starts with YAML frontmatter:

- `name` (required) — must equal the directory name exactly; the loader rejects a mismatch.
- `description` (required) — one line shown in skill listings and used for routing.
- `requires` (optional) — list of other skill names whose bodies get inlined when this skill runs; the required skills must be installed too.
- `model` (optional) — model alias the skill runs on; must be an alias defined in the model catalog in config.yaml (e.g. `fable`, `opus`, `sonnet`).
- `profile` (optional) — execution profile override.
- `internal_tools` (optional) — in-process capability servers the skill opts into (currently `issues`, `enqueue`).
