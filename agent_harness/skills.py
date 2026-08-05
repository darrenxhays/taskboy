"""repository-shipped skill discovery, parsing, and prompt rendering."""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


class SkillError(ValueError):
    pass


# in-process capability servers a skill may opt into via `internal_tools:` frontmatter (wired by the runner)
KNOWN_INTERNAL_TOOLS = {"issues", "enqueue"}


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


def render(root: str | Path, name: str) -> str:
    first = load(root, name)
    rendered = [first.body]
    seen = {name}
    queue = list(first.requires)
    while queue:
        required_name = queue.pop(0)
        if required_name in seen:
            continue
        required = load(root, required_name)
        seen.add(required_name)
        rendered.append(f"### Included skill: /{required_name}\n{required.body}")
        queue.extend(required.requires)
    return "\n\n".join(rendered)
