# skills/

Installed skills live here, one directory per skill: `skills/<name>/SKILL.md`. This directory ships **empty** — install skills from the templates in `templates/skills/`, either through the `agent-harness setup` wizard (its skills picker copies the templates you choose and fills in every `{{variable}}`) or by hand: copy a template directory here and replace every `{{variable}}` placeholder with your value (see `templates/skills/README.md` for the variable table).

A `SKILL.md` is YAML frontmatter followed by a markdown body. The frontmatter requires `name` (must equal the directory name) and `description` (one line, used for listings and routing), and optionally `requires` (other installed skill names inlined at render time), `model` (a catalog alias from config.yaml), `profile`, and `internal_tools`. The body is the procedure the agent follows when the skill is invoked as `/<name> {args}`.

Installed skills can also be edited live from the dashboard's Config page.

(The loader treats any subdirectory containing a `SKILL.md` as a skill; this README file itself is ignored.)
