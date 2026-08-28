"""repository-shipped skill discovery, parsing, and prompt rendering."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


class SkillError(ValueError):
    pass


# in-process capability servers a skill may opt into via `internal_tools:` frontmatter (wired by the runner)
KNOWN_INTERNAL_TOOLS = {"issues", "enqueue"}

# skills the application itself invokes — the review poller (/review), the issues pipeline
# (/refineissue, /spec2pr, /implementapprovedissues), and scheduler seeds (/discoverissues).
# resolve() falls back to the packaged template for these, so an operator who never installed
# them still gets working app-driven features; installing a copy in SKILLS_ROOT overrides.
BUILTIN_SKILLS = ("discoverissues", "implementapprovedissues", "refineissue", "review", "spec2pr")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    requires: list[str]
    model: str | None = None  # model alias this skill runs on (e.g. fable); None uses the config skills.tier default
    profile: str | None = None  # execution profile override; None uses the config skills.profile default
    internal_tools: list[str] = None  # type: ignore[assignment]  # normalized to a list in load()


def parse_invocation(text: str) -> tuple[str, str] | None:
    match = re.match(r"^/([a-z0-9_-]+)\s*(.*)$", text, flags=re.DOTALL)
    if not match:
        return None
    return match.group(1), match.group(2)


def available(root: str | Path) -> list[str]:
    path = Path(root)
    if not path.is_dir():
        return []
    return sorted(child.name for child in path.iterdir() if child.is_dir() and (child / "SKILL.md").is_file())


def runtime_variables(config) -> dict[str, str]:
    """the {{var}} values the built-in templates use, derived from live config at task time.
    duck-typed on purpose: importing Config here would be circular."""
    github = (config.raw.get("github") or {}) if config.service_enabled("github") else {}
    return {
        "agent_name": config.agent_name,
        "self_repo": str(github.get("self_repo") or ""),
        # the injected workspace copy is always named CONVENTIONS.md, so that reads fine when no file is configured
        "conventions_file": str((config.raw.get("conventions") or {}).get("file") or "CONVENTIONS.md"),
    }


def resolve(root: str | Path, name: str, variables: dict[str, str] | None = None) -> Skill | None:
    """an operator-installed skill always wins; module-invoked built-ins fall back to the packaged
    template rendered with `variables`. returns None for a name that is neither installed nor built-in."""
    if name in available(root):
        return load(root, name)
    if name not in BUILTIN_SKILLS:
        return None
    from taskboy import assets

    skill = load(assets.TEMPLATES_ROOT / "skills", name)
    description, body = skill.description, skill.body
    for key, value in (variables or {}).items():
        description = description.replace("{{" + key + "}}", value)
        body = body.replace("{{" + key + "}}", value)
    return Skill(name=skill.name, description=description, body=body, requires=skill.requires, model=skill.model, profile=skill.profile, internal_tools=skill.internal_tools)


def load(root: str | Path, name: str) -> Skill:
    path = Path(root) / name / "SKILL.md"
    try:
        text = path.read_text()
    except FileNotFoundError as e:
        raise SkillError(f"skill /{name} is not installed") from e
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", text, flags=re.DOTALL)
    if not match:
        raise SkillError(f"skill /{name} has invalid frontmatter")
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        raise SkillError(f"skill /{name} has invalid frontmatter: {e}") from e
    if not isinstance(metadata, dict):
        raise SkillError(f"skill /{name} has invalid frontmatter")
    skill_name = metadata.get("name")
    description = metadata.get("description")
    requires = metadata.get("requires") or []
    model = metadata.get("model")
    profile = metadata.get("profile")
    internal_tools = metadata.get("internal_tools") or []
    if not isinstance(skill_name, str) or not isinstance(description, str):
        raise SkillError(f"skill /{name} frontmatter needs name and description")
    if skill_name != name:
        raise SkillError(f"skill /{name} frontmatter name does not match its directory")
    if not isinstance(requires, list) or not all(isinstance(item, str) for item in requires):
        raise SkillError(f"skill /{name} requires must be a list of skill names")
    if model is not None and not isinstance(model, str):
        raise SkillError(f"skill /{name} model must be a model alias string")
    if profile is not None and not isinstance(profile, str):
        raise SkillError(f"skill /{name} profile must be a profile name string")
    if not isinstance(internal_tools, list) or not all(isinstance(item, str) for item in internal_tools):
        raise SkillError(f"skill /{name} internal_tools must be a list of strings")
    unknown = set(internal_tools) - KNOWN_INTERNAL_TOOLS
    if unknown:
        raise SkillError(f"skill /{name} internal_tools contains unknown entries: {sorted(unknown)}")
    return Skill(name=skill_name, description=description, body=match.group(2).rstrip(), requires=requires, model=model or None, profile=profile or None, internal_tools=internal_tools)


def render(root: str | Path, name: str, variables: dict[str, str] | None = None) -> str:
    first = resolve(root, name, variables)
    if first is None:
        raise SkillError(f"skill /{name} is not installed")
    rendered = [first.body]
    seen = {name}
    queue = list(first.requires)
    while queue:
        required_name = queue.pop(0)
        if required_name in seen:
            continue
        # requires resolve through the built-ins too, so an installed skill may depend on /review without a local copy
        required = resolve(root, required_name, variables)
        if required is None:
            raise SkillError(f"skill /{required_name} is not installed")
        seen.add(required_name)
        rendered.append(f"### Included skill: /{required_name}\n{required.body}")
        queue.extend(required.requires)
    return "\n\n".join(rendered)
